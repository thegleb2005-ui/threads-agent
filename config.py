"""
Централизованная конфигурация. Все значения берутся из переменных окружения
(см. .env.example). Ничего секретного здесь не хардкодим.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

# --- kie.ai используется и для транскрибации (elevenlabs/speech-to-text),
#     и для генерации текста поста (OpenAI-совместимый chat completions) ---
KIE_API_KEY = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL = os.getenv("KIE_BASE_URL", "https://api.kie.ai/v1")
KIE_MODEL = os.getenv("KIE_MODEL", "gpt-5-2")

# --- Threads (Meta Graph API) — пока НЕ используется, публикация ручная.
#     Переменные оставлены на случай, если решишь включить автопубликацию
#     позже (см. threads_api.py, scheduler.py и README). ---
THREADS_USER_ID = os.getenv("THREADS_USER_ID", "")
THREADS_ACCESS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN", "")
THREADS_API_BASE = "https://graph.threads.net/v1.0"
POST_TIMES = [t.strip() for t in os.getenv("POST_TIMES", "09:00,14:00,19:00").split(",") if t.strip()]

# --- Стиль контента ---
POST_LANGUAGE = os.getenv("POST_LANGUAGE", "English")
POST_STYLE_PROMPT = os.getenv("POST_STYLE_PROMPT", "")

# --- Пути ---
DB_PATH = os.getenv("DB_PATH", "agent.db")
DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "downloads")


def validate():
    """Проверяет, что критичные переменные заданы. Вызывается при старте бота.

    Threads-токены сюда намеренно не входят — пока публикация ручная,
    они не нужны для запуска бота.
    """
    missing = []
    for name in ["TELEGRAM_BOT_TOKEN", "KIE_API_KEY"]:
        if not globals().get(name):
            missing.append(name)
    if ADMIN_USER_ID == 0:
        missing.append("ADMIN_USER_ID")
    if missing:
        raise RuntimeError(
            f"Не заданы переменные окружения: {', '.join(missing)}. "
            f"Проверь файл .env (см. .env.example)."
        )
