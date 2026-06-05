"""
Telegram-бот для конвертации медиа:
  • видео/GIF   → видео-кружок (с выбором области кадра: сверху/центр/снизу/слева/справа)
  • аудио/видео → голосовое сообщение (ogg/opus)
  • видео/кружок → извлечение аудио (mp3)
  • видео/кружок → GIF (с выбором временного отрезка, по одному файлу за раз)

Версия 2.2.0 — кнопка «Отмена» в каждом меню (убивает текущую конвертацию), обязательный
выбор части кадра для кружка, остановка всей работы при блокировке бота пользователем,
повышенная устойчивость к «битым»/необычным потокам и понятное логирование ошибок ffmpeg.
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
    ReplyParameters,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest, Forbidden

try:
    from dotenv import load_dotenv          # опционально: загрузка токена из .env
except ModuleNotFoundError:
    load_dotenv = None

# --- НАСТРОЙКИ ---
if load_dotenv is not None:
    load_dotenv(Path(__file__).with_name(".env"))

BOT_TOKEN = os.environ.get("BOT_TOKEN")

MAX_DOWNLOAD_SIZE     = 20 * 1024 * 1024     # лимит Telegram на скачивание ботом (~20 МБ)
MAX_CONCURRENT_FFMPEG = 2                    # сколько ffmpeg-задач одновременно (на всех); запас по памяти на 1 ГБ
PROGRESS_INTERVAL     = 2.0                  # как часто обновлять прогресс (сек)
CONVERSION_TIMEOUT    = 240                  # потолок на одну операцию ffmpeg (сек) — защита от зависаний

# Ограничения длительности на входе (сек)
LIMIT_VIDEO_NOTE = 60
LIMIT_AUDIO      = 300
LIMIT_GIF        = 30

VIDEO_NOTE_SIZE  = 512                       # сторона кружка (px)
VIDEO_NOTE_FPS   = 30                        # фикс. частота кадров кружка (60 fps с айфона вдвое тяжелее)

# Ключи в user_data
STATE_KEY   = "mode"                         # текущий режим
CROP_KEY    = "crop"                         # область кадра кружка (None — ещё не выбрана)
PENDING_KEY = "pending_gif"                  # видео, ожидающее ввода отрезка для GIF
GROUP_KEY   = "gif_group"                    # media_group_id, про который уже сказали «один за раз»
MENU_KEY    = "menu"                         # текущий уровень меню (для кнопки «Назад»)
LOCK_KEY    = "lock"                         # пер-юзер очередь обработки
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
    title: str
    prompt: str
    status: str
    output_ext: str
    limit: int
    valid_types: frozenset = field(default_factory=frozenset)
    error: str = "❌ Неподходящий файл."
    same_types: frozenset = field(default_factory=frozenset)   # типы, которые уже являются результатом
    same_type_error: str = ""


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
        same_types=frozenset({"video_note"}),
        same_type_error="🔁 Это уже кружок — конвертировать не нужно.",
    ),
    MODE_TO_VOICE: Mode(
        command="tovoice",
        title="голосовое",
        prompt="🎵 Пришлите аудио или видео — можно несколько подряд.",
        status="🎵 Конвертирую в голосовое",
        output_ext=".ogg",
        limit=LIMIT_AUDIO,
        valid_types=frozenset({"audio", "video", "video_note", "audio_doc"}),
        error="❌ Для голосового нужно аудио или видео — отправьте подходящий файл.",
        same_types=frozenset({"voice"}),
        same_type_error="🔁 Это уже голосовое сообщение.",
    ),
    MODE_EXTRACT_AUDIO: Mode(
        command="extractaudio",
        title="извлечение аудио",
        prompt="🎶 Пришлите видео или кружок — можно несколько подряд.",
        status="🎶 Извлекаю аудио",
        output_ext=".mp3",
        limit=LIMIT_AUDIO,
        valid_types=frozenset({"video", "video_note"}),
        error="❌ Для извлечения аудио нужно видео или кружок — отправьте подходящий файл.",
        same_types=frozenset({"audio", "voice", "audio_doc"}),
        same_type_error="🔁 Это уже аудио — извлекать не нужно.",
    ),
    MODE_TO_GIF: Mode(
        command="togif",
        title="GIF",
        prompt="🖼️ Пришлите видео или кружок (за раз — один файл). Потом укажете отрезок — "
               "например 0:05-0:25 или 5-25 (в секундах) — либо нажмёте «Всё».",
        status="🖼️ Создаю GIF",
        output_ext=".gif",
        limit=LIMIT_GIF,
        valid_types=frozenset({"video", "video_note"}),
        error="❌ Для GIF нужно видео или кружок — отправьте подходящий файл.",
        same_types=frozenset({"animation"}),
        same_type_error="🔁 Это уже GIF.",
    ),
}

AUDIO_OUTPUT_MODES = frozenset({MODE_TO_VOICE, MODE_EXTRACT_AUDIO})
COMMAND_TO_MODE = {m.command: mid for mid, m in MODES.items()}

CROP_LABELS = {"top": "сверху", "center": "по центру", "bottom": "снизу",
               "left": "слева", "right": "справа"}


# --- НИЖНЕЕ МЕНЮ (reply-клавиатуры) ---
BTN_VIDEO, BTN_AUDIO        = "🎬 Видео", "🎵 Аудио"
BTN_CIRCLE, BTN_GIF         = "⭕ Кружок", "🖼️ GIF"
BTN_VOICE, BTN_EXTRACT      = "🎙️ В голосовое", "🎶 Извлечь аудио"
BTN_TOP, BTN_CENTER, BTN_BOTTOM = "⬆️ Сверху", "⏺️ Центр", "⬇️ Снизу"
BTN_LEFT, BTN_RIGHT         = "⬅️ Слева", "➡️ Справа"
BTN_ALL, BTN_BACK           = "✅ Всё", "🔙 Назад"
BTN_CANCEL                  = "❌ Отмена"

CROP_BUTTONS = {BTN_TOP: "top", BTN_CENTER: "center", BTN_BOTTOM: "bottom",
                BTN_LEFT: "left", BTN_RIGHT: "right"}

KB_ROOT  = ReplyKeyboardMarkup([[BTN_VIDEO, BTN_AUDIO], [BTN_CANCEL]], resize_keyboard=True)
KB_VIDEO = ReplyKeyboardMarkup([[BTN_CIRCLE, BTN_GIF], [BTN_BACK, BTN_CANCEL]], resize_keyboard=True)
KB_AUDIO = ReplyKeyboardMarkup([[BTN_VOICE, BTN_EXTRACT], [BTN_BACK, BTN_CANCEL]], resize_keyboard=True)
KB_CROP  = ReplyKeyboardMarkup(
    [[BTN_TOP, BTN_CENTER, BTN_BOTTOM], [BTN_LEFT, BTN_RIGHT], [BTN_BACK, BTN_CANCEL]], resize_keyboard=True)
KB_GIF   = ReplyKeyboardMarkup([[BTN_ALL], [BTN_BACK, BTN_CANCEL]], resize_keyboard=True)

NAV_BUTTONS = {BTN_VIDEO, BTN_AUDIO, BTN_CIRCLE, BTN_GIF, BTN_VOICE, BTN_EXTRACT,
               BTN_TOP, BTN_CENTER, BTN_BOTTOM, BTN_LEFT, BTN_RIGHT, BTN_BACK}

# Куда ведёт «Назад» с каждого уровня
BACK_TARGET = {"video": "root", "audio": "root", "crop": "video", "gif": "video"}
LEVEL_KB = {"root": KB_ROOT, "video": KB_VIDEO, "audio": KB_AUDIO, "crop": KB_CROP, "gif": KB_GIF}


# --- FFMPEG: ленивый семафор ---
_ffmpeg_sem: Optional[asyncio.Semaphore] = None


def ffmpeg_semaphore() -> asyncio.Semaphore:
    global _ffmpeg_sem
    if _ffmpeg_sem is None:
        _ffmpeg_sem = asyncio.Semaphore(MAX_CONCURRENT_FFMPEG)
    return _ffmpeg_sem


# --- УТИЛИТЫ ---

def guess_extension(file_ref, file_type: str) -> str:
    """Расширение для скачанного файла (ffmpeg определяет формат по содержимому)."""
    if file_type in ("video", "video_note", "animation"):
        return ".mp4"
    if file_type == "voice":
        return ".ogg"
    name = getattr(file_ref, "file_name", None) or ""
    mime = (getattr(file_ref, "mime_type", None) or "").lower()
    ext = os.path.splitext(name)[1].lower()
    if file_type == "audio":
        return ext or ".mp3"
    if ext:
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
    if message.animation:  return message.animation,  "animation"
    if message.audio:      return message.audio,      "audio"
    if message.voice:      return message.voice,      "voice"
    if message.photo:      return message.photo[-1],  "photo"
    if message.document:
        mime = (message.document.mime_type or "").lower()
        ext  = os.path.splitext(message.document.file_name or "")[1].lower()
        if mime == "image/gif" or ext == ".gif":
            return message.document, "animation"                      # GIF файлом → как анимация
        if mime.startswith("audio/") or ext in AUDIO_EXTS:
            return message.document, "audio_doc"
        if mime.startswith("video/") or ext in VIDEO_EXTS:
            return message.document, "video"
        return message.document, "document"
    return None, None


def probe_duration(src: str) -> Optional[float]:
    try:
        info = ffmpeg.probe(src)
        dur = info.get("format", {}).get("duration")
        return float(dur) if dur else None
    except Exception:
        return None


def has_audio_stream(src: str) -> bool:
    """True, если в файле есть звуковая дорожка."""
    try:
        info = ffmpeg.probe(src)
        return any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    except Exception:
        return True   # не смогли проверить — не блокируем


def parse_time(s: str) -> Optional[float]:
    """'SS' / 'M:SS' / 'MM:SS' / 'H:MM:SS' → секунды или None."""
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
    """'0:05-0:25' / '5-25' / '0:05 0:25' → (start, end) или None."""
    t = (text or "").strip().replace("–", "-").replace("—", "-")
    parts = [p for p in re.split(r"\s*-\s*|\s+", t) if p]
    if len(parts) != 2:
        return None
    a, b = parse_time(parts[0]), parse_time(parts[1])
    if a is None or b is None or a < 0 or b <= a:
        return None
    return a, b


def read_progress_ratio(path: str, total: float) -> Optional[float]:
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
    last_text = None
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=PROGRESS_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass
        ratio = read_progress_ratio(path, total)
        if ratio is None:
            continue
        pct = max(0, min(99, int(ratio * 100)))
        line = f"{base}…\n{progress_bar(pct)} {pct}%"
        if ratio > 0.03:
            elapsed = time.monotonic() - start
            line += f" · осталось {format_eta(elapsed * (1 - ratio) / ratio)}"
        if line != last_text:
            last_text = line
            try:
                await status_msg.edit_text(line)
            except Exception:
                pass


def _tail(stderr_bytes, n: int = 6) -> str:
    """Последние значимые строки stderr ffmpeg — там настоящая причина ошибки."""
    text = (stderr_bytes or b"").decode(errors="ignore")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines[-n:]) if lines else "(пусто)"


# --- FFMPEG: построение конвертаций ---

def robust_input(src: str, **kwargs):
    """Вход с повышенной устойчивостью к битым/необычным потокам (айфон HEVC/HDR, пересланные файлы)."""
    return ffmpeg.input(src, err_detect="ignore_err",
                        analyzeduration="100M", probesize="100M", **kwargs)


# Квадратный кроп min(iw,ih); x — по горизонтали, y — по вертикали.
CROP_POS = {
    "top":    ("(iw-min(iw,ih))/2", "0"),
    "center": ("(iw-min(iw,ih))/2", "(ih-min(iw,ih))/2"),
    "bottom": ("(iw-min(iw,ih))/2", "ih-min(iw,ih)"),
    "left":   ("0",                 "(ih-min(iw,ih))/2"),
    "right":  ("iw-min(iw,ih)",     "(ih-min(iw,ih))/2"),
}


def build_video_note(src: str, dst: str, crop: str = "center"):
    has_audio = has_audio_stream(src)
    x, y = CROP_POS.get(crop, CROP_POS["center"])
    inp = robust_input(src, t=LIMIT_VIDEO_NOTE)
    v = (inp["v:0"]
         .filter("crop", "min(iw,ih)", "min(iw,ih)", x, y)          # квадрат с выбранной областью
         .filter("scale", VIDEO_NOTE_SIZE, VIDEO_NOTE_SIZE, flags="lanczos"))
    common = {
        "vcodec": "libx264",
        "pix_fmt": "yuv420p",          # 8-бит → совместимость + устойчивость к 10-бит/HDR
        "crf": 23,                     # качество по CRF (меньше — лучше)
        "preset": "veryfast",
        "r": VIDEO_NOTE_FPS,           # фикс. fps → легче и стабильнее
        "movflags": "+faststart",
        "format": "mp4",
        "map_metadata": -1,            # выкидываем метаданные (в т.ч. Dolby Vision)
    }
    if has_audio:
        return ffmpeg.output(v, inp["a:0"], dst, acodec="aac",
                             **{"b:a": "128k"}, **common)
    return ffmpeg.output(v, dst, an=None, **common)


def build_to_voice(src: str, dst: str):
    inp = robust_input(src, t=LIMIT_AUDIO)
    return ffmpeg.output(inp["a:0"], dst, acodec="libopus", format="ogg", map_metadata=-1)


def build_extract_audio(src: str, dst: str):
    inp = robust_input(src, t=LIMIT_AUDIO)
    return ffmpeg.output(inp["a:0"], dst, acodec="libmp3lame", format="mp3",
                         map_metadata=-1, **{"b:a": "192k"})


def build_to_gif(src: str, dst: str, palette: str, start: float = 0.0, duration: float = LIMIT_GIF):
    scale_w = "if(gte(iw,ih),320,-2)"
    scale_h = "if(gte(iw,ih),-2,320)"
    base = (robust_input(src, ss=start, t=duration)
            .filter("fps", fps=15)
            .filter("scale", scale_w, scale_h))
    p1 = base.filter("palettegen").output(palette, format="image2")
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
        raise ffmpeg.Error("ffmpeg", b"", stderr or b"")
    return stderr


# --- ОТПРАВКА РЕЗУЛЬТАТА ---

def _reply_params(msg_id):
    return ReplyParameters(message_id=msg_id, allow_sending_without_reply=True)


async def send_result(bot, chat_id, mode, path, ext, reply_to):
    kwargs = {
        "reply_parameters": _reply_params(reply_to),
        "read_timeout": 120, "write_timeout": 120, "connect_timeout": 180,
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

async def run_conversion(context, chat_id, reply_to, file_ref, file_type, mode,
                         status=None, crop="center", gif_start=0.0, gif_duration=None):
    cfg = MODES[mode]
    gif_duration = gif_duration or float(cfg.limit)
    work_dir = tempfile.mkdtemp(prefix="tgconv_")
    src      = os.path.join(work_dir, f"in{guess_extension(file_ref, file_type)}")
    dst      = os.path.join(work_dir, f"out{cfg.output_ext}")
    palette  = os.path.join(work_dir, "palette.png")
    prog     = os.path.join(work_dir, "progress.txt")

    try:
        if status is None:
            status = await context.bot.send_message(
                chat_id, f"{cfg.status}…", reply_parameters=_reply_params(reply_to))
        else:
            await _safe_edit(status, f"{cfg.status}…")

        tg_file = await file_ref.get_file()
        await tg_file.download_to_drive(
            src, read_timeout=180, write_timeout=180, connect_timeout=180)

        if mode in AUDIO_OUTPUT_MODES and not has_audio_stream(src):
            await status.edit_text("❌ В этом файле нет звука — обрабатывать нечего.")
            return

        real = probe_duration(src)
        if mode == MODE_TO_GIF:
            if real and gif_start >= real:
                await status.edit_text("❌ Указанное время за пределами длины видео.")
                return
            total = gif_duration if not real else min(gif_duration, max(0.1, real - gif_start))
        else:
            total = min(real, cfg.limit) if real else float(cfg.limit)

        open(prog, "w").close()

        async with ffmpeg_semaphore():
            stop = asyncio.Event()
            start = time.monotonic()
            updater = asyncio.create_task(progress_loop(status, cfg.status, prog, total, stop, start))
            try:
                if mode == MODE_VIDEO_NOTE:
                    await run_ffmpeg(build_video_note(src, dst, crop), prog)
                elif mode == MODE_TO_VOICE:
                    await run_ffmpeg(build_to_voice(src, dst), prog)
                elif mode == MODE_EXTRACT_AUDIO:
                    await run_ffmpeg(build_extract_audio(src, dst), prog)
                else:
                    p1, p2 = build_to_gif(src, dst, palette, gif_start, gif_duration)
                    await run_ffmpeg(p1)              # палитра — быстро, без прогресса
                    await run_ffmpeg(p2, prog)
            finally:
                stop.set()
                await updater

        await send_result(context.bot, chat_id, mode, dst, cfg.output_ext, reply_to)
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
        logger.error("ffmpeg error: %s", _tail(e.stderr))
        await _safe_edit(status, "❌ Ошибка обработки файла. Проверьте формат.")
    except Exception as e:
        logger.error("Conversion error: %s", e, exc_info=True)
        await _safe_edit(status, "❌ Произошла ошибка.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def process_now(context, chat_id, reply_to, file_ref, file_type, mode,
                      crop="center", gif_start=0.0, gif_duration=None):
    """Очередь на пользователя + плашка «в очереди» + запуск конвертации + учёт задачи для отмены."""
    ud = context.user_data
    lock = ud.get(LOCK_KEY)
    if lock is None:
        lock = asyncio.Lock()
        ud[LOCK_KEY] = lock

    tasks = ud.setdefault(TASKS_KEY, set())
    task = asyncio.current_task()
    tasks.add(task)
    try:
        status = None
        if lock.locked():
            try:
                status = await context.bot.send_message(
                    chat_id, "🕓 В очереди…", reply_parameters=_reply_params(reply_to))
            except Forbidden:
                ud[BLOCKED_KEY] = True
                return

        async with lock:
            if ud.get(BLOCKED_KEY):
                return                                   # пользователь заблокировал — не тратим ресурсы
            await run_conversion(context, chat_id, reply_to, file_ref, file_type, mode,
                                 status=status, crop=crop, gif_start=gif_start, gif_duration=gif_duration)
    finally:
        tasks.discard(task)


# --- МЕНЮ И ВСПОМОГАТЕЛЬНОЕ ---

GROUP_REPLY_NOTE = (
    "\n\n📎 В группе пришлите файл ответом (reply) на это сообщение бота — "
    "иначе из-за настроек приватности бот его не получит."
)


def with_group_note(text: str, chat) -> str:
    if chat.type in ("group", "supergroup"):
        return text + GROUP_REPLY_NOTE
    return text


def set_mode(context, mode: int, crop: Optional[str] = None) -> None:
    context.user_data[STATE_KEY] = mode
    context.user_data[PENDING_KEY] = None
    if crop:
        context.user_data[CROP_KEY] = crop


def clear_blocked(context) -> None:
    """Раз пришло сообщение — пользователь нас не блокирует (Telegram бы его не доставил)."""
    context.user_data.pop(BLOCKED_KEY, None)


def confirm_text(mode: int, crop: Optional[str] = None) -> str:
    cfg = MODES[mode]
    if mode == MODE_VIDEO_NOTE and crop:
        head = f"✅ Режим: кружок (кадр {CROP_LABELS.get(crop, 'по центру')})."
    else:
        head = f"✅ Режим: {cfg.title}."
    return f"{head}\n{cfg.prompt}"


def circle_entry_text(context) -> str:
    crop = context.user_data.get(CROP_KEY)
    if crop:
        return (f"⭕ Кружок (сейчас кадр {CROP_LABELS[crop]}). Присылайте видео "
                "или выберите другую часть кадра ниже.")
    return "⭕ Кружок. Сначала выберите часть кадра ниже."


async def prompt_send_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Реакция на текст/стикер: просим файл (если режим выбран) или показываем меню."""
    if context.user_data.get(STATE_KEY) is not None:
        await update.message.reply_text("📎 Отправьте медиафайл — текст и стикеры я не конвертирую.")
    else:
        context.user_data[MENU_KEY] = "root"
        await update.message.reply_text("⚠️ Сначала выберите режим в меню ниже.", reply_markup=KB_ROOT)


