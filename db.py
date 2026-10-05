"""
Хранилище очереди постов на SQLite (через aiosqlite, асинхронно).

Жизненный цикл записи (поле status):
  awaiting_prompt -> ссылка добавлена, бот ждёт, какой промпт использовать
                     (индивидуальный текстом или базовый по кнопке)
  queued          -> промпт выбран, ждёт обработки воркером
  downloading     -> качается аудио
  transcribing    -> идёт распознавание речи
  generating      -> LLM пишет черновик поста
  draft_ready     -> черновик готов, ждёт решения админа в Telegram
  done            -> админ пометил готовым (публикует вручную)
  approved        -> (только при включённой автопубликации) ждёт слота
  published       -> опубликовано в Threads
  rejected        -> админ удалил черновик
  error           -> что-то упало на любом из шагов (см. error_message)
"""
import aiosqlite
from datetime import datetime, timezone
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,
    video_title TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_prompt',
    custom_prompt TEXT,
    transcript TEXT,
    draft_text TEXT,
    threads_post_id TEXT,
    error_message TEXT,
    status_chat_id INTEGER,
    status_message_id INTEGER,
    mode TEXT DEFAULT 'post',
    user_id INTEGER,
    user_name TEXT,
    transcript_source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT
);
"""

# Колонки, которые появились позже первой версии. Для уже существующих
# баз (например, на сервере, где бот уже работал) добавляем их через
# ALTER TABLE — CREATE TABLE IF NOT EXISTS сам по себе старую таблицу
# не обновляет.
MIGRATIONS = [
    ("custom_prompt", "ALTER TABLE posts ADD COLUMN custom_prompt TEXT"),
    ("status_chat_id", "ALTER TABLE posts ADD COLUMN status_chat_id INTEGER"),
    ("status_message_id", "ALTER TABLE posts ADD COLUMN status_message_id INTEGER"),
    # Режим обработки: 'post' — черновик поста, 'transcript' — расшифровка на русском
    ("mode", "ALTER TABLE posts ADD COLUMN mode TEXT DEFAULT 'post'"),
    # Многопользовательский режим: чьё это видео
    ("user_id", "ALTER TABLE posts ADD COLUMN user_id INTEGER"),
    ("user_name", "ALTER TABLE posts ADD COLUMN user_name TEXT"),
    # Откуда текст: 'subtitles' (субтитры YouTube) или 'whisper' (распознавание)
    ("transcript_source", "ALTER TABLE posts ADD COLUMN transcript_source TEXT"),
]

ACTIVE_STATUSES = ("queued", "downloading", "transcribing", "generating")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


MEDIA_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_cache (
    source_url TEXT NOT NULL,
    kind TEXT NOT NULL,          -- 'video' или 'audio'
    file_id TEXT NOT NULL,       -- id файла в Telegram: пересылается без нового скачивания
    title TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_url, kind)
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(SCHEMA)
        await db.execute(MEDIA_CACHE_SCHEMA)
        cursor = await db.execute("PRAGMA table_info(posts)")
        existing = {row[1] for row in await cursor.fetchall()}
        for column, sql in MIGRATIONS:
            if column not in existing:
                await db.execute(sql)
        await db.commit()


async def add_post(source_url: str, user_id: int | None = None, user_name: str = "") -> int:
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO posts (source_url, status, user_id, user_name, created_at, updated_at) "
            "VALUES (?, 'awaiting_prompt', ?, ?, ?, ?)",
            (source_url, user_id, user_name, now, now),
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


async def recover_stuck_posts() -> int:
    """Возвращает в очередь посты, застрявшие в процессе обработки —
    такое бывает, если процесс упал (например, из-за нехватки памяти на
    длинном видео) прямо посреди шага. Без этого такая запись зависает
    в статусе 'downloading'/'transcribing'/'generating' навсегда, и её
    никто не подхватит. Вызывается один раз при старте бота.
    Возвращает количество восстановленных записей."""
    stuck_statuses = ("downloading", "transcribing", "generating")
    now = _now()
    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" for _ in stuck_statuses)
        cursor = await db.execute(
            f"UPDATE posts SET status = 'queued', updated_at = ? "
            f"WHERE status IN ({placeholders})",
            (now, *stuck_statuses),
        )
        await db.commit()
        return cursor.rowcount


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


async def get_all_queued():
    """Все ожидающие обработки, старые первыми."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM posts WHERE status = 'queued' ORDER BY created_at ASC")
        return await cursor.fetchall()


def _today_start() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")


async def count_user_posts_today(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id = ? AND created_at >= ?", (user_id, _today_start()))
        return (await cursor.fetchone())[0]


async def count_user_active(user_id: int) -> int:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM posts WHERE user_id = ? AND status IN ({placeholders})",
            (user_id, *ACTIVE_STATUSES))
        return (await cursor.fetchone())[0]


async def get_user_posts(user_id: int | None, statuses: tuple, limit: int = 20):
    """Видео пользователя в указанных статусах (user_id=None — всех пользователей)."""
    placeholders = ",".join("?" for _ in statuses)
    where, args = f"status IN ({placeholders})", list(statuses)
    if user_id is not None:
        where += " AND user_id = ?"
        args.append(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"SELECT * FROM posts WHERE {where} ORDER BY created_at ASC LIMIT ?", (*args, limit))
        return await cursor.fetchall()


async def get_stats() -> dict:
    today = _today_start()
    async with aiosqlite.connect(DB_PATH) as db:
        async def one(sql, *a):
            return (await (await db.execute(sql, a)).fetchone())[0]
        return {
            "users_total": await one("SELECT COUNT(DISTINCT user_id) FROM posts WHERE user_id IS NOT NULL"),
            "users_today": await one("SELECT COUNT(DISTINCT user_id) FROM posts WHERE created_at >= ?", today),
            "videos_today": await one("SELECT COUNT(*) FROM posts WHERE created_at >= ?", today),
            "errors_today": await one("SELECT COUNT(*) FROM posts WHERE status = 'error' AND created_at >= ?", today),
            "subtitles_today": await one(
                "SELECT COUNT(*) FROM posts WHERE transcript_source = 'subtitles' AND created_at >= ?", today),
            "downloads_today": await one(
                "SELECT COUNT(*) FROM posts WHERE mode IN ('video','audio') AND created_at >= ?", today),
            "in_queue": await one("SELECT COUNT(*) FROM posts WHERE status = 'queued'"),
            "in_work": await one(
                "SELECT COUNT(*) FROM posts WHERE status IN ('downloading','transcribing','generating')"),
        }


async def get_media_cache(source_url: str, kind: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM media_cache WHERE source_url = ? AND kind = ?", (source_url, kind))
        return await cursor.fetchone()


async def save_media_cache(source_url: str, kind: str, file_id: str, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO media_cache (source_url, kind, file_id, title, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (source_url, kind, file_id, title, _now()))
        await db.commit()
