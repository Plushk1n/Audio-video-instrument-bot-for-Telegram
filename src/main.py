"""
Telegram-бот для конвертации медиа:
  • видео/GIF  → видео-кружок (video note)
  • аудио/видео → голосовое сообщение (ogg/opus)
  • видео/ГС   → извлечение аудио (mp3)
  • видео      → GIF
"""

import asyncio
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import ffmpeg
from telegram import Update, ReplyKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

try:
    from dotenv import load_dotenv          # опционально: загрузка токена из .env
except ModuleNotFoundError:
    load_dotenv = None

# --- НАСТРОЙКИ ---
# Токен берётся из файла .env рядом со скриптом или из переменной окружения.
# В самом коде токена быть не должно.
if load_dotenv is not None:
    load_dotenv(Path(__file__).with_name(".env"))
BOT_TOKEN = os.environ.get("BOT_TOKEN")

MAX_DOWNLOAD_SIZE     = 20 * 1024 * 1024     # лимит Telegram на скачивание ботом (~20 МБ)
MAX_CONCURRENT_FFMPEG = 3                    # сколько ffmpeg-задач крутить одновременно (на всех)
PROGRESS_INTERVAL     = 2.0                  # как часто обновлять прогресс в сообщении (сек)

# Ограничения длительности на входе (сек)
LIMIT_VIDEO_NOTE = 60
LIMIT_AUDIO      = 300
LIMIT_GIF        = 30

STATE_KEY = "mode"
LOCK_KEY  = "lock"

BUTTON_STOP = "🛑 Остановить"

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# --- РЕЖИМЫ (единый источник правды) ---
MODE_VIDEO_NOTE, MODE_TO_VOICE, MODE_EXTRACT_AUDIO, MODE_TO_GIF = range(1, 5)


@dataclass(frozen=True)
class Mode:
    command: str
    button: str
    prompt: str
    status: str
    output_ext: str
    limit: int                                   # потолок длительности (для прогресса)
    valid_types: frozenset = field(default_factory=frozenset)
    error: str = "❌ Неподходящий файл."


MODES: dict[int, Mode] = {
    MODE_VIDEO_NOTE: Mode(
        command="videonote",
        button="🎬 Сделать кружок (видео/GIF)",
        prompt="🎬 Отправьте видео или GIF (до 1 мин). Можно слать несколько подряд.",
        status="🎬 Создаю кружок",
        output_ext=".mp4",
        limit=LIMIT_VIDEO_NOTE,
        valid_types=frozenset({"video", "video_note", "animation"}),
        error="❌ Этот файл не подходит для создания кружка.",
    ),
    MODE_TO_VOICE: Mode(
        command="tovoice",
        button="🎵 Конвертировать в ГС (MP3/видео)",
        prompt="🎵 Отправьте аудио или видео. Можно слать несколько подряд.",
        status="🎵 Конвертирую в ГС",
        output_ext=".ogg",
        limit=LIMIT_AUDIO,
        valid_types=frozenset({"audio", "video", "video_note", "animation", "audio_doc"}),
        error="❌ Этот файл не подходит для создания голосового.",
    ),
    MODE_EXTRACT_AUDIO: Mode(
        command="extractaudio",
        button="🎶 Извлечь аудио (видео/ГС)",
        prompt="🎶 Отправьте видео или голосовое. Можно слать несколько подряд.",
        status="🎶 Извлекаю аудио",
        output_ext=".mp3",
        limit=LIMIT_AUDIO,
        valid_types=frozenset({"video", "video_note", "voice", "animation"}),
        error="❌ Этот файл не подходит для извлечения аудио.",
    ),
    MODE_TO_GIF: Mode(
        command="togif",
        button="🖼️ Конвертировать в GIF",
        prompt="🖼️ Отправьте видео. Можно слать несколько подряд.",
        status="🖼️ Создаю GIF",
        output_ext=".gif",
        limit=LIMIT_GIF,
        valid_types=frozenset({"video", "video_note", "animation"}),
        error="❌ Этот файл не подходит для создания GIF.",
    ),
}

BUTTON_TO_MODE = {m.button: mid for mid, m in MODES.items()}
KEYBOARD = [
    [MODES[MODE_VIDEO_NOTE].button, MODES[MODE_EXTRACT_AUDIO].button],
    [MODES[MODE_TO_VOICE].button,   MODES[MODE_TO_GIF].button],
    [BUTTON_STOP],
]

