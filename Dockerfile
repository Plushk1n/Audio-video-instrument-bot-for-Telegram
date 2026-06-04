# --- ffmpeg 7.0 (статический бинарник свежей версии) ---
# Берём готовый ffmpeg/ffprobe из образа mwader/static-ffmpeg, чтобы не зависеть
# от устаревшего ffmpeg в системных пакетах Debian (там 5.1, который спотыкается
# на видео с айфона: HEVC 10-бит, HDR, Dolby Vision).
FROM mwader/static-ffmpeg:7.0 AS ffmpeg

FROM python:3.11-slim
COPY --from=ffmpeg /ffmpeg  /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
CMD ["python", "src/main.py"]
