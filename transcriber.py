"""
Распознавание речи через kie.ai (модель elevenlabs/speech-to-text).

Схема работы kie.ai для любых асинхронных задач одна и та же:
  1. Файл должен лежать по публичной ссылке -> сначала грузим mp3
     на их временный файловый хостинг (kieai.redpandaai.co), 3 дня хранения.
  2. Создаём задачу транскрибации по этой ссылке -> получаем taskId.
  3. Опрашиваем статус задачи, пока не станет success/fail.
  4. Достаём готовый текст из результата.

ВАЖНО: у kie.ai/ElevenLabs, судя по всему, есть свой внутренний лимит
по ВРЕМЕНИ обработки одного запроса (не по размеру файла) — на практике
файлы длиннее ~15 минут аудио могут прилетать назад с ошибкой
"upstream API service timed out" даже если сам mp3 весит немного.
Поэтому режем на куски по ДЛИТЕЛЬНОСТИ (не по байтам), с запасом.
Плюс: если задача всё равно упала с таймаутом на стороне kie.ai,
делаем автоматический повтор — сам kie.ai рекомендует "please try again".
"""
import os
import re
import json
import math
import time
import logging
import asyncio
import subprocess
import tempfile

import imageio_ffmpeg
import requests

from config import KIE_API_KEY, TRANSCRIBE_PROVIDER, GEMINI_MODEL

logger = logging.getLogger(__name__)

KIE_API_BASE = "https://api.kie.ai/api/v1"
KIE_UPLOAD_BASE = "https://kieai.redpandaai.co/api"
KIE_GEMINI_URL_TMPL = "https://api.kie.ai/{model}/v1/chat/completions"

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

CHUNK_SECONDS = 600  # 10 минут на кусок — с запасом от предполагаемого лимита kie.ai

POLL_INTERVAL_SECONDS = 5
POLL_MAX_ATTEMPTS = 180  # до ~15 минут ожидания на один кусок

MAX_TIMEOUT_RETRIES = 2  # сколько раз повторить кусок при таймауте upstream


def _headers() -> dict:
    return {"Authorization": f"Bearer {KIE_API_KEY}"}


def _upload_file(file_path: str) -> str:
    """Заливает локальный mp3 на временный хостинг kie.ai. Возвращает публичный URL."""
    with open(file_path, "rb") as f:
        resp = requests.post(
            f"{KIE_UPLOAD_BASE}/file-stream-upload",
            headers=_headers(),
            files={"file": (os.path.basename(file_path), f, "audio/mpeg")},
            data={"uploadPath": "threads-agent/audio"},
            timeout=120,
        )
    resp.raise_for_status()
    payload = resp.json()
    info = payload.get("data", {})
    file_url = info.get("fileUrl") or info.get("downloadUrl")
    if not file_url:
        raise RuntimeError(f"kie.ai не вернул ссылку на загруженный файл: {payload}")
    return file_url


