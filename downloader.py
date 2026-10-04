"""
Скачивание аудиодорожки из видео (YouTube, Instagram Reels, TikTok и др.) через yt-dlp.
Та же проверенная версия, что в коннекторе whisper-mcp-connector.

Для YouTube:
  - нужен JavaScript-движок Deno (пакет deno в requirements.txt) — без него
    YouTube почти не отдаёт форматы, особенно при входе через cookies;
  - cookies передаются переменной COOKIES_B64 (см. config.py);
  - перебираются несколько "клиентов" плеера, первым — стандартный выбор yt-dlp.
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
# None в конце — не подменяем клиента вообще, пусть yt-dlp сам решает.
PLAYER_CLIENT_FALLBACKS = [
    None,  # стандартный выбор yt-dlp — с Deno это лучший вариант
    ["tv", "web_safari"],
    ["ios"],
    ["android"],
    ["mweb"],
    ["web_safari"],
]


def _cookies_active() -> bool:
    return bool(COOKIES_FILE and os.path.exists(COOKIES_FILE))


def _build_ydl_opts(out_dir: str, player_clients: list[str] | None) -> dict:
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
        "ffmpeg_location": FFMPEG_PATH,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if player_clients is not None:
        ydl_opts["extractor_args"] = {"youtube": {"player_client": player_clients}}

    if _cookies_active():
        ydl_opts["cookiefile"] = COOKIES_FILE
        # Клиент "tv" с cookies не смешиваем — может оборвать сессию аккаунта.
        if player_clients is not None:
            safe_clients = [c for c in player_clients if c != "tv"]
            if safe_clients:
                ydl_opts["extractor_args"] = {"youtube": {"player_client": safe_clients}}
            else:
                ydl_opts.pop("extractor_args", None)
    return ydl_opts


def _is_youtube(url: str) -> bool:
    return "youtube.com" in url.lower() or "youtu.be" in url.lower()


def _download_sync(url: str, out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)

    client_variants = PLAYER_CLIENT_FALLBACKS if _is_youtube(url) else [PLAYER_CLIENT_FALLBACKS[0]]

    last_error = None
    cookies_used = _cookies_active()
    for i, player_clients in enumerate(client_variants, start=1):
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
                        f"player_client={player_clients}, cookies={cookies_used}"
                    )
                return audio_path, title
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            logger.warning(
                f"Не удалось скачать (попытка {i}/{len(client_variants)}, "
                f"player_client={player_clients}, cookies={cookies_used}): {e}"
            )
            continue

    raise last_error


async def download_audio(url: str) -> tuple[str, str]:
    """Скачивает аудио по ссылке на видео. Возвращает (путь_к_файлу, название)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_sync, url, DOWNLOADS_DIR)
