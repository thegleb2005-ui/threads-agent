"""
Обновляет long-lived токен Threads до истечения (продлевает ещё на 60 дней).
Запускай раз в месяц вручную, либо повесь на cron.

Использование:
    python scripts/refresh_threads_token.py

Токен для обновления берётся из THREADS_ACCESS_TOKEN в config.env (через config.py).
Новый токен нужно будет вручную обновить в config.env / переменных окружения хостинга.
"""
import sys
import requests

from config import THREADS_ACCESS_TOKEN

REFRESH_URL = "https://graph.threads.net/refresh_access_token"


def main():
    if not THREADS_ACCESS_TOKEN:
        print("THREADS_ACCESS_TOKEN не задан в config.env")
        sys.exit(1)

    resp = requests.get(
        REFRESH_URL,
        params={
            "grant_type": "th_refresh_token",
            "access_token": THREADS_ACCESS_TOKEN,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Ошибка обновления токена: {resp.status_code} {resp.text}")
        sys.exit(1)

    data = resp.json()
    new_token = data["access_token"]
    expires_in_days = data.get("expires_in", 0) // 86400

    print("Токен обновлён. Новый THREADS_ACCESS_TOKEN:\n")
    print(new_token)
    print(f"\nДействителен ещё ~{expires_in_days} дней.")
    print("Обнови значение THREADS_ACCESS_TOKEN в config.env / переменных окружения хостинга.")


if __name__ == "__main__":
    main()
