# ffmpeg больше НЕ ставится через apt — используется imageio-ffmpeg (pip-пакет
# со встроенным статическим бинарником), поэтому обычный Python-образ
# без системных пакетов подходит. Этот Dockerfile нужен только если твой
# хостинг требует явный Dockerfile для деплоя — для Bothost с native Python
# деплоем он не обязателен вовсе.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
