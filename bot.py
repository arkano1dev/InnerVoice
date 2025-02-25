import os
import asyncio
import logging
import whisper
import subprocess
import psutil
import time
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

# Ensure bot instance is created first
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=None))
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

async def wait_for_cpu():
    """Pauses processing if CPU is overloaded."""
    while psutil.cpu_percent(interval=1) > 80:
        logging.warning("CPU usage high, waiting...")
        await send_message_safe(user_id, "⚠️ CPU usage is high. Waiting before processing...")
        await asyncio.sleep(5)

async def send_message_safe(user_id, text):
    try:
        await bot.send_message(user_id, text)
    except Exception as e:
        logging.error(f"Error sending message: {e}")

async def process_audio_async(user_id, file_id, file_path):
    try:
        await send_message_safe(user_id, "✅ Audio received, processing...")
        await wait_for_cpu()

        wav_path = f"{file_path}.wav"
        start_time = time.time()
        
        if os.path.exists(file_path):
            subprocess.run([
                "ionice", "-c3", "ffmpeg", "-i", file_path, "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", wav_path, "-y"
            ], check=True)
        else:
            await send_message_safe(user_id, "❌ Audio file missing.")
            return
        
        await wait_for_cpu()
        cpu_before = psutil.cpu_percent()
        ram_before = psutil.virtual_memory().used / (1024 * 1024)
        
        result = model.transcribe(wav_path, fp16=False)
        text = result["text"]
        
        translation = model.transcribe(wav_path, task="translate", fp16=False)["text"]
        
        cpu_after = psutil.cpu_percent()
        ram_after = psutil.virtual_memory().used / (1024 * 1024)
        elapsed_time = time.time() - start_time
        
        await send_message_safe(user_id, f"🎙️ Transcription: \n{text.strip()}\n\n🌍 Translation: \n{translation.strip()}")
        await send_message_safe(user_id, f"🕒 Processing Time: {elapsed_time:.2f}s\n🖥️ CPU Usage Before/After: {cpu_before}% / {cpu_after}%\n💾 RAM Usage Before/After: {ram_before:.2f}MB / {ram_after:.2f}MB")
    
    except Exception as e:
        logging.error(f"Error processing audio {file_id}: {e}")
        await send_message_safe(user_id, f"❌ Error processing audio: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
        if os.path.exists(wav_path):
            os.remove(wav_path)
        audio_queue.task_done()

async def audio_worker():
    while True:
        user_id, file_id, file_path = await audio_queue.get()
        await process_audio_async(user_id, file_id, file_path)

@dp.message(CommandStart())
async def start(message: types.Message):
    await message.answer("Send a voice message and I'll transcribe it.")

@dp.message(F.voice)
async def handle_voice(message: types.Message):
    user_id = message.from_user.id
    voice = await bot.download(message.voice)
    file_id = message.voice.file_id
    file_path = os.path.join(AUDIO_DIR, f"{file_id}.ogg")

    with open(file_path, "wb") as f:
        f.write(voice.read())

    await audio_queue.put((user_id, file_id, file_path))

async def main():
    asyncio.create_task(audio_worker())
    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        print("\n🛑 Bot manually stopped.")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    os.nice(10)  # Set lower process priority
    asyncio.run(main())
