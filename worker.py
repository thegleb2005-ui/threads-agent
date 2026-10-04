"""
Фоновый воркер: периодически проверяет очередь на новые ссылки и прогоняет
их через пайплайн download -> transcribe -> generate -> draft_ready.

По ходу работы редактирует одно и то же сообщение в Telegram, показывая
живой статус ("Скачиваю аудио..." -> "Распознаю речь..." -> ...), чтобы
не спамить чат новыми сообщениями на каждый шаг. Готовый черновик уходит
отдельным сообщением с кнопками.
"""
import os
import asyncio
import logging

from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import get_next_queued, get_post, update_post
from downloader import download_audio
from transcriber import transcribe_audio, current_model
from generator import generate_draft, translate_to_russian, is_mostly_russian
from aiogram.types import BufferedInputFile

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5

# Шаги пайплайна для отрисовки прогресса. Порядок важен.
STEPS = [
    ("downloading", "Скачиваю аудио"),
    ("transcribing", "Распознаю речь"),
    ("generating", "Пишу пост"),
]
FINAL_STEP_LABEL = {"post": "Пишу пост", "transcript": "Готовлю расшифровку на русском"}

# Лимит длины сообщения в Telegram — 4096 символов. Длиннее — отправляем файлом.
MESSAGE_LIMIT = 3900


def _draft_keyboard(post_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=f"done:{post_id}")
    kb.button(text="✏️ Редактировать", callback_data=f"edit:{post_id}")
    kb.button(text="🗑 Удалить", callback_data=f"reject:{post_id}")
    kb.adjust(3)
    return kb.as_markup()


def _render_progress(post_id: int, current_step: str, note: str = "", mode: str = "post") -> str:
    """Собирает текст статусного сообщения: пройденные шаги галочками,
    текущий — стрелкой, будущие — точками."""
    step_keys = [key for key, _ in STEPS]
    current_index = step_keys.index(current_step) if current_step in step_keys else -1

    lines = [f"⏳ Обрабатываю видео #{post_id}", ""]
    for i, (key, label) in enumerate(STEPS):
        if key == "generating":
            label = FINAL_STEP_LABEL.get(mode, label)
        if i < current_index:
            lines.append(f"✅ {label}")
        elif i == current_index:
            lines.append(f"▶️ {label}...")
        else:
            lines.append(f"⬜️ {label}")
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


async def _set_status(bot, post, current_step: str, note: str = ""):
    """Редактирует статусное сообщение. Молча игнорирует ошибки — статус
    это украшение, из-за него не должна падать обработка поста."""
    chat_id = post["status_chat_id"]
    message_id = post["status_message_id"]
    if not chat_id or not message_id:
        return
    try:
        await bot.edit_message_text(
            _render_progress(post["id"], current_step, note, post["mode"] or "post"),
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception as e:
        logger.debug(f"Не удалось обновить статусное сообщение: {e}")


async def process_queue_forever(bot, admin_id: int):
    while True:
        try:
            await _process_one(bot, admin_id)
        except Exception:
            logger.exception("Ошибка в цикле воркера")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def _transcript_keyboard(post_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Сделать пост", callback_data=f"baseprompt:{post_id}")
    kb.button(text="✍️ Пост со своим промптом", callback_data=f"customprompt:{post_id}")
    kb.adjust(2)
    return kb.as_markup()


async def _send_transcript(bot, chat_id: int, post_id: int, title: str, text: str, translated: bool):
    header = f"📝 Расшифровка #{post_id}" + (" (переведено на русский)" if translated else "")
    if title:
        header += f"\n🎬 {title}"
    if len(header) + len(text) + 2 <= MESSAGE_LIMIT:
        await bot.send_message(chat_id, f"{header}\n\n{text}", reply_markup=_transcript_keyboard(post_id))
        return
    # Длинная расшифровка — файлом, с началом текста в подписи (лимит подписи 1024).
    preview = text[:600].rsplit(" ", 1)[0] + "…"
    caption = f"{header}\n\n{preview}\n\nПолный текст — в файле."[:1024]
    await bot.send_document(
        chat_id,
        document=BufferedInputFile(text.encode("utf-8"), filename=f"transcript_{post_id}.txt"),
        caption=caption,
        reply_markup=_transcript_keyboard(post_id),
    )


async def _finish_status(bot, post, text: str):
    if post["status_chat_id"] and post["status_message_id"]:
        try:
            await bot.edit_message_text(text, chat_id=post["status_chat_id"], message_id=post["status_message_id"])
        except Exception as e:
            logger.debug(f"Не удалось обновить статусное сообщение: {e}")


async def _cancelled(post_id: int) -> bool:
    fresh = await get_post(post_id)
    return fresh is None or fresh["status"] == "rejected"


async def _process_one(bot, admin_id: int):
    post = await get_next_queued()
    if post is None:
        return

    post_id = post["id"]
    url = post["source_url"]

    try:
        transcript = post["transcript"]
        title = post["video_title"] or ""

        # Расшифровка — общий шаг для обоих режимов. Если она уже есть (например,
        # сначала делали расшифровку, а теперь просят пост) — не качаем заново.
        if not transcript:
            await update_post(post_id, status="downloading")
            await _set_status(bot, post, "downloading")
            audio_path, title = await download_audio(url)
            if await _cancelled(post_id):
                return

            await update_post(post_id, status="transcribing", video_title=title)
            await _set_status(bot, post, "transcribing", note=f"🎬 {title}")
            transcript = await transcribe_audio(audio_path)
            try:
                os.remove(audio_path)  # аудио больше не нужно
            except OSError:
                pass
            await update_post(post_id, transcript=transcript)
            logger.info(f"Пост #{post_id}: распознано моделью {current_model()}, {len(transcript)} символов")

        # Режим и промпт читаем заново: пока шло распознавание, их могли сменить кнопкой.
        post = await get_post(post_id)
        if post is None or post["status"] == "rejected":
            return
        mode = post["mode"] or "post"
        custom_prompt = post["custom_prompt"]

        if mode == "transcript":
            await update_post(post_id, status="generating")
            await _set_status(bot, post, "generating", note=f"🎬 {title}")
            translated = not is_mostly_russian(transcript)
            text = await translate_to_russian(transcript) if translated else transcript
            await update_post(post_id, status="done", draft_text=text)
            await _finish_status(bot, post, f"✅ Расшифровка #{post_id} готова\n🎬 {title}")
            await _send_transcript(bot, post["status_chat_id"] or admin_id, post_id, title, text, translated)
            return

        await update_post(post_id, status="generating")
        prompt_note = "🎯 Индивидуальный промпт" if custom_prompt else "📋 Базовый промпт"
        await _set_status(bot, post, "generating", note=f"🎬 {title}\n{prompt_note}")
        draft = await generate_draft(transcript, custom_prompt)

        await update_post(post_id, status="draft_ready", draft_text=draft)
        # Статусное сообщение превращаем в финальную отметку, а черновик
        # отправляем отдельно — так его удобнее копировать целиком.
        await _finish_status(bot, post, f"✅ Пост #{post_id} готов\n🎬 {title}")
        await bot.send_message(
            admin_id,
            f"📝 Черновик поста #{post_id}\n\n{draft}",
            reply_markup=_draft_keyboard(post_id),
        )

    except Exception as e:
        logger.exception(f"Не удалось обработать пост #{post_id}")
        await update_post(post_id, status="error", error_message=str(e))
        await _finish_status(bot, post, f"⚠️ Видео #{post_id} — ошибка при обработке")
        await bot.send_message(admin_id, f"⚠️ Ошибка при обработке #{post_id}: {e}")
