# InnerVoice

**Version 3.0.0** - Privacy-First Voice Transcription Bot

InnerVoice is a Telegram bot that transcribes and translates voice messages using OpenAI's Whisper model. Built with [aiogram](https://docs.aiogram.dev), the bot runs as **two Docker containers**: a lightweight Telegram bot and a Whisper API service (Whisper on **AMD GPU via ROCm**). Send voice → get transcription + translation.

## 📚 Documentation

- **[DOCUMENTATION.md](DOCUMENTATION.md)** - Complete user & technical guide
- **[CHANGELOG.md](CHANGELOG.md)** - Version history and changes

## Private Voice Gateway candidate

The private Gateway, operator CLI, speech provider and Ubuntu recorder are available on `refactor/private-voice-gateway`. Target-host acceptance is pending. The legacy bot instructions below describe the original deployment.

Run `./bin/innervoice` from the candidate checkout with one of these commands.

| Command | Purpose |
| --- | --- |
| `initialize`, `config setup`, `config edit` | Prepare or edit deployment settings. Follow the provisioning guide before starting services. |
| `config show`, `config validate` | Inspect redacted settings or validate the saved configuration. |
| `config set KEY=VALUE` | Save a configuration change for the next activation. |
| `credential --identity LABEL` | Register or rotate a client token using a masked prompt. |
| `start`, `start --setup` | Activate a prepared deployment, optionally running setup first. |
| `restart --engine ENGINE` | Drain work and activate a configured openai-whisper or faster-whisper profile. |
| `stop`, `status` | Drain and stop the active services, or inspect service and configuration status. |
| `dashboard [--watch] [--job ID]` | Inspect job metadata once, continuously, or for one job. |
| `desktop setup`, `desktop start`, `desktop status`, `desktop stop` | Configure the recorder, launch it in an X11 session, inspect its user service, or stop it. |

The Gateway accepts authenticated audio jobs, tracks progress, supports retries with an idempotency key and serves recoverable recording sessions. The agent provider converts an incoming voice note to text for the agent. The desktop client records audio and retains local state for recovery. See the [operator guide](docs/private-gateway.md) for provisioning, client installation, direct smoke checks, benchmark criteria and rollback.

### Acceptance record · 2026-11-24

| Scope | Recorded evidence | Remaining check |
| --- | --- | --- |
| Baseline and stages 1–3 | Gateway, durable job store, worker profiles, provider adapter and CLI present at `cfdf7cb`. | Verify clean installation, provider configuration, restart recovery, retention and existing bot behavior on the target. |
| Stage 4 · Gateway | Smoke client and operator runbook present. | Record direct smoke first, then Telegram delivery, a one-hour restart case, latency, quality, peak resources and service rollback. |
| Stage 5 · Ubuntu | Desktop capture, session handling and editing code present. | Exercise hotkey, microphone, recovery and insertion into the original window in a real graphical session. |
| Public branding | Arkano credits and project support details restored. | Candidate remains on the refactor branch pending acceptance. |

## Quick Start

Run all commands from the **project root** (directory that contains `docker-compose.yml`).

```bash
# Clone and go to project root
git clone https://github.com/arkano1dev/InnerVoice.git && cd InnerVoice

# Configure your bot token in .env
echo "BOT_TOKEN=your_token_here" > .env

# Start both containers (Whisper + Bot)
make up
# or: docker compose up -d --build

# Test: Send /start to your bot in Telegram, then a voice message
```

