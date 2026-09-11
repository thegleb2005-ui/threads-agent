"""
Фоновый воркер: раз в POLL_INTERVAL_SECONDS проверяет очередь на новые ссылки
и прогоняет их через пайплайн download -> transcribe -> generate -> draft_ready.
Готовый черновик уходит админу в Telegram с кнопками модерации.
"""
import asyncio
import logging

from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import get_next_queued, update_post
from downloader import download_audio
from transcriber import transcribe_audio
from generator import generate_draft

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30


def _moderation_keyboard(post_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"approve:{post_id}")
    kb.button(text="✏️ Редактировать", callback_data=f"edit:{post_id}")
    kb.button(text="🗑 Удалить", callback_data=f"reject:{post_id}")
    kb.adjust(3)
    return kb.as_markup()


async def process_queue_forever(bot, admin_id: int):
    while True:
        try:
            await _process_one(bot, admin_id)
        except Exception:
            logger.exception("Ошибка в цикле воркера")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _process_one(bot, admin_id: int):
    post = await get_next_queued()
    if post is None:
        return

    post_id = post["id"]
    url = post["source_url"]

    try:
        await update_post(post_id, status="downloading")
        audio_path, title = await download_audio(url)

        await update_post(post_id, status="transcribing", video_title=title)
        transcript = await transcribe_audio(audio_path)

        await update_post(post_id, status="generating", transcript=transcript)
        draft = await generate_draft(transcript)

        await update_post(post_id, status="draft_ready", draft_text=draft)

        text = f"📝 Черновик поста #{post_id}\nИсточник: {title}\n\n{draft}"
        await bot.send_message(admin_id, text, reply_markup=_moderation_keyboard(post_id))

    except Exception as e:
        logger.exception(f"Не удалось обработать пост #{post_id}")
        await update_post(post_id, status="error", error_message=str(e))
        await bot.send_message(admin_id, f"⚠️ Ошибка при обработке поста #{post_id}: {e}")
