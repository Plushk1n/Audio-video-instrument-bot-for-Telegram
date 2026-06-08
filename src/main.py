"""
Telegram-бот для конвертации медиа (лайт-версия, 4 режима):
  • видео/GIF   → видео-кружок (video note)
  • аудио/видео → голосовое сообщение (ogg/opus)
  • видео/ГС    → извлечение аудио (mp3)
  • видео       → GIF (первые 30 секунд)

Версия 1.1.2 — исправлена работа в группах: там бот управляется командами (/videonote, /tovoice,
/extractaudio, /togif, /cancel) без нерабочих кнопок-меню, кружок делается по центру, GIF берёт первые
30 секунд. В личных сообщениях всё работает по-прежнему.
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
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile, ReplyParameters
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest, Forbidden

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
CONVERSION_TIMEOUT    = 240                  # потолок на одну операцию ffmpeg (сек) — защита от зависаний

# Ограничения длительности на входе (сек)
LIMIT_VIDEO_NOTE = 60
LIMIT_AUDIO      = 300
LIMIT_GIF        = 30

VIDEO_NOTE_SIZE  = 512                       # сторона кружка (px); Telegram показывает его кругом

# Ключи в user_data
STATE_KEY   = "mode"
LOCK_KEY    = "lock"
BLOCKED_KEY = "blocked"                      # пользователь заблокировал бота → не тратим ресурсы
TASKS_KEY   = "tasks"                        # набор активных задач конвертации (для отмены)

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
    same_types: frozenset = field(default_factory=frozenset)   # типы входа, которые уже являются результатом
    same_type_error: str = ""                                  # сообщение для случая «уже такой формат»


MODES: dict[int, Mode] = {
    MODE_VIDEO_NOTE: Mode(
        command="videonote",
        button="🎬 Сделать кружок (видео/GIF)",
        prompt="🎬 Отправьте видео или GIF. Можно слать несколько подряд.\n"
               "⚠️ Видео длиннее 1 минуты обрежется до 1 минуты автоматически.",
        status="🎬 Создаю кружок",
        output_ext=".mp4",
        limit=LIMIT_VIDEO_NOTE,
        valid_types=frozenset({"video", "video_note", "animation"}),
        error="❌ Для кружка нужно видео или GIF — отправьте подходящий файл.",
        same_types=frozenset({"video_note"}),
        same_type_error="🔁 Это уже кружок — конвертировать не нужно.",
    ),
    MODE_TO_VOICE: Mode(
        command="tovoice",
        button="🎵 Конвертировать в ГС (аудио/видео)",
        prompt="🎵 Отправьте аудио или видео. Можно слать несколько подряд.",
        status="🎵 Конвертирую в ГС",
        output_ext=".ogg",
        limit=LIMIT_AUDIO,
        valid_types=frozenset({"audio", "video", "video_note", "audio_doc"}),
        error="❌ Для голосового нужно аудио или видео — отправьте подходящий файл.",
        same_types=frozenset({"voice"}),
        same_type_error="🔁 Это уже голосовое сообщение.",
    ),
    MODE_EXTRACT_AUDIO: Mode(
        command="extractaudio",
        button="🎶 Извлечь аудио (видео/ГС)",
        prompt="🎶 Отправьте видео или голосовое. Можно слать несколько подряд.",
        status="🎶 Извлекаю аудио",
        output_ext=".mp3",
        limit=LIMIT_AUDIO,
        valid_types=frozenset({"video", "video_note", "voice"}),
        error="❌ Для извлечения аудио нужно видео или голосовое — отправьте подходящий файл.",
        same_types=frozenset({"audio", "audio_doc"}),
        same_type_error="🔁 Это уже аудиофайл — извлекать не нужно.",
    ),
    MODE_TO_GIF: Mode(
        command="togif",
        button="🖼️ Конвертировать в GIF",
        prompt="🖼️ Отправьте видео. Можно слать несколько подряд.\n"
               "⚠️ Для GIF беру первые 30 секунд.",
        status="🖼️ Создаю GIF",
        output_ext=".gif",
        limit=LIMIT_GIF,
        valid_types=frozenset({"video", "video_note", "animation"}),
        error="❌ Для GIF нужно видео — отправьте подходящий файл.",
        same_types=frozenset({"animation"}),
        same_type_error="🔁 Это уже GIF.",
    ),
}

BUTTON_TO_MODE = {m.button: mid for mid, m in MODES.items()}

BTN_CANCEL = "❌ Отмена"
KEYBOARD = [
    [MODES[MODE_VIDEO_NOTE].button, MODES[MODE_EXTRACT_AUDIO].button],
    [MODES[MODE_TO_VOICE].button,   MODES[MODE_TO_GIF].button],
    [BTN_CANCEL],
]

# Режимы, которым на выходе нужен звук — для них проверяем наличие аудиодорожки.
AUDIO_OUTPUT_MODES = frozenset({MODE_TO_VOICE, MODE_EXTRACT_AUDIO})

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


# Расширения, по которым опознаём видео/аудио, присланные как «документ».
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv",
              ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".m2ts", ".ogv"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".oga", ".opus",
              ".m4a", ".wma", ".aiff", ".aif", ".alac", ".ape", ".wv"}


def pick_file(message):
    """Возвращает (объект файла, тип) или (None, None)."""
    if message.video:      return message.video,      "video"
    if message.video_note: return message.video_note, "video_note"
    if message.animation:  return message.animation,  "animation"   # GIF приходит как animation
    if message.audio:      return message.audio,      "audio"
    if message.voice:      return message.voice,      "voice"
    if message.photo:      return message.photo[-1],  "photo"        # фото → как медиа (неподходящий тип)
    if message.document:
        mime = (message.document.mime_type or "").lower()
        ext  = os.path.splitext(message.document.file_name or "")[1].lower()
        # Многие форматы Telegram шлёт как «документ» с mime application/octet-stream —
        # тогда ориентируемся на расширение файла.
        if mime == "image/gif" or ext == ".gif":
            return message.document, "animation"                    # большой GIF приходит файлом
        if mime.startswith("audio/") or ext in AUDIO_EXTS:
            return message.document, "audio_doc"
        if mime.startswith("video/") or ext in VIDEO_EXTS:
            return message.document, "video"
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


def has_audio_stream(src: str) -> bool:
    """True, если в файле есть звуковая дорожка (для аудио-режимов)."""
    try:
        info = ffmpeg.probe(src)
        return any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    except Exception:
        return True   # не смогли проверить — не блокируем, пусть решает ffmpeg


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


def progress_bar(pct: int, width: int = 10) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "▓" * filled + "░" * (width - filled)


def format_eta(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"~{s} с"
    m, s = divmod(s, 60)
    return f"~{m} мин {s} с"


def _cancel_user_tasks(context) -> int:
    """Отменяет все активные задачи конвертации пользователя; возвращает их количество."""
    active = [t for t in list(context.user_data.get(TASKS_KEY) or set()) if not t.done()]
    for t in active:
        t.cancel()
    return len(active)


def cancel_all_user_tasks(context) -> None:
    """Пользователь заблокировал бота: помечаем это и гасим все его задачи (текущую и очередь)."""
    context.user_data[BLOCKED_KEY] = True
    _cancel_user_tasks(context)


async def progress_loop(status_msg, base: str, path: str, total: float,
                        stop: asyncio.Event, start: float, context):
    """Периодически обновляет статусное сообщение прогрессом и ETA, пока stop не выставлен.

    Если обновление падает с Forbidden — значит, пользователь заблокировал бота прямо во время
    конвертации. Тогда сразу гасим ВСЕ его задачи (текущую и очередь), чтобы ffmpeg не молотил впустую.
    """
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
        line = f"{base}…\n{progress_bar(pct)} {pct}%"
        if ratio > 0.03:                            # ETA — только когда есть осмысленный сигнал
            elapsed = time.monotonic() - start
            line += f" · осталось {format_eta(elapsed * (1 - ratio) / ratio)}"
        if line != last_text:
            last_text = line
            try:
                await status_msg.edit_text(line)
            except Forbidden:
                cancel_all_user_tasks(context)      # пользователь заблокировал бота — гасим всё
                return
            except Exception:
                pass                                 # "not modified", флуд-лимит и т.п. — игнорируем


def _tail(stderr_bytes, n: int = 6) -> str:
    """Последние значимые строки stderr ffmpeg — там настоящая причина ошибки."""
    text = (stderr_bytes or b"").decode(errors="ignore")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines[-n:]) if lines else "(пусто)"


# --- FFMPEG ---

def _input(src: str, **kwargs):
    """Вход с декодом в 1 поток — экономия памяти на контейнере (защита от OOM на тяжёлых HEVC/HDR-видео).

    Число потоков влияет только на память и скорость декода, но не на качество результата.
    """
    return ffmpeg.input(src, threads=1, **kwargs)


def build_video_note(src: str, dst: str):
    probe = ffmpeg.probe(src)
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not video:
        raise ValueError("Видеопоток не найден.")

    side = min(int(video.get("width", VIDEO_NOTE_SIZE)), int(video.get("height", VIDEO_NOTE_SIZE)))
    inp = _input(src, t=LIMIT_VIDEO_NOTE)
    v = (inp["v:0"]
         .filter("crop", side, side)
         .filter("scale", VIDEO_NOTE_SIZE, VIDEO_NOTE_SIZE, flags="lanczos"))

    params = {
        "vcodec": "libx264",
        "pix_fmt": "yuv420p",          # совместимость со всеми плеерами
        "crf": 23,                     # качество по CRF (меньше — лучше) вместо фиксированного битрейта
        "preset": "veryfast",
        "movflags": "+faststart",      # moov-атом в начало → быстрый старт воспроизведения
        "format": "mp4",
    }
    if has_audio:
        return ffmpeg.output(v, inp["a:0"], dst, acodec="aac",
                             **{"b:a": "128k"}, **params)
    return ffmpeg.output(v, dst, an=None, **params)


def build_to_voice(src: str, dst: str):
    return ffmpeg.output(
        _input(src, t=LIMIT_AUDIO),
        dst,
        vn=None,
        acodec="libopus",
        format="ogg",
        map_metadata=-1,
    )


def build_extract_audio(src: str, dst: str):
    return ffmpeg.output(
        _input(src, t=LIMIT_AUDIO),
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
        _input(src, t=LIMIT_GIF)
        .filter("fps", fps=15)
        .filter("scale", scale_w, scale_h)
    )
    p1 = base.filter("palettegen").output(palette, f="image2")
    used = ffmpeg.filter([base, ffmpeg.input(palette)], "paletteuse")
    p2 = ffmpeg.output(used, dst, format="gif", an=None, loop=0)
    return p1, p2


async def run_ffmpeg(spec, prog: Optional[str] = None, timeout: int = CONVERSION_TIMEOUT):
    """Запуск ffmpeg как асинхронного процесса; убиваем процесс при таймауте и отмене."""
    if prog:
        spec = spec.global_args("-progress", prog, "-nostats")
    args = ffmpeg.compile(spec, overwrite_output=True)
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise
    if proc.returncode:
        err = ffmpeg.Error("ffmpeg", b"", stderr or b"")
        err.returncode = proc.returncode
        raise err
    return stderr


# --- ОТПРАВКА РЕЗУЛЬТАТА ---

def _reply_params(msg_id):
    return ReplyParameters(message_id=msg_id, allow_sending_without_reply=True)


async def send_result(bot, chat_id, mode, path, ext, reply_to):
    kwargs = {
        "reply_parameters": _reply_params(reply_to),
        "read_timeout": 120,
        "write_timeout": 120,
        "connect_timeout": 180,
    }
    with open(path, "rb") as fh:
        media = InputFile(fh, filename=f"result{ext}")
        if mode == MODE_VIDEO_NOTE:
            await bot.send_video_note(chat_id, video_note=media, length=VIDEO_NOTE_SIZE, **kwargs)
        elif mode == MODE_TO_VOICE:
            await bot.send_voice(chat_id, voice=media, **kwargs)
        elif mode == MODE_EXTRACT_AUDIO:
            await bot.send_audio(chat_id, audio=media, title="Audio", **kwargs)
        else:  # MODE_TO_GIF
            await bot.send_animation(chat_id, animation=media, **kwargs)


async def _safe_edit(msg, text):
    if msg is None:
        return
    try:
        await msg.edit_text(text)
    except Exception:
        pass


# --- КОНВЕРТАЦИЯ ---

async def run_conversion(update, context, file_ref, mode, file_type, reply_to, status=None):
    cfg = MODES[mode]
    work_dir = tempfile.mkdtemp(prefix="tgconv_")          # уникальная папка → безопасно при параллельных задачах
    src      = os.path.join(work_dir, f"in{guess_extension(update.message, file_type)}")
    dst      = os.path.join(work_dir, f"out{cfg.output_ext}")
    palette  = os.path.join(work_dir, "palette.png")
    prog     = os.path.join(work_dir, "progress.txt")

    try:
        # status может быть уже создан (плашка «в очереди») — тогда переиспользуем его.
        if status is None:
            status = await update.message.reply_text(f"{cfg.status}…", do_quote=True)
        else:
            await _safe_edit(status, f"{cfg.status}…")

        tg_file = await file_ref.get_file()
        await tg_file.download_to_drive(
            src, read_timeout=180, write_timeout=180, connect_timeout=180
        )

        # Если режим даёт аудио, а звуковой дорожки нет — понятное сообщение вместо ошибки формата.
        if mode in AUDIO_OUTPUT_MODES and not has_audio_stream(src):
            await status.edit_text("❌ В этом файле нет звука — обрабатывать нечего.")
            return

        # Сколько секунд реально обрабатываем (с учётом потолка режима) — для процентов.
        real = probe_duration(src)
        total = min(real, cfg.limit) if real else float(cfg.limit)
        open(prog, "w").close()                            # progress-файл должен существовать

        async with ffmpeg_semaphore():
            stop = asyncio.Event()
            start = time.monotonic()                       # отсчёт для ETA — с момента старта ffmpeg
            updater = asyncio.create_task(progress_loop(status, cfg.status, prog, total, stop, start, context))
            try:
                if mode == MODE_VIDEO_NOTE:
                    await run_ffmpeg(build_video_note(src, dst), prog)
                elif mode == MODE_TO_VOICE:
                    await run_ffmpeg(build_to_voice(src, dst), prog)
                elif mode == MODE_EXTRACT_AUDIO:
                    await run_ffmpeg(build_extract_audio(src, dst), prog)
                else:
                    p1, p2 = build_to_gif(src, dst, palette)
                    await run_ffmpeg(p1)                   # палитра считается быстро, без прогресса
                    await run_ffmpeg(p2, prog)
            finally:
                stop.set()
                await updater

        await send_result(context.bot, update.effective_chat.id, mode, dst, cfg.output_ext, reply_to)

        try:
            await status.delete()
        except Exception:
            pass

    except asyncio.CancelledError:
        # пользователь нажал «Отмена» — ffmpeg уже убит в run_ffmpeg, убираем статус
        try:
            await status.delete()
        except Exception:
            pass
        raise
    except Forbidden:
        # бот заблокирован пользователем — помечаем и больше ничего не отправляем
        context.user_data[BLOCKED_KEY] = True
        logger.info("Бот заблокирован пользователем — останавливаю обработку и очередь.")
    except asyncio.TimeoutError:
        await _safe_edit(status, "❌ Обработка заняла слишком долго и была остановлена. Попробуйте файл покороче.")
    except BadRequest as e:
        text = "❌ Файл слишком большой." if "too big" in str(e).lower() else f"❌ Ошибка отправки: {e}"
        await _safe_edit(status, text)
    except ffmpeg.Error as e:
        logger.error("ffmpeg error (rc=%s): %s", getattr(e, "returncode", "?"), _tail(e.stderr))
        await _safe_edit(status, "❌ Ошибка обработки файла. Проверьте формат.")
    except Exception as e:
        logger.error("Conversion error: %s", e, exc_info=True)
        await _safe_edit(status, "❌ Произошла ошибка.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)        # одна уборка вместо удаления каждого файла


# --- ХЭНДЛЕРЫ ---

GROUP_REPLY_NOTE = (
    "\n\n📎 В группе пришлите файл ответом (reply) на это сообщение бота — "
    "иначе из-за настроек приватности бот его не получит."
)

GROUP_HELP = (
    "👋 Привет! В группе я работаю по командам (кнопок-меню тут нет):\n"
    "• /videonote — видеокружок (по центру)\n"
    "• /tovoice — в голосовое\n"
    "• /extractaudio — извлечь аудио\n"
    "• /togif — в GIF (первые 30 секунд)\n"
    "• /cancel — отменить конвертацию\n\n"
    "Выберите команду и пришлите файл ответом (reply) на моё сообщение."
)


def is_group(chat) -> bool:
    return chat.type in ("group", "supergroup")


def with_group_note(text: str, chat) -> str:
    """В группе добавляет примечание: файл нужно слать ответом на сообщение бота."""
    if chat.type in ("group", "supergroup"):
        return text + GROUP_REPLY_NOTE
    return text


async def clear_blocked_pre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Любое входящее сообщение → пользователь нас не блокирует (Telegram бы его не доставил)."""
    if context.user_data is not None:
        context.user_data.pop(BLOCKED_KEY, None)


