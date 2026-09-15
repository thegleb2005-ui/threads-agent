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
    kb.button(text="📋 Базовый промпт", callback_data=f"baseprompt:{post_id}")
    kb.button(text="❌ Отмена", callback_data=f"cancel:{post_id}")
    kb.adjust(2)
    return kb.as_markup()


def _draft_keyboard(post_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=f"done:{post_id}")
    kb.button(text="✏️ Редактировать", callback_data=f"edit:{post_id}")
    kb.button(text="🗑 Удалить", callback_data=f"reject:{post_id}")
    kb.adjust(3)
    return kb.as_markup()


def _is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_USER_ID


async def _start_processing(post_id: int, chat_id: int, custom_prompt: str | None):
    """Переводит пост в очередь на обработку и создаёт статусное сообщение,
    которое воркер потом будет редактировать по ходу работы."""
    status_msg = await bot.send_message(chat_id, f"⏳ Пост #{post_id} поставлен в очередь...")
    await update_post(
        post_id,
        status="queued",
        custom_prompt=custom_prompt,
        status_chat_id=chat_id,
        status_message_id=status_msg.message_id,
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "Привет! Кидай ссылку на видео (YouTube, Instagram Reels, TikTok) — "
        "спрошу, каким промптом его обработать.\n\n"
        "Можно ответить своим текстом (индивидуальная инструкция для ИИ) "
        "или нажать кнопку базового промпта.\n\n"
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

    await state.update_data(pending_post_id=post_id)
    await state.set_state(PromptState.waiting_for_custom_prompt)

    await message.answer(
        f"🔗 Ссылка принята (#{post_id}).\n\n"
        f"Напиши, что сделать с этим видео — например:\n"
        f"• «переведи текст дословно, ничего не меняя»\n"
        f"• «сделай пост в 3 предложения, дерзкий тон»\n"
        f"• «оставь как есть, только разбей на абзацы»\n\n"
        f"Или жми кнопку, чтобы использовать базовый промпт.",
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
    await message.answer("🎯 Принял индивидуальный промпт, начинаю обработку.")
    await _start_processing(post_id, message.chat.id, custom_prompt)


@dp.callback_query(F.data.startswith("baseprompt:"))
async def cb_base_prompt(callback: CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        return
    post_id = int(callback.data.split(":")[1])
    await state.clear()
    await callback.message.edit_text(
        callback.message.text + "\n\n📋 Использую базовый промпт."
    )
    await _start_processing(post_id, callback.message.chat.id, None)
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

    asyncio.create_task(process_queue_forever(bot, config.ADMIN_USER_ID))
    logger.info(
        f"Бот запущен, жду сообщений... "
        f"(TRANSCRIBE_PROVIDER={config.TRANSCRIBE_PROVIDER!r}, KIE_MODEL={config.KIE_MODEL!r})"
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