# --- ХЭНДЛЕРЫ ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_blocked(context)
    context.user_data[MENU_KEY] = "root"
    await update.message.reply_text(
        "👋 Привет! Я делаю кружки и голосовые, извлекаю аудио и собираю GIF.\n"
        "Выберите, что нужно, в меню ниже.",
        reply_markup=KB_ROOT,
    )


def make_mode_command(mode: int):
    """Команды (/videonote и т.д.) — быстрый доступ к режиму."""
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        clear_blocked(context)
        chat = update.effective_chat
        if mode == MODE_VIDEO_NOTE:
            context.user_data[STATE_KEY] = MODE_VIDEO_NOTE          # часть кадра не задаём — выберет сам
            context.user_data[PENDING_KEY] = None
            context.user_data[MENU_KEY] = "crop"
            await update.message.reply_text(with_group_note(circle_entry_text(context), chat), reply_markup=KB_CROP)
        elif mode == MODE_TO_GIF:
            set_mode(context, mode)
            context.user_data[MENU_KEY] = "gif"
            await update.message.reply_text(with_group_note(confirm_text(mode), chat), reply_markup=KB_GIF)
        else:
            set_mode(context, mode)
            context.user_data[MENU_KEY] = "audio"
            await update.message.reply_text(with_group_note(confirm_text(mode), chat), reply_markup=KB_AUDIO)
    return handler