async def cancel_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Отмена»: гасим активные конвертации пользователя (или сообщаем, что нечего отменять)."""
    if _cancel_user_tasks(context):
        await update.message.reply_text("❌ Останавливаю конвертацию…")
    else:
        await update.message.reply_text("Сейчас нечего отменять — конвертация не идёт.")


async def prompt_send_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Реакция на текст/стикер: просим файл (если режим выбран) или выбрать действие."""
    if context.user_data.get(STATE_KEY) is not None:
        await update.message.reply_text("📎 Отправьте медиафайл — текст и стикеры я не конвертирую.")
    else:
        await update.message.reply_text("⚠️ Сначала выберите режим в меню и отправьте медиафайл.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_group(update.effective_chat):
        await update.message.reply_text(GROUP_HELP, reply_markup=ReplyKeyboardRemove())
        return
    text = "👋 Привет! Выберите действие и отправляйте файлы — можно несколько подряд."
    await update.message.reply_text(
        text,
        reply_markup=ReplyKeyboardMarkup(KEYBOARD, resize_keyboard=True),
    )


def make_mode_command(mode: int):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data[STATE_KEY] = mode
        chat = update.effective_chat
        # В группе убираем любые кнопки (их там нет), в личке клавиатуру не трогаем.
        markup = ReplyKeyboardRemove() if is_group(chat) else None
        await update.message.reply_text(with_group_note(MODES[mode].prompt, chat), reply_markup=markup)
    return handler


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    if text == BTN_CANCEL:
        await cancel_conversion(update, context)
        return
    mode = BUTTON_TO_MODE.get(text)
    if mode is not None:
        context.user_data[STATE_KEY] = mode
        await update.message.reply_text(MODES[mode].prompt)
        return
    # не кнопка — обычный текст
    await prompt_send_file(update, context)


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    file_ref, file_type = pick_file(message)
    if not file_ref:
        return

    mode = context.user_data.get(STATE_KEY)
    if mode is None:
        if update.effective_chat.type == "private":
            await message.reply_text("⚠️ Сначала выберите режим в меню.", do_quote=True)
        return

    cfg = MODES[mode]
    # Уже готовый результат (GIF в режиме GIF, аудиофайл в извлечении и т.п.) — сообщаем и выходим.
    if file_type in cfg.same_types:
        await message.reply_text(cfg.same_type_error, do_quote=True)
        return
    # Неподходящий формат (например, GIF в режиме голосового или извлечения).
    if file_type not in cfg.valid_types:
        await message.reply_text(cfg.error, do_quote=True)
        return
    # Слишком большой для скачивания ботом.
    size = getattr(file_ref, "file_size", None) or 0
    if size > MAX_DOWNLOAD_SIZE:
        await message.reply_text("❌ Файл слишком большой — Telegram позволяет боту скачивать до 20 МБ.", do_quote=True)
        return

    # Липкий режим: STATE_KEY НЕ сбрасываем — пользователь может слать файлы подряд.
    # Per-user lock: файлы одного пользователя обрабатываются строго по очереди.
    lock = context.user_data.get(LOCK_KEY)
    if lock is None:
        lock = asyncio.Lock()
        context.user_data[LOCK_KEY] = lock

    tasks = context.user_data.setdefault(TASKS_KEY, set())
    task = asyncio.current_task()
    tasks.add(task)
    try:
        # Если очередь занята — сразу даём знать; это же сообщение станет статусным.
        status = None
        if lock.locked():
            try:
                status = await message.reply_text("🕓 В очереди…", do_quote=True)
            except Forbidden:
                context.user_data[BLOCKED_KEY] = True
                return

        try:
            async with lock:
                if context.user_data.get(BLOCKED_KEY):
                    return                             # пользователь заблокировал — не тратим ресурсы
                await run_conversion(update, context, file_ref, mode, file_type, message.message_id, status)
        except asyncio.CancelledError:
            # отмена, пока файл ждал очереди — убираем плашку «в очереди»
            if status is not None:
                try:
                    await status.delete()
                except Exception:
                    pass
            raise
    finally:
        tasks.discard(task)


async def on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Неизвестная команда. Выберите действие в меню.")


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

    # Снимаем метку «заблокирован» при любом входящем апдейте (раньше всех остальных хэндлеров).
    app.add_handler(TypeHandler(Update, clear_blocked_pre), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cancel_conversion))
    for mode_id, cfg in MODES.items():
        app.add_handler(CommandHandler(cfg.command, make_mode_command(mode_id)))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        on_text,
    ))

    # Стикеры (как и текст) — не файлы: просим прислать файл / выбрать режим.
    app.add_handler(MessageHandler(
        filters.Sticker.ALL & filters.ChatType.PRIVATE,
        prompt_send_file,
    ))

    media_filter = (
        filters.VIDEO | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE |
        filters.ANIMATION | filters.PHOTO | filters.Document.ALL
    )
    app.add_handler(MessageHandler(media_filter, on_media))

    # Неизвестные команды — регистрируем после всех конкретных, только в личке.
    app.add_handler(MessageHandler(
        filters.COMMAND & filters.ChatType.PRIVATE,
        on_unknown_command,
    ))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
