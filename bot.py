import os
import asyncio
import logging
import whisper
import subprocess
import time
import tiktoken
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
from datetime import datetime
from typing import Dict, List, Optional
from pathlib import Path
from contextlib import suppress
from collections import defaultdict

# Initialize logging
logging.basicConfig(level=logging.INFO)

# Load environment variables
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

# Adjust these constants
TELEGRAM_TIMEOUT = 200  # Seconds for Telegram API calls
PROCESSING_UPDATE_INTERVAL = 30  # Seconds between processing status updates
MAX_SEGMENT_RETRIES = 5

# Ensure bot instance is created first
bot = Bot(
    token=TOKEN, 
    default=DefaultBotProperties(parse_mode=None),
    session_timeout=TELEGRAM_TIMEOUT,
    connect_timeout=TELEGRAM_TIMEOUT
)
dp = Dispatcher()

# Load Whisper model
model = whisper.load_model("medium")  

# Directories and logging configuration
AUDIO_DIR = "audios"
LOG_FILE = "bot.log"
os.makedirs(AUDIO_DIR, exist_ok=True)

# Queue to process audios in order
audio_queue = asyncio.Queue()

# Language configuration
SUPPORTED_LANGUAGES = {
    'es': {'name': 'Spanish', 'local': 'Español', 'flag': '🇪🇸'},
    'en': {'name': 'English', 'local': 'English', 'flag': '🇬🇧'},
    'fr': {'name': 'French', 'local': 'Français', 'flag': '🇫🇷'},
    'nl': {'name': 'Dutch', 'local': 'Nederlands', 'flag': '🇳🇱'},
    'pt': {'name': 'Portuguese', 'local': 'Português', 'flag': '🇵🇹'},
    'it': {'name': 'Italian', 'local': 'Italiano', 'flag': '🇮🇹'},
    'ja': {'name': 'Japanese', 'local': '日本語', 'flag': '🇯🇵'},
    'zh': {'name': 'Chinese', 'local': '中文', 'flag': '🇨🇳'},
}

# Processing modes
PROCESSING_MODES = {
    'fast': {'name': 'Fast Mode', 'icon': '🚀', 'description': 'English translation only'},
    'full': {'name': 'Full Mode', 'icon': '📝', 'description': 'Original + English translation'}
}

# User preferences storage (user_id -> settings)
user_preferences = defaultdict(lambda: {
    'language': 'es',           # Audio language for Whisper optimization
    'mode': 'full',             # Processing mode (fast/full)
    'show_stats': True,         # Show statistics after processing
    'timestamps': False,        # Add timestamps to transcription
    'ui_language': None         # Bot UI language (es/en) - None means not set yet
})

# UI Text translations
UI_TEXTS = {
    'en': {
        'welcome_title': '🎙️ <b>Welcome to InnerVoice!</b>',
        'select_language': 'Please select your preferred language for the bot interface:',
        'language_selected': '✅ Language set to English!\n\nSend me a voice message to get started.',
        'audio_received': '🎵 <b>Audio Received</b>',
        'duration': 'Duration',
        'language': 'Language',
        'mode': 'Mode',
        'segments': 'Segments',
        'processing': '⏳ Processing...',
        'transcription_header': '🎤 <b>Transcription</b>',
        'original_language': 'Original language',
        'translation_header': '🌐 <b>Translation</b>',
        'english': 'English',
        'processing_complete': '✅ <b>Processing Complete</b>',
        'time': 'Time',
        'help_title': '📖 <b>How to Use InnerVoice</b>',
        'about_title': '🔐 <b>Privacy-First Voice Transcription</b>',
        'settings_title': '⚙️ <b>Your Settings</b>',
        'configure': 'Configure your InnerVoice experience:',
        'stats': 'Stats',
        'timestamps': 'Timestamps',
        'change_ui_lang': 'Change bot language',
    },
    'es': {
        'welcome_title': '🎙️ <b>¡Bienvenido a InnerVoice!</b>',
        'select_language': 'Por favor, selecciona tu idioma preferido para la interfaz del bot:',
        'language_selected': '✅ ¡Idioma configurado a Español!\n\nEnvíame un mensaje de voz para comenzar.',
        'audio_received': '🎵 <b>Audio Recibido</b>',
        'duration': 'Duración',
        'language': 'Idioma',
        'mode': 'Modo',
        'segments': 'Segmentos',
        'processing': '⏳ Procesando...',
        'transcription_header': '🎤 <b>Transcripción</b>',
        'original_language': 'Idioma original',
        'translation_header': '🌐 <b>Traducción</b>',
        'english': 'Inglés',
        'processing_complete': '✅ <b>Procesamiento Completo</b>',
        'time': 'Tiempo',
        'help_title': '📖 <b>Cómo Usar InnerVoice</b>',
        'about_title': '🔐 <b>Transcripción de Voz con Privacidad</b>',
        'settings_title': '⚙️ <b>Tus Configuraciones</b>',
        'configure': 'Configura tu experiencia InnerVoice:',
        'stats': 'Estadísticas',
        'timestamps': 'Marcas de tiempo',
        'change_ui_lang': 'Cambiar idioma del bot',
    }
}

