"""
Распознавание речи через kie.ai (модель elevenlabs/speech-to-text).

Схема работы kie.ai для любых асинхронных задач одна и та же:
  1. Файл должен лежать по публичной ссылке -> сначала грузим mp3
     на их временный файловый хостинг (kieai.redpandaai.co), 3 дня хранения.
  2. Создаём задачу транскрибации по этой ссылке -> получаем taskId.
  3. Опрашиваем статус задачи, пока не станет success/fail.
  4. Достаём готовый текст из результата.

Явного лимита на длину аудио в документации kie.ai для этой модели нет,
но на всякий случай (и по аналогии с рекомендацией kie.ai по размеру
файла при загрузке) файлы больше ~95 МБ режутся на получасовые куски
через ffmpeg, каждый кусок транскрибируется отдельно, тексты склеиваются.
"""
import os
import json
import math
import time
import asyncio
import subprocess
import tempfile

import requests

from config import KIE_API_KEY

KIE_API_BASE = "https://api.kie.ai/api/v1"
KIE_UPLOAD_BASE = "https://kieai.redpandaai.co/api"

MAX_BYTES = 95 * 1024 * 1024
CHUNK_SECONDS = 1800  # 30 минут на кусок

POLL_INTERVAL_SECONDS = 5
POLL_MAX_ATTEMPTS = 120  # до ~10 минут ожидания на один файл/кусок


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


def _poll_task(task_id: str) -> dict:
    for _ in range(POLL_MAX_ATTEMPTS):
        resp = requests.get(
            f"{KIE_API_BASE}/jobs/recordInfo",
            headers=_headers(),
            params={"taskId": task_id},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        state = data.get("state")
        if state == "success":
            return json.loads(data["resultJson"])
        if state == "fail":
            raise RuntimeError(f"Транскрибация не удалась: {data.get('failMsg')}")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"Транскрибация не завершилась за отведённое время (task {task_id})")


def _extract_text(result: dict) -> str:
    # ElevenLabs STT обычно отдаёт {"text": "...", "language_code": "...", ...}.
    # На случай, если kie.ai обернёт ответ иначе (например, ссылкой на файл
    # с результатом), пробуем запасной вариант, а не падаем молча.
    if "text" in result:
        return result["text"]
    if result.get("resultUrls"):
        text_resp = requests.get(result["resultUrls"][0], timeout=30)
        text_resp.raise_for_status()
        return text_resp.text
    raise RuntimeError(f"Не удалось извлечь текст из ответа kie.ai: {result}")


def _transcribe_file_sync(file_path: str) -> str:
    audio_url = _upload_file(file_path)
    task_id = _create_stt_task(audio_url)
    result = _poll_task(task_id)
    return _extract_text(result)


def _get_duration_seconds(file_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", file_path],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _split_audio(file_path: str, tmp_dir: str) -> list[str]:
    duration = _get_duration_seconds(file_path)
    n_chunks = max(1, math.ceil(duration / CHUNK_SECONDS))
    chunk_paths = []
    for i in range(n_chunks):
        start = i * CHUNK_SECONDS
        chunk_path = os.path.join(tmp_dir, f"chunk_{i}.mp3")
        subprocess.run(
            ["ffmpeg", "-y", "-i", file_path, "-ss", str(start),
             "-t", str(CHUNK_SECONDS), "-c", "copy", chunk_path],
            capture_output=True, check=True,
        )
        chunk_paths.append(chunk_path)
    return chunk_paths


def _transcribe_sync(file_path: str) -> str:
    if os.path.getsize(file_path) <= MAX_BYTES:
        return _transcribe_file_sync(file_path)

    with tempfile.TemporaryDirectory() as tmp_dir:
        chunks = _split_audio(file_path, tmp_dir)
        texts = [_transcribe_file_sync(chunk) for chunk in chunks]
    return " ".join(texts)


async def transcribe_audio(file_path: str) -> str:
    """Распознаёт речь в mp3-файле. Возвращает распознанный текст."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, file_path)
