"""
Главный вход. Telegram-бот на aiogram 3.x — интерфейс управления агентом:
  - принимает ссылки на видео (YouTube, Instagram Reels, TikTok)
  - спрашивает, каким промптом обрабатывать: базовым или индивидуальным
  - показывает живой статус обработки
  - присылает черновики постов (готово / редактировать / удалить)

Публикация в Threads пока ручная — бот только готовит текст, ты сам
копируешь его и постишь. Автопубликация (threads_api.py, scheduler.py)
уже написана и лежит в проекте на случай, если решишь включить её позже
(см. README, раздел "Как включить автопубликацию").

Запуск: python bot.py  (см. README.md для полной инструкции по деплою)
"""
import asyncio
import logging
import re

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import config
import downloader
from db import (init_db, recover_stuck_posts, add_post, update_post, get_post,
                count_user_posts_today, count_user_active, get_user_posts, get_stats, ACTIVE_STATUSES)
from worker import process_queue_forever, choice_keyboard, DOWNLOAD_MODES

BOT_VERSION = "2026-10-05 v6: скачивание видео и звука"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Поддерживаемые площадки. yt-dlp умеет скачивать со всех трёх, но
# Instagram и TikTok заметно агрессивнее блокируют запросы с серверных IP,
# чем YouTube — если упрёмся в "требуется авторизация", поможет COOKIES_FILE
# (см. README, раздел про блокировки).
VIDEO_LINK_RE = re.compile(
    r"(https?://)?(www\.)?("
    r"youtube\.com/\S+|youtu\.be/\S+"
    r"|instagram\.com/\S+"
    r"|tiktok\.com/\S+|vm\.tiktok\.com/\S+"
    r")",
    re.IGNORECASE,
)


def _contains_video_link(message: Message) -> bool:
    return bool(message.text and VIDEO_LINK_RE.search(message.text))


class PromptState(StatesGroup):
    waiting_for_custom_prompt = State()


class EditState(StatesGroup):
    waiting_for_text = State()


def _is_youtube(url: str) -> bool:
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def _draft_keyboard(post_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=f"done:{post_id}")
    kb.button(text="✏️ Редактировать", callback_data=f"edit:{post_id}")
    kb.button(text="🗑 Удалить", callback_data=f"reject:{post_id}")
    kb.adjust(1)
    return kb.as_markup()


def _is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_USER_ID


def _allowed(user_id: int) -> bool:
    """Пустой ALLOWED_USERS — бот открыт для всех."""
    return not config.ALLOWED_USERS or user_id in config.ALLOWED_USERS or _is_admin(user_id)


async def _allowed_or_tell(message: Message) -> bool:
    if _allowed(message.from_user.id):
        return True
    await message.answer("Доступ к боту ограничен. Напиши его владельцу, если хочешь пользоваться.")
    return False


async def _owns(user_id: int, post_id: int) -> bool:
    post = await get_post(post_id)
    if post is None:
        return False
    return _is_admin(user_id) or post["user_id"] == user_id or (post["user_id"] is None and _is_admin(user_id))


async def _start_processing(post_id: int, chat_id: int, custom_prompt: str | None, mode: str = "post"):
    """Переводит видео в очередь и создаёт статусное сообщение, которое воркер
    потом редактирует по ходу работы."""
    status_msg = await bot.send_message(chat_id, f"⏳ Видео #{post_id} поставлено в очередь...")
    await update_post(
        post_id,
        status="queued",
        mode=mode,
        custom_prompt=custom_prompt,
        status_chat_id=chat_id,
        status_message_id=status_msg.message_id,
    )


