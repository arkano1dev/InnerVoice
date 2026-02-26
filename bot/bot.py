"""InnerVoice Telegram Bot - uses remote Whisper API for transcription."""
import os
import asyncio
import logging
import subprocess
import time
import tiktoken
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
from typing import Dict, List, Optional, Tuple
from pathlib import Path
from contextlib import suppress
from collections import defaultdict

# Initialize logging
logging.basicConfig(level=logging.INFO)

# Load environment variables
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
WHISPER_API_URL = os.getenv("WHISPER_API_URL", "http://whisper:9000")

# Adjust these constants
TELEGRAM_TIMEOUT = 200
WHISPER_TIMEOUT = 600  # long audio = many segments; allow enough time per request (GPU can be slow or recovering)
WHISPER_RETRIES = 2    # retry segment on 5xx / connection error (e.g. after service restart)
DUPLICATE_COOLDOWN_SEC = 60
LAST_PROCESSED_MAX_AGE = 600  # Evict entries older than 10 min

# Ensure bot instance is created first
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=None),
    session_timeout=TELEGRAM_TIMEOUT,
    connect_timeout=TELEGRAM_TIMEOUT,
)
dp = Dispatcher()

# Directories and logging configuration
AUDIO_DIR = "audios"
os.makedirs(AUDIO_DIR, exist_ok=True)

# Queue to process audios in order
audio_queue = asyncio.Queue()

# Duplicate guard: (user_id, file_id) -> timestamp of last processing
last_processed: Dict[Tuple[int, str], float] = {}

# Pending retry: user_id -> (file_id, file_path) for Retry button
pending_retry: Dict[int, Tuple[str, str]] = {}

# Language configuration
SUPPORTED_LANGUAGES = {
    "es": {"name": "Spanish", "local": "Español", "flag": "🇪🇸"},
    "en": {"name": "English", "local": "English", "flag": "🇬🇧"},
    "fr": {"name": "French", "local": "Français", "flag": "🇫🇷"},
    "nl": {"name": "Dutch", "local": "Nederlands", "flag": "🇳🇱"},
    "pt": {"name": "Portuguese", "local": "Português", "flag": "🇵🇹"},
    "it": {"name": "Italian", "local": "Italiano", "flag": "🇮🇹"},
    "ja": {"name": "Japanese", "local": "日本語", "flag": "🇯🇵"},
    "zh": {"name": "Chinese", "local": "中文", "flag": "🇨🇳"},
}

# Processing modes
PROCESSING_MODES = {
    "fast": {"name": "Fast Mode", "icon": "🚀", "description": "English translation only"},
    "full": {"name": "Full Mode", "icon": "📝", "description": "Original + English translation"},
}

# User preferences - default Spanish for immediate first-audio flow
user_preferences = defaultdict(
    lambda: {
        "language": "es",
        "mode": "full",
        "show_stats": True,
        "timestamps": False,
        "ui_language": "es",  # Default Spanish - no blocking language selection
    }
)

# UI Text translations
UI_TEXTS = {
    "en": {
        "welcome_title": "🎙️ <b>Welcome to InnerVoice!</b>",
        "audio_received": "🎵 <b>Audio Received</b>",
        "duration": "Duration",
        "language": "Language",
        "mode": "Mode",
        "segments": "Segments",
        "processing": "⏳ Processing...",
        "transcription_header": "🎤 <b>Transcription</b>",
        "original_language": "Original language",
        "translation_header": "🌐 <b>Translation</b>",
        "english": "English",
        "processing_complete": "✅ <b>Processing Complete</b>",
        "time": "Time",
        "help_title": "📖 <b>How to Use InnerVoice</b>",
        "about_title": "🔐 <b>Privacy-First Voice Transcription</b>",
        "settings_title": "⚙️ <b>Your Settings</b>",
        "configure": "Configure your InnerVoice experience:",
        "stats": "Stats",
        "timestamps": "Timestamps",
        "change_ui_lang": "Change bot language",
        "busy": "⚠️ <b>Whisper is busy</b>\n\nGPU/VRAM is loaded (e.g. Ollama in use). Try again when free. Use Retry below when ready.",
        "transcription_failed": "❌ <b>Transcription failed</b>\n\nSomething went wrong on the server. Please try again later.",
        "duplicate_skipped": "⏭️ Same audio already processed recently. Skipped. Send again after 1 minute to reprocess.",
    },
    "es": {
        "welcome_title": "🎙️ <b>¡Bienvenido a InnerVoice!</b>",
        "audio_received": "🎵 <b>Audio Recibido</b>",
        "duration": "Duración",
        "language": "Idioma",
        "mode": "Modo",
        "segments": "Segmentos",
        "processing": "⏳ Procesando...",
        "transcription_header": "🎤 <b>Transcripción</b>",
        "original_language": "Idioma original",
        "translation_header": "🌐 <b>Traducción</b>",
        "english": "Inglés",
        "processing_complete": "✅ <b>Procesamiento Completo</b>",
        "time": "Tiempo",
        "help_title": "📖 <b>Cómo Usar InnerVoice</b>",
        "about_title": "🔐 <b>Transcripción de Voz con Privacidad</b>",
        "settings_title": "⚙️ <b>Tus Configuraciones</b>",
        "configure": "Configura tu experiencia InnerVoice:",
        "stats": "Estadísticas",
        "timestamps": "Marcas de tiempo",
        "change_ui_lang": "Cambiar idioma del bot",
        "busy": "⚠️ <b>Whisper está ocupado</b>\n\nGPU/VRAM cargada (ej. Ollama en uso). Intenta de nuevo cuando esté libre. Usa Reintentar abajo cuando estés listo.",
        "transcription_failed": "❌ <b>Transcripción fallida</b>\n\nAlgo falló en el servidor. Intenta de nuevo más tarde.",
        "duplicate_skipped": "⏭️ Mismo audio ya procesado recientemente. Omitido. Envía de nuevo tras 1 minuto para reprocesar.",
    },
}

