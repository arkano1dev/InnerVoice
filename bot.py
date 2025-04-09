import os
import asyncio
import logging
import whisper
import subprocess
import psutil
import time
import shutil
import tiktoken
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
from datetime import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass
from pathlib import Path
from contextlib import suppress

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

# Logging configuration
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

# Queue to process audios in order
audio_queue = asyncio.Queue()

@dataclass
class SystemResources:
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    temperature: Optional[float] = None

    def is_healthy(self, config: 'ProcessingConfig') -> bool:
        return (
            self.cpu_percent < config.max_cpu_percent and
            self.memory_percent < config.max_memory_percent and
            self.disk_percent < config.max_disk_percent
        )

class CPUMonitor:
    def __init__(self):
        self.max_cpu = 0
        self.max_memory = 0
        self._running = True
        
    async def monitor(self):
        while self._running:
            # Get per-CPU usage with shorter interval
            cpu_percent = max(psutil.cpu_percent(interval=1, percpu=True))
            memory = psutil.virtual_memory().percent
            self.max_cpu = max(self.max_cpu, cpu_percent)
            self.max_memory = max(self.max_memory, memory)
            await asyncio.sleep(1)  # Shorter sleep for better accuracy
    
    def stop(self):
        self._running = False

class ProcessingConfig:
    def __init__(self):
        self.max_cpu_percent: float = 85.0  # Slightly higher threshold for CPU
        self.max_memory_percent: float = 90.0
        self.max_disk_percent: float = 90.0
        self.min_free_space_mb: int = 500
        self.chunk_size_seconds: int = 30  
        self.max_retries: int = 3
        self.backoff_base: int = 5
        self.checkpoint_interval: int = 300

# Global configuration
config = ProcessingConfig()
processing_states: Dict[str, Dict] = {}

async def get_system_resources() -> SystemResources:
    """Get current system resource usage."""
    cpu = psutil.cpu_percent()
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage(AUDIO_DIR).percent
    
    try:
        temperature = psutil.sensors_temperatures().get('coretemp', [{}])[0].current
    except:
        temperature = None
        
    return SystemResources(cpu, memory, disk, temperature)

async def wait_for_resources(user_id: int):
    """Enhanced resource monitoring with backoff."""
    attempt = 0
    while True:
        resources = await get_system_resources()
        
        if resources.is_healthy(config):
            return
        
        backoff_time = config.backoff_base * (2 ** attempt)
        status_msg = (
            f"⚠️ System under load:\n"
            f"CPU: {resources.cpu_percent:.1f}%\n"
            f"Memory: {resources.memory_percent:.1f}%\n"
            f"Disk: {resources.disk_percent:.1f}%\n"
            f"Waiting {backoff_time}s before retry..."
        )
        
        if resources.temperature:
            status_msg += f"\nTemperature: {resources.temperature:.1f}°C"
            
        await send_message_safe(user_id, status_msg)
        await asyncio.sleep(backoff_time)
        attempt = min(attempt + 1, 4)  # Max backoff of 5 * 2^4 = 80 seconds

async def ensure_disk_space(required_mb: int = None) -> bool:
    """Ensure sufficient disk space is available."""
    if required_mb is None:
        required_mb = config.min_free_space_mb
        
    free_space_mb = psutil.disk_usage(AUDIO_DIR).free / (1024 * 1024)
    return free_space_mb >= required_mb

async def process_audio_chunk(segment: str, model, task: str = "transcribe", cpu_monitor: CPUMonitor = None) -> str:
    """Process a single audio chunk optimized for CPU."""
    try:
        # Basic Whisper settings for better performance
        result = model.transcribe(
            str(segment), 
            task=task,
            fp16=False,
            language='es'  # Set if you know the language
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
        
    for attempt in range(3):  # Try 3 times
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
                            await asyncio.sleep(0.5)  # Rate limiting prevention
            else:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        bot.send_message(user_id, text),
                        timeout=TELEGRAM_TIMEOUT
                    )
            return True
        except Exception as e:
            logging.error(f"Error sending message (attempt {attempt + 1}/3): {e}")
            if attempt < 2:  # Don't sleep on last attempt
                await asyncio.sleep(2 ** attempt)
    return False