async def _apply_choice(post_id: int, chat_id: int, mode: str, custom_prompt: str | None) -> str:
    """Применяет выбор (расшифровка / пост) в любой момент жизни видео:
      - ещё не запускали — запускаем;
      - качается или распознаётся — меняем режим, воркер учтёт его в конце;
      - уже готово (или упало) — запускаем заново; если расшифровка уже есть,
        повторно ничего не качаем и не распознаём.
    Возвращает короткий текст для пользователя."""
    post = await get_post(post_id)
    if post is None:
        return "Видео не найдено."
    st = post["status"]
    if st == "awaiting_prompt":
        await _start_processing(post_id, chat_id, custom_prompt, mode)
        return "Запускаю обработку."
    if st in ("queued", "downloading", "transcribing"):
        if (post["mode"] or "post") == mode and (custom_prompt or None) == (post["custom_prompt"] or None):
            return "Уже делаю."
        if mode in DOWNLOAD_MODES or (post["mode"] or "post") in DOWNLOAD_MODES:
            return "Дождись, пока закончится текущее действие, и нажми кнопку ещё раз."
        await update_post(post_id, mode=mode, custom_prompt=custom_prompt)
        return "Ок, учту это, как только закончится распознавание."
    if st == "generating":
        return "Сейчас идёт последний шаг — дождись результата и нажми кнопку ещё раз."
    await _start_processing(post_id, chat_id, custom_prompt, mode)
    return "Запускаю заново." + (" Расшифровка уже есть — будет быстро." if post["transcript"] else "")


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not await _allowed_or_tell(message):
        return
    await message.answer(
        "Привет! Пришли ссылку на видео (YouTube, Instagram Reels, TikTok), "
        "голосовое, кружок, видео или аудиофайл до 20 МБ.\n\n"
        "Я спрошу, что сделать:\n"
        "📝 Расшифровка на русском — полный текст файлом и выжимка сути\n"
        "📱 Пост для соц сетей\n"
        "🎬 Скачать видео / 🎵 звук (mp3) — до 50 МБ, качество подберу сам\n"
        "Или просто напиши свой промпт для поста.\n\n"
        "Команды:\n"
        "/queue — что сейчас в обработке\n"
        "/pending — черновики, ожидающие решения"
    )


async def _limits_ok(message: Message) -> bool:
    uid = message.from_user.id
    if _is_admin(uid):
        return True
    if config.DAILY_LIMIT_PER_USER and await count_user_posts_today(uid) >= config.DAILY_LIMIT_PER_USER:
        await message.answer(f"На сегодня лимит исчерпан ({config.DAILY_LIMIT_PER_USER} видео в сутки). "
                             f"Приходи завтра!")
        return False
    if await count_user_active(uid) >= config.MAX_QUEUED_PER_USER:
        await message.answer(f"У тебя уже {config.MAX_QUEUED_PER_USER} видео в работе. "
                             f"Дождись результата и присылай следующее. Список — /queue")
        return False
    return True


async def _offer_choice(message: Message, state: FSMContext, source: str, intro: str):
    """Создаёт видео и показывает кнопки. Обработка начнётся только после выбора
    (или после того, как пользователь напишет свой промпт)."""
    name = message.from_user.username or message.from_user.full_name or ""
    post_id = await add_post(source, message.from_user.id, name)
    await state.update_data(pending_post_id=post_id)
    await state.set_state(PromptState.waiting_for_custom_prompt)
    await message.answer(
        f"{intro} (#{post_id})\n\nЧто сделать? Можно нажать кнопку или написать свой промпт для поста.",
        reply_markup=choice_keyboard(post_id, source),
    )


@dp.message(_contains_video_link)
async def handle_link(message: Message, state: FSMContext):
    if not await _allowed_or_tell(message) or not await _limits_ok(message):
        return
    url = VIDEO_LINK_RE.search(message.text).group(0)
    await _offer_choice(message, state, url, "🔗 Ссылка принята")


