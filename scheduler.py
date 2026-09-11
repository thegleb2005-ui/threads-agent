"""
Планировщик автопубликации. В моменты времени из POST_TIMES (см. config.py)
берёт САМЫЙ СТАРЫЙ пост со статусом 'approved' и публикует его в Threads.

Если одобренных постов нет — просто ничего не делает в этот слот.
"""
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from db import get_oldest_approved, update_post
from threads_api import publish_post
from config import POST_TIMES

logger = logging.getLogger(__name__)


def setup_scheduler(bot, admin_id: int) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    for time_str in POST_TIMES:
        hour, minute = time_str.split(":")
        scheduler.add_job(
            _publish_next_approved,
            CronTrigger(hour=int(hour), minute=int(minute)),
            args=[bot, admin_id],
        )
    scheduler.start()
    logger.info(f"Планировщик запущен, слоты публикации: {POST_TIMES}")
    return scheduler


async def _publish_next_approved(bot, admin_id: int):
    post = await get_oldest_approved()
    if post is None:
        logger.info("Слот публикации наступил, но одобренных постов нет")
        return

    post_id = post["id"]
    try:
        threads_post_id = await publish_post(post["draft_text"])
        await update_post(post_id, status="published", threads_post_id=threads_post_id)
        await bot.send_message(
            admin_id, f"🚀 Пост #{post_id} опубликован в Threads (id: {threads_post_id})"
        )
    except Exception as e:
        logger.exception(f"Не удалось опубликовать пост #{post_id}")
        await update_post(post_id, status="error", error_message=str(e))
        await bot.send_message(admin_id, f"⚠️ Не удалось опубликовать пост #{post_id}: {e}")