# Семафор для ffmpeg создаётся лениво — внутри работающего event loop.
_ffmpeg_sem: Optional[asyncio.Semaphore] = None


def ffmpeg_semaphore() -> asyncio.Semaphore:
    global _ffmpeg_sem
    if _ffmpeg_sem is None:
        _ffmpeg_sem = asyncio.Semaphore(MAX_CONCURRENT_FFMPEG)
    return _ffmpeg_sem


# --- УТИЛИТЫ ---

def guess_extension(message, file_type: str) -> str:
    """Подбирает расширение для скачанного файла (ffmpeg всё равно определяет формат по содержимому)."""
    if file_type in ("video", "video_note", "animation"):
        return ".mp4"
    if file_type == "voice":
        return ".ogg"
    if file_type == "audio" and message.audio and message.audio.file_name:
        return os.path.splitext(message.audio.file_name)[1] or ".mp3"
    if message.document:
        mime = (message.document.mime_type or "").lower()
        for needle, ext in (("wav", ".wav"), ("mp3", ".mp3"), ("mpeg", ".mp3"),
                            ("ogg", ".ogg"), ("video", ".mp4")):
            if needle in mime:
                return ext
    return ".mp3" if file_type in ("audio", "audio_doc") else ".bin"


def pick_file(message):
    """Возвращает (объект файла, тип) или (None, None)."""
    if message.video:      return message.video,      "video"
    if message.video_note: return message.video_note, "video_note"
    if message.animation:  return message.animation,  "animation"   # GIF приходит как animation
    if message.audio:      return message.audio,      "audio"
    if message.voice:      return message.voice,      "voice"
    if message.document:
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("audio/"): return message.document, "audio_doc"
        if mime.startswith("video/"): return message.document, "video"
        return message.document, "document"
    return None, None


def probe_duration(src: str) -> Optional[float]:
    """Длительность входного файла в секундах (для расчёта процентов)."""
    try:
        info = ffmpeg.probe(src)
        dur = info.get("format", {}).get("duration")
        return float(dur) if dur else None
    except Exception:
        return None


def _parse_timestamp(ts: str) -> Optional[float]:
    try:
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None


def read_progress_ratio(path: str, total: float) -> Optional[float]:
    """Читает progress-файл ffmpeg и возвращает долю выполнения (0.0–0.999) или None."""
    if not total or total <= 0:
        return None
    try:
        with open(path, "r", errors="ignore") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.startswith("out_time="):
            val = _parse_timestamp(line.split("=", 1)[1].strip())
            if val is not None:
                return max(0.0, min(0.999, val / total))
    return None


def progress_bar(pct: int, width: int = 12) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "▓" * filled + "░" * (width - filled)


def format_eta(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"~{s} с"
    m, s = divmod(s, 60)
    return f"~{m} мин {s} с"


async def progress_loop(status_msg, base: str, path: str, total: float,
                        stop: asyncio.Event, start: float):
    """Периодически обновляет статусное сообщение прогрессом и ETA, пока stop не выставлен."""
    last_text = None
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=PROGRESS_INTERVAL)
            break                                   # stop выставлен — выходим
        except asyncio.TimeoutError:
            pass
        ratio = read_progress_ratio(path, total)
        if ratio is None:
            continue
        pct = max(0, min(99, int(ratio * 100)))
        line = f"{base}… {progress_bar(pct)} {pct}%"
        if ratio > 0.03:                            # ETA — только когда есть осмысленный сигнал
            elapsed = time.monotonic() - start
            line += f" · осталось {format_eta(elapsed * (1 - ratio) / ratio)}"
        if line != last_text:
            last_text = line
            try:
                await status_msg.edit_text(line)
            except Exception:
                pass                                 # "not modified", флуд-лимит и т.п. — игнорируем


# --- FFMPEG ---

def build_video_note(src: str, dst: str):
    probe = ffmpeg.probe(src)
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not video:
        raise ValueError("Видеопоток не найден.")

    side = min(int(video.get("width", 360)), int(video.get("height", 360)))
    inp = ffmpeg.input(src, t=LIMIT_VIDEO_NOTE)
    v = inp["v:0"].filter("crop", side, side).filter("scale", 360, 360)

    params = {
        "vcodec": "libx264",
        "pix_fmt": "yuv420p",          # совместимость со всеми плеерами
        "b:v": "1M",
        "tune": "zerolatency",
        "preset": "veryfast",
        "movflags": "+faststart",      # moov-атом в начало → быстрый старт воспроизведения
        "format": "mp4",
    }
    if has_audio:
        return ffmpeg.output(v, inp["a:0"], dst, acodec="aac", **params)
    return ffmpeg.output(v, dst, an=None, **params)


