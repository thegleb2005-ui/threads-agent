"""
Фоновая обработка видео для многих пользователей сразу — с честной очередью.

Что сколько стоит серверу:
  - скачивание, субтитры YouTube, перевод, выжимка, пост — это сеть, памяти
    почти не едят, поэтому идут ПАРАЛЛЕЛЬНО;
  - распознавание Whisper — тяжёлое, на тарифе с 1 ГБ может работать только
    одно. Поэтому видео режутся на куски по 5 минут, а куски распознаются
    ПО КРУГУ между пользователями: кусок одного, кусок другого и т.д.
    Короткий Reels не ждёт полчаса, пока распознаётся чужое часовое видео.

У каждого пользователя в работе не больше одного видео одновременно,
остальные его видео ждут своей очереди.

Этапы одного видео:
  queued -> downloading (субтитры или скачивание + нарезка)
         -> transcribing (куски в общей круговой очереди Whisper; пропускается,
            если текст взят из субтитров)
         -> generating (перевод/выжимка или пост) -> done / draft_ready / error
"""
import os
import time
import shutil
import asyncio
import logging
from dataclasses import dataclass, field

from aiogram.types import BufferedInputFile, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from db import get_all_queued, get_post, update_post, get_media_cache, save_media_cache
from downloader import (download_audio, fetch_subtitles, download_video_file,
                        download_audio_file, extract_mp3, TooLargeError)
from transcriber import split_for_whisper, transcribe_chunk, current_model, to_mp3
from generator import generate_draft, translate_to_russian, is_mostly_russian, summarize_ru

logger = logging.getLogger(__name__)

INTAKE_INTERVAL_SECONDS = 2
WHISPER_IDLE_SECONDS = 1
# Лимит длины сообщения в Telegram — 4096 символов. Длиннее — режем/файлом.
MESSAGE_LIMIT = 3900

FINAL_STEP_LABEL = {"post": "Пишу пост", "transcript": "Готовлю расшифровку и выжимку"}


# --- Состояние планировщика (живёт в памяти; после перезапуска незаконченные
#     видео возвращаются в очередь функцией recover_stuck_posts) ------------

@dataclass
class WhisperJob:
    post_id: int
    user_id: int
    title: str
    chunks: list
    tmp_dir: str | None
    audio_path: str
    texts: list = field(default_factory=list)


BUSY_USERS: set = set()              # у кого сейчас видео в работе
ACTIVE_POSTS: set = set()            # какие видео сейчас в работе
WHISPER_JOBS: dict = {}              # user_id -> WhisperJob (по одному на человека)
_RR_ORDER: list = []                 # порядок обхода пользователей по кругу
_DOWNLOAD_SLOTS = None               # семафор параллельных скачиваний
_TASKS: set = set()                  # ссылки на фоновые задачи


def _spawn(coro):
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task


def _release(post_id: int, user_id: int):
    ACTIVE_POSTS.discard(post_id)
    BUSY_USERS.discard(user_id)


def _owner(post) -> int:
    return post["user_id"] or post["status_chat_id"] or config.ADMIN_USER_ID


def _chat(post) -> int:
    return post["status_chat_id"] or post["user_id"] or config.ADMIN_USER_ID


# --- Клавиатуры и сообщения ---------------------------------------------------

DOWNLOAD_MODES = ("video", "audio")

ACTION_BUTTONS = [
    ("transcript", "📝 Расшифровка на русском"),
    ("social", "📱 Пост для соц сетей"),
    ("video", "🎬 Скачать видео"),
    ("audio", "🎵 Скачать звук (mp3)"),
]


def available_actions(source_url: str) -> list[str]:
    """Какие действия имеют смысл для источника."""
    if not source_url.startswith("tg:"):
        return ["transcript", "social", "video", "audio"]
    kind = source_url.split(":", 2)[1]
    if kind in ("video", "video_note", "document"):
        return ["transcript", "social", "audio"]   # видео у человека уже есть, а звук — пригодится
    return ["transcript", "social"]                 # голосовые и аудио