async def cancel_conversion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Отмена»: убиваем активные конвертации пользователя (или сообщаем, что нечего отменять)."""
    tasks = context.user_data.get(TASKS_KEY) or set()
    active = [t for t in list(tasks) if not t.done()]
    if not active:
        await update.message.reply_text("Сейчас нечего отменять — конвертация не идёт.")
        return
    for t in active:
        t.cancel()
    await update.message.reply_text("❌ Останавливаю конвертацию…")


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Навигация по нижнему меню и выбор режима."""
    ud = context.user_data
    ud[PENDING_KEY] = None                                   # любая навигация отменяет ожидание отрезка
    chat = update.effective_chat

    if text == BTN_VIDEO:
        ud[MENU_KEY] = "video"
        await update.message.reply_text("🎬 Видео:", reply_markup=KB_VIDEO)
    elif text == BTN_AUDIO:
        ud[MENU_KEY] = "audio"
        await update.message.reply_text("🎵 Аудио:", reply_markup=KB_AUDIO)
    elif text == BTN_CIRCLE:
        ud[STATE_KEY] = MODE_VIDEO_NOTE                      # входим в режим кружка, кроп пока не задан
        ud[MENU_KEY] = "crop"
        await update.message.reply_text(with_group_note(circle_entry_text(context), chat), reply_markup=KB_CROP)
    elif text in CROP_BUTTONS:
        crop = CROP_BUTTONS[text]
        set_mode(context, MODE_VIDEO_NOTE, crop=crop)
        ud[MENU_KEY] = "crop"
        await update.message.reply_text(with_group_note(confirm_text(MODE_VIDEO_NOTE, crop), chat), reply_markup=KB_CROP)
    elif text == BTN_GIF:
        set_mode(context, MODE_TO_GIF)
        ud[MENU_KEY] = "gif"
        await update.message.reply_text(with_group_note(confirm_text(MODE_TO_GIF), chat), reply_markup=KB_GIF)
    elif text == BTN_VOICE:
        set_mode(context, MODE_TO_VOICE)
        ud[MENU_KEY] = "audio"
        await update.message.reply_text(with_group_note(confirm_text(MODE_TO_VOICE), chat), reply_markup=KB_AUDIO)
    elif text == BTN_EXTRACT:
        set_mode(context, MODE_EXTRACT_AUDIO)
        ud[MENU_KEY] = "audio"
        await update.message.reply_text(with_group_note(confirm_text(MODE_EXTRACT_AUDIO), chat), reply_markup=KB_AUDIO)
    elif text == BTN_BACK:
        target = BACK_TARGET.get(ud.get(MENU_KEY), "root")
        ud[MENU_KEY] = target
        titles = {"root": "Что делаем?", "video": "🎬 Видео:", "audio": "🎵 Аудио:"}
        await update.message.reply_text(titles.get(target, "Что делаем?"), reply_markup=LEVEL_KB[target])


