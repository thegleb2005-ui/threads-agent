"""
Статистика бота для админа.

  /stats   — подробная сводка: пользователи, активность, что выбирают, топ
  /export  — выгрузка в CSV для Excel (пользователи, действия, видео)
  /backup  — резервная копия базы прямо сейчас
  /restore — восстановить базу из присланного файла копии (подпись /restore)

Каждый день в DAILY_REPORT_HOUR_UTC (по умолчанию 09:00 по Москве) бот сам
присылает админу отчёт за вчера и файл резервной копии базы. Отметка о
последнем отчёте хранится в базе, поэтому после перезапусков он не дублируется.
"""
import io
import csv
import os
import shutil
import sqlite3
import asyncio
import logging
import tempfile
from datetime import datetime, timedelta, timezone

from aiogram.types import BufferedInputFile

import config
import db

logger = logging.getLogger(__name__)

ACTION_LABELS = {
    "transcript": "Расшифровка", "social": "Пост для соц сетей", "custom": "Свой промпт",
    "base": "Базовый промпт", "video": "Скачать видео", "audio": "Скачать звук",
}
PLATFORM_LABELS = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok", "other": "другое"}
MEDIA_LABELS = {"voice": "голосовые", "video_note": "кружки", "audio": "аудио", "video": "видео", "document": "файлы"}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_start(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _pct(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "—"


async def _counts(type_: str, since: str, until: str | None = None) -> dict:
    sql = "SELECT detail, COUNT(*) AS n FROM events WHERE type = ? AND created_at >= ?"
    args = [type_, since]
    if until:
        sql += " AND created_at < ?"
        args.append(until)
    rows = await db.query(sql + " GROUP BY detail", *args)
    return {r["detail"] or "": r["n"] for r in rows}


async def _top_users(since: str, limit: int = 5):
    return await db.query(
        "SELECT e.user_id, u.username, u.full_name, COUNT(*) AS n FROM events e "
        "LEFT JOIN users u ON u.user_id = e.user_id "
        "WHERE e.created_at >= ? AND e.type IN ('link','media','choice') AND e.user_id IS NOT NULL "
        "GROUP BY e.user_id ORDER BY n DESC LIMIT ?", since, limit)


def _name(row) -> str:
    if row["username"]:
        return f"@{row['username']}"
    return row["full_name"] or f"id {row['user_id']}"


def _line(counts: dict, labels: dict) -> str:
    parts = [f"{labels.get(k, k)}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]) if v]
    return " · ".join(parts) if parts else "—"


async def full_stats_text() -> str:
    now = _utc_now()
    today = _iso(_day_start(now))
    d7, d30 = _iso(now - timedelta(days=7)), _iso(now - timedelta(days=30))

    total = await db.scalar("SELECT COUNT(*) FROM users")
    new_today = await db.scalar("SELECT COUNT(*) FROM users WHERE first_seen >= ?", today)
    new_7 = await db.scalar("SELECT COUNT(*) FROM users WHERE first_seen >= ?", d7)
    new_30 = await db.scalar("SELECT COUNT(*) FROM users WHERE first_seen >= ?", d30)
    act_today = await db.scalar("SELECT COUNT(*) FROM users WHERE last_seen >= ?", today)
    act_7 = await db.scalar("SELECT COUNT(*) FROM users WHERE last_seen >= ?", d7)
    act_30 = await db.scalar("SELECT COUNT(*) FROM users WHERE last_seen >= ?", d30)
    silent = await db.scalar(
        "SELECT COUNT(*) FROM users u WHERE NOT EXISTS (SELECT 1 FROM events e "
        "WHERE e.user_id = u.user_id AND e.type IN ('link','media'))")

    links = await _counts("link", d7)
    media = await _counts("media", d7)
    choices = await _counts("choice", d7)
    done = sum((await _counts("done", d7)).values())
    errors = sum((await _counts("error", d7)).values())
    limits = sum((await _counts("limit", d7)).values())

    lines = [
        "📊 Статистика", "",
        "👥 Пользователи",
        f"Всего: {total} (новых: сегодня {new_today} · 7 дней {new_7} · 30 дней {new_30})",
        f"Активны: сегодня {act_today} · 7 дней {act_7} · 30 дней {act_30}",
        f"Зашли, но ничего не отправили: {silent} ({_pct(silent, total)})",
        "",
        "🎬 За 7 дней",
        f"Ссылок: {sum(links.values())} ({_line(links, PLATFORM_LABELS)})",
        f"Файлов из Telegram: {sum(media.values())} ({_line(media, MEDIA_LABELS)})",
        f"Выбор: {_line(choices, ACTION_LABELS)}",
        f"Готово: {done} · ошибок: {errors} (успешно {_pct(done, done + errors)})",
    ]
    if limits:
        lines.append(f"Упёрлись в лимит: {limits} раз")
    top = await _top_users(d30)
    if top:
        lines += ["", "🏆 Самые активные за 30 дней"]
        lines += [f"{i}. {_name(r)} — {r['n']}" for i, r in enumerate(top, 1)]
    in_queue = await db.scalar("SELECT COUNT(*) FROM posts WHERE status = 'queued'")
    in_work = await db.scalar("SELECT COUNT(*) FROM posts WHERE status IN ('downloading','transcribing','generating')")
    lines += ["", "⚙️ Сейчас",
              f"В очереди: {in_queue} · в работе: {in_work}",
              f"Доступ: {'по списку' if config.ALLOWED_USERS else 'для всех'} · "
              f"лимит: {config.DAILY_LIMIT_PER_USER or 'нет'} видео/сутки",
              "", "Выгрузка в Excel — /export, копия базы — /backup"]
    return "\n".join(lines)


async def daily_report_text(day: datetime) -> str:
    """Отчёт за сутки day (UTC)."""
    since, until = _iso(_day_start(day)), _iso(_day_start(day) + timedelta(days=1))
    new = await db.scalar("SELECT COUNT(*) FROM users WHERE first_seen >= ? AND first_seen < ?", since, until)
    active = await db.scalar(
        "SELECT COUNT(DISTINCT user_id) FROM events WHERE created_at >= ? AND created_at < ?", since, until)
    total = await db.scalar("SELECT COUNT(*) FROM users")
    choices = await _counts("choice", since, until)
    done = sum((await _counts("done", since, until)).values())
    errors = sum((await _counts("error", since, until)).values())
    lines = [
        f"🗓 Отчёт за {day.strftime('%d.%m.%Y')}", "",
        f"👥 Новых пользователей: {new} (всего {total})",
        f"🔥 Активных: {active}",
        f"🎬 Выбор: {_line(choices, ACTION_LABELS)}",
        f"✅ Готово: {done} · ⚠️ ошибок: {errors}",
    ]
    top = await db.query(
        "SELECT e.user_id, u.username, u.full_name, COUNT(*) AS n FROM events e "
        "LEFT JOIN users u ON u.user_id = e.user_id WHERE e.created_at >= ? AND e.created_at < ? "
        "AND e.type IN ('link','media','choice') AND e.user_id IS NOT NULL "
        "GROUP BY e.user_id ORDER BY n DESC LIMIT 3", since, until)
    if top:
        lines.append("🏆 Активнее всех: " + ", ".join(f"{_name(r)} ({r['n']})" for r in top))
    lines += ["", "Файл ниже — резервная копия базы. Сохрани его: если данные пропадут, "
                  "пришли файл боту с подписью /restore."]
    return "\n".join(lines)


# --- Резервная копия и восстановление ------------------------------------------

def make_backup_file() -> str:
    """Согласованная копия базы (безопасно даже во время работы бота)."""
    dst = os.path.join(tempfile.gettempdir(), f"backup_{_utc_now().strftime('%Y-%m-%d_%H%M')}.db")
    src = sqlite3.connect(db.DB_PATH)
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    src.close()
    out.close()
    return dst


async def send_backup(bot, chat_id: int, caption: str = "💾 Резервная копия базы"):
    path = await asyncio.get_event_loop().run_in_executor(None, make_backup_file)
    try:
        with open(path, "rb") as f:
            data = f.read()
        await bot.send_document(chat_id, BufferedInputFile(data, filename=os.path.basename(path)), caption=caption)
    finally:
        os.remove(path)


def restore_from_file(path: str) -> int:
    """Проверяет, что файл — наша база, и подменяет ею текущую. Возвращает число пользователей."""
    con = sqlite3.connect(path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "posts" not in tables:
            raise ValueError("Это не резервная копия бота (нет таблицы posts).")
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] if "users" in tables else 0
    finally:
        con.close()
    keep = db.DB_PATH + ".before_restore"
    if os.path.exists(db.DB_PATH):
        shutil.copy2(db.DB_PATH, keep)     # на всякий случай — прежняя версия рядом
    os.replace(path, db.DB_PATH)
    return users


# --- Выгрузка в Excel ------------------------------------------------------------

def _csv(rows, headers) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")   # точка с запятой — Excel с русскими настройками открывает сразу
    w.writerow(headers)
    for r in rows:
        w.writerow([r[h] for h in headers])
    return buf.getvalue().encode("utf-8-sig")


async def export_files() -> list[tuple[str, bytes]]:
    users = await db.query(
        "SELECT u.user_id, u.username, u.full_name, u.first_seen, u.last_seen, "
        "(SELECT COUNT(*) FROM events e WHERE e.user_id = u.user_id AND e.type IN ('link','media')) AS videos_sent, "
        "(SELECT COUNT(*) FROM events e WHERE e.user_id = u.user_id AND e.type = 'choice') AS actions "
        "FROM users u ORDER BY u.first_seen")
    events = await db.query("SELECT created_at, user_id, type, detail FROM events ORDER BY id")
    posts = await db.query(
        "SELECT id, created_at, user_id, user_name, mode, status, source_url, video_title FROM posts ORDER BY id")
    return [
        ("users.csv", _csv(users, ["user_id", "username", "full_name", "first_seen", "last_seen",
                                   "videos_sent", "actions"])),
        ("events.csv", _csv(events, ["created_at", "user_id", "type", "detail"])),
        ("videos.csv", _csv(posts, ["id", "created_at", "user_id", "user_name", "mode", "status",
                                    "source_url", "video_title"])),
    ]


# --- Ежедневный отчёт ------------------------------------------------------------

async def send_daily_report_if_due(bot, admin_id: int, now: datetime | None = None) -> bool:
    now = now or _utc_now()
    today = now.strftime("%Y-%m-%d")
    if now.hour < config.DAILY_REPORT_HOUR_UTC or await db.get_meta("last_daily_report") == today:
        return False
    await db.set_meta("last_daily_report", today)   # сначала отметка — чтобы не задвоить при сбое отправки
    await bot.send_message(admin_id, await daily_report_text(now - timedelta(days=1)))
    await send_backup(bot, admin_id, caption="💾 Ежедневная резервная копия базы")
    return True


async def report_loop(bot, admin_id: int):
    while True:
        try:
            await send_daily_report_if_due(bot, admin_id)
        except Exception:
            logger.exception("Не удалось отправить ежедневный отчёт")
        await asyncio.sleep(600)