async def send_heartbeat(user_id: int, file_id: str):
    """Send periodic updates to keep the connection alive."""
    while file_id in processing_states:
        try:
            state = processing_states[file_id]
            if state['status'].startswith('processing_segment'):
                progress = (state['segments_processed'] / state['total_segments'] * 100)
                msg = f"🔄 Still processing...\nProgress: {progress:.1f}%"
                await send_message_safe(user_id, msg)
        except Exception as e:
            logging.warning(f"Heartbeat error: {e}")
        finally:
            await asyncio.sleep(PROCESSING_UPDATE_INTERVAL)

def count_tokens(text: str, model: str = "gpt-4"):
    """Count the number of tokens in a given text for a specified model."""
    encoding = tiktoken.encoding_for_model(model)
    return len(encoding.encode(text))


async def process_audio_async(user_id, file_id, file_path):
    wav_path = Path(file_path).with_suffix('.wav')
    cpu_monitor = CPUMonitor()
    
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
            await send_message_safe(user_id, "❌ Audio file missing.")
            return
        
        segments = await split_audio(wav_path) if wav_path.stat().st_size > 1024 * 1024 else [wav_path]
        if not segments:
            await send_message_safe(user_id, "❌ No audio segments were created.")
            return
            
        # Calculate initial ETA (assuming ~30 seconds per segment as baseline)
        estimated_time = len(segments) * 30  
        
        # Initial processing status message
        initial_msg = (
            f"✅ Audio received\n"
            f"📊 Segments to process: {len(segments)}\n"
            f"⏱️ Estimated time: {estimated_time:.2f}s\n"
            f"🔄 Processing..."
        )
        await send_message_safe(user_id, initial_msg)
        
        # Start CPU monitoring
        start_time = time.time()
        resources_before = await get_system_resources()
        monitor_task = asyncio.create_task(cpu_monitor.monitor())
        
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
        
        # Stop CPU monitoring
        cpu_monitor.stop()
        await monitor_task
        
        # Calculate elapsed time and resource usage
        elapsed_time = time.time() - start_time
        resources_after = await get_system_resources()
        
        # Send results
        if full_transcription.strip():
            await send_message_safe(user_id, "📄 Transcription:")
            await send_message_safe(user_id, full_transcription.strip())
        
        if full_translation.strip():
            await send_message_safe(user_id, "🌍 Translation:")
            await send_message_safe(user_id, full_translation.strip())
        
        # Processing statistics
        stats_msg = (
            f"📊 Processing Statistics:\n"
            f"🕒 Total Time: {elapsed_time:.2f}s\n"
            f"📊 Segments: {len(segments)}\n"
            f"📝 Transcription Tokens: {transcription_tokens}\n"
            f"🌍 Translation Tokens: {translation_tokens}\n"
            f"🖥️ CPU: {resources_before.cpu_percent:.1f}% ➡️ {resources_after.cpu_percent:.1f}%\n"
            f"💾 RAM: {resources_before.memory_percent:.1f}% ➡️ {resources_after.memory_percent:.1f}%\n"
            f"💿 Disk: {resources_before.disk_percent:.1f}%"
        )
        
        if resources_before.temperature and resources_after.temperature:
            stats_msg += f"\n🌡️ Temp: {resources_before.temperature:.1f}°C ➡️ {resources_after.temperature:.1f}°C"
        
        await send_message_safe(user_id, stats_msg)
        
    except Exception as e:
        logging.error(f"Error processing audio {file_id}: {e}")
        await send_message_safe(user_id, f"❌ Error processing audio: {e}")
    finally:
        Path(file_path).unlink(missing_ok=True)
        Path(wav_path).unlink(missing_ok=True)
        processing_states.pop(file_id, None)
        audio_queue.task_done()


async def audio_worker():
    while True:
        user_id, file_id, file_path = await audio_queue.get()
        await process_audio_async(user_id, file_id, file_path)

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    lang = message.from_user.language_code
    if lang == "es":
        await message.answer(
            "👋 ¡Bienvenido a InnerVoice!\n\n"
            "🎙️ Envíame un mensaje de voz y te enviaré la transcripción y una traducción automática.\n"
            "✅ Simple, rápido y gratuito.\n\n"
            "¡Pruébalo ahora!"
        )
    else:
        await message.answer(
            "👋 Welcome to InnerVoice!\n\n"
            "🎙️ Send me a voice message, and I’ll transcribe and translate it into Spanish.\n"
            "✅ Simple, fast, and free.\n\n"
            "Try it now!"
        )

