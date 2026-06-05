# Системный ffmpeg из Debian (apt). Именно он корректно обрабатывает «тяжёлые»
# видео с айфона (HEVC 10-бит, HDR, Dolby Vision) — на этих файлах спотыкалась
# статическая сборка mwader/static-ffmpeg:7.0, которую мы пробовали ранее.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
CMD ["python", "src/main.py"]