def _telegram_media(message: Message):
    """(тип, file_id, размер, подпись) для голосовых, кружочков, видео и аудио."""
    if message.voice:
        return "voice", message.voice.file_id, message.voice.file_size, "🎙 Голосовое принято"
    if message.video_note:
        return "video_note", message.video_note.file_id, message.video_note.file_size, "⏺ Кружок принят"
    if message.audio:
        return "audio", message.audio.file_id, message.audio.file_size, "🎵 Аудио принято"
    if message.video:
        return "video", message.video.file_id, message.video.file_size, "🎬 Видео принято"
    doc = message.document
    if doc and (doc.mime_type or "").startswith(("audio/", "video/")):
        return "document", doc.file_id, doc.file_size, "📎 Файл принят"
    return None


@dp.message(F.voice | F.video_note | F.audio | F.video | F.document)
async def handle_media(message: Message, state: FSMContext):
    if not await _allowed_or_tell(message):
        return
    media = _telegram_media(message)
    if media is None:
        await message.answer("Пришли ссылку на видео, голосовое, кружок, видео или аудиофайл.")
        return
    kind, file_id, size, intro = media
    if size and size > config.TELEGRAM_FILE_LIMIT_MB * 1024 * 1024:
        await message.answer(
            f"Файл больше {config.TELEGRAM_FILE_LIMIT_MB} МБ — Telegram не даёт ботам скачивать такие. "
            f"Пришли ссылку на видео или файл покороче.")
        return
    if not await _limits_ok(message):
        return
    await _offer_choice(message, state, f"tg:{kind}:{file_id}", intro)


@dp.message(PromptState.waiting_for_custom_prompt, ~F.text.startswith("/"))
async def handle_custom_prompt(message: Message, state: FSMContext):
    if not await _allowed_or_tell(message):
        return
    data = await state.get_data()
    post_id = data.get("pending_post_id")
    if not post_id:
        await state.clear()
        return

    custom_prompt = message.text.strip()
    result = await _apply_choice(post_id, message.chat.id, "post", custom_prompt)
    await message.answer(
        f"🎯 Принял свой промпт для #{post_id}. {result}\n"
        f"Можно прислать ещё промпт — сделаю другой вариант."
    )


async def _mark_choice(callback: CallbackQuery, note: str):
    """Дописывает выбор к сообщению с кнопками и убирает кнопки."""
    try:
        if callback.message.text:
            await callback.message.edit_text(callback.message.text + f"\n\n{note}")
        else:  # сообщение с файлом — меняем только кнопки
            await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("baseprompt:"))
async def cb_base_prompt(callback: CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split(":")[1])
    if not await _owns(callback.from_user.id, post_id):
        await callback.answer("Это видео другого пользователя", show_alert=True)
        return
    await state.update_data(pending_post_id=post_id)
    await state.set_state(PromptState.waiting_for_custom_prompt)
    result = await _apply_choice(post_id, callback.message.chat.id, "post", None)
    await _mark_choice(callback, f"📋 Пост по базовому промпту. {result}")
    await callback.answer()


@dp.callback_query(F.data.startswith("social:") | F.data.startswith("igpost:"))
async def cb_social_post(callback: CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split(":")[1])
    if not await _owns(callback.from_user.id, post_id):
        await callback.answer("Это видео другого пользователя", show_alert=True)
        return
    await state.update_data(pending_post_id=post_id)
    await state.set_state(PromptState.waiting_for_custom_prompt)
    result = await _apply_choice(post_id, callback.message.chat.id, "post", config.SOCIAL_POST_PROMPT)
    await _mark_choice(callback, f"📱 Пост для соц сетей. {result}")
    await callback.answer()


@dp.callback_query(F.data.startswith("transcript:"))
async def cb_transcript(callback: CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split(":")[1])
    if not await _owns(callback.from_user.id, post_id):
        await callback.answer("Это видео другого пользователя", show_alert=True)
        return
    await state.update_data(pending_post_id=post_id)
    await state.set_state(PromptState.waiting_for_custom_prompt)
    result = await _apply_choice(post_id, callback.message.chat.id, "transcript", None)
    await _mark_choice(callback, f"📝 Расшифровка на русском. {result}")
    await callback.answer()