@dp.message(Command("help"))
async def help_handler(message: types.Message):
    lang = message.from_user.language_code
    if lang == "es":
        await message.answer(
            "ℹ️ *¿Cómo usar InnerVoice?*\n\n"
            "1. Graba y envía un mensaje de voz.\n"
            "2. Recibirás el texto transcrito y una traducción al inglés.\n"
            "3. Úsalo para estudiar, guardar ideas, o comunicarte mejor.\n\n"
            "📌 Solo funciona con mensajes de voz, no llamadas ni videos.",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "ℹ️ *How to use InnerVoice?*\n\n"
            "1. Record and send a voice message.\n"
            "2. You’ll get a transcription and a translation into spanish.\n"
            "3. Use it for studying, capturing ideas, or better communication.\n\n"
            "📌 Only works with voice messages, not calls or videos.",
            parse_mode="Markdown"
        )

@dp.message(Command("about"))
async def about_handler(message: types.Message):
    lang = message.from_user.language_code
    if lang == "es":
        await message.answer(
            "📚 *Acerca de InnerVoice*\n\n"
            "Este bot convierte tus mensajes de voz en texto y luego traduce ese texto automáticamente.\n\n"
            "Usa modelos de inteligencia artificial para ofrecer transcripciones precisas y traducciones confiables.\n\n"
            "🔗 Código fuente: [github.com/arkano1dev/InnerVoice](https://github.com/arkano1dev/InnerVoice)",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "📚 *About InnerVoice*\n\n"
            "This bot converts your voice messages into text and then automatically translates that text.\n\n"
            "It uses AI models to provide accurate transcriptions and reliable translations.\n\n"
            "🔗 Source code: [github.com/arkano1dev/InnerVoice](https://github.com/arkano1dev/InnerVoice)",
            parse_mode="Markdown"
        )
@dp.message(Command("contribute"))
async def contribute_handler(message: types.Message):
    lang = message.from_user.language_code
    if lang == "es":
        await message.answer(
            "💖 *Apoya el desarrollo de InnerVoice*\n\n"
            "Si encuentras útil este bot y quieres apoyar su mantenimiento y mejora, puedes contribuir con una donación:\n\n"
            "🟠 *Bitcoin (on-chain)*: `bc1qwktevffc57rkk8lwyd6yqwxrvcd4vjxggcpsrn`\n"
            "⚡ *Lightning Address*: `buffswan6@primal.net`\n"
            "⚡ *Nostr*: `npub1p2x3t3njq44vsk24qjkauzurvfd59c224qyu2mpgu9jverk9tfrqnz0ql5`\n\n"
            "¡Gracias por tu apoyo! 🙌",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "💖 *Support InnerVoice Development*\n\n"
            "If you find this bot useful and want to help keep it running and improving, consider a small donation:\n\n"
            "🟠 *Bitcoin (on-chain)*: `bc1qwktevffc57rkk8lwyd6yqwxrvcd4vjxggcpsrn`\n"
            "⚡ *Lightning Address*: `buffswan6@primal.net`\n"
            "⚡ *Nostr*: `npub1p2x3t3njq44vsk24qjkauzurvfd59c224qyu2mpgu9jverk9tfrqnz0ql5`\n\n"
            "Thanks for your support! 🙌",
            parse_mode="Markdown"
        )


@dp.message(F.voice)
async def handle_voice(message: types.Message):
    user_id = message.from_user.id
    voice = await bot.download(message.voice)
    file_id = message.voice.file_id
    file_path = os.path.join(AUDIO_DIR, f"{file_id}.ogg")

    with open(file_path, "wb") as f:
        f.write(voice.read())

    await audio_queue.put((user_id, file_id, file_path))

async def split_audio(wav_path: Path) -> List[Path]:
    """Split audio into manageable chunks with overlap."""
    segments = []
    output_template = str(wav_path.with_name(f"{wav_path.stem}_part%d{wav_path.suffix}"))
    
    try:
        subprocess.run([
            "ffmpeg", "-i", str(wav_path),
            "-f", "segment",
            "-segment_time", str(config.chunk_size_seconds),
            "-c", "copy",
            output_template
        ], check=True, capture_output=True)
        
        # Collect all generated segments
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

async def main():
    audio_worker_task = asyncio.create_task(audio_worker())
    
    while True:
        try:
            await dp.start_polling(bot)
        except Exception as e:
            logging.error(f"Polling error: {e}")
            await asyncio.sleep(5)  # Wait before retry
        finally:
            if not audio_worker_task.done():
                audio_worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await audio_worker_task

if __name__ == "__main__":
    os.nice(10)  # Set lower process priority
    asyncio.run(main())