"""
Скачивание аудиодорожки из YouTube-видео через yt-dlp.

ffmpeg не ставится через apt на всех хостингах (например, Bothost без
Docker его не даёт), поэтому используем imageio-ffmpeg — pip-пакет,
внутри которого уже лежит готовый статический бинарник ffmpeg. Ничего
скачивать при старте не нужно, работает сразу после pip install.

YouTube в 2026 году постоянно меняет протокол стриминга и то, какие
"клиенты" (player_client) у yt-dlp работают, а какие внезапно ломаются —
это открытая гонка между YouTube и разработчиками yt-dlp, конкретный
рабочий вариант держится неделями, а не годами. Поэтому вместо одного
жёстко зашитого клиента перебираем НЕСКОЛЬКО вариантов по очереди: если
один сломался из-за очередного изменения на стороне YouTube, код сам
попробует следующий, не падая сразу в ошибку.
"""
import os
import logging
import asyncio
import imageio_ffmpeg
import yt_dlp

from config import DOWNLOADS_DIR, COOKIES_FILE

logger = logging.getLogger(__name__)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

# Порядок важен: пробуем от "скорее всего рабочего сейчас" к запасным.
# Если YouTube в очередной раз что-то сломает — правь этот список первым,
# не обязательно переписывать всю логику.
PLAYER_CLIENT_FALLBACKS = [
    ["default", "web_embedded"],
    ["tv", "web_safari"],
    ["android"],
    ["web_safari"],
]


def _build_ydl_opts(out_dir: str, player_clients: list[str]) -> dict:
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
        "ffmpeg_location": FFMPEG_PATH,
        "extractor_args": {"youtube": {"player_client": player_clients}},
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    # Запасной путь: если задан файл с cookies (экспортированными из браузера) —
    # используем его. НЕ смешиваем cookies с клиентом "tv" — это может
    # инвалидировать сессию в самом браузере, откуда куки экспортированы.
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
        safe_clients = [c for c in player_clients if c != "tv"] or ["web_safari"]
        ydl_opts["extractor_args"] = {"youtube": {"player_client": safe_clients}}
    return ydl_opts


def _download_sync(url: str, out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)

    last_error = None
    for i, player_clients in enumerate(PLAYER_CLIENT_FALLBACKS, start=1):
        ydl_opts = _build_ydl_opts(out_dir, player_clients)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                video_id = info["id"]
                title = info.get("title", video_id)
                audio_path = os.path.join(out_dir, f"{video_id}.mp3")
                if i > 1:
                    logger.info(
                        f"Скачано успешно с {i}-й попытки, "
                        f"player_client={player_clients}"
                    )
                return audio_path, title
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            logger.warning(
                f"player_client={player_clients} не сработал "
                f"(попытка {i}/{len(PLAYER_CLIENT_FALLBACKS)}): {e}"
            )
            continue

    raise last_error


async def download_audio(url: str) -> tuple[str, str]:
    """Скачивает аудио по ссылке на видео. Возвращает (путь_к_файлу, название)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_sync, url, DOWNLOADS_DIR)
