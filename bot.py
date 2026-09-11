"""
Главный вход. Telegram-бот на aiogram 3.x — интерфейс управления агентом:
  - принимает ссылки на YouTube-видео
  - показывает статус очереди
  - присылает черновики постов (редактировать / готово / удалить)

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
from db import init_db, add_post, update_post, get_posts_by_status
from worker import process_queue_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)/")


class EditState(StatesGroup):
    waiting_for_text = State()


def _draft_keyboard(post_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=f"done:{post_id}")
    kb.button(text="✏️ Редактировать", callback_data=f"edit:{post_id}")
    kb.button(text="🗑 Удалить", callback_data=f"reject:{post_id}")
    kb.adjust(3)
    return kb.as_markup()


def _is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_USER_ID


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "Привет! Кидай ссылку на YouTube-видео — поставлю в очередь на обработку.\n\n"
        "Когда черновик поста готов, пришлю его сюда — текст просто копируешь "
        "и публикуешь в Threads вручную.\n\n"
        "Команды:\n"
        "/queue — что сейчас в обработке\n"
        "/pending — черновики, ожидающие решения"
    )


@dp.message(F.text.regexp(YOUTUBE_RE))
async def handle_link(message: Message):
    if not _is_admin(message.from_user.id):
        return
    url = message.text.strip()
    post_id = await add_post(url)
    await message.answer(f"✅ Добавлено в очередь (#{post_id}). Обработаю в фоне и пришлю черновик.")


@dp.message(Command("queue"))
async def cmd_queue(message: Message):
    if not _is_admin(message.from_user.id):
        return
    lines = []
    for status in ["queued", "downloading", "transcribing", "generating"]:
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


@dp.message(EditState.waiting_for_text)
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
    asyncio.create_task(process_queue_forever(bot, config.ADMIN_USER_ID))
    logger.info("Бот запущен, жду сообщений...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
