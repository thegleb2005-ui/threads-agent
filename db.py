"""
Хранилище очереди постов на SQLite (через aiosqlite, асинхронно).

Жизненный цикл записи (поле status):
  queued        -> ссылка добавлена, ждёт обработки воркером
  downloading   -> качается аудио
  transcribing  -> идёт распознавание речи
  generating    -> LLM пишет черновик поста
  draft_ready   -> черновик готов, ждёт решения админа в Telegram
  approved      -> админ одобрил, ждёт своего слота в расписании публикации
  published     -> опубликовано в Threads
  rejected      -> админ удалил черновик
  error         -> что-то упало на любом из шагов (см. error_message)
"""
import aiosqlite
from datetime import datetime, timezone
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,
    video_title TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    transcript TEXT,
    draft_text TEXT,
    threads_post_id TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(SCHEMA)
        await db.commit()


async def add_post(source_url: str) -> int:
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO posts (source_url, status, created_at, updated_at) "
            "VALUES (?, 'queued', ?, ?)",
            (source_url, now, now),
        )
        await db.commit()
        return cursor.lastrowid


async def update_post(post_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = _now()
    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [post_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE posts SET {columns} WHERE id = ?", values)
        await db.commit()


async def get_post(post_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        return await cursor.fetchone()


async def get_posts_by_status(status: str, limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM posts WHERE status = ? ORDER BY created_at ASC LIMIT ?",
            (status, limit),
        )
        return await cursor.fetchall()


async def get_next_queued():
    rows = await get_posts_by_status("queued", limit=1)
    return rows[0] if rows else None


async def get_oldest_approved():
    rows = await get_posts_by_status("approved", limit=1)
    return rows[0] if rows else None