# Global configuration
CHUNK_SIZE_SECONDS = 30
processing_states: Dict[str, Dict] = {}
progress_messages: Dict[str, int] = {}  # file_id -> message_id for progress updates

def get_text(user_id: int, key: str) -> str:
    """Get translated text for user's UI language."""
    ui_lang = user_preferences[user_id].get('ui_language', 'en')
    if ui_lang not in UI_TEXTS:
        ui_lang = 'en'
    return UI_TEXTS[ui_lang].get(key, key)

def create_ui_language_keyboard() -> InlineKeyboardMarkup:
    """Create keyboard for UI language selection (Spanish/English only)."""
    keyboard = [
        [InlineKeyboardButton(text="🇪🇸 Español", callback_data="ui_lang_es")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="ui_lang_en")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def create_language_keyboard() -> InlineKeyboardMarkup:
    """Create inline keyboard for audio language selection with flag emojis."""
    keyboard = []
    # Create rows of 2 languages each for better layout
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
        [InlineKeyboardButton(
            text=f"{info['icon']} {info['name']}", 
            callback_data=f"mode_{mode}"
        )] for mode, info in PROCESSING_MODES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def create_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Create settings keyboard showing current preferences."""
    prefs = user_preferences[user_id]
    lang_info = SUPPORTED_LANGUAGES[prefs['language']]
    mode_info = PROCESSING_MODES[prefs['mode']]
    
    keyboard = [
        [InlineKeyboardButton(text=f"Language: {lang_info['flag']} {lang_info['name']}", callback_data="change_lang")],
        [InlineKeyboardButton(text=f"Mode: {mode_info['icon']} {mode_info['name']}", callback_data="change_mode")],
        [InlineKeyboardButton(
            text=f"Stats: {'✅' if prefs['show_stats'] else '❌'}", 
            callback_data="toggle_stats"
        )],
        [InlineKeyboardButton(
            text=f"Timestamps: {'✅' if prefs['timestamps'] else '❌'}", 
            callback_data="toggle_timestamps"
        )],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    prefs = user_preferences[user_id]
    
    # If UI language not set, ask user to select
    if prefs['ui_language'] is None:
        await message.answer(
            "🎙️ <b>Welcome to InnerVoice!</b>\n"
            "¡Bienvenido a InnerVoice!\n\n"
            "Please select your preferred language:\n"
            "Por favor, selecciona tu idioma preferido:",
            reply_markup=create_ui_language_keyboard(),
            parse_mode="HTML"
        )
        return
    
    # Show main welcome message in user's language
    ui_lang = prefs['ui_language']
    
    if ui_lang == 'es':
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
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Contacto: @arkano21\n\n"
            "<i>Apoya el desarrollo:</i>\n"
            "₿ bc1qwktevffc57rkk8lwyd6yqwxrvcd4vjxggcpsrn\n"
            "⚡ buffswan6@primal.net",
            parse_mode="HTML"
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
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Contact: @arkano21\n\n"
            "<i>Support development:</i>\n"
            "₿ bc1qwktevffc57rkk8lwyd6yqwxrvcd4vjxggcpsrn\n"
            "⚡ buffswan6@primal.net",
            parse_mode="HTML"
        )

@dp.message(Command("help"))
async def help_handler(message: types.Message):
    user_id = message.from_user.id
    prefs = user_preferences[user_id]
    lang_info = SUPPORTED_LANGUAGES[prefs['language']]
    mode_info = PROCESSING_MODES[prefs['mode']]
    
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
        "<b>🌐 Language Selection:</b>\n\n"
        "The language setting optimizes Whisper AI for your spoken language.\n"
        "This improves accuracy but doesn't limit auto-detection.\n\n"
        f"Current: {lang_info['flag']} {lang_info['name']}\n"
        f"Mode: {mode_info['icon']} {mode_info['name']}\n\n"
        "Change via /settings\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>💡 Tips:</b>\n\n"
        "• Speak clearly for best results\n"
        "• Long audios (15-30+ min) are supported\n"
        "• All messages use code blocks for easy copying\n"
        "• Enable timestamps in settings for time markers\n"
        "• Your audio is never stored permanently",
        parse_mode="HTML"
    )

@dp.message(Command("about"))
async def about_handler(message: types.Message):
    await message.answer(
        "🔐 <b>Privacy-First Voice Transcription</b>\n\n"
        "InnerVoice runs entirely on your own infrastructure, keeping your conversations "
        "completely private. No data is sent to external servers - everything happens locally.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🛠️ Technology Stack:</b>\n\n"
        "🤖 <b>OpenAI Whisper (Medium)</b>\n"
        "   State-of-the-art speech recognition\n"
        "   Runs 100% locally - no API calls\n\n"
        "🎵 <b>FFmpeg</b>\n"
        "   Professional audio processing\n"
        "   Handles any audio format\n\n"
        "🐍 <b>Python & aiogram</b>\n"
        "   Async processing for speed\n"
        "   Reliable Telegram integration\n\n"
        "🐳 <b>Docker</b>\n"
        "   Easy deployment & updates\n"
        "   Consistent environment\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>💪 Capabilities:</b>\n\n"
        "• Transcribe 30+ minute recordings\n"
        "• Support for 12docker + languages\n"
        "• Automatic language detection\n"
        "• Translation to English\n"
        "• Optional timestamps\n"
        "• Segment-based processing\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>⚡ Requirements:</b>\n\n"
        "• Modern laptop or PC (4+ GB RAM)\n"
        "• No GPU needed (CPU-only)\n"
        "• ~1.5GB disk space for Whisper model\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<b>📝 License & Source:</b>\n\n"
        "Open source - Self-hosted solution\n"
        "Your data, your control\n\n"
        "Made with ❤️ by @arkano21",
        parse_mode="HTML"
    )

@dp.message(Command("settings"))
async def settings_handler(message: types.Message):
    """Show comprehensive settings."""
    user_id = message.from_user.id
    prefs = user_preferences[user_id]
    
    await message.answer(
        "⚙️ <b>Your Settings</b>\n\n"
        "Configure your InnerVoice experience:",
        reply_markup=create_settings_keyboard(user_id),
        parse_mode="HTML"
    )

@dp.message(Command("lang"))
async def lang_handler(message: types.Message):
    """Quick language change."""
    user_id = message.from_user.id
    prefs = user_preferences[user_id]
    try:
        current_lang_info = SUPPORTED_LANGUAGES[prefs['language']]
        await message.answer(
            f"🌐 <b>Language Optimization</b>\n\n"
            f"Current: {current_lang_info['flag']} {current_lang_info['name']}\n\n"
            f"<i>This optimizes Whisper AI for your spoken language.</i>\n\n"
            "👇 Select your language:",
            reply_markup=create_language_keyboard(),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Error in lang handler: {e}")
        await message.answer("❌ Error showing language options. Please try again.")

@dp.message(Command("mode"))
async def mode_handler(message: types.Message):
    """Quick mode change."""
    user_id = message.from_user.id
    prefs = user_preferences[user_id]
    mode_info = PROCESSING_MODES[prefs['mode']]
    
    await message.answer(
        f"⚡ <b>Processing Mode</b>\n\n"
        f"Current: {mode_info['icon']} {mode_info['name']}\n"
        f"<i>{mode_info['description']}</i>\n\n"
        "👇 Select mode:",
        reply_markup=create_mode_keyboard(),
        parse_mode="HTML"
    )

@dp.callback_query(lambda c: c.data and c.data.startswith('ui_lang_'))
async def process_ui_language_callback(callback_query: types.CallbackQuery):
    """Handle UI language selection."""
    lang_code = callback_query.data.split('_')[2]  # ui_lang_es -> es
    user_id = callback_query.from_user.id
    try:
        if lang_code in ['es', 'en']:
            user_preferences[user_id]['ui_language'] = lang_code
            
            if lang_code == 'es':
                msg = (
                    "✅ ¡Idioma configurado a Español!\n\n"
                    "🎙️ Envíame un mensaje de voz para comenzar.\n\n"
                    "Usa /help para más información."
                )
            else:
                msg = (
                    "✅ Language set to English!\n\n"
                    "🎙️ Send me a voice message to get started.\n\n"
                    "Use /help for more information."
                )
            
            await callback_query.message.edit_text(msg, parse_mode="HTML")
        await callback_query.answer()
    except Exception as e:
        logging.error(f"Error in UI language callback: {e}")
        await callback_query.answer("❌ Error setting language.")

@dp.callback_query(lambda c: c.data and c.data.startswith('lang_'))
async def process_language_callback(callback_query: types.CallbackQuery):
    """Handle audio language selection."""
    lang_code = callback_query.data.split('_')[1]
    user_id = callback_query.from_user.id
    try:
        if lang_code in SUPPORTED_LANGUAGES:
            user_preferences[user_id]['language'] = lang_code
            lang_info = SUPPORTED_LANGUAGES[lang_code]
            ui_lang = user_preferences[user_id].get('ui_language', 'en')
            
            if ui_lang == 'es':
                msg = (
                    f"✅ <b>Idioma Actualizado</b>\n\n"
                    f"{lang_info['flag']} {lang_info['name']}\n\n"
                    f"<i>Whisper está ahora optimizado para audio en {lang_info['name']}.</i>\n\n"
                    f"🎙️ ¡Envía un mensaje de voz para probarlo!"
                )
            else:
                msg = (
                    f"✅ <b>Language Updated</b>\n\n"
                    f"{lang_info['flag']} {lang_info['name']}\n\n"
                    f"<i>Whisper is now optimized for {lang_info['name']} audio.</i>\n\n"
                    f"🎙️ Send a voice message to try it out!"
                )
            
            await callback_query.message.edit_text(msg, parse_mode="HTML")
        await callback_query.answer()
    except Exception as e:
        logging.error(f"Error in language callback: {e}")
        await callback_query.answer("❌ Error setting language.")

@dp.callback_query(lambda c: c.data and c.data.startswith('mode_'))
async def process_mode_callback(callback_query: types.CallbackQuery):
    """Handle mode selection."""
    mode = callback_query.data.split('_')[1]
    user_id = callback_query.from_user.id
    try:
        if mode in PROCESSING_MODES:
            user_preferences[user_id]['mode'] = mode
            mode_info = PROCESSING_MODES[mode]
            await callback_query.message.edit_text(
                f"✅ <b>Mode Updated</b>\n\n"
                f"{mode_info['icon']} {mode_info['name']}\n"
                f"<i>{mode_info['description']}</i>\n\n"
                f"🎙️ Ready to process your audio!",
                parse_mode="HTML"
            )
        await callback_query.answer()
    except Exception as e:
        logging.error(f"Error in mode callback: {e}")
        await callback_query.answer("❌ Error setting mode.")

@dp.callback_query(lambda c: c.data == 'change_lang')
async def change_lang_callback(callback_query: types.CallbackQuery):
    """Show language selection from settings."""
    user_id = callback_query.from_user.id
    prefs = user_preferences[user_id]
    current_lang_info = SUPPORTED_LANGUAGES[prefs['language']]
    await callback_query.message.edit_text(
        f"🌐 <b>Language Optimization</b>\n\n"
        f"Current: {current_lang_info['flag']} {current_lang_info['name']}\n\n"
        "👇 Select your language:",
        reply_markup=create_language_keyboard(),
        parse_mode="HTML"
    )
    await callback_query.answer()

@dp.callback_query(lambda c: c.data == 'change_mode')
async def change_mode_callback(callback_query: types.CallbackQuery):
    """Show mode selection from settings."""
    user_id = callback_query.from_user.id
    prefs = user_preferences[user_id]
    mode_info = PROCESSING_MODES[prefs['mode']]
    await callback_query.message.edit_text(
        f"⚡ <b>Processing Mode</b>\n\n"
        f"Current: {mode_info['icon']} {mode_info['name']}\n\n"
        "👇 Select mode:",
        reply_markup=create_mode_keyboard(),
        parse_mode="HTML"
    )
    await callback_query.answer()

@dp.callback_query(lambda c: c.data == 'toggle_stats')
async def toggle_stats_callback(callback_query: types.CallbackQuery):
    """Toggle statistics display."""
    user_id = callback_query.from_user.id
    user_preferences[user_id]['show_stats'] = not user_preferences[user_id]['show_stats']
    await callback_query.message.edit_reply_markup(
        reply_markup=create_settings_keyboard(user_id)
    )
    await callback_query.answer(
        f"Stats {'enabled' if user_preferences[user_id]['show_stats'] else 'disabled'}!"
    )

@dp.callback_query(lambda c: c.data == 'toggle_timestamps')
async def toggle_timestamps_callback(callback_query: types.CallbackQuery):
    """Toggle timestamp display."""
    user_id = callback_query.from_user.id
    user_preferences[user_id]['timestamps'] = not user_preferences[user_id]['timestamps']
    await callback_query.message.edit_reply_markup(
        reply_markup=create_settings_keyboard(user_id)
    )
    await callback_query.answer(
        f"Timestamps {'enabled' if user_preferences[user_id]['timestamps'] else 'disabled'}!"
    )

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    user_id = message.from_user.id
    prefs = user_preferences[user_id]
    
    # Check if user has selected UI language
    if prefs['ui_language'] is None:
        await message.answer(
            "Please select your language first using /start",
            reply_markup=create_ui_language_keyboard()
        )
        return
    
    voice = await bot.download(message.voice)
    file_id = message.voice.file_id
    file_path = os.path.join(AUDIO_DIR, f"{file_id}.ogg")

    with open(file_path, "wb") as f:
        f.write(voice.read())

    await audio_queue.put((user_id, file_id, file_path))

async def process_audio_chunk(segment: str, model, task: str = "transcribe", language: str = None, return_segments: bool = False) -> dict:
    """Process a single audio chunk with selected language.
    
    Args:
        segment: Path to audio segment
        model: Whisper model
        task: 'transcribe' or 'translate'
        language: Language code for optimization (None for auto-detect)
        return_segments: Whether to return segment-level timestamps
        
    Returns:
        dict with 'text' and optionally 'segments' keys
    """
    try:
        result = model.transcribe(
            str(segment), 
            task=task,
            fp16=False,
            language=language,
            verbose=False
        )
        output = {"text": result.get("text", "").strip()}
        if return_segments and "segments" in result:
            output["segments"] = result["segments"]
        return output
    except Exception as e:
        logging.error(f"Error processing chunk: {e}")
        raise

async def send_message_safe(user_id: int, text: str, parse_mode: str = None, reply_to_message_id: int = None) -> Optional[types.Message]:
    """Safely send a message to user with retry logic."""
    if not text or text.isspace():
        logging.warning(f"Attempted to send empty message to {user_id}")
        return None
        
    for attempt in range(3):
        try:
            msg = await asyncio.wait_for(
                bot.send_message(user_id, text, parse_mode=parse_mode, reply_to_message_id=reply_to_message_id),
                timeout=TELEGRAM_TIMEOUT
            )
            return msg
        except Exception as e:
            logging.error(f"Error sending message (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    return None

async def send_text_in_chunks(user_id: int, text: str, max_length: int = 4096) -> bool:
    """Send long text split into multiple messages, respecting Telegram's limits.
    Uses plain text (no code blocks) for better readability and easy copying.
    
    Args:
        user_id: Telegram user ID
        text: Text to send
        max_length: Maximum length per message (Telegram limit is 4096)
        
    Returns:
        bool: Success status
    """
    if not text or text.isspace():
        return False
    
    if len(text) <= max_length:
        # Single message - just send plain text
        await send_message_safe(user_id, text)
        return True
    
    # Split into chunks at paragraph/line boundaries
    chunks = []
    current_chunk = ""
    
    # Split by lines first to avoid breaking mid-sentence
    lines = text.split('\n')
    
    for line in lines:
        if len(current_chunk) + len(line) + 1 <= max_length:
            current_chunk += line + '\n'
        else:
            if current_chunk:
                chunks.append(current_chunk.rstrip())
            # If single line is too long, split it by sentences
            if len(line) > max_length:
                sentences = line.split('. ')
                temp = ""
                for sentence in sentences:
                    if len(temp) + len(sentence) + 2 <= max_length:
                        temp += sentence + '. '
                    else:
                        if temp:
                            chunks.append(temp.rstrip())
                        temp = sentence + '. '
                if temp:
                    current_chunk = temp
                else:
                    current_chunk = ""
            else:
                current_chunk = line + '\n'
    
    if current_chunk:
        chunks.append(current_chunk.rstrip())
    
    # Send chunks with part indicator if multiple
    for i, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            prefix = f"━━ Part {i}/{len(chunks)} ━━\n\n"
            await send_message_safe(user_id, prefix + chunk)
        else:
            await send_message_safe(user_id, chunk)
        await asyncio.sleep(0.2)  # Small delay between chunks
    
    return True

async def update_progress(user_id: int, message_id: int, current: int, total: int, elapsed_time: float, extra_info: str = "") -> None:
    """Update progress message with a progress bar.
    
    Args:
        user_id: Telegram user ID
        message_id: Message ID to edit
        current: Current segment number
        total: Total segments
        elapsed_time: Time elapsed in seconds
        extra_info: Additional information to display
    """
    try:
        percentage = (current / total) * 100 if total > 0 else 0
        filled = int(percentage / 10)
        bar = "▓" * filled + "░" * (10 - filled)
        
        # Estimate remaining time
        if current > 0:
            avg_time_per_segment = elapsed_time / current
            remaining_segments = total - current
            eta = avg_time_per_segment * remaining_segments
            eta_str = f"{int(eta)}s"
        else:
            eta_str = "calculating..."
        
        progress_text = (
            f"⚡ <b>Processing Audio</b>\n\n"
            f"Progress: {bar} {percentage:.0f}%\n"
            f"Segment: {current}/{total}\n"
            f"Elapsed: {elapsed_time:.1f}s\n"
            f"ETA: {eta_str}\n"
        )
        
        if extra_info:
            progress_text += f"\n{extra_info}"
        
        await bot.edit_message_text(
            progress_text,
            user_id,
            message_id,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"Error updating progress: {e}")

def count_tokens(text: str) -> int:
    """Count tokens in text using tiktoken."""
    try:
        encoding = tiktoken.encoding_for_model("gpt-3.5-turbo")
        return len(encoding.encode(text))
    except Exception:
        return len(text.split())

async def process_audio_async(user_id, file_id, file_path):
    """Main audio processing function with progress tracking and clean output."""
    wav_path = Path(file_path).with_suffix('.wav')
    prefs = user_preferences[user_id]
    progress_msg_id = None
    
    try:
        # Get audio info
        duration_result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ], capture_output=True, text=True)
        duration = float(duration_result.stdout.strip()) if duration_result.returncode == 0 else 0
        
        # Initial conversion
        if Path(file_path).exists():
            subprocess.run([
                "ffmpeg", "-i", file_path, 
                "-ac", "1", "-ar", "16000", 
                "-sample_fmt", "s16",
                str(wav_path), 
                "-y"
            ], check=True, capture_output=True)
        else:
            await send_message_safe(user_id, "❌ Audio file not found.")
            return
        
        # Split audio if needed
        segments = await split_audio(wav_path) if wav_path.stat().st_size > 1024 * 1024 else [wav_path]
        if not segments:
            await send_message_safe(user_id, "❌ Failed to process audio segments.")
            return
        
        # Get user preferences
        lang_info = SUPPORTED_LANGUAGES[prefs['language']]
        mode_info = PROCESSING_MODES[prefs['mode']]
        
        # Get translated text
        audio_received = get_text(user_id, 'audio_received')
        duration_text = get_text(user_id, 'duration')
        language_text = get_text(user_id, 'language')
        mode_text = get_text(user_id, 'mode')
        segments_text = get_text(user_id, 'segments')
        processing_text = get_text(user_id, 'processing')
        
        # Send initial status with progress
        status_msg = await send_message_safe(
            user_id,
            f"{audio_received}\n\n"
            f"{duration_text}: {int(duration)}s\n"
            f"{language_text}: {lang_info['flag']} {lang_info['name']}\n"
            f"{mode_text}: {mode_info['icon']} {mode_info['name']}\n"
            f"{segments_text}: {len(segments)}\n\n"
            f"{processing_text}",
            parse_mode="HTML"
        )
        
        if status_msg:
            progress_msg_id = status_msg.message_id
        
        start_time = time.time()
        full_transcription = ""
        full_translation = ""
        transcription_segments = []
        translation_segments = []
        
        # Process segments
        for i, segment in enumerate(segments, 1):
            try:
                # Update progress - always show, update every segment for short audio, every 2 for long
                if progress_msg_id:
                    elapsed = time.time() - start_time
                    # Update more frequently for short audio (<=5 segments), less for long
                    if len(segments) <= 5 or i % 2 == 0:
                        await update_progress(user_id, progress_msg_id, i-1, len(segments), elapsed)
                
                # Process based on mode
                if prefs['mode'] == 'fast':
                    # Fast mode: Only English translation
                    result = await process_audio_chunk(
                        segment, model, 
                        task="translate", 
                        language=prefs['language'],
                        return_segments=prefs['timestamps']
                    )
                    full_translation += result['text'] + " "
                    if prefs['timestamps'] and 'segments' in result:
                        translation_segments.extend(result['segments'])
                        
                else:
                    # Full mode: Both transcription and translation
                    trans_result = await process_audio_chunk(
                        segment, model, 
                        task="transcribe", 
                        language=prefs['language'],
                        return_segments=prefs['timestamps']
                    )
                    full_transcription += trans_result['text'] + " "
                    if prefs['timestamps'] and 'segments' in trans_result:
                        transcription_segments.extend(trans_result['segments'])
                    
                    transl_result = await process_audio_chunk(
                        segment, model, 
                        task="translate", 
                        language=prefs['language'],
                        return_segments=False
                    )
                    full_translation += transl_result['text'] + " "
                    
            except Exception as e:
                logging.error(f"Error processing segment {i}/{len(segments)}: {e}")
                continue
            finally:
                if segment != wav_path:  # Don't delete the original wav if it's the only segment
                    Path(segment).unlink(missing_ok=True)
        
        elapsed_time = time.time() - start_time
        
        # Final progress update
        if progress_msg_id:
            await update_progress(user_id, progress_msg_id, len(segments), len(segments), elapsed_time)
            await asyncio.sleep(0.5)
            # Delete progress message
            try:
                await bot.delete_message(user_id, progress_msg_id)
            except:
                pass
        
        # Format timestamps if enabled
        def format_with_timestamps(text, segments):
            if not segments:
                return text
            formatted = []
            for seg in segments:
                start_time = int(seg['start'])
                mins, secs = divmod(start_time, 60)
                timestamp = f"[{mins:02d}:{secs:02d}]"
                formatted.append(f"{timestamp} {seg['text'].strip()}")
            return '\n'.join(formatted)
        
        # Send results based on mode
        if prefs['mode'] == 'full':
            # Send transcription (original language)
            if full_transcription.strip():
                header_text = get_text(user_id, 'transcription_header')
                orig_lang_text = get_text(user_id, 'original_language')
                
                await send_message_safe(
                    user_id,
                    f"{header_text} ({lang_info['name']})\n<i>{orig_lang_text}</i>",
                    parse_mode="HTML"
                )
                
                if prefs['timestamps'] and transcription_segments:
                    text_to_send = format_with_timestamps(full_transcription, transcription_segments)
                else:
                    text_to_send = full_transcription.strip()
                
                # Send plain text (no code blocks) - easy to read and copy
                await send_text_in_chunks(user_id, text_to_send)
        
        # Send translation (always English)
        if full_translation.strip():
            if prefs['mode'] == 'full':
                header_text = get_text(user_id, 'translation_header')
            else:
                header_text = get_text(user_id, 'transcription_header')
            
            english_text = get_text(user_id, 'english')
            
            await send_message_safe(
                user_id,
                f"{header_text} ({english_text})",
                parse_mode="HTML"
            )
            
            if prefs['timestamps'] and translation_segments and prefs['mode'] == 'fast':
                text_to_send = format_with_timestamps(full_translation, translation_segments)
            else:
                text_to_send = full_translation.strip()
            
            # Send plain text (no code blocks) - easy to read and copy
            await send_text_in_chunks(user_id, text_to_send)
        
        # Send statistics if enabled
        if prefs['show_stats']:
            transcription_tokens = count_tokens(full_transcription) if full_transcription else 0
            translation_tokens = count_tokens(full_translation)
            
            complete_text = get_text(user_id, 'processing_complete')
            time_text = get_text(user_id, 'time')
            segments_text = get_text(user_id, 'segments')
            
            stats_msg = (
                f"{complete_text}\n\n"
                f"⏱️ {time_text}: {elapsed_time:.1f}s\n"
                f"🔢 {segments_text}: {len(segments)}\n"
            )
            
            if full_transcription:
                stats_msg += f"📝 {transcription_tokens} tokens\n"
            stats_msg += f"🔄 {translation_tokens} tokens"
            
            await send_message_safe(user_id, stats_msg, parse_mode="HTML")
        
    except Exception as e:
        logging.error(f"Error processing audio {file_id}: {e}")
        await send_message_safe(user_id, f"❌ Error processing audio: {str(e)}")
    finally:
        # Cleanup
        Path(file_path).unlink(missing_ok=True)
        if wav_path.exists():
            Path(wav_path).unlink(missing_ok=True)
        processing_states.pop(file_id, None)
        if file_id in progress_messages:
            del progress_messages[file_id]
        audio_queue.task_done()

async def split_audio(wav_path: Path) -> List[Path]:
    """Split audio into manageable chunks with overlap."""
    segments = []
    output_template = str(wav_path.with_name(f"{wav_path.stem}_part%d{wav_path.suffix}"))
    
    try:
        subprocess.run([
            "ffmpeg", "-i", str(wav_path),
            "-f", "segment",
            "-segment_time", str(CHUNK_SIZE_SECONDS),
            "-c", "copy",
            output_template
        ], check=True, capture_output=True)
        
        index = 0
        while True:
            segment_path = Path(output_template % index)
            if not segment_path.exists():
                break
            segments.append(segment_path)
            index += 1
            
        return segments
    except subprocess.CalledProcessError as e:
        logging.error(f"Error splitting audio: {e.stderr.decode()}")
        raise RuntimeError(f"Failed to split audio: {e}")

async def audio_worker():
    """Background worker to process audio files from the queue."""
    while True:
        try:
            user_id, file_id, file_path = await audio_queue.get()
            await process_audio_async(user_id, file_id, file_path)
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