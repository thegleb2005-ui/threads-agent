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


# --- Скачивание файлов для отправки пользователю -------------------------------
# Telegram даёт ботам отправлять файлы до 50 МБ. Видео подбираем по качеству
# так, чтобы влезло; звук при необходимости пережимаем с меньшим битрейтом.
import subprocess as _sp

TELEGRAM_UPLOAD_LIMIT = 49 * 1024 * 1024   # с запасом от 50 МБ
MIN_AUDIO_KBPS = 32
MAX_AUDIO_KBPS = 128


class TooLargeError(Exception):
    """Даже в самом низком качестве файл больше лимита Telegram."""


def _fsize(f: dict, duration: float) -> float | None:
    size = f.get("filesize") or f.get("filesize_approx")
    if not size and f.get("tbr") and duration:
        size = f["tbr"] * 1000 / 8 * duration
    return size


def choose_video_formats(info: dict, limit: int = TELEGRAM_UPLOAD_LIMIT) -> list[str]:
    """Список вариантов формата от лучшего к худшему, которые должны влезть в лимит.
    Пустой список — у площадки нет данных о размерах (тогда пробуем «как есть»)."""
    duration = info.get("duration") or 0
    fmts = info.get("formats") or []
    audios = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    videos = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("acodec") == "none"]
    progressive = [f for f in fmts if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]

    # Звук: m4a лучше всего склеивается с mp4; среди подходящих — самый лёгкий
    # из нормальных по качеству (до ~130 кбит/с), чтобы оставить место картинке.
    audios = [a for a in audios if _fsize(a, duration)]
    audios.sort(key=lambda a: (a.get("ext") != "m4a", abs((a.get("abr") or 128) - 128)))
    best_audio = audios[0] if audios else None

    def is_h264(f):
        return (f.get("vcodec") or "").startswith(("avc1", "h264"))

    candidates = []
    if best_audio:
        a_size = _fsize(best_audio, duration)
        for v in videos:
            v_size = _fsize(v, duration)
            if v_size and v_size + a_size <= limit * 0.95:
                candidates.append((v.get("height") or 0, is_h264(v), f"{v['format_id']}+{best_audio['format_id']}"))
    for f in progressive:
        size = _fsize(f, duration)
        if size and size <= limit * 0.95:
            candidates.append((f.get("height") or 0, is_h264(f), f["format_id"]))
    # Выше качество — лучше; при равном — H.264 (играет прямо в Telegram).
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    seen, result = set(), []
    for _, _, fid in candidates:
        if fid not in seen:
            seen.add(fid)
            result.append(fid)
    return result


def _probe(url: str):
    """Информация о видео без скачивания. Возвращает (info, player_clients)."""
    variants = PLAYER_CLIENT_FALLBACKS if _is_youtube(url) else [PLAYER_CLIENT_FALLBACKS[0]]
    last_error = None
    for clients in variants:
        opts = _build_ydl_opts(DOWNLOADS_DIR, clients)
        opts.pop("postprocessors", None)
        opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False), clients
        except yt_dlp.utils.DownloadError as e:
            last_error = e
    raise last_error


def _download_video_sync(url: str, out_dir: str) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    info, clients = _probe(url)
    title = info.get("title") or info.get("id") or "video"
    choices = choose_video_formats(info)
    if not choices:
        # Нет данных о размерах (часто у Instagram/TikTok) — берём mp4 как есть и проверяем.
        choices = ["best[ext=mp4]/best"]
    for fmt in choices[:3]:
        opts = _build_ydl_opts(out_dir, clients)
        opts.pop("postprocessors", None)
        opts.update({"format": fmt, "merge_output_format": "mp4",
                     "outtmpl": os.path.join(out_dir, "%(id)s_video.%(ext)s")})
        with yt_dlp.YoutubeDL(opts) as ydl:
            got = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(got)
            path = os.path.splitext(path)[0] + ".mp4" if not os.path.exists(path) else path
        if os.path.exists(path) and os.path.getsize(path) <= TELEGRAM_UPLOAD_LIMIT:
            return path, title
        try:
            os.remove(path)
        except OSError:
            pass
        logger.info(f"Видео в формате {fmt} оказалось больше лимита, пробую качество ниже")
    raise TooLargeError("Видео слишком большое для Telegram даже в низком качестве.")


def _duration_seconds(path: str) -> float:
    out = _sp.run([FFMPEG_PATH, "-i", path], capture_output=True, text=True).stderr
    m = _re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0


def fit_mp3(src: str, limit: int = TELEGRAM_UPLOAD_LIMIT, kbps: int = MAX_AUDIO_KBPS) -> str:
    """Делает mp3 не больше лимита: битрейт подбирается по длительности,
    при перелёте — ещё раз с битрейтом пониже."""
    duration = _duration_seconds(src)
    if duration:
        kbps = min(kbps, int(limit * 8 / duration / 1000 * 0.9))
    while kbps >= MIN_AUDIO_KBPS:
        dst = os.path.splitext(src)[0] + f"_{kbps}k.mp3"
        _sp.run([FFMPEG_PATH, "-y", "-i", src, "-vn", "-b:a", f"{kbps}k", dst], capture_output=True, check=True)
        if os.path.getsize(dst) <= limit:
            return dst
        os.remove(dst)
        kbps = int(kbps * 0.85)
    raise TooLargeError("Аудио слишком длинное для Telegram даже в низком качестве.")


def _download_audio_file_sync(url: str, out_dir: str) -> tuple[str, str]:
    path, title = _download_sync(url, out_dir)          # mp3 128 кбит/с
    if os.path.getsize(path) <= TELEGRAM_UPLOAD_LIMIT:
        return path, title
    smaller = fit_mp3(path)
    os.remove(path)
    return smaller, title


async def download_video_file(url: str) -> tuple[str, str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_video_sync, url, DOWNLOADS_DIR)


async def download_audio_file(url: str) -> tuple[str, str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_audio_file_sync, url, DOWNLOADS_DIR)


async def extract_mp3(src: str) -> str:
    """Звук из файла (например, кружка или видео из Telegram) в mp3."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, fit_mp3, src)