async def gif_from_pending(context, update, start, duration):
    """Запускает GIF из отложенного файла на заданном отрезке."""
    pending = context.user_data.get(PENDING_KEY)
    if not pending:
        return
    context.user_data[PENDING_KEY] = None
    await process_now(context, update.message.chat_id, pending["reply_to"],
                      pending["file_ref"], pending["file_type"], MODE_TO_GIF,
                      gif_start=start, gif_duration=duration)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_blocked(context)
    text = (update.message.text or "").strip()
    ud = context.user_data

    # Кнопка «Отмена» — отдельно (останавливает конвертацию)
    if text == BTN_CANCEL:
        await cancel_conversion(update, context)
        return

    # Кнопка «Всё» — отдельно (использует отложенный файл)
    if text == BTN_ALL:
        if ud.get(PENDING_KEY):
            await gif_from_pending(context, update, 0.0, float(LIMIT_GIF))
        else:
            await update.message.reply_text("Сначала пришлите видео или кружок для GIF.")
        return

    # Навигация по меню
    if text in NAV_BUTTONS:
        await handle_menu_button(update, context, text)
        return

    # Ждём отрезок для GIF?
    if ud.get(PENDING_KEY):
        if text.lower() in ("всё", "все", "all"):
            await gif_from_pending(context, update, 0.0, float(LIMIT_GIF))
            return
        seg = parse_interval(text)
        if seg is None:
            await update.message.reply_text(
                f"Не понял время. Формат 0:05-0:25 или 5-25 (в секундах), "
                f"максимум {LIMIT_GIF} секунд, либо нажмите «✅ Всё».")
            return
        start, end = seg
        if end - start > LIMIT_GIF:
            await update.message.reply_text(
                f"❌ Максимум {LIMIT_GIF} секунд для GIF. Укажите отрезок покороче.")
            return
        await gif_from_pending(context, update, start, end - start)
        return

    # Прочий текст
    await prompt_send_file(update, context)