# Global configuration
CHUNK_SIZE_SECONDS = 30
processing_states: Dict[str, Dict] = {}
progress_messages: Dict[str, int] = {}


def get_text(user_id: int, key: str) -> str:
    """Get translated text for user's UI language."""
    ui_lang = user_preferences[user_id].get("ui_language", "es")
    if ui_lang not in UI_TEXTS:
        ui_lang = "es"
    return UI_TEXTS[ui_lang].get(key, key)


def create_ui_language_keyboard() -> InlineKeyboardMarkup:
    """Create keyboard for UI language selection (Spanish/English only)."""
    keyboard = [
        [InlineKeyboardButton(text="🇪🇸 Español", callback_data="ui_lang_es")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="ui_lang_en")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def create_retry_keyboard(file_id: str) -> InlineKeyboardMarkup:
    """Create Retry button for when Whisper is busy."""
    keyboard = [[InlineKeyboardButton(text="🔄 Retry", callback_data=f"retry_{file_id}")]]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def create_language_keyboard() -> InlineKeyboardMarkup:
    """Create inline keyboard for audio language selection with flag emojis."""
    keyboard = []
    row = []
    for code, info in SUPPORTED_LANGUAGES.items():
        row.append(InlineKeyboardButton(text=f"{info['flag']} {info['local']}", callback_data=f"lang_{code}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def create_mode_keyboard() -> InlineKeyboardMarkup:
    """Create inline keyboard for mode selection."""
    keyboard = [
        [InlineKeyboardButton(text=f"{info['icon']} {info['name']}", callback_data=f"mode_{mode}")]
        for mode, info in PROCESSING_MODES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def create_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Create settings keyboard showing current preferences."""
    prefs = user_preferences[user_id]
    lang_info = SUPPORTED_LANGUAGES[prefs["language"]]
    mode_info = PROCESSING_MODES[prefs["mode"]]
    ui_lang = prefs.get("ui_language", "es")
    ui_lang_label = "🇪🇸 ES" if ui_lang == "es" else "🇬🇧 EN"

    keyboard = [
        [InlineKeyboardButton(text=f"Language: {lang_info['flag']} {lang_info['name']}", callback_data="change_lang")],
        [InlineKeyboardButton(text=f"Mode: {mode_info['icon']} {mode_info['name']}", callback_data="change_mode")],
        [InlineKeyboardButton(text=f"UI: {ui_lang_label}", callback_data="change_ui_lang")],
        [InlineKeyboardButton(text=f"Stats: {'✅' if prefs['show_stats'] else '❌'}", callback_data="toggle_stats")],
        [InlineKeyboardButton(text=f"Timestamps: {'✅' if prefs['timestamps'] else '❌'}", callback_data="toggle_timestamps")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _evict_old_last_processed():
    """Remove entries older than LAST_PROCESSED_MAX_AGE."""
    now = time.time()
    to_remove = [k for k, ts in last_processed.items() if now - ts > LAST_PROCESSED_MAX_AGE]
    for k in to_remove:
        del last_processed[k]


class WhisperBusyError(Exception):
    """Raised when Whisper API returns 503 gpu_busy."""

    pass


async def fetch_whisper_health() -> Optional[dict]:
    """Fetch Whisper /health for GPU stats."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{WHISPER_API_URL.rstrip('/')}/health",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logging.debug(f"Could not fetch Whisper health: {e}")
    return None


# --- Command handlers ---


@dp.message(Command("start"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    prefs = user_preferences[user_id]
    ui_lang = prefs.get("ui_language", "es")

    if ui_lang == "es":
        await message.answer(
            "🎙️ <b>¡Bienvenido a InnerVoice!</b>\n\n"
            "Tu bot de transcripción de voz con privacidad.\n"
            "Envía un mensaje de voz y obtén:\n\n"
            "📝 Transcripción en idioma original\n"
            "🌐 Traducción al inglés\n"
            "⚡ Procesamiento rápido con Whisper AI\n\n"
            "Todo el procesamiento es local - tu audio permanece privado.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Comandos:</b>\n"
            "/settings - Configurar idioma y modo\n"
            "/lang - Cambiar idioma de audio\n"
            "/mode - Cambiar modo\n"
            "/help - Ayuda\n"
            "/about - Detalles técnicos\n\n"
            "Cambia idioma de interfaz en /settings",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "🎙️ <b>Welcome to InnerVoice!</b>\n\n"
            "Your privacy-first voice transcription bot.\n"
            "Send a voice message and get:\n\n"
            "📝 Transcription in original language\n"
            "🌐 Translation to English\n"
            "⚡ Fast processing with Whisper AI\n\n"
            "All processing happens locally - your audio stays private.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Commands:</b>\n"
            "/settings - Configure language & mode\n"
            "/lang - Quick language change\n"
            "/mode - Quick mode change\n"
            "/help - Learn more\n"
            "/about - Technical details\n\n"
            "Change UI language in /settings",
            parse_mode="HTML",
        )


@dp.message(Command("help"))
async def help_handler(message: types.Message):
    user_id = message.from_user.id
    prefs = user_preferences[user_id]
    lang_info = SUPPORTED_LANGUAGES[prefs["language"]]
    mode_info = PROCESSING_MODES[prefs["mode"]]
    ui_lang = prefs.get("ui_language", "es")

    if ui_lang == "es":
        await message.answer(
            "📖 <b>Cómo Usar InnerVoice</b>\n\n"
            "1️⃣ <b>Envía un mensaje de voz</b>\n"
            "   Graba cualquier duración - desde segundos hasta 30+ minutos\n\n"
            "2️⃣ <b>Procesamiento</b>\n"
            "   Observa la barra de progreso mientras se transcribe tu audio\n\n"
            "3️⃣ <b>Obtén resultados</b>\n"
            "   • Transcripción original (texto limpio)\n"
            "   • Traducción al inglés (texto limpio)\n"
            "   • ¡Ambos listos para copiar y pegar!\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>⚙️ Modos:</b>\n\n"
            "🚀 <b>Modo Rápido</b>\n"
            "   Solo traducción al inglés (más rápido)\n\n"
            "📝 <b>Modo Completo</b>\n"
            "   Original + traducción al inglés\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Actual: {lang_info['flag']} {lang_info['name']}\n"
            f"Modo: {mode_info['icon']} {mode_info['name']}\n\n"
            "Cambia en /settings",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "📖 <b>How to Use InnerVoice</b>\n\n"
            "1️⃣ <b>Send a voice message</b>\n"
            "   Record any length - from seconds to 30+ minutes\n\n"
            "2️⃣ <b>Processing</b>\n"
            "   Watch the progress bar as your audio is transcribed\n\n"
            "3️⃣ <b>Get results</b>\n"
            "   • Original transcription (clean text)\n"
            "   • English translation (clean text)\n"
            "   • Both ready to copy & paste!\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>⚙️ Modes:</b>\n\n"
            "🚀 <b>Fast Mode</b>\n"
            "   Get only English translation (faster)\n\n"
            "📝 <b>Full Mode</b>\n"
            "   Get both original + English\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Current: {lang_info['flag']} {lang_info['name']}\n"
            f"Mode: {mode_info['icon']} {mode_info['name']}\n\n"
            "Change via /settings",
            parse_mode="HTML",
        )


@dp.message(Command("about"))
async def about_handler(message: types.Message):
    user_id = message.from_user.id
    ui_lang = user_preferences[user_id].get("ui_language", "es")

    if ui_lang == "es":
        await message.answer(
            "🔐 <b>Transcripción de Voz con Privacidad</b>\n\n"
            "InnerVoice se ejecuta completamente en tu propia infraestructura.\n"
            "Whisper corre en un contenedor separado (ROCm/eGPU).\n\n"
            "Stack: Whisper (medium), FFmpeg, Python, aiogram, Docker",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "🔐 <b>Privacy-First Voice Transcription</b>\n\n"
            "InnerVoice runs on your own infrastructure.\n"
            "Whisper runs in a separate container (ROCm/eGPU).\n\n"
            "Stack: Whisper (medium), FFmpeg, Python, aiogram, Docker",
            parse_mode="HTML",
        )


@dp.message(Command("settings"))
async def settings_handler(message: types.Message):
    user_id = message.from_user.id
    prefs = user_preferences[user_id]
    ui_lang = prefs.get("ui_language", "es")

    msg = (
        "⚙️ <b>Tus Configuraciones</b>\n\nConfigura tu experiencia InnerVoice:"
        if ui_lang == "es"
        else "⚙️ <b>Your Settings</b>\n\nConfigure your InnerVoice experience:"
    )
    await message.answer(msg, reply_markup=create_settings_keyboard(user_id), parse_mode="HTML")


@dp.message(Command("lang"))
async def lang_handler(message: types.Message):
    user_id = message.from_user.id
    prefs = user_preferences[user_id]
    ui_lang = prefs.get("ui_language", "es")
    current_lang_info = SUPPORTED_LANGUAGES[prefs["language"]]

    if ui_lang == "es":
        msg = f"🌐 <b>Optimización de Idioma</b>\n\nActual: {current_lang_info['flag']} {current_lang_info['name']}\n\n👇 Selecciona:"
    else:
        msg = f"🌐 <b>Language Optimization</b>\n\nCurrent: {current_lang_info['flag']} {current_lang_info['name']}\n\n👇 Select:"
    await message.answer(msg, reply_markup=create_language_keyboard(), parse_mode="HTML")


@dp.message(Command("mode"))
async def mode_handler(message: types.Message):
    user_id = message.from_user.id
    prefs = user_preferences[user_id]
    mode_info = PROCESSING_MODES[prefs["mode"]]
    ui_lang = prefs.get("ui_language", "es")

    if ui_lang == "es":
        msg = f"⚡ <b>Modo de Procesamiento</b>\n\nActual: {mode_info['icon']} {mode_info['name']}\n\n👇 Selecciona:"
    else:
        msg = f"⚡ <b>Processing Mode</b>\n\nCurrent: {mode_info['icon']} {mode_info['name']}\n\n👇 Select:"
    await message.answer(msg, reply_markup=create_mode_keyboard(), parse_mode="HTML")


# --- Callback handlers ---


@dp.callback_query(lambda c: c.data and c.data.startswith("ui_lang_"))
async def process_ui_language_callback(callback_query: types.CallbackQuery):
    lang_code = callback_query.data.split("_")[2]
    user_id = callback_query.from_user.id
    if lang_code in ("es", "en"):
        user_preferences[user_id]["ui_language"] = lang_code
        msg = "✅ Idioma configurado a Español!" if lang_code == "es" else "✅ Language set to English!"
        await callback_query.message.edit_text(msg, parse_mode="HTML")
    await callback_query.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("change_ui_lang"))
async def change_ui_lang_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    prefs = user_preferences[user_id]
    ui_lang = prefs.get("ui_language", "es")
    msg = "Selecciona idioma de interfaz:" if ui_lang == "es" else "Select UI language:"
    await callback_query.message.edit_text(msg, reply_markup=create_ui_language_keyboard(), parse_mode="HTML")
    await callback_query.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("lang_"))
async def process_language_callback(callback_query: types.CallbackQuery):
    lang_code = callback_query.data.split("_")[1]
    user_id = callback_query.from_user.id
    if lang_code in SUPPORTED_LANGUAGES:
        user_preferences[user_id]["language"] = lang_code
        lang_info = SUPPORTED_LANGUAGES[lang_code]
        await callback_query.message.edit_text(
            f"✅ {lang_info['flag']} {lang_info['name']}\n🎙️ ¡Envía un mensaje de voz!",
            parse_mode="HTML",
        )
    await callback_query.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("mode_"))
async def process_mode_callback(callback_query: types.CallbackQuery):
    mode = callback_query.data.split("_")[1]
    user_id = callback_query.from_user.id
    if mode in PROCESSING_MODES:
        user_preferences[user_id]["mode"] = mode
        mode_info = PROCESSING_MODES[mode]
        await callback_query.message.edit_text(f"✅ {mode_info['icon']} {mode_info['name']}\n🎙️ Ready!", parse_mode="HTML")
    await callback_query.answer()


@dp.callback_query(lambda c: c.data == "change_lang")
async def change_lang_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    prefs = user_preferences[user_id]
    current_lang_info = SUPPORTED_LANGUAGES[prefs["language"]]
    await callback_query.message.edit_text(
        f"🌐 Actual: {current_lang_info['flag']} {current_lang_info['name']}\n👇 Select:",
        reply_markup=create_language_keyboard(),
        parse_mode="HTML",
    )
    await callback_query.answer()


@dp.callback_query(lambda c: c.data == "change_mode")
async def change_mode_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    prefs = user_preferences[user_id]
    mode_info = PROCESSING_MODES[prefs["mode"]]
    await callback_query.message.edit_text(
        f"⚡ Actual: {mode_info['icon']} {mode_info['name']}\n👇 Select:",
        reply_markup=create_mode_keyboard(),
        parse_mode="HTML",
    )
    await callback_query.answer()


@dp.callback_query(lambda c: c.data == "toggle_stats")
async def toggle_stats_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    user_preferences[user_id]["show_stats"] = not user_preferences[user_id]["show_stats"]
    await callback_query.message.edit_reply_markup(reply_markup=create_settings_keyboard(user_id))
    await callback_query.answer()


@dp.callback_query(lambda c: c.data == "toggle_timestamps")
async def toggle_timestamps_callback(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    user_preferences[user_id]["timestamps"] = not user_preferences[user_id]["timestamps"]
    await callback_query.message.edit_reply_markup(reply_markup=create_settings_keyboard(user_id))
    await callback_query.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("retry_"))
async def retry_callback(callback_query: types.CallbackQuery):
    """Re-enqueue last rejected audio (Whisper busy) for processing."""
    user_id = callback_query.from_user.id
    if user_id not in pending_retry:
        await callback_query.answer("No hay audio pendiente de reintento.", show_alert=True)
        return
    file_id, file_path = pending_retry[user_id]
    if not Path(file_path).exists():
        await callback_query.answer("Archivo de audio ya no disponible.", show_alert=True)
        del pending_retry[user_id]
        return
    await audio_queue.put((user_id, file_id, file_path))
    del pending_retry[user_id]
    await callback_query.message.edit_text("🔄 Reintentando...", parse_mode="HTML")
    await callback_query.answer()


# --- Voice handler ---


@dp.message(F.voice)
async def handle_voice(message: types.Message):
    user_id = message.from_user.id
    prefs = user_preferences[user_id]

    # Duplicate guard: same (user_id, file_id) within 60s = skip
    file_id = message.voice.file_id
    key = (user_id, file_id)
    now = time.time()
    _evict_old_last_processed()
    if key in last_processed and (now - last_processed[key]) < DUPLICATE_COOLDOWN_SEC:
        await message.answer(get_text(user_id, "duplicate_skipped"), parse_mode="HTML")
        return

    voice = await bot.download(message.voice)
    file_path = os.path.join(AUDIO_DIR, f"{file_id}.ogg")
    with open(file_path, "wb") as f:
        f.write(voice.read())

    # Telegram API expects business_connection_id as string when editing messages in business context
    business_connection_id = getattr(message, "business_connection_id", None)
    await audio_queue.put((user_id, file_id, file_path, business_connection_id))


# --- Helpers ---


async def send_message_safe(
    user_id: int,
    text: str,
    parse_mode: str = None,
    reply_to_message_id: int = None,
    reply_markup: InlineKeyboardMarkup = None,
    business_connection_id=None,
) -> Optional[types.Message]:
    if not text or text.isspace():
        return None
    send_kwargs = {
        "chat_id": user_id,
        "text": text,
        "parse_mode": parse_mode,
        "reply_to_message_id": reply_to_message_id,
        "reply_markup": reply_markup,
    }
    if business_connection_id is not None:
        send_kwargs["business_connection_id"] = str(business_connection_id)
    for attempt in range(3):
        try:
            msg = await asyncio.wait_for(
                bot.send_message(**send_kwargs),
                timeout=TELEGRAM_TIMEOUT,
            )
            return msg
        except Exception as e:
            logging.error(f"Error sending message (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    return None


def _escape_html(text: str) -> str:
    """Escape for HTML so <pre> content is safe."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_text_chunks(text: str, max_length: int) -> List[str]:
    """Split text into chunks of at most max_length, breaking at newlines when possible."""
    if not text or not text.strip():
        return []
    if len(text) <= max_length:
        return [text.strip()]
    chunks = []
    current = ""
    for line in text.split("\n"):
        line_avec = line + "\n"
        if len(current) + len(line_avec) <= max_length:
            current += line_avec
        else:
            if current:
                chunks.append(current.rstrip())
            if len(line) <= max_length:
                current = line_avec
            else:
                # Single line longer than max_length: split by character
                current = ""
                start = 0
                while start < len(line):
                    end = min(start + max_length, len(line))
                    chunks.append(line[start:end])
                    start = end
    if current:
        chunks.append(current.rstrip())
    return chunks


def _chunk_then_escape(text: str, max_escaped: int) -> List[str]:
    """Split text into chunks that after HTML escape are each <= max_escaped. Never splits inside &...;."""
    if not text or not text.strip():
        return []
    # Start with a safe raw chunk size; escaping can grow (e.g. & -> &amp;)
    raw_max = max(800, max_escaped - 100)
    raw_chunks = _split_text_chunks(text, raw_max)
    out = []
    for raw in raw_chunks:
        escaped = _escape_html(raw)
        if len(escaped) <= max_escaped:
            out.append(escaped)
        else:
            # Escape made it too long; split raw into smaller pieces
            out.extend(_chunk_then_escape(raw, max_escaped))
    return out


async def send_text_in_chunks(
    user_id: int,
    text: str,
    max_length: int = 4096,
    plain: bool = False,
    copyable: bool = True,
) -> bool:
    """Send text in messages of at most max_length (Telegram limit 4096).
    If plain=True, no 'Part X/Y' labels. If copyable=True, wrap in <pre> so tap-to-copy works."""
    if copyable:
        pre_max = max_length - 11  # "<pre></pre>"
        chunks = _chunk_then_escape(text, pre_max)
    elif not plain:
        chunks = _split_text_chunks(text, max_length - 25)
    else:
        chunks = _split_text_chunks(text, max_length)
    if not chunks:
        return False
    for i, chunk in enumerate(chunks):
        if not plain and len(chunks) > 1:
            prefix = f"━━ Part {i + 1}/{len(chunks)} ━━\n\n"
            await send_message_safe(user_id, prefix + chunk)
        elif copyable:
            await send_message_safe(user_id, f"<pre>{chunk}</pre>", parse_mode="HTML")
        else:
            await send_message_safe(user_id, chunk)
        await asyncio.sleep(0.2)
    return True


async def update_progress(
    user_id: int,
    message_id: int,
    current: int,
    total: int,
    elapsed_time: float,
    extra_info: str = "",
    business_connection_id=None,
) -> None:
    try:
        pct = (current / total) * 100 if total > 0 else 0
        filled = int(pct / 10)
        bar = "▓" * filled + "░" * (10 - filled)
        eta = (elapsed_time / current * (total - current)) if current > 0 else 0
        text = f"⚡ <b>Processing</b>\n\n{bar} {pct:.0f}%\nSegment: {current}/{total}\nElapsed: {elapsed_time:.1f}s\nETA: {int(eta)}s\n"
        if extra_info:
            text += f"\n{extra_info}"
        kwargs = {"text": text, "chat_id": user_id, "message_id": message_id, "parse_mode": "HTML"}
        if business_connection_id is not None:
            kwargs["business_connection_id"] = str(business_connection_id)
        await bot.edit_message_text(**kwargs)
    except Exception as e:
        logging.error(f"Error updating progress: {e}")


def count_tokens(text: str) -> int:
    try:
        encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")
        return len(encoding.encode(text))
    except Exception:
        return len(text.split())


# --- Fix aiohttp file upload: we need to pass file content, not reopen ---


async def _call_whisper_api(
    segment_path: Path, task: str, language: Optional[str], return_segments: bool
) -> dict:
    """Call Whisper API for transcription. Raises WhisperBusyError on 503. Retries on 5xx/connection errors."""
    url = f"{WHISPER_API_URL.rstrip('/')}/transcribe"
    data = aiohttp.FormData()
    data.add_field("task", task)
    data.add_field("return_segments", str(return_segments).lower())
    if language:
        data.add_field("language", language)
    data.add_field("audio", segment_path.read_bytes(), filename=segment_path.name, content_type="audio/wav")

    last_error = None
    for attempt in range(WHISPER_RETRIES + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=WHISPER_TIMEOUT)) as resp:
                    if resp.status == 503:
                        try:
                            body = await resp.json()
                            err = body.get("error")
                            if err in ("gpu_busy", "gpu_oom"):
                                raise WhisperBusyError(body.get("message", "GPU busy"))
                        except aiohttp.ContentTypeError:
                            pass
                        raise WhisperBusyError("Service unavailable")
                    if resp.status >= 500:
                        # Treat OOM in response body like busy so user gets retry UX
                        try:
                            body = await resp.json()
                            err_msg = (body.get("error") or "").lower()
                            if "out of memory" in err_msg or "outofmemoryerror" in err_msg:
                                raise WhisperBusyError(
                                    body.get("message") or "GPU ran out of memory. Try again in a moment."
                                )
                        except aiohttp.ContentTypeError:
                            pass
                        last_error = RuntimeError(f"Whisper HTTP {resp.status}")
                        if attempt < WHISPER_RETRIES:
                            await asyncio.sleep(3 * (attempt + 1))
                            continue
                        resp.raise_for_status()
                    resp.raise_for_status()
                    result = await resp.json()
                    return {"text": result.get("text", "").strip(), "segments": result.get("segments", [])}
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
            last_error = e
            if attempt < WHISPER_RETRIES:
                logging.warning("Whisper request failed (attempt %s/%s), retrying: %s", attempt + 1, WHISPER_RETRIES + 1, e)
                await asyncio.sleep(3 * (attempt + 1))
            else:
                raise
    if last_error:
        raise last_error
    raise RuntimeError("Whisper request failed")


async def process_audio_chunk(
    segment: Path, task: str = "transcribe", language: str = None, return_segments: bool = False
) -> dict:
    """Process a single audio chunk via Whisper API."""
    return await _call_whisper_api(segment, task, language, return_segments)


async def process_audio_async(
    user_id: int, file_id: str, file_path: str, business_connection_id=None
):
    """Main audio processing with Whisper API."""
    wav_path = Path(file_path).with_suffix(".wav")
    prefs = user_preferences[user_id]
    progress_msg_id = None

    try:
        duration_result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True,
            text=True,
        )
        duration = float(duration_result.stdout.strip()) if duration_result.returncode == 0 else 0

        if not Path(file_path).exists():
            await send_message_safe(user_id, "❌ Audio file not found.")
            return

        subprocess.run(
            ["ffmpeg", "-i", file_path, "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(wav_path), "-y"],
            check=True,
            capture_output=True,
        )

        segments = await split_audio(wav_path) if wav_path.stat().st_size > 1024 * 1024 else [wav_path]
        if not segments:
            await send_message_safe(user_id, "❌ Failed to process audio segments.")
            return

        lang_info = SUPPORTED_LANGUAGES[prefs["language"]]
        mode_info = PROCESSING_MODES[prefs["mode"]]
        estimated_time = int(duration) if duration > 0 else len(segments) * 30

        status_msg = await send_message_safe(
            user_id,
            f"{get_text(user_id, 'audio_received')}\n\n"
            f"{get_text(user_id, 'duration')}: {int(duration)}s\n"
            f"{get_text(user_id, 'language')}: {lang_info['flag']} {lang_info['name']}\n"
            f"{get_text(user_id, 'mode')}: {mode_info['icon']} {mode_info['name']}\n"
            f"{get_text(user_id, 'segments')}: {len(segments)}\n"
            f"⏱️ ~{estimated_time}s\n\n{get_text(user_id, 'processing')}",
            parse_mode="HTML",
            business_connection_id=business_connection_id,
        )
        if status_msg:
            progress_msg_id = status_msg.message_id

        start_time = time.time()
        full_transcription = ""
        full_translation = ""
        transcription_segments = []
        translation_segments = []
        gpu_busy_raised = False

        for i, segment in enumerate(segments, 1):
            try:
                if progress_msg_id:
                    elapsed = time.time() - start_time
                    if len(segments) <= 5 or i % 2 == 0:
                        await update_progress(
                            user_id,
                            progress_msg_id,
                            i - 1,
                            len(segments),
                            elapsed,
                            business_connection_id=business_connection_id,
                        )

                if prefs["mode"] == "fast":
                    result = await process_audio_chunk(
                        segment, task="translate", language=prefs["language"], return_segments=prefs["timestamps"]
                    )
                    full_translation += result["text"] + " "
                    if prefs["timestamps"] and result.get("segments"):
                        translation_segments.extend(result["segments"])
                else:
                    trans_result = await process_audio_chunk(
                        segment, task="transcribe", language=prefs["language"], return_segments=prefs["timestamps"]
                    )
                    full_transcription += trans_result["text"] + " "
                    if prefs["timestamps"] and trans_result.get("segments"):
                        transcription_segments.extend(trans_result["segments"])
                    transl_result = await process_audio_chunk(
                        segment, task="translate", language=prefs["language"], return_segments=False
                    )
                    full_translation += transl_result["text"] + " "
            except WhisperBusyError:
                gpu_busy_raised = True
                break
            except Exception as e:
                logging.error(f"Error processing segment {i}/{len(segments)}: {e}")
            finally:
                if segment != wav_path:
                    Path(segment).unlink(missing_ok=True)

        if gpu_busy_raised:
            pending_retry[user_id] = (file_id, file_path)
            await send_message_safe(
                user_id,
                get_text(user_id, "busy"),
                parse_mode="HTML",
                reply_markup=create_retry_keyboard(file_id),
            )
            return

        elapsed_time = time.time() - start_time
        last_processed[(user_id, file_id)] = time.time()

        if progress_msg_id:
            try:
                done_text = "✨ ¡Completado!" if prefs.get("ui_language") == "es" else "✨ Complete!"
                edit_kwargs = {
                    "text": (
                        f"{get_text(user_id, 'audio_received')}\n\n"
                        f"{get_text(user_id, 'duration')}: {int(duration)}s\n"
                        f"{get_text(user_id, 'language')}: {lang_info['flag']} {lang_info['name']}\n"
                        f"⏱️ Real: {elapsed_time:.1f}s\n\n{done_text}"
                    ),
                    "chat_id": user_id,
                    "message_id": progress_msg_id,
                    "parse_mode": "HTML",
                }
                if business_connection_id is not None:
                    edit_kwargs["business_connection_id"] = str(business_connection_id)
                await bot.edit_message_text(**edit_kwargs)
            except Exception as e:
                logging.error(f"Error updating final status: {e}")

        def format_with_timestamps(text, segs):
            if not segs:
                return text
            return "\n".join(f"[{int(s['start'])//60:02d}:{int(s['start'])%60:02d}] {s['text'].strip()}" for s in segs)

        if prefs["mode"] == "full" and full_transcription.strip():
            await send_message_safe(
                user_id,
                f"{get_text(user_id, 'transcription_header')} ({lang_info['name']})\n<i>{get_text(user_id, 'original_language')}</i>",
                parse_mode="HTML",
            )
            text_to_send = (
                format_with_timestamps(full_transcription, transcription_segments)
                if prefs["timestamps"] and transcription_segments
                else full_transcription.strip()
            )
            await send_text_in_chunks(user_id, text_to_send, plain=True)

        if full_translation.strip():
            header = get_text(user_id, "translation_header") if prefs["mode"] == "full" else get_text(user_id, "transcription_header")
            await send_message_safe(user_id, f"{header} ({get_text(user_id, 'english')})", parse_mode="HTML")
            text_to_send = (
                format_with_timestamps(full_translation, translation_segments)
                if prefs["timestamps"] and translation_segments and prefs["mode"] == "fast"
                else full_translation.strip()
            )
            await send_text_in_chunks(user_id, text_to_send, plain=True)

        if prefs["show_stats"]:
            transcription_tokens = count_tokens(full_transcription) if full_transcription else 0
            translation_tokens = count_tokens(full_translation)
            stats_msg = (
                f"{get_text(user_id, 'processing_complete')}\n\n"
                f"⏱️ {get_text(user_id, 'time')}: {elapsed_time:.1f}s\n"
                f"🔢 {get_text(user_id, 'segments')}: {len(segments)}\n"
            )
            if full_transcription:
                stats_msg += f"📝 {transcription_tokens} tokens\n"
            stats_msg += f"🔄 {translation_tokens} tokens"
            # GPU stats from Whisper health
            health = await fetch_whisper_health()
            if health and "vram_used_mb" in health and "vram_total_mb" in health:
                stats_msg += f"\n🖥️ GPU: {health['vram_used_mb']} / {health['vram_total_mb']} MB VRAM"
            await send_message_safe(user_id, stats_msg, parse_mode="HTML")

    except WhisperBusyError:
        # GPU busy/OOM: show retry message (may escape segment loop in edge cases)
        pending_retry[user_id] = (file_id, file_path)
        await send_message_safe(
            user_id,
            get_text(user_id, "busy"),
            parse_mode="HTML",
            reply_markup=create_retry_keyboard(file_id),
        )
        return
    except Exception as e:
        logging.error(f"Error processing audio {file_id}: {e}")
        await send_message_safe(
            user_id,
            get_text(user_id, "transcription_failed"),
            parse_mode="HTML",
        )
    finally:
        Path(file_path).unlink(missing_ok=True)
        if wav_path.exists():
            Path(wav_path).unlink(missing_ok=True)
        processing_states.pop(file_id, None)
        if file_id in progress_messages:
            del progress_messages[file_id]
        audio_queue.task_done()


async def split_audio(wav_path: Path) -> List[Path]:
    segments = []
    output_template = str(wav_path.with_name(f"{wav_path.stem}_part%d{wav_path.suffix}"))
    subprocess.run(
        ["ffmpeg", "-i", str(wav_path), "-f", "segment", "-segment_time", str(CHUNK_SIZE_SECONDS), "-c", "copy", output_template],
        check=True,
        capture_output=True,
    )
    index = 0
    while True:
        segment_path = Path(output_template % index)
        if not segment_path.exists():
            break
        segments.append(segment_path)
        index += 1
    return segments


async def audio_worker():
    while True:
        try:
            item = await audio_queue.get()
            # Support both (user_id, file_id, file_path) and (user_id, file_id, file_path, business_connection_id)
            if len(item) == 4:
                user_id, file_id, file_path, business_connection_id = item
            else:
                user_id, file_id, file_path = item
                business_connection_id = None
            await process_audio_async(user_id, file_id, file_path, business_connection_id)
        except Exception as e:
            logging.error(f"Error in audio worker: {e}")
            await asyncio.sleep(1)


async def main():
    audio_worker_task = asyncio.create_task(audio_worker())
    while True:
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logging.error(f"Polling error: {e}")
            await asyncio.sleep(5)
        finally:
            if not audio_worker_task.done():
                audio_worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await audio_worker_task


if __name__ == "__main__":
    os.nice(10)
    asyncio.run(main())