def choice_keyboard(post_id: int, source_url: str, exclude: tuple = (), with_cancel: bool = True):
    kb = InlineKeyboardBuilder()
    for action, label in ACTION_BUTTONS:
        if action in available_actions(source_url) and action not in exclude:
            data = {"video": "dlvideo", "audio": "dlaudio"}.get(action, action)
            kb.button(text=label, callback_data=f"{data}:{post_id}")
    if with_cancel:
        kb.button(text="❌ Отмена", callback_data=f"cancel:{post_id}")
    kb.adjust(1)
    return kb.as_markup()


def _draft_keyboard(post_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=f"done:{post_id}")
    kb.button(text="✏️ Редактировать", callback_data=f"edit:{post_id}")
    kb.button(text="🗑 Удалить", callback_data=f"reject:{post_id}")
    kb.adjust(1)
    return kb.as_markup()


def _transcript_keyboard(post_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Пост для соц сетей", callback_data=f"social:{post_id}")
    kb.button(text="✍️ Пост со своим промптом", callback_data=f"customprompt:{post_id}")
    kb.adjust(1)
    return kb.as_markup()


def _render_progress(post_id: int, mode: str, stage: str, title: str = "",
                     percent: int | None = None, extra: str = "") -> str:
    order = ["downloading", "transcribing", "generating"]
    pos = order.index(stage) if stage in order else -1
    labels = {
        "downloading": "Загружаю видео",
        "transcribing": "Распознаю речь" + (f" — {percent}%" if percent is not None else ""),
        "generating": FINAL_STEP_LABEL.get(mode, "Пишу пост"),
    }
    lines = [f"⏳ Обрабатываю видео #{post_id}", ""]
    for i, key in enumerate(order):
        mark = "✅" if i < pos else ("▶️" if i == pos else "⬜️")
        lines.append(f"{mark} {labels[key]}{'...' if i == pos else ''}")
    if title:
        lines += ["", f"🎬 {title}"]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


BUSY_NOTE = "⏳ Сейчас много запросов — это может занять чуть больше времени."


async def _edit_status(bot, post, text: str):
    """Статус — украшение: ошибки его обновления не должны ронять обработку."""
    if not (post["status_chat_id"] and post["status_message_id"]):
        return
    try:
        await bot.edit_message_text(text, chat_id=post["status_chat_id"], message_id=post["status_message_id"])
    except Exception as e:
        logger.debug(f"Не удалось обновить статус #{post['id']}: {e}")


async def _send_transcript(bot, chat_id: int, post_id: int, title: str, text: str,
                           translated: bool, summary: str | None):
    """Полный текст — файлом .txt, выжимка — отдельным сообщением с кнопками."""
    caption = f"📄 Полная расшифровка #{post_id}" + (" (переведено на русский)" if translated else "")
    if title:
        caption += f"\n🎬 {title}"
    await bot.send_document(
        chat_id,
        document=BufferedInputFile(text.encode("utf-8"), filename=f"transcript_{post_id}.txt"),
        caption=caption[:1024],
    )
    body = (f"🧠 Суть видео #{post_id}\n\n{summary}" if summary
            else f"🧠 Выжимку для #{post_id} сделать не удалось — полный текст в файле выше.")
    if len(body) > MESSAGE_LIMIT:
        body = body[:MESSAGE_LIMIT].rsplit("\n", 1)[0] + "\n…"
    await bot.send_message(chat_id, body, reply_markup=_transcript_keyboard(post_id))


async def _fail(bot, post_id: int, user_id: int, error: Exception | str):
    logger.error(f"Видео #{post_id}: ошибка — {error}")
    await update_post(post_id, status="error", error_message=str(error)[:2000])
    post = await get_post(post_id)
    _release(post_id, user_id)
    if post:
        await _edit_status(bot, post, f"⚠️ Видео #{post_id} — ошибка при обработке")
        try:
            await bot.send_message(_chat(post), f"⚠️ Не получилось обработать видео #{post_id}: {error}")
        except Exception:
            pass


async def _cancelled(post_id: int) -> bool:
    fresh = await get_post(post_id)
    return fresh is None or fresh["status"] == "rejected"


def _cleanup_files(job: WhisperJob):
    if job.tmp_dir:
        shutil.rmtree(job.tmp_dir, ignore_errors=True)
    try:
        os.remove(job.audio_path)
    except OSError:
        pass


# --- Файлы из Telegram -----------------------------------------------------------
# source_url вида "tg:<тип>:<file_id>". Файл скачивается через Bot API (лимит
# Telegram — 20 МБ) и перегоняется в mp3, дальше — обычный путь.
TG_TITLES = {"voice": "Голосовое сообщение", "video_note": "Видеосообщение (кружок)",
             "audio": "Аудиофайл", "video": "Видео", "document": "Файл"}


async def _download_telegram_file(bot, file_id: str, path: str):
    await bot.download(file_id, destination=path)


async def _download_telegram_media_raw(bot, url: str, post_id: int):
    _, kind, file_id = url.split(":", 2)
    os.makedirs(config.DOWNLOADS_DIR, exist_ok=True)
    raw_path = os.path.join(config.DOWNLOADS_DIR, f"tg_{post_id}.bin")
    await _download_telegram_file(bot, file_id, raw_path)
    return raw_path, TG_TITLES.get(kind, "Файл")


async def _download_telegram_media(bot, url: str, post_id: int):
    _, kind, file_id = url.split(":", 2)
    os.makedirs(config.DOWNLOADS_DIR, exist_ok=True)
    raw_path = os.path.join(config.DOWNLOADS_DIR, f"tg_{post_id}.bin")
    await _download_telegram_file(bot, file_id, raw_path)
    try:
        audio_path = await to_mp3(raw_path)
    finally:
        try:
            os.remove(raw_path)
        except OSError:
            pass
    return audio_path, TG_TITLES.get(kind, "Файл")


# --- Этап 1: субтитры или скачивание (параллельно) ----------------------------

async def _prepare(bot, post):
    post_id, user_id, url = post["id"], _owner(post), post["source_url"]
    try:
        async with _DOWNLOAD_SLOTS:
            await update_post(post_id, status="downloading")
            await _edit_status(bot, post, _render_progress(post_id, post["mode"] or "post", "downloading"))

            # Ссылка на YouTube с субтитрами — текст берём из них, без распознавания.
            if not url.startswith("tg:") and config.USE_YOUTUBE_SUBTITLES:
                subs = await fetch_subtitles(url)
                if subs:
                    title, text, label = subs
                    if await _cancelled(post_id):
                        return _release(post_id, user_id)
                    await update_post(post_id, video_title=title, transcript=text, transcript_source="subtitles")
                    logger.info(f"Видео #{post_id}: {label}, распознавание не нужно")
                    _spawn(_finalize(bot, post_id, user_id))
                    return

            if url.startswith("tg:"):
                audio_path, title = await _download_telegram_media(bot, url, post_id)
            else:
                audio_path, title = await download_audio(url)

        if await _cancelled(post_id):
            try:
                os.remove(audio_path)
            except OSError:
                pass
            return _release(post_id, user_id)

        tmp_dir, chunks, duration = await split_for_whisper(audio_path)
        await update_post(post_id, status="transcribing", video_title=title, transcript_source="whisper")
        WHISPER_JOBS[user_id] = WhisperJob(post_id, user_id, title, chunks, tmp_dir, audio_path)
        if user_id not in _RR_ORDER:
            _RR_ORDER.append(user_id)
        fresh = await get_post(post_id)
        await _edit_status(bot, fresh, _render_progress(
            post_id, fresh["mode"] or "post", "transcribing", title, percent=0,
            extra=BUSY_NOTE if len(WHISPER_JOBS) > 1 else ""))
        logger.info(f"Видео #{post_id}: {duration:.0f} сек, кусков: {len(chunks)}, в очереди Whisper: {len(WHISPER_JOBS)}")
    except Exception as e:
        await _fail(bot, post_id, user_id, e)


# --- Этап 2: Whisper по кругу (единственный тяжёлый шаг) ----------------------

def _next_whisper_job() -> WhisperJob | None:
    """Следующий по кругу пользователь, у которого есть куски."""
    for _ in range(len(_RR_ORDER)):
        user_id = _RR_ORDER.pop(0)
        if user_id in WHISPER_JOBS:
            _RR_ORDER.append(user_id)
            return WHISPER_JOBS[user_id]
    return None


async def _whisper_loop(bot):
    while True:
        job = _next_whisper_job()
        if job is None:
            await asyncio.sleep(WHISPER_IDLE_SECONDS)
            continue
        if await _cancelled(job.post_id):
            WHISPER_JOBS.pop(job.user_id, None)
            _cleanup_files(job)
            _release(job.post_id, job.user_id)
            continue

        idx = len(job.texts)
        try:
            text = await transcribe_chunk(job.chunks[idx])
        except Exception as e:
            WHISPER_JOBS.pop(job.user_id, None)
            _cleanup_files(job)
            await _fail(bot, job.post_id, job.user_id, e)
            continue
        job.texts.append(text)

        post = await get_post(job.post_id)
        if len(job.texts) < len(job.chunks):
            await _edit_status(bot, post, _render_progress(
                job.post_id, post["mode"] or "post", "transcribing", job.title,
                percent=int(100 * len(job.texts) / len(job.chunks)),
                extra=BUSY_NOTE if len(WHISPER_JOBS) > 1 else ""))
            continue

        WHISPER_JOBS.pop(job.user_id, None)
        _cleanup_files(job)
        transcript = " ".join(job.texts)
        await update_post(job.post_id, transcript=transcript)
        logger.info(f"Видео #{job.post_id}: распознано моделью {current_model()}, {len(transcript)} символов")
        _spawn(_finalize(bot, job.post_id, job.user_id))


# --- Этап 3: перевод/выжимка или пост (параллельно) ---------------------------

async def _finalize(bot, post_id: int, user_id: int):
    try:
        post = await get_post(post_id)   # режим и промпт — свежие: их могли сменить кнопкой
        if post is None or post["status"] == "rejected":
            return _release(post_id, user_id)
        mode = post["mode"] or "post"
        title = post["video_title"] or ""
        transcript = post["transcript"]
        chat_id = _chat(post)

        await update_post(post_id, status="generating")
        prompt_note = ""
        if mode == "post":
            cp = post["custom_prompt"]
            prompt_note = ("📱 Пост для соц сетей" if cp and cp == config.SOCIAL_POST_PROMPT
                           else "🎯 Свой промпт" if cp else "📋 Базовый промпт")
        await _edit_status(bot, post, _render_progress(post_id, mode, "generating", title, extra=prompt_note))

        if mode == "transcript":
            translated = not is_mostly_russian(transcript)
            text = await translate_to_russian(transcript) if translated else transcript
            try:
                summary = await summarize_ru(transcript)
            except Exception:
                logger.exception(f"Видео #{post_id}: не удалось сделать выжимку")
                summary = None
            await update_post(post_id, status="done", draft_text=text)
            await _edit_status(bot, post, f"✅ Расшифровка #{post_id} готова\n🎬 {title}")
            await _send_transcript(bot, chat_id, post_id, title, text, translated, summary)
        else:
            custom_prompt = post["custom_prompt"]
            draft = await generate_draft(transcript, custom_prompt)
            await update_post(post_id, status="draft_ready", draft_text=draft)
            await _edit_status(bot, post, f"✅ Пост #{post_id} готов\n🎬 {title}")
            await bot.send_message(chat_id, f"📝 Черновик поста #{post_id}\n\n{draft}",
                                   reply_markup=_draft_keyboard(post_id))
        _release(post_id, user_id)
    except Exception as e:
        await _fail(bot, post_id, user_id, e)


# --- Скачивание видео / звука для пользователя (параллельно, мимо Whisper) -----

def _safe_name(title: str) -> str:
    import re
    name = re.sub(r"[^\w\s.-]", "", title or "file", flags=re.UNICODE).strip()
    return (name or "file")[:60]


async def _send_media(bot, chat_id, mode, media, title, post_id, url):
    """Отправляет видео/звук (путь к файлу или file_id из кэша). Возвращает file_id."""
    kb = choice_keyboard(post_id, url, exclude=(mode,), with_cancel=False)
    caption = f"🎬 {title}" if mode == "video" else f"🎵 {title}"
    if isinstance(media, str) and os.path.exists(media):
        ext = ".mp4" if mode == "video" else ".mp3"
        media = FSInputFile(media, filename=_safe_name(title) + ext)
    if mode == "video":
        msg = await bot.send_video(chat_id, video=media, caption=caption[:1024], supports_streaming=True,
                                   reply_markup=kb, request_timeout=300)
        obj = msg.video or msg.document
    else:
        msg = await bot.send_audio(chat_id, audio=media, title=(title or "audio")[:64], caption=caption[:1024],
                                   reply_markup=kb, request_timeout=300)
        obj = msg.audio or msg.document
    return obj.file_id if obj else None


async def _deliver_media(bot, post):
    post_id, user_id, url = post["id"], _owner(post), post["source_url"]
    mode = post["mode"]
    what = "видео" if mode == "video" else "звук"
    chat_id = _chat(post)
    path = None
    try:
        async with _DOWNLOAD_SLOTS:
            await update_post(post_id, status="downloading")
            await _edit_status(bot, post, f"⏳ Скачиваю {what} #{post_id}...")

            cached = None if url.startswith("tg:") else await get_media_cache(url, mode)
            if cached:
                title = cached["title"] or ""
                await _send_media(bot, chat_id, mode, cached["file_id"], title, post_id, url)
                await update_post(post_id, status="done", video_title=title)
                await _edit_status(bot, post, f"✅ Готово #{post_id}")
                return _release(post_id, user_id)

            if url.startswith("tg:"):
                raw, title = await _download_telegram_media_raw(bot, url, post_id)
                try:
                    path = await extract_mp3(raw)
                finally:
                    try:
                        os.remove(raw)
                    except OSError:
                        pass
            elif mode == "video":
                path, title = await download_video_file(url)
            else:
                path, title = await download_audio_file(url)

        if await _cancelled(post_id):
            return _release(post_id, user_id)

        await _edit_status(bot, post, f"📤 Отправляю {what} #{post_id}...")
        file_id = await _send_media(bot, chat_id, mode, path, title, post_id, url)
        if file_id and not url.startswith("tg:"):
            await save_media_cache(url, mode, file_id, title)
        await update_post(post_id, status="done", video_title=title)
        await _edit_status(bot, post, f"✅ Готово #{post_id}")
        _release(post_id, user_id)
    except TooLargeError:
        await update_post(post_id, status="error", error_message="too large")
        _release(post_id, user_id)
        await _edit_status(bot, post, f"⚠️ #{post_id}: файл слишком большой")
        if mode == "video":
            text = ("Это видео не помещается в лимит Telegram (50 МБ) даже в низком качестве. "
                    "Могу прислать звук или сделать расшифровку:")
        else:
            text = "Звук этого видео слишком длинный для Telegram (50 МБ). Могу сделать расшифровку:"
        await bot.send_message(chat_id, text,
                               reply_markup=choice_keyboard(post_id, url, exclude=(mode, "video"), with_cancel=False))
    except Exception as e:
        await _fail(bot, post_id, user_id, e)
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


# --- Приём новых видео в работу -----------------------------------------------

async def _intake_once(bot):
    for post in await get_all_queued():
        post_id, user_id = post["id"], _owner(post)
        if post_id in ACTIVE_POSTS or user_id in BUSY_USERS:
            continue   # у человека уже идёт видео — это подождёт своей очереди
        BUSY_USERS.add(user_id)
        ACTIVE_POSTS.add(post_id)
        if (post["mode"] or "post") in DOWNLOAD_MODES:
            _spawn(_deliver_media(bot, post))
        elif post["transcript"]:
            # Текст уже есть (например, из расшифровки просят пост) — сразу к финалу.
            _spawn(_finalize(bot, post_id, user_id))
        else:
            _spawn(_prepare(bot, post))


async def process_queue_forever(bot, admin_id: int):
    global _DOWNLOAD_SLOTS
    _DOWNLOAD_SLOTS = asyncio.Semaphore(config.MAX_PARALLEL_DOWNLOADS)
    _spawn(_whisper_loop(bot))
    while True:
        try:
            await _intake_once(bot)
        except Exception:
            logger.exception("Ошибка в приёме очереди")
        await asyncio.sleep(INTAKE_INTERVAL_SECONDS)
