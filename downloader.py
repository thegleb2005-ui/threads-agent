"""
Скачивание аудиодорожки из YouTube-видео через yt-dlp.

ffmpeg не ставится через apt на всех хостингах (например, Bothost без
Docker его не даёт), поэтому используем imageio-ffmpeg — pip-пакет,
внутри которого уже лежит готовый статический бинарник ffmpeg. Ничего
скачивать при старте не нужно, работает сразу после pip install.
"""
import os
import asyncio
import imageio_ffmpeg
import yt_dlp

from config import DOWNLOADS_DIR

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


def _download_sync(url: str, out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
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
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        video_id = info["id"]
        title = info.get("title", video_id)
        audio_path = os.path.join(out_dir, f"{video_id}.mp3")
        return audio_path, title


async def download_audio(url: str) -> tuple[str, str]:
    """Скачивает аудио по ссылке на видео. Возвращает (путь_к_файлу, название)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_sync, url, DOWNLOADS_DIR)