async def on_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_blocked(context)
    if context.user_data.get(PENDING_KEY):
        await update.message.reply_text(
            f"✂️ Жду время для GIF (0:05-0:25 или 5-25, максимум {LIMIT_GIF} секунд) или нажмите «✅ Всё».")
        return
    await prompt_send_file(update, context)


async def handle_gif_media(update: Update, context: ContextTypes.DEFAULT_TYPE, file_ref, file_type) -> None:
    """GIF: по одному файлу за раз; альбом → первый файл в работу, на остальные одно предупреждение."""
    message = update.message
    ud = context.user_data
    mg = message.media_group_id

    # Уже есть отложенный файл → за раз только один
    if ud.get(PENDING_KEY):
        if mg is not None and ud.get(GROUP_KEY) == mg:
            return                                           # про этот альбом уже сказали
        ud[GROUP_KEY] = mg
        await message.reply_text(
            "🖼️ За раз обрабатываю один файл. Сначала закончите с текущим — "
            "пришлите отрезок или нажмите «✅ Всё».", do_quote=True)
        return

    # Отрезок в подписи?
    seg = parse_interval(message.caption or "")
    if seg:
        start, end = seg
        if end - start > LIMIT_GIF:
            await message.reply_text(
                f"❌ Максимум {LIMIT_GIF} секунд для GIF. Укажите отрезок покороче.", do_quote=True)
            return
        await process_now(context, message.chat_id, message.message_id, file_ref, file_type,
                          MODE_TO_GIF, gif_start=start, gif_duration=end - start)
        return

    if message.chat.type != "private":
        # в группе пошаговый диалог не работает — берём первые 30 секунд
        await process_now(context, message.chat_id, message.message_id, file_ref, file_type,
                          MODE_TO_GIF, gif_start=0.0, gif_duration=float(LIMIT_GIF))
        return

    # Личка: запоминаем файл (синхронно, до await) и спрашиваем отрезок
    ud[PENDING_KEY] = {"file_ref": file_ref, "file_type": file_type, "reply_to": message.message_id}
    ud[GROUP_KEY] = None
    await message.reply_text(
        "✂️ На какой отрезок делать GIF?\n"
        f"Пришлите время в формате 0:05-0:25 или 5-25 (в секундах), максимум {LIMIT_GIF} секунд, "
        "или нажмите «✅ Всё» — возьму первые 30 секунд.", do_quote=True)


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_blocked(context)
    message = update.message
    chat = message.chat
    file_ref, file_type = pick_file(message)
    if not file_ref:
        return

    mode = context.user_data.get(STATE_KEY)
    if mode is None:
        if chat.type == "private":
            context.user_data[MENU_KEY] = "root"
            await message.reply_text("⚠️ Сначала выберите режим в меню ниже.",
                                     reply_markup=KB_ROOT, do_quote=True)
        return

    cfg = MODES[mode]
    if file_type in cfg.same_types:
        await message.reply_text(cfg.same_type_error, do_quote=True)
        return
    if file_type not in cfg.valid_types:
        await message.reply_text(cfg.error, do_quote=True)
        return

    size = getattr(file_ref, "file_size", None) or 0
    if size > MAX_DOWNLOAD_SIZE:
        await message.reply_text("❌ Файл слишком большой — бот может скачивать файлы до 20 МБ.", do_quote=True)
        return

    if mode == MODE_TO_GIF:
        await handle_gif_media(update, context, file_ref, file_type)
        return

    if mode == MODE_VIDEO_NOTE:
        crop = context.user_data.get(CROP_KEY)
        if crop is None:
            context.user_data[MENU_KEY] = "crop"
            await message.reply_text(
                "⚠️ Сначала выберите часть кадра для кружка (кнопки ниже).",
                reply_markup=KB_CROP, do_quote=True)
            return
        await process_now(context, chat.id, message.message_id, file_ref, file_type, mode, crop=crop)
        return

    await process_now(context, chat.id, message.message_id, file_ref, file_type, mode)


async def on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_blocked(context)
    await update.message.reply_text("Неизвестная команда. Откройте меню кнопками ниже или /start.")


# --- ЗАПУСК ---

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан (через .env или переменную окружения).")

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", cmd_start))
    for mode_id, cfg in MODES.items():
        app.add_handler(CommandHandler(cfg.command, make_mode_command(mode_id)))

    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))

    app.add_handler(MessageHandler(
        filters.Sticker.ALL & filters.ChatType.PRIVATE, on_sticker))

    media_filter = (
        filters.VIDEO | filters.AUDIO | filters.VOICE | filters.VIDEO_NOTE |
        filters.ANIMATION | filters.PHOTO | filters.Document.ALL
    )
    app.add_handler(MessageHandler(media_filter, on_media))

    app.add_handler(MessageHandler(
        filters.COMMAND & filters.ChatType.PRIVATE, on_unknown_command))

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