**GPU (ROCm):** Default is AMD GPU (`rocm/pytorch:latest`, medium model, Gunicorn, lazy load). If Whisper exits 139 or restarts: ensure user is in `video` and `render` groups; see DOCUMENTATION.md troubleshooting. Optional local file **GPU-TROUBLESHOOTING.md** (gitignored) may exist with extra steps. CPU fallback: set `dockerfile: Dockerfile.cpu` for whisper and remove ROCm options in `docker-compose.yml`.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Requirements](#requirements)
- [Installation & Deployment](#installation--deployment)
- [Usage](#usage)
- [Customization & Contributing](#customization--contributing)

---

## Overview

InnerVoice enables Telegram users to simply send a voice message and receive:

- A **transcription** of the audio.
- A **translation** of the spoken content.

The bot is designed for reproducibility and ease of deployment on various servers. You can clone or download the repository and follow the instructions below to set up your environment.

---

## Features

- **Voice-to-Text Transcription:** Converts audio messages to text.
- **Translation:** Provides a translation of the transcribed text.
- **Multi-language Support:** Supports Spanish, English, French, Dutch, Portuguese, and German.
- **Privacy-First Design:** Runs locally on your machine, keeping your data private.
- **Docker Support:** Easy deployment with containerization.
- **Resource-Efficient:** Optimized for basic hardware without GPU requirement.
- **Logging:** Detailed logs aid in debugging and performance monitoring.

---

## Requirements

The bot requires:

- **Python 3.8+**
- **ffmpeg** for audio conversion
- **A Telegram Bot API Token** (generated via BotFather)
- **Linux/macOS/Windows environment**

### System Dependencies

Ensure the following packages are installed before running the bot:

```bash
sudo apt update
sudo apt install python3-venv
sudo apt install ffmpeg
```

> **Note:** `ffmpeg` is required to convert OGG voice messages to WAV format.

---

## Installation & Deployment

### 1. Clone the Repository

```bash
git clone https://github.com/arkano1dev/InnerVoice.git
cd InnerVoice
```

### 2. Set Up the Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate  # For Linux/macOS
venv\Scripts\activate    # For Windows (Command Prompt)
```

### 3. Install Dependencies

#### **For CPU (No CUDA/GPU)**
```bash
pip install aiogram python-dotenv psutil
pip install tiktoken
pip install openai-whisper --no-cache-dir
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
```

#### **For GPU (CUDA Support)**
```bash
pip install -r requirements.txt
```

> **Note:** The `requirements.txt` includes the standard installation with CUDA support. If you are running the bot on a system without a GPU, follow the CPU installation steps instead.

### 4. Set Up Environment Variables

Create a `.env` file inside the `InnerVoice` folder:

```bash
echo 'BOT_TOKEN=your_telegram_token_here' > .env
```

Replace `your_telegram_token_here` with your actual Telegram Bot API key.

---

## Running the Bot

You can run the bot either directly or using Docker.

### Option 1: Direct Execution

1. Activate the Virtual Environment:
```bash
source venv/bin/activate  # Linux/macOS
venv\Scripts\activate    # Windows
```

2. Start the Bot:
```bash
python3 bot.py
```

### Option 2: Using Docker (Recommended)

1. Build and Start:
```bash
docker-compose up -d
```

2. View Logs:
```bash
docker-compose logs -f
```

3. Stop the Bot:
```bash
docker-compose down
```

### Docker Benefits
- No need to install Python or dependencies locally
- Automatic restart on failure
- Easy deployment across different systems
- Isolated environment
- Resource management
- Clean temporary file handling
---

## Usage

1. **Start the Bot:**
   - Run `python3 bot.py` after setting up your environment.

2. **Interacting via Telegram:**
   - Send the `/start` command to receive a welcome message in Spanish and English.
   - Available commands:
     - `/lang` - Change the language for transcription/translation
     - `/help` - View usage instructions and hardware requirements
     - `/about` - Learn about the bot's privacy features and technology
   - Send a voice message. The bot will:
     - Download and convert the audio.
     - Process the audio using the Whisper model.
     - Return both transcription and translation.
     - Provide processing statistics (time, segments, tokens).

3. **Language Selection:**
   - Use `/lang` to view and change the language.
   - Supported languages:
     - Spanish (default)
     - English
     - French
     - Dutch
     - Portuguese
     - German

4. **Logging:**
   - Refer to `bot.log` for detailed logs and troubleshooting information.

---
The performance of Inervoice depends on the Whisper model variant selected. See the guide below:

|**Model Variant**|**CPU Requirements**|**Memory (RAM)**|**GPU (Optional)**|**Notes**|
|---|---|---|---|---|
|**Tiny**|2+ cores|≥ 2 GB|Not required|Fastest response; lower accuracy. Ideal for low-resource devices.|
|**Base**|2+ cores|≥ 2–3 GB|Not required|Improved accuracy over Tiny; minimal resource use.|
|**Small**|4+ cores|≥ 4 GB|Beneficial: ~2–3 GB VRAM if using GPU|Balances speed and accuracy well.|
|**Medium**|4–8 cores|≥ 8 GB|Recommended: at least 4 GB VRAM for GPU use|Better accuracy; default model used in Inervoice.|
|**Large**|8+ cores|≥ 16 GB|Strongly recommended: high-end GPU (≥ 8 GB VRAM)|Highest accuracy; most resource intensive.|

---
## Customization & Contributing

- **Change Model Variant:**
  For the candidate, provision matching model weights and checksum, update the engine profile with `config set`, validate it, then use `restart`. For the legacy worker, configure `WHISPER_MODEL` in Compose.

- **Contributions:**
  Contributions are welcome! Please open an issue or submit a pull request for improvements.

---

## License

This project is licensed under the MIT License. Feel free to use and modify it as needed.