async def _download_choice(callback: CallbackQuery, state: FSMContext, mode: str, note: str):
    post_id = int(callback.data.split(":")[1])
    if not await _owns(callback.from_user.id, post_id):
        await callback.answer("Это видео другого пользователя", show_alert=True)
        return
    await state.update_data(pending_post_id=post_id)
    await state.set_state(PromptState.waiting_for_custom_prompt)
    result = await _apply_choice(post_id, callback.message.chat.id, mode, None)
    await _mark_choice(callback, f"{note} {result}")
    await callback.answer()


@dp.callback_query(F.data.startswith("dlvideo:"))
async def cb_download_video(callback: CallbackQuery, state: FSMContext):
    await _download_choice(callback, state, "video", "🎬 Скачиваю видео.")


@dp.callback_query(F.data.startswith("dlaudio:"))
async def cb_download_audio(callback: CallbackQuery, state: FSMContext):
    await _download_choice(callback, state, "audio", "🎵 Скачиваю звук.")


@dp.callback_query(F.data.startswith("customprompt:"))
async def cb_custom_prompt(callback: CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split(":")[1])
    if not await _owns(callback.from_user.id, post_id):
        await callback.answer("Это видео другого пользователя", show_alert=True)
        return
    await state.update_data(pending_post_id=post_id)
    await state.set_state(PromptState.waiting_for_custom_prompt)
    await callback.message.answer(f"✍️ Напиши промпт для поста по видео #{post_id}:")
    await callback.answer()


@dp.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split(":")[1])
    if not await _owns(callback.from_user.id, post_id):
        await callback.answer("Это видео другого пользователя", show_alert=True)
        return
    await state.clear()
    await update_post(post_id, status="rejected")
    await callback.message.edit_text(f"❌ Отменено (#{post_id}).")
    await callback.answer()


STATUS_RU = {"awaiting_prompt": "ждёт выбора", "queued": "в очереди", "downloading": "скачивается",
             "transcribing": "распознаётся", "generating": "финальный шаг"}


@dp.message(Command("queue"))
async def cmd_queue(message: Message):
    if not await _allowed_or_tell(message):
        return
    uid = message.from_user.id
    rows = await get_user_posts(uid, ("awaiting_prompt",) + ACTIVE_STATUSES)
    lines = [f"#{r['id']} — {STATUS_RU.get(r['status'], r['status'])}: {r['video_title'] or r['source_url']}" for r in rows]
    text = "Твои видео в работе:\n" + "\n".join(lines) if lines else "У тебя нет видео в работе."
    if _is_admin(uid):
        everyone = await get_user_posts(None, ACTIVE_STATUSES, limit=50)
        text += f"\n\nВсего в работе у всех: {len(everyone)}. Подробнее — /stats"
    await message.answer(text)


@dp.message(Command("pending"))
async def cmd_pending(message: Message):
    if not await _allowed_or_tell(message):
        return
    rows = await get_user_posts(message.from_user.id, ("draft_ready",), limit=10)
    if not rows:
        await message.answer("Нет черновиков, ожидающих решения.")
        return
    for row in rows:
        text = f"📝 #{row['id']}: {row['video_title']}\n\n{row['draft_text']}"
        await message.answer(text, reply_markup=_draft_keyboard(row["id"]))


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not _is_admin(message.from_user.id):
        return
    s = await get_stats()
    await message.answer(
        f"📊 Статистика\n\n"
        f"Пользователей всего: {s['users_total']}, сегодня: {s['users_today']}\n"
        f"Видео сегодня: {s['videos_today']} (из субтитров: {s['subtitles_today']}, "
        f"скачиваний: {s['downloads_today']}, ошибок: {s['errors_today']})\n"
        f"Сейчас в очереди: {s['in_queue']}, в работе: {s['in_work']}\n\n"
        f"Доступ: {'только список ALLOWED_USERS' if config.ALLOWED_USERS else 'открыт для всех'}, "
        f"лимит: {config.DAILY_LIMIT_PER_USER or 'без лимита'} видео/сутки на человека"
    )