def build_to_voice(src: str, dst: str):
    return ffmpeg.output(
        ffmpeg.input(src, t=LIMIT_AUDIO),
        dst,
        vn=None,
        acodec="libopus",
        format="ogg",
        map_metadata=-1,
    )


def build_extract_audio(src: str, dst: str):
    return ffmpeg.output(
        ffmpeg.input(src, t=LIMIT_AUDIO),
        dst,
        vn=None,
        acodec="libmp3lame",
        format="mp3",
        **{"b:a": "192k"},
    )


def build_to_gif(src: str, dst: str, palette: str):
    scale_w = "if(gte(iw,ih),320,-2)"
    scale_h = "if(gte(iw,ih),-2,320)"
    base = (
        ffmpeg.input(src, t=LIMIT_GIF)
        .filter("fps", fps=15)
        .filter("scale", scale_w, scale_h)
    )
    p1 = base.filter("palettegen").output(palette, f="image2").overwrite_output()
    used = ffmpeg.filter([base, ffmpeg.input(palette)], "paletteuse")
    p2 = ffmpeg.output(used, dst, format="gif", an=None, loop=0)
    return p1, p2


# --- ОТПРАВКА РЕЗУЛЬТАТА ---

async def send_result(bot, chat_id, mode, path, ext, reply_to):
    kwargs = {
        "reply_to_message_id": reply_to,
        "read_timeout": 120,
        "write_timeout": 120,
        "connect_timeout": 180,
    }
    with open(path, "rb") as fh:
        media = InputFile(fh, filename=f"result{ext}")
        if mode == MODE_VIDEO_NOTE:
            await bot.send_video_note(chat_id, video_note=media, length=360, **kwargs)
        elif mode == MODE_TO_VOICE:
            await bot.send_voice(chat_id, voice=media, **kwargs)
        elif mode == MODE_EXTRACT_AUDIO:
            await bot.send_audio(chat_id, audio=media, title="Audio", **kwargs)
        else:  # MODE_TO_GIF
            await bot.send_animation(chat_id, animation=media, **kwargs)


# --- КОНВЕРТАЦИЯ ---

