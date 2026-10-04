#!/usr/bin/env python3
"""
Одноразовый патч для threads-agent: добавляет поддержку YouTube-cookies из
переменной COOKIES_B64 и JavaScript-движок Deno. Запускать из папки проекта:
    python3 patch_cookies.py
Ничего не удаляет; повторный запуск безопасен.
"""
import os, re, sys

COOKIES_BLOCK = '''

# --- YouTube cookies из переменной COOKIES_B64 (добавлено patch_cookies.py) ---
# Содержимое cookies.txt в base64. При запуске разворачивается в файл, который
# потом использует downloader.py. Так cookies не попадают в git.
_COOKIES_B64 = os.getenv("COOKIES_B64", "").strip()
if _COOKIES_B64:
    import base64 as _b64
    _cookies_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    try:
        with open(_cookies_path, "wb") as _f:
            _f.write(_b64.b64decode(_COOKIES_B64))
        COOKIES_FILE = _cookies_path
    except Exception as _e:
        print(f"ВНИМАНИЕ: не удалось раскодировать COOKIES_B64: {_e}")
'''

def need(path):
    if not os.path.exists(path):
        sys.exit(f"Не найден {path} — запусти скрипт из папки threads-agent")

need("config.py"); need("requirements.txt"); need("downloader.py")

# 1) config.py
cfg = open("config.py", encoding="utf-8").read()
if "COOKIES_B64" in cfg:
    print("config.py: поддержка COOKIES_B64 уже есть")
else:
    if not re.search(r"^import os\b|^import .*\bos\b", cfg, re.M):
        cfg = "import os\n" + cfg
    if "COOKIES_FILE" not in cfg:
        cfg += '\nCOOKIES_FILE = os.getenv("COOKIES_FILE", "")\n'
    cfg = cfg.rstrip() + "\n" + COOKIES_BLOCK
    open("config.py", "w", encoding="utf-8").write(cfg)
    print("config.py: добавлена поддержка COOKIES_B64")

# 2) requirements.txt
req = open("requirements.txt", encoding="utf-8").read().splitlines()
out, has_ytdlp, has_deno = [], False, False
for line in req:
    name = re.split(r"[<>=\[ ]", line.strip(), 1)[0].lower()
    if name == "yt-dlp":
        out.append("yt-dlp[default]>=2026.8.19"); has_ytdlp = True
    elif name == "deno":
        out.append(line); has_deno = True
    else:
        out.append(line)
if not has_ytdlp:
    out.append("yt-dlp[default]>=2026.8.19")
if not has_deno:
    out.append("deno>=2.0  # JavaScript-движок, нужен yt-dlp для YouTube")
open("requirements.txt", "w", encoding="utf-8").write("\n".join(out) + "\n")
print("requirements.txt: yt-dlp[default] и deno на месте")

# 3) .gitignore — cookies.txt не должен попасть в репозиторий
gi = open(".gitignore", encoding="utf-8").read() if os.path.exists(".gitignore") else ""
if "cookies.txt" not in gi:
    open(".gitignore", "a", encoding="utf-8").write(("\n" if gi and not gi.endswith("\n") else "") + "cookies.txt\n")
    print(".gitignore: добавлен cookies.txt")

print("\nГотово. Теперь: git add . && git commit -m \"youtube cookies + deno\" && git push")
