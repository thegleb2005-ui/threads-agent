"""
Фоновый воркер: периодически проверяет очередь на новые ссылки и прогоняет
их через пайплайн download -> transcribe -> generate -> draft_ready.

По ходу работы редактирует одно и то же сообщение в Telegram, показывая
живой статус ("Скачиваю аудио..." -> "Распознаю речь..." -> ...), чтобы
не спамить чат новыми сообщениями на каждый шаг. Готовый черновик уходит
отдельным сообщением с кнопками.
"""
import asyncio
import logging

from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import get_next_queued, update_post
from downloader import download_audio
from transcriber import transcribe_audio
from generator import generate_draft

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5

# Шаги пайплайна для отрисовки прогресса. Порядок важен.
STEPS = [
    ("downloading", "Скачиваю аудио"),
    ("transcribing", "Распознаю речь"),
    ("generating", "Пишу пост"),
]


def _draft_keyboard(post_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data=f"done:{post_id}")
    kb.button(text="✏️ Редактировать", callback_data=f"edit:{post_id}")
    kb.button(text="🗑 Удалить", callback_data=f"reject:{post_id}")
    kb.adjust(3)
    return kb.as_markup()


def _render_progress(post_id: int, current_step: str, note: str = "") -> str:
    """Собирает текст статусного сообщения: пройденные шаги галочками,
    текущий — стрелкой, будущие — точками."""
    step_keys = [key for key, _ in STEPS]
    current_index = step_keys.index(current_step) if current_step in step_keys else -1

    lines = [f"⏳ Обрабатываю пост #{post_id}", ""]
    for i, (key, label) in enumerate(STEPS):
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
            _render_progress(post["id"], current_step, note),
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


async def _process_one(bot, admin_id: int):
    post = await get_next_queued()
    if post is None:
        return

    post_id = post["id"]
    url = post["source_url"]
    custom_prompt = post["custom_prompt"]

    try:
        await update_post(post_id, status="downloading")
        await _set_status(bot, post, "downloading")
        audio_path, title = await download_audio(url)

        await update_post(post_id, status="transcribing", video_title=title)
        await _set_status(bot, post, "transcribing", note=f"🎬 {title}")
        transcript = await transcribe_audio(audio_path)

        await update_post(post_id, status="generating", transcript=transcript)
        prompt_note = "🎯 Индивидуальный промпт" if custom_prompt else "📋 Базовый промпт"
        await _set_status(bot, post, "generating", note=f"🎬 {title}\n{prompt_note}")
        draft = await generate_draft(transcript, custom_prompt)

        await update_post(post_id, status="draft_ready", draft_text=draft)

        # Статусное сообщение превращаем в финальную отметку, а черновик
        # отправляем отдельно — так его удобнее копировать целиком.
        if post["status_chat_id"] and post["status_message_id"]:
            try:
                await bot.edit_message_text(
                    f"✅ Пост #{post_id} готов\n🎬 {title}",
                    chat_id=post["status_chat_id"],
                    message_id=post["status_message_id"],
                )
            except Exception as e:
                logger.debug(f"Не удалось финализировать статусное сообщение: {e}")

        await bot.send_message(
            admin_id,
            f"📝 Черновик поста #{post_id}\n\n{draft}",
            reply_markup=_draft_keyboard(post_id),
        )

    except Exception as e:
        logger.exception(f"Не удалось обработать пост #{post_id}")
        await update_post(post_id, status="error", error_message=str(e))
        if post["status_chat_id"] and post["status_message_id"]:
            try:
                await bot.edit_message_text(
                    f"⚠️ Пост #{post_id} — ошибка при обработке",
                    chat_id=post["status_chat_id"],
                    message_id=post["status_message_id"],
                )
            except Exception:
                pass
        await bot.send_message(admin_id, f"⚠️ Ошибка при обработке поста #{post_id}: {e}")