async def run_conversion(update, context, file_ref, mode, file_type, reply_to, status=None):
    cfg = MODES[mode]
    work_dir = tempfile.mkdtemp(prefix="tgconv_")          # уникальная папка → безопасно при параллельных задачах
    src      = os.path.join(work_dir, f"in{guess_extension(update.message, file_type)}")
    dst      = os.path.join(work_dir, f"out{cfg.output_ext}")
    palette  = os.path.join(work_dir, "palette.png")
    prog     = os.path.join(work_dir, "progress.txt")

    # status может быть уже создан (плашка «в очереди») — тогда переиспользуем его.
    if status is None:
        status = await update.message.reply_text(f"{cfg.status}…")
    else:
        try:
            await status.edit_text(f"{cfg.status}…")
        except Exception:
            pass
    try:
        tg_file = await file_ref.get_file()
        await tg_file.download_to_drive(
            src, read_timeout=180, write_timeout=180, connect_timeout=180
        )

        # Сколько секунд реально обрабатываем (с учётом потолка режима) — для процентов.
        real = probe_duration(src)
        total = min(real, cfg.limit) if real else float(cfg.limit)
        open(prog, "w").close()                            # progress-файл должен существовать

        def process():
            g = ("-progress", prog, "-nostats")
            if mode == MODE_VIDEO_NOTE:
                build_video_note(src, dst).global_args(*g).run(overwrite_output=True, quiet=True)
            elif mode == MODE_TO_VOICE:
                build_to_voice(src, dst).global_args(*g).run(overwrite_output=True, quiet=True)
            elif mode == MODE_EXTRACT_AUDIO:
                build_extract_audio(src, dst).global_args(*g).run(overwrite_output=True, quiet=True)
            else:
                p1, p2 = build_to_gif(src, dst, palette)
                p1.run(quiet=True)                         # палитра считается быстро, без прогресса
                p2.global_args(*g).run(overwrite_output=True, quiet=True)

        async with ffmpeg_semaphore():
            stop = asyncio.Event()
            start = time.monotonic()                       # отсчёт для ETA — с момента старта ffmpeg
            updater = asyncio.create_task(progress_loop(status, cfg.status, prog, total, stop, start))
            try:
                await asyncio.to_thread(process)
            finally:
                stop.set()
                await updater

        await send_result(context.bot, update.effective_chat.id, mode, dst, cfg.output_ext, reply_to)

        try:
            await status.delete()
        except Exception:
            pass

    except BadRequest as e:
        text = "❌ Файл слишком большой." if "too big" in str(e).lower() else f"❌ Ошибка отправки: {e}"
        await status.edit_text(text)
    except ffmpeg.Error as e:
        logger.error("ffmpeg error: %s", (e.stderr or b"").decode(errors="ignore"))
        await status.edit_text("❌ Ошибка обработки файла. Проверьте формат.")
    except Exception as e:
        logger.error("Conversion error: %s", e, exc_info=True)
        await status.edit_text("❌ Произошла ошибка.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)        # одна уборка вместо удаления каждого файла


# --- ХЭНДЛЕРЫ ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Выберите действие и отправляйте файлы — можно несколько подряд.\n"
        "Чтобы сбросить выбор, нажмите «🛑 Остановить» или /stop.",
        reply_markup=ReplyKeyboardMarkup(KEYBOARD, resize_keyboard=True),
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(STATE_KEY, None)
    await update.message.reply_text("🛑 Остановлено. Выберите действие в меню, когда понадобится.")


def make_mode_command(mode: int):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data[STATE_KEY] = mode
        await update.message.reply_text(MODES[mode].prompt)
    return handler


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if text == BUTTON_STOP:
        await cmd_stop(update, context)
        return
    mode = BUTTON_TO_MODE.get(text)
    if mode is None:
        await update.message.reply_text("Выберите команду из меню.")
        return
    context.user_data[STATE_KEY] = mode
    await update.message.reply_text(MODES[mode].prompt)


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    file_ref, file_type = pick_file(message)
    if not file_ref:
        return

    mode = context.user_data.get(STATE_KEY)
    if mode is None:
        if update.effective_chat.type == "private":
            await message.reply_text("⚠️ Сначала выберите действие в меню.")
        return

    if file_type not in MODES[mode].valid_types:
        await message.reply_text(MODES[mode].error)
        return

    size = getattr(file_ref, "file_size", None) or 0
    if size > MAX_DOWNLOAD_SIZE:
        await message.reply_text("❌ Файл слишком большой — Telegram позволяет боту скачивать до 20 МБ.")
        return

    # Липкий режим: STATE_KEY НЕ сбрасываем — пользователь может слать файлы подряд.
    # Per-user lock: файлы одного пользователя обрабатываются строго по очереди.
    lock = context.user_data.get(LOCK_KEY)
    if lock is None:
        lock = asyncio.Lock()
        context.user_data[LOCK_KEY] = lock

    # Если очередь занята — сразу даём знать; это же сообщение станет статусным.
    status = None
    if lock.locked():
        status = await message.reply_text("🕓 В очереди…")

    async with lock:
        await run_conversion(update, context, file_ref, mode, file_type, message.message_id, status)


# --- ЗАПУСК ---

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан токен BOT_TOKEN. Создайте файл .env рядом со скриптом "
            "(BOT_TOKEN=ваш_токен) и установите python-dotenv: pip install python-dotenv. "
            "Либо задайте переменную окружения BOT_TOKEN."
        )

    logger.info("🚀 Бот запускается...")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(120)
        .write_timeout(120)
        .connect_timeout(120)
        .pool_timeout(120)
        .concurrent_updates(True)        # параллельная обработка апдейтов разных пользователей
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    for mode_id, cfg in MODES.items():
        app.add_handler(CommandHandler(cfg.command, make_mode_command(mode_id)))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        on_text,
    ))

    media_filter = (
        filters.VIDEO | filters.AUDIO | filters.VOICE |
        filters.VIDEO_NOTE | filters.ANIMATION | filters.Document.ALL
    )
    app.add_handler(MessageHandler(media_filter, on_media))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
