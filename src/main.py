"""
Telegram-бот для конвертации медиа:
  • видео/GIF  → видео-кружок (video note), с выбором области кадра
  • аудио/видео → голосовое сообщение (ogg/opus)
  • видео/ГС   → извлечение аудио (mp3)
  • видео/кружок → GIF, с выбором временного отрезка (до 30 сек)

Версия 2.0.0 — меню по категориям (Видео/Аудио), выбор области кадра для кружка,
выбор отрезка для GIF. Управление файлами и обработчики ситуаций сохранены из 1.x.
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import ffmpeg
from telegram import (
    Update,
    InputFile,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
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

VIDEO_NOTE_SIZE  = 512                       # сторона кружка (px); Telegram показывает его кругом

# Ключи в user_data
STATE_KEY   = "mode"                         # текущий режим
CROP_KEY    = "crop"                         # область кадра для кружка: top / center / bottom
PENDING_KEY = "pending_gif"                  # видео, ожидающее ввода отрезка для GIF
LOCK_KEY    = "lock"                         # пер-юзер очередь обработки

MENU_BUTTON = "☰ Меню"                       # постоянная кнопка снизу

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
    title: str                                   # короткое название для подтверждений
    prompt: str
    status: str
    output_ext: str
    limit: int                                   # потолок длительности (для прогресса)
    valid_types: frozenset = field(default_factory=frozenset)
    error: str = "❌ Неподходящий файл."
    same_type: Optional[str] = None              # тип входа, который уже является результатом
    same_type_error: str = ""                    # сообщение для случая «уже такой формат»


MODES: "dict[int, Mode]" = {
    MODE_VIDEO_NOTE: Mode(
        command="videonote",
        title="кружок",
        prompt="🎬 Пришлите видео или GIF — можно несколько подряд.\n"
               "⚠️ Видео длиннее 1 минуты обрежется до 1 минуты автоматически.",
        status="🎬 Создаю кружок",
        output_ext=".mp4",
        limit=LIMIT_VIDEO_NOTE,
        valid_types=frozenset({"video", "animation"}),
        error="❌ Для кружка нужно видео или GIF — отправьте подходящий файл.",
        same_type="video_note",
        same_type_error="🔁 Это уже кружок — конвертировать не нужно.",
    ),
    MODE_TO_VOICE: Mode(
        command="tovoice",
        title="голосовое",
        prompt="🎵 Пришлите аудио или видео — можно несколько подряд.",
        status="🎵 Конвертирую в голосовое",
        output_ext=".ogg",
        limit=LIMIT_AUDIO,
        valid_types=frozenset({"audio", "video", "video_note", "animation", "audio_doc"}),
        error="❌ Для голосового нужно аудио или видео — отправьте подходящий файл.",
        same_type="voice",
        same_type_error="🔁 Это уже голосовое сообщение.",
    ),
    MODE_EXTRACT_AUDIO: Mode(
        command="extractaudio",
        title="извлечение аудио",
        prompt="🎶 Пришлите видео или голосовое — можно несколько подряд.",
        status="🎶 Извлекаю аудио",
        output_ext=".mp3",
        limit=LIMIT_AUDIO,
        valid_types=frozenset({"video", "video_note", "voice", "animation"}),
        error="❌ Для извлечения аудио нужно видео или голосовое — отправьте подходящий файл.",
    ),
    MODE_TO_GIF: Mode(
        command="togif",
        title="GIF",
        prompt="🖼️ Пришлите видео или кружок — потом выберите отрезок (или «Всё»).",
        status="🖼️ Создаю GIF",
        output_ext=".gif",
        limit=LIMIT_GIF,
        valid_types=frozenset({"video", "video_note"}),
        error="❌ Для GIF нужно видео или кружок — отправьте подходящий файл.",
        same_type="animation",
        same_type_error="🔁 Это уже GIF.",
    ),
}

# Режимы, которым на выходе нужен звук — для них проверяем наличие аудиодорожки.
AUDIO_OUTPUT_MODES = frozenset({MODE_TO_VOICE, MODE_EXTRACT_AUDIO})

COMMAND_TO_MODE = {m.command: mid for mid, m in MODES.items()}


# --- МЕНЮ ПО КАТЕГОРИЯМ ---
# Категория → (подпись, список режимов)
CATEGORIES = {
    "video": ("🎬 Видео", [MODE_VIDEO_NOTE, MODE_TO_GIF]),
    "audio": ("🎵 Аудио", [MODE_TO_VOICE, MODE_EXTRACT_AUDIO]),
}

# Подписи кнопок режимов в меню
MODE_BUTTON = {
    MODE_VIDEO_NOTE:    "⭕ Кружок",
    MODE_TO_GIF:        "🖼️ GIF",
    MODE_TO_VOICE:      "🎙️ В голосовое",
    MODE_EXTRACT_AUDIO: "🎶 Извлечь аудио",
}

CROP_LABELS = {"top": "сверху", "center": "по центру", "bottom": "снизу"}

# callback_data: навигация и действия
CB_ROOT, CB_VIDEO, CB_AUDIO, CB_GIF_ALL = "nav:root", "nav:video", "nav:audio", "gif:all"
# режимы — "set:<command>", области кадра — "crop:<pos>"


def kb_root() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(CATEGORIES["video"][0], callback_data=CB_VIDEO),
        InlineKeyboardButton(CATEGORIES["audio"][0], callback_data=CB_AUDIO),
    ]])


def kb_category(cat: str) -> InlineKeyboardMarkup:
    modes = CATEGORIES[cat][1]
    row = [InlineKeyboardButton(MODE_BUTTON[m], callback_data=f"set:{MODES[m].command}") for m in modes]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("⬅️ Назад", callback_data=CB_ROOT)]])


def kb_crop() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️ Сверху", callback_data="crop:top"),
         InlineKeyboardButton("⏺️ Центр",  callback_data="crop:center"),
         InlineKeyboardButton("⬇️ Снизу",  callback_data="crop:bottom")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=CB_VIDEO)],
    ])


def kb_gif() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Всё",   callback_data=CB_GIF_ALL),
        InlineKeyboardButton("⬅️ Назад", callback_data=CB_VIDEO),
    ]])


REPLY_MENU = ReplyKeyboardMarkup([[MENU_BUTTON]], resize_keyboard=True)


# --- FFMPEG: ленивый семафор ---
_ffmpeg_sem: Optional[asyncio.Semaphore] = None


def ffmpeg_semaphore() -> asyncio.Semaphore:
    global _ffmpeg_sem
    if _ffmpeg_sem is None:
        _ffmpeg_sem = asyncio.Semaphore(MAX_CONCURRENT_FFMPEG)
    return _ffmpeg_sem


# --- УТИЛИТЫ ---

def guess_extension(file_ref, file_type: str) -> str:
    """Подбирает расширение для скачанного файла (ffmpeg определяет формат по содержимому)."""
    if file_type in ("video", "video_note", "animation"):
        return ".mp4"
    if file_type == "voice":
        return ".ogg"
    name = getattr(file_ref, "file_name", None) or ""
    mime = (getattr(file_ref, "mime_type", None) or "").lower()
    ext = os.path.splitext(name)[1].lower()
    if file_type == "audio":
        return ext or ".mp3"
    if ext:                                        # audio_doc / document с именем
        return ext
    for needle, e in (("wav", ".wav"), ("mp3", ".mp3"), ("mpeg", ".mp3"),
                      ("ogg", ".ogg"), ("video", ".mp4")):
        if needle in mime:
            return e
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


def parse_time(s: str) -> Optional[float]:
    """'SS' / 'M:SS' / 'MM:SS' / 'H:MM:SS' → секунды (float) или None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        nums = [float(p) for p in s.split(":")]
    except ValueError:
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def parse_interval(text: str):
    """'0:05-0:25' / '5-25' / '0:05 0:25' → (start, end) в секундах или None."""
    t = (text or "").strip().replace("–", "-").replace("—", "-")
    parts = [p for p in re.split(r"\s*-\s*|\s+", t) if p]
    if len(parts) != 2:
        return None
    a, b = parse_time(parts[0]), parse_time(parts[1])
    if a is None or b is None or a < 0 or b <= a:
        return None
    return a, b


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
            val = parse_time(line.split("=", 1)[1].strip())
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
        line = f"{base}…\n{progress_bar(pct)} {pct}%"
        if ratio > 0.03:                            # ETA — только когда есть осмысленный сигнал
            elapsed = time.monotonic() - start
            line += f" · осталось {format_eta(elapsed * (1 - ratio) / ratio)}"
        if line != last_text:
            last_text = line
            try:
                await status_msg.edit_text(line)
            except Exception:
                pass                                 # "not modified", флуд-лимит и т.п. — игнорируем