@dp.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery):
    post_id = int(callback.data.split(":")[1])
    if not await _owns(callback.from_user.id, post_id):
        await callback.answer("Это видео другого пользователя", show_alert=True)
        return
    await update_post(post_id, status="done")
    await callback.message.edit_text(
        callback.message.text + "\n\n✅ Готово — текст выше можно копировать и публиковать."
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("reject:"))
async def cb_reject(callback: CallbackQuery):
    post_id = int(callback.data.split(":")[1])
    if not await _owns(callback.from_user.id, post_id):
        await callback.answer("Это видео другого пользователя", show_alert=True)
        return
    await update_post(post_id, status="rejected")
    await callback.message.edit_text(callback.message.text + "\n\n🗑 Удалено.")
    await callback.answer()


@dp.callback_query(F.data.startswith("edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split(":")[1])
    if not await _owns(callback.from_user.id, post_id):
        await callback.answer("Это видео другого пользователя", show_alert=True)
        return
    await state.update_data(post_id=post_id)
    await state.set_state(EditState.waiting_for_text)
    await callback.message.answer(f"Пришли новый текст для поста #{post_id}:")
    await callback.answer()


@dp.message(EditState.waiting_for_text, ~F.text.startswith("/"))
async def process_edit(message: Message, state: FSMContext):
    if not await _allowed_or_tell(message):
        return
    data = await state.get_data()
    post_id = data["post_id"]
    await update_post(post_id, status="draft_ready", draft_text=message.text)
    await state.clear()
    await message.answer(
        f"Обновлено #{post_id}:\n\n{message.text}",
        reply_markup=_draft_keyboard(post_id),
    )


@dp.message(F.text, ~F.text.startswith("/"))
async def fallback_text(message: Message):
    """Текст без ссылки и без выбранного видео — подсказываем, а не молчим."""
    if not await _allowed_or_tell(message):
        return
    await message.answer(
        "Не понял, к какому видео это относится. Пришли ссылку на видео, "
        "а потом свой промпт — или нажми кнопку под нужной расшифровкой."
    )


async def main():
    config.validate()
    await init_db()

    recovered = await recover_stuck_posts()
    if recovered:
        logger.warning(f"Восстановлено {recovered} зависших поста(ов) после предыдущего сбоя")

    cookies_status = "используются" if downloader._cookies_active() else "НЕ используются"

    asyncio.create_task(process_queue_forever(bot, config.ADMIN_USER_ID))
    import transcriber as _tr
    logger.info(
        f"ВЕРСИЯ БОТА: {BOT_VERSION} | Whisper: модель={config.WHISPER_MODEL_SIZE}, "
        f"beam={config.WHISPER_BEAM_SIZE}, ядер={config.WHISPER_CPU_THREADS}, "
        f"кусок={_tr.CHUNK_SECONDS}с | "
        f"доступ: {'список' if config.ALLOWED_USERS else 'все'}, лимит {config.DAILY_LIMIT_PER_USER}/сутки, "
        f"субтитры YouTube: {'да' if config.USE_YOUTUBE_SUBTITLES else 'нет'}"
    )
    logger.info(
        f"Бот запущен, жду сообщений... "
        f"(TRANSCRIBE_PROVIDER={config.TRANSCRIBE_PROVIDER!r}, KIE_MODEL={config.KIE_MODEL!r}, "
        f"COOKIES_FILE={config.COOKIES_FILE!r}, cookies {cookies_status})"
    )

    if recovered:
        try:
            await bot.send_message(
                config.ADMIN_USER_ID,
                f"⚠️ Бот перезапустился и вернул в очередь {recovered} зависший "
                f"пост(ов) — вероятно, прошлый процесс упал во время обработки. "
                f"Обработаю их заново."
            )
        except Exception:
            pass

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
