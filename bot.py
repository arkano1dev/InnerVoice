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
from typing import Dict, List
from pathlib import Path
from contextlib import suppress

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
    'es': {'name': 'Spanish', 'local': 'Español'},
    'en': {'name': 'English', 'local': 'English'},
    'fr': {'name': 'French', 'local': 'Français'},
    'nl': {'name': 'Dutch', 'local': 'Nederlands'},
    'pt': {'name': 'Portuguese', 'local': 'Português'},
    'de': {'name': 'German', 'local': 'Deutsch'}
}

# Default language
current_language = 'es'

# Global configuration
CHUNK_SIZE_SECONDS = 30
processing_states: Dict[str, Dict] = {}

def create_language_keyboard() -> InlineKeyboardMarkup:
    """Create inline keyboard for language selection."""
    keyboard = []
    for code, info in SUPPORTED_LANGUAGES.items():
        button_text = f"{info['local']} ({info['name']})"
        keyboard.append([InlineKeyboardButton(text=button_text, callback_data=f"lang_{code}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    # Spanish message
    await message.answer(
        "¡Bienvenido a InnerVoice!\n\n"
        "Envíame un mensaje de voz y te enviaré la transcripción y traducción automática.\n"
        "Simple, rápido y gratuito.\n\n"
        "Comandos:\n"
        "/lang - Cambiar idioma\n"
        "/help - Ayuda\n"
        "/about - Acerca de"
    )
    
    # English message
    await message.answer(
        "Welcome to InnerVoice!\n\n"
        "Send me a voice message, and I'll transcribe and translate it.\n"
        "Simple, fast, and free.\n\n"
        "Commands:\n"
        "/lang - Change language\n"
        "/help - Help\n"
        "/about - About"
    )
    
    # Contact info
    await message.answer(
        "Contact: @arkano21\n\n"
        "Support development:\n"
        "Bitcoin: bc1qwktevffc57rkk8lwyd6yqwxrvcd4vjxggcpsrn\n"
        "Lightning: buffswan6@primal.net\n"
        "Nostr: npub1p2x3t3njq44vsk24qjkauzurvfd59c224qyu2mpgu9jverk9tfrqnz0ql5"
    )

@dp.message(Command("help"))
async def help_handler(message: types.Message):
    # Spanish help
    await message.answer(
        "¿Cómo usar InnerVoice?\n\n"
        "1. Graba y envía un mensaje de voz\n"
        "2. Recibirás el texto transcrito y una traducción\n"
        "3. Úsalo para estudiar, guardar ideas, o comunicarte mejor\n\n"
        "Cambio de Idioma:\n"
        "- Usa /lang para ver las opciones\n"
        "- Por defecto: Español\n\n"
        "Requisitos:\n"
        "- Cualquier portátil o PC moderno\n"
        "- 4GB RAM mínimo\n"
        "- No requiere GPU"
    )
    
    # English help
    await message.answer(
        "How to use InnerVoice?\n\n"
        "1. Record and send a voice message\n"
        "2. You'll get a transcription and translation\n"
        "3. Use it for studying, capturing ideas, or communication\n\n"
        "Language Settings:\n"
        "- Use /lang to see options\n"
        "- Default: Spanish\n\n"
        "Requirements:\n"
        "- Any modern laptop or PC\n"
        "- 4GB RAM minimum\n"
        "- No GPU needed"
    )

@dp.message(Command("about"))
async def about_handler(message: types.Message):
    # Spanish about
    await message.answer(
        "Transcripción de Voz con Privacidad\n\n"
        "Este bot se ejecuta en tu propia computadora, manteniendo tus datos bajo tu control.\n"
        "Utiliza el modelo Whisper de OpenAI localmente, requiriendo solo un portátil básico para "
        "proporcionar transcripciones y traducciones precisas.\n\n"
        "Tecnologías:\n"
        "- OpenAI Whisper (IA local)\n"
        "- FFmpeg (procesamiento de audio)\n"
        "- Python y Docker (fácil configuración)"
    )
    
    # English about
    await message.answer(
        "Privacy-First Voice Transcription\n\n"
        "This bot runs on your own computer, keeping your data under your control.\n"
        "It uses OpenAI's Whisper model locally, requiring only a basic laptop to "
        "provide accurate transcriptions and translations.\n\n"
        "Technology:\n"
        "- OpenAI Whisper (local AI)\n"
        "- FFmpeg (audio processing)\n"
        "- Python & Docker (easy setup)"
    )

@dp.message(Command("lang"))
async def lang_handler(message: types.Message):
    try:
        current_lang_info = SUPPORTED_LANGUAGES[current_language]
        await message.answer(
            f"Current/Actual: {current_lang_info['local']}\n"
            "Select language / Seleccione idioma:",
            reply_markup=create_language_keyboard()
        )
    except Exception as e:
        logging.error(f"Error in lang handler: {e}")
        await message.answer("Error showing language options. Please try again.")

@dp.callback_query(lambda c: c.data and c.data.startswith('lang_'))
async def process_language_callback(callback_query: types.CallbackQuery):
    lang_code = callback_query.data.split('_')[1]
    try:
        if lang_code in SUPPORTED_LANGUAGES:
            global current_language
            current_language = lang_code
            lang_info = SUPPORTED_LANGUAGES[lang_code]
            await callback_query.message.edit_text(
                f"Language set to: {lang_info['local']} ({lang_info['name']})\n"
                f"Send a voice message to try it out!"
            )
        await callback_query.answer()
    except Exception as e:
        logging.error(f"Error in language callback: {e}")
        await callback_query.answer("Error setting language. Please try again.")

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    user_id = message.from_user.id
    voice = await bot.download(message.voice)
    file_id = message.voice.file_id
    file_path = os.path.join(AUDIO_DIR, f"{file_id}.ogg")

    with open(file_path, "wb") as f:
        f.write(voice.read())

    await audio_queue.put((user_id, file_id, file_path))

async def process_audio_chunk(segment: str, model, task: str = "transcribe") -> str:
    """Process a single audio chunk with selected language."""
    try:
        result = model.transcribe(
            str(segment), 
            task=task,
            fp16=False,
            language=current_language
        )
        return result.get("text", "").strip() + " "
    except Exception as e:
        logging.error(f"Error processing chunk: {e}")
        raise

async def send_message_safe(user_id: int, text: str) -> bool:
    """Safely send a message to user with retry logic."""
    if not text or text.isspace():
        logging.warning(f"Attempted to send empty message to {user_id}")
        return False
        
    for attempt in range(3):
        try:
            if len(text) > 4000:
                chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
                for chunk in chunks:
                    if chunk.strip():
                        with suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(
                                bot.send_message(user_id, chunk),
                                timeout=TELEGRAM_TIMEOUT
                            )
                            await asyncio.sleep(0.5)
            else:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        bot.send_message(user_id, text),
                        timeout=TELEGRAM_TIMEOUT
                    )
            return True
        except Exception as e:
            logging.error(f"Error sending message (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    return False

async def process_audio_async(user_id, file_id, file_path):
    wav_path = Path(file_path).with_suffix('.wav')
    
    try:
        # Initial conversion and segmentation
        if Path(file_path).exists():
            subprocess.run([
                "ffmpeg", "-i", file_path, 
                "-ac", "1", "-ar", "16000", 
                "-sample_fmt", "s16",
                str(wav_path), 
                "-y"
            ], check=True, capture_output=True)
        else:
            await send_message_safe(user_id, "Audio file missing.")
            return
        
        segments = await split_audio(wav_path) if wav_path.stat().st_size > 1024 * 1024 else [wav_path]
        if not segments:
            await send_message_safe(user_id, "No audio segments were created.")
            return
            
        # Calculate initial ETA
        estimated_time = len(segments) * 30  
        
        await send_message_safe(user_id, 
            f"Audio received\n"
            f"Segments to process: {len(segments)}\n"
            f"Estimated time: {estimated_time:.2f}s\n"
            f"Processing..."
        )
        
        start_time = time.time()
        full_transcription = ""
        full_translation = ""
        transcription_tokens = 0
        translation_tokens = 0
        
        for i, segment in enumerate(segments, 1):
            try:
                transcription = await process_audio_chunk(segment, model)
                translation = await process_audio_chunk(segment, model, task="translate")
                
                if transcription.strip():
                    full_transcription += transcription
                    transcription_tokens += count_tokens(transcription)

                if translation.strip():
                    full_translation += translation
                    translation_tokens += count_tokens(translation)
                    
            except Exception as e:
                logging.error(f"Error processing segment {i}: {e}")
                continue
            finally:
                Path(segment).unlink(missing_ok=True)
        
        elapsed_time = time.time() - start_time
        
        if full_transcription.strip():
            await send_message_safe(user_id, "Transcription:")
            await send_message_safe(user_id, full_transcription.strip())
        
        if full_translation.strip():
            await send_message_safe(user_id, "Translation:")
            await send_message_safe(user_id, full_translation.strip())
        
        await send_message_safe(user_id,
            f"Processing Statistics:\n"
            f"Total Time: {elapsed_time:.2f}s\n"
            f"Segments: {len(segments)}\n"
            f"Transcription Tokens: {transcription_tokens}\n"
            f"Translation Tokens: {translation_tokens}"
        )
        
    except Exception as e:
        logging.error(f"Error processing audio {file_id}: {e}")
        await send_message_safe(user_id, f"Error processing audio: {e}")
    finally:
        Path(file_path).unlink(missing_ok=True)
        Path(wav_path).unlink(missing_ok=True)
        processing_states.pop(file_id, None)
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