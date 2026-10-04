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


# --- Субтитры YouTube ---------------------------------------------------------
# Если у видео есть субтитры, текст берём из них — это секунды вместо минут
# распознавания. Берём только ОРИГИНАЛЬНЫЕ субтитры (ручные или автоматические
# на языке видео), а не автоперевод YouTube — он хуже нашего перевода.
import re as _re
import json as _json

SUBTITLE_FORMATS = ("json3", "vtt")
MIN_SUBTITLE_CHARS = 40
MIN_CHARS_PER_MINUTE = 150   # меньше — субтитры неполные, лучше распознать звук


def _pick_subtitle_track(info: dict):
    """Возвращает (список форматов дорожки, описание) или None."""
    lang = (info.get("language") or "").split("-")[0].lower()
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat" and v}
    auto = {k: v for k, v in (info.get("automatic_captions") or {}).items() if v}

    for key in [k for k in (lang, f"{lang}-orig") if k] + ["ru", "en"]:
        if key in manual:
            return manual[key], f"субтитры автора ({key})"
    if manual:
        key = next(iter(manual))
        return manual[key], f"субтитры автора ({key})"

    # Автосубтитры: среди них десятки автопереводов. Оригинал помечен "-orig";
    # если такой пометки нет — берём только язык видео, если он известен.
    orig = [k for k in auto if k.endswith("-orig")]
    if orig:
        return auto[orig[0]], f"автосубтитры ({orig[0].replace('-orig', '')})"
    if lang and lang in auto:
        return auto[lang], f"автосубтитры ({lang})"
    return None


def _subtitle_text(raw: str, ext: str) -> str:
    if ext == "json3":
        data = _json.loads(raw)
        text = "".join(seg.get("utf8", "") for ev in data.get("events", []) for seg in ev.get("segs", []) or [])
    else:  # vtt: убираем служебное и повторяющиеся строки автосубтитров
        lines, prev = [], None
        for line in raw.splitlines():
            line = _re.sub(r"<[^>]+>", "", line).strip()
            if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:")) or line.isdigit():
                continue
            if line != prev:
                lines.append(line)
            prev = line
        text = " ".join(lines)
    text = _re.sub(r"\[[^\]]{1,30}\]", " ", text)   # [Music], [Музыка], [Applause]
    return _re.sub(r"\s+", " ", text).strip()


def _fetch_subtitles_sync(url: str):
    """Возвращает (название, текст, описание источника) или None."""
    for player_clients in PLAYER_CLIENT_FALLBACKS[:3]:
        opts = _build_ydl_opts(DOWNLOADS_DIR, player_clients)
        opts.pop("postprocessors", None)
        opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                track = _pick_subtitle_track(info)
                if not track:
                    logger.info("Субтитров у видео нет — нужно распознавание")
                    return None
                formats, label = track
                fmt = next((f for ext in SUBTITLE_FORMATS for f in formats if f.get("ext") == ext), None)
                if not fmt:
                    return None
                raw = ydl.urlopen(fmt["url"]).read().decode("utf-8", "replace")
                text = _subtitle_text(raw, fmt["ext"])
                duration_min = (info.get("duration") or 0) / 60
                if len(text) < MIN_SUBTITLE_CHARS or (
                        duration_min > 1 and len(text) / duration_min < MIN_CHARS_PER_MINUTE):
                    logger.info(f"Субтитры слишком короткие ({len(text)} симв.) — будет распознавание")
                    return None
                logger.info(f"Взял {label}: {len(text)} символов")
                return info.get("title") or info.get("id"), text, label
        except Exception as e:
            logger.warning(f"Не удалось получить субтитры (player_client={player_clients}): {e}")
            continue
    return None


async def fetch_subtitles(url: str):
    """(название, текст, источник) из субтитров YouTube или None, если их нет."""
    if not _is_youtube(url):
        return None
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_subtitles_sync, url)
