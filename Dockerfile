# Нужен именно Docker-путь деплоя (не обычный "Python-buildpack"), потому что
# боту требуется системный пакет ffmpeg — pip его поставить не может.
FROM python:3.11-slim

# ffmpeg нужен для извлечения аудио (downloader.py) и нарезки длинных
# файлов при транскрибации (transcriber.py)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