def _create_stt_task(audio_url: str) -> str:
    resp = requests.post(
        f"{KIE_API_BASE}/jobs/createTask",
        headers={**_headers(), "Content-Type": "application/json"},
        json={
            "model": "elevenlabs/speech-to-text",
            "input": {
                "audio_url": audio_url,
                "language_code": "",
                "tag_audio_events": False,
                "diarize": False,
            },
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("code") != 200:
        raise RuntimeError(f"kie.ai отклонил задачу транскрибации: {payload}")
    return payload["data"]["taskId"]


class UpstreamTimeoutError(RuntimeError):
    """Отдельный тип ошибки — специально для таймаутов upstream (kie.ai сам
    говорит "please try again"), чтобы можно было отличить от прочих fail
    и автоматически повторить попытку."""
    pass


def _poll_task(task_id: str) -> dict:
    last_state = None
    last_progress = None
    for attempt in range(POLL_MAX_ATTEMPTS):
        resp = requests.get(
            f"{KIE_API_BASE}/jobs/recordInfo",
            headers=_headers(),
            params={"taskId": task_id},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        state = data.get("state")
        last_state = state
        last_progress = data.get("progress")

        if attempt % 12 == 0:
            logger.info(
                f"kie.ai task {task_id}: state={state}, progress={last_progress}, "
                f"попытка {attempt + 1}/{POLL_MAX_ATTEMPTS}"
            )

        if state == "success":
            return json.loads(data["resultJson"])
        if state == "fail":
            fail_msg = data.get("failMsg") or ""
            if "timed out" in fail_msg.lower() or data.get("failCode") == "500":
                raise UpstreamTimeoutError(
                    f"Таймаут на стороне kie.ai (task {task_id}): {fail_msg}"
                )
            raise RuntimeError(f"Транскрибация не удалась (task {task_id}): {fail_msg}")
        time.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"Транскрибация не завершилась за отведённое время (task {task_id}). "
        f"Последний известный статус: state={last_state}, progress={last_progress}. "
        f"Проверь статус задачи вручную: "
        f"curl -H 'Authorization: Bearer <твой KIE_API_KEY>' "
        f"'https://api.kie.ai/api/v1/jobs/recordInfo?taskId={task_id}'"
    )


def _extract_text(result: dict) -> str:
    if "text" in result:
        return result["text"]
    if result.get("resultUrls"):
        text_resp = requests.get(result["resultUrls"][0], timeout=30)
        text_resp.raise_for_status()
        return text_resp.text
    raise RuntimeError(f"Не удалось извлечь текст из ответа kie.ai: {result}")


def _transcribe_via_elevenlabs(file_path: str) -> str:
    """Транскрибирует один файл через elevenlabs/speech-to-text (async job).
    При таймауте upstream — повторяет попытку до MAX_TIMEOUT_RETRIES раз."""
    last_error = None
    for attempt in range(1, MAX_TIMEOUT_RETRIES + 2):  # 1 обычная + N повторов
        try:
            audio_url = _upload_file(file_path)
            task_id = _create_stt_task(audio_url)
            result = _poll_task(task_id)
            return _extract_text(result)
        except UpstreamTimeoutError as e:
            last_error = e
            logger.warning(
                f"Таймаут upstream на попытке {attempt}/{MAX_TIMEOUT_RETRIES + 1} "
                f"для {file_path}: {e}"
            )
            if attempt <= MAX_TIMEOUT_RETRIES:
                time.sleep(5)
                continue
            raise
    raise last_error


def _transcribe_via_gemini(file_path: str) -> str:
    """Транскрибирует файл через мультимодальный чат-запрос к Gemini.

    ЭКСПЕРИМЕНТАЛЬНЫЙ путь: официальный пример kie.ai для Gemini показывает
    передачу файла по ссылке через content-блок типа "image_url" (несмотря
    на название, так передаются файлы вообще, не только картинки) — но
    именно для аудио это нигде явно не задокументировано. Работает быстрее
    и проще ElevenLabs-варианта (один синхронный запрос, без очереди задач),
    но если не сработает совсем — возвращайся на TRANSCRIBE_PROVIDER=elevenlabs.
    """
    audio_url = _upload_file(file_path)
    url = KIE_GEMINI_URL_TMPL.format(model=GEMINI_MODEL)

    last_error = None
    for attempt in range(1, MAX_TIMEOUT_RETRIES + 2):
        try:
            resp = requests.post(
                url,
                headers={**_headers(), "Content-Type": "application/json"},
                json={
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "Transcribe this audio file exactly, word for word, "
                                        "in the original spoken language. Return ONLY the "
                                        "transcript text, no commentary, no timestamps, "
                                        "no speaker labels."
                                    ),
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": audio_url,
                                        "mime_type": "audio/mpeg",
                                    },
                                },
                            ],
                        }
                    ],
                },
                timeout=180,
            )
            resp.raise_for_status()
            payload = resp.json()
            text = payload["choices"][0]["message"]["content"]
            if not text or not text.strip():
                raise RuntimeError(f"Gemini вернул пустой транскрипт: {payload}")
            return text.strip()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_error = e
            logger.warning(
                f"Сетевая ошибка при обращении к Gemini (попытка "
                f"{attempt}/{MAX_TIMEOUT_RETRIES + 1}) для {file_path}: {e}"
            )
            if attempt <= MAX_TIMEOUT_RETRIES:
                time.sleep(5)
                continue
            raise
    raise last_error


def _transcribe_file_sync(file_path: str) -> str:
    """Транскрибирует один файл (уже достаточно короткий кусок) выбранным
    в конфиге движком (TRANSCRIBE_PROVIDER)."""
    if TRANSCRIBE_PROVIDER == "gemini":
        return _transcribe_via_gemini(file_path)
    return _transcribe_via_elevenlabs(file_path)


def _get_duration_seconds(file_path: str) -> float:
    # ffprobe отдельно не ставим (imageio-ffmpeg даёт только ffmpeg), поэтому
    # достаём длительность из служебного вывода самого ffmpeg — он всегда
    # печатает "Duration: HH:MM:SS.xx" в stderr при открытии файла.
    result = subprocess.run(
        [FFMPEG_PATH, "-i", file_path],
        capture_output=True, text=True,
    )
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", result.stderr)
    if not match:
        raise RuntimeError(f"Не удалось определить длительность файла {file_path}: {result.stderr[-500:]}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _split_audio(file_path: str, tmp_dir: str) -> list[str]:
    duration = _get_duration_seconds(file_path)
    n_chunks = max(1, math.ceil(duration / CHUNK_SECONDS))
    chunk_paths = []
    for i in range(n_chunks):
        start = i * CHUNK_SECONDS
        chunk_path = os.path.join(tmp_dir, f"chunk_{i}.mp3")
        subprocess.run(
            [FFMPEG_PATH, "-y", "-i", file_path, "-ss", str(start),
             "-t", str(CHUNK_SECONDS), "-c", "copy", chunk_path],
            capture_output=True, check=True,
        )
        chunk_paths.append(chunk_path)
    return chunk_paths


def _transcribe_sync(file_path: str) -> str:
    duration = _get_duration_seconds(file_path)
    logger.info(f"Длительность файла {file_path}: {duration:.0f} сек")

    if duration <= CHUNK_SECONDS:
        return _transcribe_file_sync(file_path)

    logger.info(
        f"Файл длиннее {CHUNK_SECONDS} сек — режем на куски перед отправкой в kie.ai"
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        chunks = _split_audio(file_path, tmp_dir)
        texts = [_transcribe_file_sync(chunk) for chunk in chunks]
    return " ".join(texts)


async def transcribe_audio(file_path: str) -> str:
    """Распознаёт речь в mp3-файле. Возвращает распознанный текст."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, file_path)
