"""
Централизованная конфигурация. Все значения берутся из файла config.env
(см. config.env.example). Ничего секретного здесь не хардкодим.

Файл называется НЕ ".env" специально — файлы с точкой в начале Finder
на Mac скрывает по умолчанию, из-за чего их легко потерять после
распаковки архива. "config.env" ничем не хуже для той же задачи и всегда
виден в проводнике.
"""
import os
from dotenv import load_dotenv

load_dotenv("config.env")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

# --- kie.ai используется и для транскрибации, и для генерации текста поста ---
KIE_API_KEY = os.getenv("KIE_API_KEY", "")
KIE_BASE_URL = os.getenv("KIE_BASE_URL", "https://api.kie.ai/v1")
KIE_MODEL = os.getenv("KIE_MODEL", "gpt-5-2")

# Какой движок использовать для распознавания речи:
#   "local"      — Whisper прямо на сервере (faster-whisper), БЕЗ сетевых
#                  запросов вообще. Самый надёжный вариант — нет ни таймаутов,
#                  ни странных форматов ответа, ни платного API. Модель
#                  скачивается один раз при первом запуске (~150 МБ для "base").
#   "elevenlabs" — модель elevenlabs/speech-to-text через kie.ai (асинхронные
#                  jobs) — умеет глючить/таймаутить на стороне kie.ai.
#   "gemini"     — мультимодальный чат-запрос к Gemini через kie.ai —
#                  экспериментальный путь, формат ответа не задокументирован.
TRANSCRIBE_PROVIDER = os.getenv("TRANSCRIBE_PROVIDER", "local")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Размер модели Whisper для локального распознавания (только если
# TRANSCRIBE_PROVIDER=local). Варианты (от быстрого/грубого к медленному/точному):
# tiny, base, small, medium, large-v3. "base" — разумный баланс для CPU-сервера.
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")

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

# Запасной путь на случай, если YouTube всё равно блокирует скачивание
# с IP хостинга ("Sign in to confirm you're not a bot") даже после смены
# player_client. Путь к файлу cookies.txt, экспортированному из браузера
# (см. README, раздел про блокировку YouTube). Пусто — не используется.
COOKIES_FILE = os.getenv("COOKIES_FILE", "")


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
            f"Проверь файл config.env (см. config.env.example)."
        )
