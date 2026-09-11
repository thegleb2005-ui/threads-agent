"""
Разовый скрипт для получения THREADS_USER_ID и THREADS_ACCESS_TOKEN.

Запускается один раз локально (не на сервере) — проводит через OAuth-авторизацию
Threads и на выходе печатает готовые значения для .env файла агента.

Использование:
    python scripts/get_threads_token.py

Понадобится (получить в App Dashboard -> Use cases -> Access the Threads API -> Settings):
  - Threads App ID
  - Threads App Secret
  - Redirect URI, добавленный в Valid OAuth Redirect URIs
    (если своего сайта нет — используй https://oauth.pstmn.io/v1/callback)

Перед запуском обязательно:
  1. Добавь себя как Threads Tester (App roles -> Roles -> Add People)
  2. Прими приглашение в приложении Threads на телефоне
     (Настройки -> Website permissions -> Invites)
"""
import sys
import urllib.parse

import requests

AUTH_BASE = "https://threads.net/oauth/authorize"
TOKEN_EXCHANGE_URL = "https://graph.threads.net/oauth/access_token"
LONG_LIVED_URL = "https://graph.threads.net/access_token"

SCOPES = "threads_basic,threads_content_publish"


def ask(prompt: str) -> str:
    value = input(prompt).strip()
    if not value:
        print("Пустое значение недопустимо.")
        sys.exit(1)
    return value


def main():
    print("=== Получение токена Threads API ===\n")

    app_id = ask("Threads App ID: ")
    app_secret = ask("Threads App Secret: ")
    redirect_uri = ask("Redirect URI (как в настройках приложения): ")

    auth_url = (
        f"{AUTH_BASE}?"
        f"client_id={urllib.parse.quote(app_id)}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
        f"&scope={SCOPES}"
        f"&response_type=code"
    )

    print("\n1. Открой эту ссылку в браузере и авторизуйся под своим Threads-аккаунтом:\n")
    print(f"   {auth_url}\n")
    print("2. После согласия браузер перекинет на твой redirect_uri с параметром ?code=...")
    print("   Страница может не загрузиться — это нормально, код всё равно в адресной строке.\n")

    redirected_url = ask("Вставь сюда полный URL, на который тебя перекинуло (или просто code): ")

    if "code=" in redirected_url:
        parsed = urllib.parse.urlparse(redirected_url)
        query = urllib.parse.parse_qs(parsed.query)
        code = query.get("code", [None])[0]
        if not code and "#" in redirected_url:
            # иногда code оказывается в фрагменте после #_
            fragment_part = redirected_url.split("code=", 1)[1]
            code = fragment_part.split("&")[0].split("#")[0]
    else:
        code = redirected_url  # предположим, что вставили сразу голый код

    if not code:
        print("Не удалось найти code в введённой строке.")
        sys.exit(1)

    print(f"\nНайден code: {code[:15]}...\n")

    # Шаг 1: обмен code на короткоживущий токен
    resp = requests.post(
        TOKEN_EXCHANGE_URL,
        data={
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"Ошибка обмена code на токен: {resp.status_code} {resp.text}")
        sys.exit(1)

    data = resp.json()
    short_token = data["access_token"]
    user_id = data["user_id"]
    print(f"Короткоживущий токен получен. user_id = {user_id}")

    # Шаг 2: обмен короткоживущего токена на long-lived
    resp2 = requests.get(
        LONG_LIVED_URL,
        params={
            "grant_type": "th_exchange_token",
            "client_secret": app_secret,
            "access_token": short_token,
        },
        timeout=30,
    )
    if resp2.status_code != 200:
        print(f"Ошибка обмена на long-lived токен: {resp2.status_code} {resp2.text}")
        sys.exit(1)

    long_data = resp2.json()
    long_token = long_data["access_token"]
    expires_in_days = long_data.get("expires_in", 0) // 86400

    print("\n=== Готово! Добавь в .env: ===\n")
    print(f"THREADS_USER_ID={user_id}")
    print(f"THREADS_ACCESS_TOKEN={long_token}")
    print(f"\nТокен действителен ~{expires_in_days} дней. Не забудь обновлять его")
    print("через scripts/refresh_threads_token.py примерно раз в месяц.")


if __name__ == "__main__":
    main()
