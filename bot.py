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
from db import init_db, recover_stuck_posts, add_post, update_post, get_post, get_posts_by_status
from worker import process_queue_forever

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


def _prompt_choice_keyboard(post_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Расшифровка на русском", callback_data=f"transcript:{post_id}")
    kb.button(text="📋 Базовый промпт", callback_data=f"baseprompt:{post_id}")
    kb.button(text="❌ Отмена", callback_data=f"cancel:{post_id}")
    kb.adjust(1, 2)
    return kb.as_markup()


def _youtube_default_keyboard(post_id: int):
    """Для YouTube расшифровка уже запущена — кнопки, чтобы вместо неё сделать пост."""
    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Пост по базовому промпту", callback_data=f"baseprompt:{post_id}")
    kb.button(text="❌ Отмена", callback_data=f"cancel:{post_id}")
    kb.adjust(1, 1)
    return kb.as_markup()


def _is_youtube(url: str) -> bool:
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def _draft_keyboard(post_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=f"done:{post_id}")
    kb.button(text="✏️ Редактировать", callback_data=f"edit:{post_id}")
    kb.button(text="🗑 Удалить", callback_data=f"reject:{post_id}")
    kb.adjust(3)
    return kb.as_markup()


def _is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_USER_ID


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
        await update_post(post_id, mode=mode, custom_prompt=custom_prompt)
        return "Ок, учту это, как только закончится распознавание."
    if st == "generating":
        return "Сейчас идёт последний шаг — дождись результата и нажми кнопку ещё раз."
    await _start_processing(post_id, chat_id, custom_prompt, mode)
    return "Запускаю заново." + (" Расшифровка уже есть — будет быстро." if post["transcript"] else "")


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "Привет! Кидай ссылку на видео (YouTube, Instagram Reels, TikTok).\n\n"
        "• Для YouTube сразу делаю расшифровку на русском. Если нужен пост — "
        "нажми кнопку или напиши свой промпт.\n"
        "• Для остальных ссылок спрошу: расшифровка, пост по базовому промпту "
        "или твой промпт текстом.\n\n"
        "Команды:\n"
        "/queue — что сейчас в обработке\n"
        "/pending — черновики, ожидающие решения"
    )


@dp.message(_contains_video_link)
async def handle_link(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    match = VIDEO_LINK_RE.search(message.text)
    url = match.group(0)
    post_id = await add_post(url)

    # Свой промпт можно прислать текстом в любой момент после ссылки.
    await state.update_data(pending_post_id=post_id)
    await state.set_state(PromptState.waiting_for_custom_prompt)

    if config.YOUTUBE_DEFAULT_TRANSCRIPT and _is_youtube(url):
        await message.answer(
            f"🔗 Ссылка принята (#{post_id}). Делаю расшифровку на русском.\n\n"
            f"Если нужен пост — нажми кнопку или просто напиши свой промпт.",
            reply_markup=_youtube_default_keyboard(post_id),
        )
        await _start_processing(post_id, message.chat.id, None, mode="transcript")
        return

    await message.answer(
        f"🔗 Ссылка принята (#{post_id}).\n\n"
        f"Выбери, что сделать, или напиши свой промпт, например:\n"
        f"• «сделай пост в 3 предложения, дерзкий тон»\n"
        f"• «оставь как есть, только разбей на абзацы»",
        reply_markup=_prompt_choice_keyboard(post_id),
    )


@dp.message(PromptState.waiting_for_custom_prompt, ~F.text.startswith("/"))
async def handle_custom_prompt(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    post_id = data.get("pending_post_id")
    if not post_id:
        await state.clear()
        return

    custom_prompt = message.text.strip()
    await state.clear()
    result = await _apply_choice(post_id, message.chat.id, "post", custom_prompt)
    await message.answer(f"🎯 Принял свой промпт для #{post_id}. {result}")


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
    if not _is_admin(callback.from_user.id):
        return
    post_id = int(callback.data.split(":")[1])
    await state.clear()
    result = await _apply_choice(post_id, callback.message.chat.id, "post", None)
    await _mark_choice(callback, f"📋 Пост по базовому промпту. {result}")
    await callback.answer()


@dp.callback_query(F.data.startswith("transcript:"))
async def cb_transcript(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    post_id = int(callback.data.split(":")[1])
    await state.clear()
    result = await _apply_choice(post_id, callback.message.chat.id, "transcript", None)
    await _mark_choice(callback, f"📝 Расшифровка на русском. {result}")
    await callback.answer()


@dp.callback_query(F.data.startswith("customprompt:"))
async def cb_custom_prompt(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    post_id = int(callback.data.split(":")[1])
    await state.update_data(pending_post_id=post_id)
    await state.set_state(PromptState.waiting_for_custom_prompt)
    await callback.message.answer(f"✍️ Напиши промпт для поста по видео #{post_id}:")
    await callback.answer()


@dp.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    post_id = int(callback.data.split(":")[1])
    await state.clear()
    await update_post(post_id, status="rejected")
    await callback.message.edit_text(f"❌ Отменено (#{post_id}).")
    await callback.answer()


@dp.message(Command("queue"))
async def cmd_queue(message: Message):
    if not _is_admin(message.from_user.id):
        return
    lines = []
    for status in ["awaiting_prompt", "queued", "downloading", "transcribing", "generating"]:
        rows = await get_posts_by_status(status, limit=10)
        for row in rows:
            lines.append(f"#{row['id']} [{status}] {row['source_url']}")
    await message.answer("\n".join(lines) if lines else "Очередь пуста.")


@dp.message(Command("pending"))
async def cmd_pending(message: Message):
    if not _is_admin(message.from_user.id):
        return
    rows = await get_posts_by_status("draft_ready", limit=10)
    if not rows:
        await message.answer("Нет черновиков, ожидающих решения.")
        return
    for row in rows:
        text = f"📝 #{row['id']}: {row['video_title']}\n\n{row['draft_text']}"
        await message.answer(text, reply_markup=_draft_keyboard(row["id"]))


@dp.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    post_id = int(callback.data.split(":")[1])
    await update_post(post_id, status="done")
    await callback.message.edit_text(
        callback.message.text + "\n\n✅ Готово — текст выше можно копировать и публиковать."
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("reject:"))
async def cb_reject(callback: CallbackQuery):
    if not _is_admin(callback.from_user.id):
        return
    post_id = int(callback.data.split(":")[1])
    await update_post(post_id, status="rejected")
    await callback.message.edit_text(callback.message.text + "\n\n🗑 Удалено.")
    await callback.answer()


@dp.callback_query(F.data.startswith("edit:"))
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    post_id = int(callback.data.split(":")[1])
    await state.update_data(post_id=post_id)
    await state.set_state(EditState.waiting_for_text)
    await callback.message.answer(f"Пришли новый текст для поста #{post_id}:")
    await callback.answer()


@dp.message(EditState.waiting_for_text, ~F.text.startswith("/"))
async def process_edit(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    post_id = data["post_id"]
    await update_post(post_id, status="draft_ready", draft_text=message.text)
    await state.clear()
    await message.answer(
        f"Обновлено #{post_id}:\n\n{message.text}",
        reply_markup=_draft_keyboard(post_id),
    )


async def main():
    config.validate()
    await init_db()

    recovered = await recover_stuck_posts()
    if recovered:
        logger.warning(f"Восстановлено {recovered} зависших поста(ов) после предыдущего сбоя")

    cookies_status = "используются" if downloader._cookies_active() else "НЕ используются"

    asyncio.create_task(process_queue_forever(bot, config.ADMIN_USER_ID))
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
