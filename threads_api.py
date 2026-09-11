"""
Публикация текстовых постов в Threads через официальный Meta Graph API.

Схема в 2 шага:
  1. POST /{threads_user_id}/threads       -> создаёт "контейнер", возвращает creation_id
  2. POST /{threads_user_id}/threads_publish -> публикует контейнер по creation_id

THREADS_USER_ID и THREADS_ACCESS_TOKEN получаются один раз при настройке
Meta-приложения (см. README, раздел "Настройка Threads API").
"""
import time
import asyncio
import requests

from config import THREADS_USER_ID, THREADS_ACCESS_TOKEN, THREADS_API_BASE


def _create_container(text: str) -> str:
    url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads"
    params = {
        "media_type": "TEXT",
        "text": text,
        "access_token": THREADS_ACCESS_TOKEN,
    }
    resp = requests.post(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def _publish_container(creation_id: str) -> str:
    url = f"{THREADS_API_BASE}/{THREADS_USER_ID}/threads_publish"
    params = {
        "creation_id": creation_id,
        "access_token": THREADS_ACCESS_TOKEN,
    }
    resp = requests.post(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def _publish_sync(text: str) -> str:
    creation_id = _create_container(text)
    time.sleep(2)  # Meta рекомендует небольшую паузу перед публикацией контейнера
    return _publish_container(creation_id)


async def publish_post(text: str) -> str:
    """Публикует текстовый пост в Threads. Возвращает id опубликованного поста."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _publish_sync, text)
