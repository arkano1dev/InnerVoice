# InnerVoice Bot - Complete Documentation

**Version**: 3.0.0  
**Last Updated**: February 2025  
**Status**: Production Ready ✅

---

## 📋 Table of Contents

1. [Quick Start](#quick-start)
2. [Two-Docker Deployment (Whisper + Bot)](#two-docker-deployment)
3. [What's New in v3](#whats-new-in-v3)
4. [Complete Feature List](#complete-feature-list)
4. [User Guide](#user-guide)
5. [Commands Reference](#commands-reference)
6. [Technical Details](#technical-details)
7. [Deployment](#deployment)
8. [Troubleshooting](#troubleshooting)

---

## Quick Start

### First Time Setup

1. **Configure** `.env` with your `BOT_TOKEN`
2. **Start both services**:
```bash
cd /path/to/InnerVoice
make up
# or: ./deploy.sh up
# or: docker compose up -d --build
```
3. **Open Telegram** and send `/start` to your bot
4. **Send a voice message** - first audio works immediately (default Spanish)

### What You'll Get

- 📝 **Transcription** in original language (if Full mode)
- 🌐 **Translation** to English (always)
- 🎯 **Plain text** format - click to copy
- ⚡ **Real-time progress** updates
- 🔒 **100% private** - all processing local

---

## Two-Docker Deployment

InnerVoice runs as **two separate containers** on the `TelegramNet` network:

| Service | Role | Limits | Hardware |
|---------|------|--------|----------|
| **whisper** | Whisper API (Gunicorn) | 5G RAM, 4 CPUs | AMD GPU (ROCm) |
| **bot** | Telegram bot (aiogram) | 512M RAM | CPU only |

### Architecture

- **Whisper container**: Uses model from `WHISPER_MODEL` (default in compose: **medium**). **Lazy-loads** on first transcription so the server binds to port 9000 immediately. Exposes `/transcribe`, `/health`, `/gpu-check`. Runs on **AMD GPU via ROCm** (image: `rocm/pytorch:latest`) with Gunicorn. Compose uses AMD-recommended options: `seccomp=unconfined`, `SYS_PTRACE`, `ipc=host`, `shm_size: 2g`, `/dev/kfd`, `/dev/dri`, `video` and `render` groups.
- **Bot container**: Receives voice from Telegram, converts OGG→WAV with ffmpeg, sends WAV to Whisper API via HTTP. Lightweight, no GPU.
- **Network**: Both join `TelegramNet`; bot reaches Whisper at `http://whisper:9000`.

### Deploy Commands

Run from the **project root** (directory containing `docker-compose.yml`); otherwise you may see "no configuration file provided".

```bash
# Start both
make up
./deploy.sh up

# Start only bot (Whisper must already be running)
make up-bot
./deploy.sh up-bot

# Start only Whisper (e.g. for other apps)
make up-whisper
./deploy.sh up-whisper

# Stop all
make down
./deploy.sh down

# Logs
make logs
make logs-bot
make logs-whisper
```

### ROCm / GPU Setup

**Default is GPU (ROCm).** The compose file uses AMD's recommended run options for the prebuilt `rocm/pytorch` image:

- `security_opt: seccomp=unconfined`, `cap_add: SYS_PTRACE`, `ipc: host`, `shm_size: 8g`
- `devices: /dev/kfd`, `/dev/dri`, `group_add: video`, `group_add: render`
- `ROCR_VISIBLE_DEVICES=0`, `HSA_OVERRIDE_GFX_VERSION=11.0.0` (compose default for APU/iGPU; change if needed)
- `WHISPER_MODEL=medium`, `VRAM_THRESHOLD_FREE_MB=1536` (compose default; use `tiny` or `small` if you have less VRAM)

**Host requirements:** AMD GPU with ROCm support, `/dev/kfd` and `/dev/dri` available, user in `video` and `render` groups.

**CPU fallback:** If Whisper keeps restarting or you have no AMD GPU, use CPU: in `docker-compose.yml` set `build.dockerfile: Dockerfile.cpu` for the whisper service and remove the ROCm-specific options (security_opt, cap_add, ipc, shm_size, devices, group_add, ROCR_VISIBLE_DEVICES).

If you run **Ollama** on the same GPU, Whisper is **lower priority**:

- Whisper checks VRAM before processing; if free VRAM &lt; threshold (`VRAM_THRESHOLD_FREE_MB`, default 1024 in compose), returns `503 gpu_busy`
- Bot shows “Whisper is busy” and a **Retry** button
- Ollama takes precedence; when Ollama frees VRAM, use Retry

To use a different GPU (e.g. eGPU on device 1), set `ROCR_VISIBLE_DEVICES=1` (or the desired index) in the whisper service `environment` in `docker-compose.yml`.

### Resource Limits

- **Whisper**: 5G memory, 4 CPUs (tune in `docker-compose.yml` if needed)
- **Bot**: 512M memory, 0.5 CPU
- `VRAM_THRESHOLD_FREE_MB`: compose default 1536 (for medium model); increase if using a larger model

### Reusing Whisper

The Whisper container can be used by other apps: it exposes `http://whisper:9000` (or `localhost:9000` if port is published) with:

- `POST /transcribe` – multipart form: `audio` (WAV), optional `language`, `task` (transcribe/translate), `return_segments`. Model is loaded on first request (lazy).
- `GET /health` – `status`, `model`; `vram_used_mb` / `vram_total_mb` only after the model has been loaded
- `GET /gpu-check` – diagnostic: GPU probe without loading model (for ROCm troubleshooting)

---

## What's New in v3

### Two-Docker Architecture
- **Whisper** and **Bot** run in separate containers on `TelegramNet`
- Whisper uses ROCm/eGPU; Bot is lightweight (CPU only)
- Deploy separately: `make up-bot`, `make up-whisper`
- Whisper API reusable for other apps

### Default Spanish & First-Audio Flow
- Default UI and audio language: **Spanish**
- First voice message works immediately (no language selection required)
- Change UI language in `/settings`

### Retry When Busy
- If GPU/VRAM is loaded (e.g. Ollama), Whisper returns "busy"
- Bot shows message + **Retry** button
- Click Retry when GPU is free (no need to resend from Telegram)

### Duplicate Audio Guard
- Same audio within 60 seconds → skipped (avoids double processing)
- Same audio after 60 seconds → processed (for testing)

### GPU Stats
- In Full mode stats: VRAM usage from Whisper `/health`

---

## What's New in v2.1

### 🎯 Major Improvements

#### 1. Plain Text Messages (No Code Blocks)
**Before**: Text in code blocks with "Copy Code" button  
**Now**: Clean plain text, click anywhere to copy

**Why This Matters**:
- ✅ Much more readable
- ✅ Easier to copy
- ✅ Professional appearance
- ✅ No annoying buttons

**Example**:
```
🎤 Transcription (Spanish)
Original language

Hello this is my transcription text that you can easily read
and copy by simply clicking on it. No more code blocks!
```

#### 2. Spanish/English UI
**Before**: All bot messages in English only  
**Now**: Choose Spanish or English interface

**What Changes Language**:
- ✅ All command responses
- ✅ Status messages (Audio received, Processing...)
- ✅ Headers (Transcription, Translation)
- ✅ Help and information messages
- ✅ Statistics and completion messages
- ✅ Error messages

**What Doesn't Change**:
- ❌ Transcription content (always in original language)
- ❌ Translation content (always in English)

#### 3. Always-Visible Progress Bar
**Before**: Sometimes missing for short audio  
**Now**: Always shows, updates in real-time

**What You See**:
```
⚡ Processing Audio

Progress: ▓▓▓▓▓░░░░░ 50%
Segment: 5/10
Elapsed: 45.2s
ETA: 43s
```

#### 4. Optimized for Personal Use
- Single user queue (no parallel overhead)
- More reliable processing
- Perfect for internal tools

---

## Complete Feature List

### Core Features

#### 🎤 Voice Transcription
- **Accuracy**: OpenAI Whisper (Medium model)
- **Languages**: 12+ languages supported
- **Quality**: State-of-the-art speech recognition
- **Speed**: ~30 seconds per 30 seconds of audio

#### 🌐 Translation
- **Target**: Always English
- **Source**: Any of 12+ supported languages
- **Method**: Whisper's built-in translation
- **Quality**: Native-level accuracy

#### 📝 Processing Modes

**Fast Mode (🚀)**:
- Only English translation
- ~50% faster
- Best for: Quick notes, when original doesn't matter

**Full Mode (📝)**:
- Original transcription + English translation
- Two separate messages
- Best for: Learning, documentation, reference

#### 🌍 Supported Languages

| Language | Code | Optimization |
|----------|------|--------------|
| Spanish | es | ✅ Default |
| English | en | ✅ |
| French | fr | ✅ |
| Dutch | nl | ✅ |
| Portuguese | pt | ✅ |
| German | de | ✅ |
| Italian | it | ✅ |
| Japanese | ja | ✅ |
| Korean | ko | ✅ |
| Chinese | zh | ✅ |
| Russian | ru | ✅ |
| Arabic | ar | ✅ |

#### ⚙️ User Settings

**Per-User Configuration**:
- **UI Language**: Spanish or English (bot messages)
- **Audio Language**: Optimize Whisper for your language
- **Mode**: Fast or Full processing
- **Statistics**: Show/hide processing stats
- **Timestamps**: Add time markers [MM:SS]

Access via: `/settings`, `/lang`, `/mode`

#### 📊 Progress Tracking
- Real-time progress bar
- Percentage display
- Segment counter
- Elapsed time
- Estimated time remaining (ETA)
- Updates automatically
- Disappears when complete

#### 🎵 Long Audio Support
- Handles 30+ minute recordings
- Segment-based processing (30s chunks)
- Memory efficient
- Automatic message splitting (4096 char limit)
- No audio loss (Telegram backup)
- Complete transcription delivered

#### 🕐 Optional Timestamps
When enabled:
```
[00:00] First sentence here
[00:15] Second sentence here
[00:32] Third sentence continues
[01:05] And so on...
```

Perfect for:
- Meeting notes
- Podcast transcription
- Interview documentation
- Reference material

---

## User Guide

### First Time Use

1. **Send `/start`** to the bot

2. **Choose your language**:
   - 🇪🇸 Español for Spanish interface
   - 🇬🇧 English for English interface

3. **Confirmation**:
   ```
   ✅ Language set to English!
   🎙️ Send me a voice message to get started.
   ```

4. **Send a voice message**

5. **Watch the progress**:
   ```
   🎵 Audio Received
   Duration: 45s
   Language: 🇪🇸 Spanish
   Mode: 📝 Full Mode
   Segments: 2
   
   ⏳ Processing...
   [Progress bar updates appear]
   ```

6. **Get your results**:
   ```
   🎤 Transcription (Spanish)
   Original language
   
   [Your transcribed text here]
   
   🌐 Translation (English)
   
   [Your translated text here]
   ```

7. **Copy text**: Just click on the message!

### Changing Settings

#### Quick Commands

**Change Mode**:
```
/mode → Select Fast or Full
```

**Change Audio Language**:
```
/lang → Select your spoken language
```

**Full Settings**:
```
/settings → Access all options
```

#### Settings Panel

The `/settings` command shows:
- **Language**: Audio language optimization
- **Mode**: Fast vs Full processing
- **Stats**: Toggle processing statistics
- **Timestamps**: Toggle time markers

Each setting has a toggle button - click to change!

### Using Different Modes

#### Fast Mode Example
Best for quick English output:

1. `/mode` → 🚀 Fast Mode
2. Send voice message
3. Get **only** English translation
4. ~50% faster processing

**Use When**:
- You don't need the original language
- Speed is priority
- Taking quick notes
- Simple voice-to-text

#### Full Mode Example
Best for complete transcription:

1. `/mode` → 📝 Full Mode
2. Send voice message
3. Get:
   - Original language transcription
   - English translation
4. Both in separate messages

**Use When**:
- Learning a language
- Need both versions
- Documentation purposes
- Original wording matters

### Long Audio Tips

**Recording 15-30+ minute audio?**

1. **Just send it** - no need to split
2. **Watch progress** - see real-time updates
3. **Wait patiently** - may take 10-20 minutes
4. **Get complete text** - all at once

The bot:
- ✅ Processes in 30s segments
- ✅ Shows progress throughout
- ✅ Accumulates complete text
- ✅ Splits into multiple messages if >4096 chars
- ✅ Cleans up temp files automatically

### Copying Text

**How to Copy**:
1. **Tap** the message with transcription/translation
2. **Hold** (long press on mobile)
3. **Select Copy** from menu
4. **Paste** wherever you need

**Or on Desktop**:
1. **Click** message to select all
2. **Ctrl+C** / **Cmd+C** to copy
3. **Paste** wherever you need

**No formatting, no emojis, no buttons - just pure text!**

---

## Commands Reference

| Command | Description | Example |
|---------|-------------|---------|
| `/start` | Welcome + language selection | First time setup |
| `/help` | Detailed usage guide | Get instructions |
| `/about` | Technical information | Learn about the tech |
| `/settings` | Full settings panel | Configure all options |
| `/lang` | Quick audio language change | Switch to French |
| `/mode` | Quick mode toggle | Fast vs Full |

### Command Details

#### `/start`
**First Use**: Shows language selection (Spanish/English)  
**After Setup**: Shows welcome message in your language

**English**:
```
🎙️ Welcome to InnerVoice!

Your privacy-first voice transcription bot.
Send a voice message and get:

📝 Transcription in original language
🌐 Translation to English
⚡ Fast processing with Whisper AI

All processing happens locally - your audio stays private.
```

**Spanish**:
```
🎙️ ¡Bienvenido a InnerVoice!

Tu bot de transcripción de voz con privacidad.
Envía un mensaje de voz y obtén:

📝 Transcripción en idioma original
🌐 Traducción al inglés
⚡ Procesamiento rápido con Whisper AI

Todo el procesamiento es local - tu audio permanece privado.
```

#### `/help`
Shows comprehensive usage instructions including:
- How to use the bot
- Mode explanations (Fast vs Full)
- Language settings info
- Current preferences
- Pro tips

#### `/about`
Technical details:
- Technology stack (Whisper, FFmpeg, Python)
- Capabilities (languages, audio length)
- Requirements (hardware)
- Privacy information
- Contact info

#### `/settings`
Interactive settings panel with buttons:
- **Language**: Change audio language optimization
- **Mode**: Toggle Fast/Full mode
- **Stats**: Show/hide processing statistics
- **Timestamps**: Enable/disable time markers

#### `/lang`
Quick access to language selection:
- Shows current audio language
- Grid of 12 languages with flags
- Click to change immediately

#### `/mode`
Quick mode toggle:
- Shows current mode
- Two options: Fast 🚀 or Full 📝
- Click to switch

---

## Technical Details

### Architecture

**Components**:
- **Bot Framework**: aiogram (async Telegram bot) – runs in `bot` container
- **Whisper API**: Gunicorn + Flask with OpenAI Whisper (Medium, ROCm) – runs in `whisper` container
- **Audio Processing**: FFmpeg in bot (OGG→WAV), then HTTP POST to Whisper
- **Token Counting**: tiktoken
- **Network**: Both on `TelegramNet`; bot calls `http://whisper:9000`

**Processing Flow**:
1. User sends voice → Telegram
2. Bot downloads → Converts OGG to WAV
3. Splits if >1MB (30s segments)
4. POST each segment to Whisper API
5. Accumulates text → Full transcription
6. Sends results → Separate messages
7. Cleanup → Deletes temp files

**Storage**:
- **User Preferences**: In-memory (defaultdict)
- **Audio Files**: Temporary (deleted after processing)
- **Models**: Cached in Whisper container volume (~1.5GB)

### Performance

**Processing Speed**:
- Short audio (<30s): ~30 seconds
- Medium audio (5 min): ~5 minutes
- Long audio (30 min): ~20-30 minutes

**Memory Usage**:
- Base: ~2GB (Whisper model loaded)
- Per segment: +~500MB (during processing)
- Peak: ~4GB (long audio with multiple segments)

**Disk Space**:
- Whisper model: ~1.5GB
- Docker image: ~3-4GB
- Temporary audio: Minimal (auto-cleanup)

### Requirements

**Minimum**:
- CPU: 4 cores (Intel i5 or equivalent)
- RAM: 4GB available
- Disk: 10GB free space
- Network: Stable internet for Telegram API

**Recommended**:
- CPU: 8 cores (Intel i7 or equivalent)
- RAM: 8GB available
- Disk: 20GB free space
- Network: Fast connection for voice downloads

**Optional**:
- GPU: Not required (CPU-only processing)
- Storage: SSD preferred for faster model loading

### Configuration

**Environment Variables** (.env):
```bash
BOT_TOKEN=your_telegram_bot_token_here
```

**Docker Compose** (see [Two-Docker Deployment](#two-docker-deployment)):
```yaml
services:
  whisper:
    build: ./whisper
    # ROCm devices, memory limits...
  bot:
    build: ./bot
    environment:
      - WHISPER_API_URL=http://whisper:9000
    volumes:
      - ./.env:/app/.env:ro
      - audio_temp:/app/audios
    restart: unless-stopped
    environment:
      - TZ=UTC
    deploy:
      resources:
        limits:
          memory: 10G
```

**Timeouts** (bot.py):
- Telegram API: 200 seconds
- Progress updates: Every 2 segments (long audio) or every segment (short audio)
- Message retry: 3 attempts with exponential backoff

### Data Flow

**User Preferences** (per user):
```python
{
    'ui_language': 'es' | 'en',      # Bot interface language
    'language': 'es',                 # Audio language for Whisper
    'mode': 'fast' | 'full',          # Processing mode
    'show_stats': True | False,       # Display statistics
    'timestamps': True | False        # Add time markers
}
```

**Processing States** (per audio):
```python
{
    'file_id': 'unique_id',
    'segments': 5,
    'current': 3,
    'start_time': timestamp,
    'progress_msg_id': message_id
}
```

### API Limits

**Telegram**:
- Message length: 4096 characters (auto-split)
- File size: 20MB (voice messages)
- API calls: Rate limited (handled automatically)

**Whisper**:
- No API calls (local processing)
- No rate limits
- No usage costs

---

## Deployment

### Docker (Recommended) – Two Containers

#### First Time Setup
```bash
# Navigate to project
cd /path/to/InnerVoice

# Create .env with BOT_TOKEN
echo "BOT_TOKEN=your_token" > .env

# Build and start both (Whisper + Bot)
make up
# or: docker compose up -d --build
```

#### Update Bot Code
```bash
# After modifying bot.py
docker compose restart bot
```

#### Rebuild from Scratch
```bash
# Clean rebuild
docker compose down
docker compose up -d --build
```

### CPU vs GPU Setup

#### CPU-Only (Default) ✅
**Default configuration** - No changes needed!

**Advantages**:
- ✅ Faster Docker build (~5-10 min vs 30+ min)
- ✅ Smaller image size (~3GB vs ~8GB)
- ✅ Works on any machine
- ✅ No CUDA drivers needed
- ✅ Good performance for most use cases

**Performance**:
- ~1:1 ratio (30s audio = 30s processing)
- Perfectly acceptable for personal use
- Handles 30+ minute audio fine

#### GPU-Enabled (Optional) 🚀
Only needed if you have NVIDIA GPU and want maximum speed.

**To Enable GPU**:

1. **Edit Dockerfile**:
```dockerfile
# Comment out this line:
# FROM python:3.10-slim

# Uncomment this line:
FROM pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime

# Comment out normal pip install
# RUN pip install --no-cache-dir -r requirements.txt

# Uncomment GPU install:
RUN pip install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu117
RUN pip install --no-cache-dir openai-whisper aiogram python-dotenv tiktoken
```

2. **Update docker-compose.yml**:
```yaml
services:
  bot:
    build: .
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

3. **Rebuild**:
```bash
docker compose down
docker compose up -d --build
```

**GPU Requirements**:
- NVIDIA GPU with CUDA support
- NVIDIA Docker runtime installed
- ~8GB+ VRAM recommended

**Performance**:
- ~3-5x faster than CPU
- Useful for very frequent usage or very long audio

**Recommendation**: **Stick with CPU** unless you're processing many hours of audio daily.

#### View Logs
```bash
# Real-time logs
docker compose logs -f bot

# Last 50 lines
docker compose logs --tail 50 bot
```

#### Stop Bot
```bash
docker compose down
```

### Direct Python (Development)

```bash
# Install dependencies
pip install -r requirements.txt

# Run bot
python3 bot.py
```

**Note**: Requires Python 3.10+, ffmpeg installed

### Updating

**Minor Changes** (bot.py only):
```bash
docker compose restart bot
```

**Dependency Changes** (requirements.txt):
```bash
docker compose down
docker compose up -d --build
```

**Configuration Changes** (.env, docker-compose.yml):
```bash
docker compose down
docker compose up -d
```

---

## Troubleshooting

### Bot Not Responding

**Check container status**:
```bash
docker compose ps
```

**Check logs**:
```bash
docker compose logs bot
```

**Common causes**:
- ❌ Invalid BOT_TOKEN in .env
- ❌ Bot not started (@BotFather)
- ❌ Container not running
- ❌ Out of memory

**Solutions**:
1. Verify BOT_TOKEN is correct
2. Restart: `docker compose restart bot`
3. Check memory: `docker stats`
4. Rebuild if needed

### Progress Bar Not Updating

**Normal behavior**:
- Updates every segment for short audio (≤5 segments)
- Updates every 2 segments for long audio
- May appear "stuck" but is processing

**Not a problem if**:
- You eventually get the result
- Logs show processing activity

**Check logs** to confirm processing:
```bash
docker compose logs -f bot
```

### Audio Processing Fails

**Common causes**:
- Audio too long (>30 min) with low memory
- Corrupted audio file
- Unsupported format (rare)

**Solutions**:
1. Check container memory: `docker compose logs bot | grep -i memory`
2. Increase memory limit in docker-compose.yml
3. Try shorter audio clip
4. Check audio plays in Telegram

### Bot: "Cannot connect to host whisper:9000"

**What you see**: Bot logs `Error processing segment X: Cannot connect to host whisper:9000 [Connect call failed (...)]` for every segment.

**Root cause**: The Whisper container is **not staying up** (restart loop). The bot error is a **symptom**: nothing is listening on port 9000 because Whisper keeps crashing or never finishes starting.

**What to do**:

1. **Confirm Whisper is unstable**:
   ```bash
   docker compose ps
   ```
   If whisper shows "restarting" or exits soon after start, the service is down most of the time.

2. **Check Whisper logs** for the real failure (OOM, I/O, ROCm, etc.):
   ```bash
   docker compose logs whisper --tail 200
   ```

3. **Relax or remove block I/O limits**: If the host or an override applies a **low block I/O limit** (e.g. ~tens of KB/s) to the whisper container, model loading and cache I/O become unsustainable and the process can hang or be killed, causing a restart loop. Ensure the whisper service is **not** subject to strict `blkio` limits (in docker-compose, on the host, or in your orchestrator). The project's compose does not set block I/O limits; if you use an override or host cgroups that cap I/O, raise or remove them for the whisper container.

4. **After Whisper is stable**: Once the whisper container runs and listens on 9000, the "Cannot connect to whisper:9000" errors in the bot will stop. The network (e.g. `TelegramNet`) is fine; the issue is the service not being up.

5. **Whisper exits 139 (segfault) or restarts**: If you have a local **GPU-TROUBLESHOOTING.md** (optional, gitignored), see it for a full brainstorm. Otherwise: ensure host has `video` and `render` groups, try `WHISPER_MODEL=tiny`, `HSA_OVERRIDE_GFX_VERSION=11.0.3`, image `rocm/pytorch:latest`; or run Whisper on the host and point the bot at it.

### Out of Memory

**Symptoms**:
- Bot crashes during processing
- Container restarts
- No response on long audio

**Solutions**:

1. **Increase memory limit**:
```yaml
# docker-compose.yml
deploy:
  resources:
    limits:
      memory: 16G  # Increase from 10G
```

2. **Restart with new limits**:
```bash
docker compose down
docker compose up -d
```

3. **Monitor memory**:
```bash
docker stats innervoice-bot-1
```

### Wrong Language Detected

**Whisper auto-detects but can be wrong**

**Solution**:
1. Set your language manually: `/lang`
2. Select your spoken language
3. Try again

**Note**: Language setting optimizes Whisper, improving accuracy

### Text Split Across Multiple Messages

**This is normal for long transcriptions!**

Each message limited to 4096 characters (Telegram limit)

**Messages will show**:
```
━━ Part 1/3 ━━

[First part of text...]
```

**Just copy each part** - they're in order

### Can't Copy Text

**On Mobile**:
1. Tap and hold message
2. Select "Copy"
3. Paste

**On Desktop**:
1. Click message to select
2. Ctrl+C (Windows) or Cmd+C (Mac)
3. Paste

**If still issues**:
- Update Telegram app
- Try different device
- Forward message to "Saved Messages" and copy from there

---

## FAQ

### Q: Is my audio stored anywhere?
**A**: No! Audio is deleted immediately after processing. Only temporary files during processing, then wiped.

### Q: Can others see my transcriptions?
**A**: No! Everything is private. Bot runs on your server, only you have access.

### Q: Does it work offline?
**A**: No. Needs internet to receive audio from Telegram and send results. Processing is local though.

### Q: How accurate is the transcription?
**A**: Very accurate! Using OpenAI Whisper (Medium) - state-of-the-art AI. Accuracy depends on audio quality and clarity.

### Q: Can I transcribe phone calls?
**A**: Forward voice messages from any chat to the bot. Direct call recording depends on your device.

### Q: What if I speak multiple languages in one audio?
**A**: Whisper handles multilingual audio but accuracy varies. Best results with single language per message.

### Q: Can I change the translation target from English?
**A**: Not currently. Translation is always to English. This is a design choice for simplicity.

### Q: How long does processing take?
**A**: Roughly 1:1 ratio. 5 minutes of audio = ~5 minutes processing. Longer for very long recordings.

### Q: Can multiple people use the same bot?
**A**: Yes, but designed for 1-2 users. Each user has separate settings. No parallel processing (sequential queue).

### Q: How do I backup my settings?
**A**: Settings are in-memory only. Set them once per user, they persist until bot restart.

### Q: Can I run this on a Raspberry Pi?
**A**: Technically yes, but very slow. Whisper Medium needs good CPU. Not recommended for Pi.

---

## Support & Contact

**Issues or Questions?**
- Contact: @arkano21 on Telegram
- Check logs: `docker compose logs bot`
- Review this documentation

**Support Development**:
- ₿ Bitcoin: `bc1qwktevffc57rkk8lwyd6yqwxrvcd4vjxggcpsrn`
- ⚡ Lightning: `buffswan6@primal.net`
- 💜 Nostr: `npub1p2x3t3njq44vsk24qjkauzurvfd59c224qyu2mpgu9jverk9tfrqnz0ql5`

---

## License & Privacy

**License**: Open source, self-hosted solution

**Privacy Guarantee**:
- ✅ All processing happens on YOUR server
- ✅ No data sent to external APIs
- ✅ No cloud storage
- ✅ No third-party access
- ✅ You control everything

**Your Data, Your Control** 🔒

---

**Version**: 2.1.0  
**Last Updated**: November 14, 2025  
**Status**: Production Ready ✅  
**Made with ❤️ by @arkano21**