# --- FFMPEG: построение конвертаций ---

# Вертикальное смещение кропа для кружка (x всегда по центру).
CROP_Y = {"top": "0", "center": "(in_h-out_h)/2", "bottom": "in_h-out_h"}


def build_video_note(src: str, dst: str, crop: str = "center"):
    probe = ffmpeg.probe(src)
    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not video:
        raise ValueError("Видеопоток не найден.")

    side = min(int(video.get("width", VIDEO_NOTE_SIZE)), int(video.get("height", VIDEO_NOTE_SIZE)))
    y = CROP_Y.get(crop, CROP_Y["center"])
    inp = ffmpeg.input(src, t=LIMIT_VIDEO_NOTE)
    v = (inp["v:0"]
         .filter("crop", side, side, "(in_w-out_w)/2", y)        # квадрат с выбранной областью
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


def build_to_gif(src: str, dst: str, palette: str, start: float = 0.0, duration: float = LIMIT_GIF):
    scale_w = "if(gte(iw,ih),320,-2)"
    scale_h = "if(gte(iw,ih),-2,320)"
    base = (
        ffmpeg.input(src, ss=start, t=duration)        # вырезаем выбранный отрезок
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
        "allow_sending_without_reply": True,           # если исходное сообщение удалили — всё равно отправим
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


# --- КОНВЕРТАЦИЯ ---

async def run_conversion(context, chat_id, reply_to, file_ref, file_type, mode,
                         status=None, crop="center", gif_start=0.0, gif_duration=None):
    cfg = MODES[mode]
    gif_duration = gif_duration or float(cfg.limit)
    work_dir = tempfile.mkdtemp(prefix="tgconv_")          # уникальная папка → безопасно при параллельных задачах
    src      = os.path.join(work_dir, f"in{guess_extension(file_ref, file_type)}")
    dst      = os.path.join(work_dir, f"out{cfg.output_ext}")
    palette  = os.path.join(work_dir, "palette.png")
    prog     = os.path.join(work_dir, "progress.txt")

    # status может быть уже создан (плашка «в очереди») — тогда переиспользуем его.
    if status is None:
        status = await context.bot.send_message(chat_id, f"{cfg.status}…")
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

        # Если режим даёт аудио, а звуковой дорожки нет — понятное сообщение вместо ошибки формата.
        if mode in AUDIO_OUTPUT_MODES and not has_audio_stream(src):
            await status.edit_text("❌ В этом файле нет звука — обрабатывать нечего.")
            return

        real = probe_duration(src)

        # Сколько секунд реально обрабатываем — для процентов.
        if mode == MODE_TO_GIF:
            if real and gif_start >= real:
                await status.edit_text("❌ Указанное время за пределами длины видео.")
                return
            total = gif_duration if not real else min(gif_duration, max(0.1, real - gif_start))
        else:
            total = min(real, cfg.limit) if real else float(cfg.limit)

        open(prog, "w").close()                            # progress-файл должен существовать

        def process():
            g = ("-progress", prog, "-nostats")
            if mode == MODE_VIDEO_NOTE:
                build_video_note(src, dst, crop).global_args(*g).run(overwrite_output=True, quiet=True)
            elif mode == MODE_TO_VOICE:
                build_to_voice(src, dst).global_args(*g).run(overwrite_output=True, quiet=True)
            elif mode == MODE_EXTRACT_AUDIO:
                build_extract_audio(src, dst).global_args(*g).run(overwrite_output=True, quiet=True)
            else:
                p1, p2 = build_to_gif(src, dst, palette, gif_start, gif_duration)
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

        await send_result(context.bot, chat_id, mode, dst, cfg.output_ext, reply_to)

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


async def process_now(context, chat_id, reply_to, file_ref, file_type, mode,
                      crop="center", gif_start=0.0, gif_duration=None):
    """Очередь на пользователя + плашка «в очереди» + запуск конвертации."""
    lock = context.user_data.get(LOCK_KEY)
    if lock is None:
        lock = asyncio.Lock()
        context.user_data[LOCK_KEY] = lock

    status = None
    if lock.locked():
        status = await context.bot.send_message(chat_id, "🕓 В очереди…")

    async with lock:
        await run_conversion(context, chat_id, reply_to, file_ref, file_type, mode,
                             status=status, crop=crop, gif_start=gif_start, gif_duration=gif_duration)


# --- ХЭНДЛЕРЫ ---

GROUP_REPLY_NOTE = (
    "\n\n📎 В группе пришлите файл ответом (reply) на это сообщение бота — "
    "иначе из-за настроек приватности бот его не получит."
)


def with_group_note(text: str, chat) -> str:
    """В группе добавляет примечание: файл нужно слать ответом на сообщение бота."""
    if chat.type in ("group", "supergroup"):
        return text + GROUP_REPLY_NOTE
    return text


def set_mode(context, mode: int, crop: Optional[str] = None) -> None:
    """Устанавливает режим, сбрасывает ожидание отрезка GIF, при необходимости — область кадра."""
    context.user_data[STATE_KEY] = mode
    context.user_data[PENDING_KEY] = None
    if crop:
        context.user_data[CROP_KEY] = crop


def confirm_text(mode: int, crop: Optional[str] = None) -> str:
    cfg = MODES[mode]
    if mode == MODE_VIDEO_NOTE and crop:
        head = f"✅ Режим: кружок (кадр {CROP_LABELS.get(crop, 'по центру')})."
    else:
        head = f"✅ Режим: {cfg.title}."
    return f"{head}\n{cfg.prompt}"


async def send_root_menu(message) -> None:
    await message.reply_text("Что делаем?", reply_markup=kb_root())


async def prompt_send_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Реакция на текст/стикер: просим файл (если режим выбран) или показываем меню."""
    if context.user_data.get(STATE_KEY) is not None:
        await update.message.reply_text("📎 Отправьте медиафайл — текст и стикеры я не конвертирую.")
    else:
        await update.message.reply_text("⚠️ Сначала выберите режим:")
        await send_root_menu(update.message)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Привет! Я делаю кружки и голосовые, извлекаю аудио и собираю GIF.\n"
        "Нажмите «☰ Меню» внизу или выберите ниже.",
        reply_markup=REPLY_MENU,
    )
    await send_root_menu(update.message)


def make_mode_command(mode: int):
    """Команды (/videonote и т.д.) — быстрый доступ к режиму в обход меню."""
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        set_mode(context, mode)
        chat = update.effective_chat
        if mode == MODE_VIDEO_NOTE:
            crop = context.user_data.get(CROP_KEY, "center")
            await update.message.reply_text(with_group_note(confirm_text(mode, crop), chat))
        elif mode == MODE_TO_GIF:
            await update.message.reply_text(with_group_note(confirm_text(mode), chat), reply_markup=kb_gif())
        else:
            await update.message.reply_text(with_group_note(confirm_text(mode), chat))
    return handler


async def gif_all(query, context) -> None:
    """Кнопка «Всё»: делает GIF из первых 30 секунд присланного видео."""
    pending = context.user_data.get(PENDING_KEY)
    if not pending:
        await query.answer("Сначала пришлите видео для GIF.", show_alert=True)
        return
    await query.answer()
    context.user_data[PENDING_KEY] = None
    await process_now(context, query.message.chat_id, pending["reply_to"],
                      pending["file_ref"], pending["file_type"], MODE_TO_GIF,
                      gif_start=0.0, gif_duration=float(LIMIT_GIF))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    chat = query.message.chat
    try:
        if data == CB_ROOT:
            await query.edit_message_text("Что делаем?", reply_markup=kb_root())
        elif data == CB_VIDEO:
            await query.edit_message_text(CATEGORIES["video"][0], reply_markup=kb_category("video"))
        elif data == CB_AUDIO:
            await query.edit_message_text(CATEGORIES["audio"][0], reply_markup=kb_category("audio"))
        elif data == "set:videonote":
            await query.edit_message_text("⭕ Кружок — какую часть кадра брать?", reply_markup=kb_crop())
        elif data.startswith("crop:"):
            crop = data.split(":", 1)[1]
            set_mode(context, MODE_VIDEO_NOTE, crop=crop)
            await query.edit_message_text(with_group_note(confirm_text(MODE_VIDEO_NOTE, crop), chat))
        elif data == "set:togif":
            set_mode(context, MODE_TO_GIF)
            await query.edit_message_text(with_group_note(confirm_text(MODE_TO_GIF), chat), reply_markup=kb_gif())
        elif data.startswith("set:"):
            mode = COMMAND_TO_MODE.get(data.split(":", 1)[1])
            if mode is not None:
                set_mode(context, mode)
                await query.edit_message_text(with_group_note(confirm_text(mode), chat))
        elif data == CB_GIF_ALL:
            await gif_all(query, context)
            return                                       # gif_all сам отвечает на query
        await query.answer()
    except BadRequest:
        # "message is not modified" и подобное — не страшно
        try:
            await query.answer()
        except Exception:
            pass
    except Exception as e:
        logger.error("Callback error: %s", e, exc_info=True)
        try:
            await query.answer()
        except Exception:
            pass


async def _start_gif(message, context, file_ref, file_type, seg) -> None:
    """Запускает GIF по готовому отрезку seg=(start,end) с проверкой лимита длины."""
    start, end = seg
    if end - start > LIMIT_GIF:
        await message.reply_text(f"❌ Максимум {LIMIT_GIF} секунд для GIF. Укажите отрезок покороче.")
        return
    await process_now(context, message.chat_id, message.message_id, file_ref, file_type,
                      MODE_TO_GIF, gif_start=start, gif_duration=end - start)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    if text == MENU_BUTTON:
        await send_root_menu(update.message)
        return

    # Ждём ввод отрезка для GIF?
    pending = context.user_data.get(PENDING_KEY)
    if pending:
        if text.lower() in ("всё", "все", "all"):
            context.user_data[PENDING_KEY] = None
            await process_now(context, update.message.chat_id, pending["reply_to"],
                              pending["file_ref"], pending["file_type"], MODE_TO_GIF,
                              gif_start=0.0, gif_duration=float(LIMIT_GIF))
            return
        seg = parse_interval(text)
        if seg:
            context.user_data[PENDING_KEY] = None
            await _start_gif(update.message, context, pending["file_ref"], pending["file_type"], seg)
            return
        await update.message.reply_text(
            f"Не понял время. Формат 0:05-0:25 (максимум {LIMIT_GIF} секунд) или нажмите «Всё»."
        )
        return

    # Обычный текст — подсказка.
    await prompt_send_file(update, context)


async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get(PENDING_KEY):
        await update.message.reply_text(
            f"✂️ Жду время для GIF: формат 0:05-0:25 (максимум {LIMIT_GIF} секунд) или нажмите «Всё»."
        )
        return
    await prompt_send_file(update, context)


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = message.chat
    file_ref, file_type = pick_file(message)
    if not file_ref:
        return

    mode = context.user_data.get(STATE_KEY)
    if mode is None:
        if chat.type == "private":
            await message.reply_text("⚠️ Сначала выберите режим:")
            await send_root_menu(message)
        return

    if file_type == MODES[mode].same_type:
        await message.reply_text(MODES[mode].same_type_error)
        return

    if file_type not in MODES[mode].valid_types:
        await message.reply_text(MODES[mode].error)
        return

    size = getattr(file_ref, "file_size", None) or 0
    if size > MAX_DOWNLOAD_SIZE:
        await message.reply_text("❌ Файл слишком большой — Telegram позволяет боту скачивать до 20 МБ.")
        return

    # GIF: нужен отрезок. Если он задан в подписи — конвертируем сразу;
    # в личке без подписи — спрашиваем; в группе диалог не работает, берём первые 30 сек.
    if mode == MODE_TO_GIF:
        seg = parse_interval(message.caption or "")
        if seg:
            await _start_gif(message, context, file_ref, file_type, seg)
        elif chat.type != "private":
            await process_now(context, chat.id, message.message_id, file_ref, file_type,
                              MODE_TO_GIF, gif_start=0.0, gif_duration=float(LIMIT_GIF))
        else:
            context.user_data[PENDING_KEY] = {
                "file_ref": file_ref, "file_type": file_type, "reply_to": message.message_id,
            }
            await message.reply_text(
                "✂️ На какой отрезок делать GIF?\n"
                f"Пришлите время в формате 0:05-0:25 (максимум {LIMIT_GIF} секунд) "
                "или нажмите «Всё» в меню — возьму первые 30 секунд."
            )
        return

    # Остальные режимы — конвертируем сразу (липкий режим: можно слать подряд).
    crop = context.user_data.get(CROP_KEY, "center")
    await process_now(context, chat.id, message.message_id, file_ref, file_type, mode, crop=crop)


async def on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Неизвестная команда. Откройте «☰ Меню».")


# --- ЗАПУСК ---

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан (через .env или переменную окружения).")

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", cmd_start))
    for mode_id, cfg in MODES.items():
        app.add_handler(CommandHandler(cfg.command, make_mode_command(mode_id)))

    app.add_handler(CallbackQueryHandler(on_callback))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        on_text,
    ))

    # Стикеры (как и текст) — не файлы: просим прислать файл / выбрать режим.
    app.add_handler(MessageHandler(
        filters.Sticker.ALL & filters.ChatType.PRIVATE,
        on_sticker,
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

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
