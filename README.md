# InnerVoice

InnerVoice is a Telegram bot that transcribes and translates voice messages using OpenAI's Whisper model. Built with [aiogram](https://docs.aiogram.dev) and other Python libraries, the bot processes incoming voice messages, converts them to WAV format via `ffmpeg`, and then uses Whisper to generate both a transcription and a translation.

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
  Modify the following line in `bot.py` to use a different Whisper model:
  
  ```python
  model = whisper.load_model("medium")
  ```
  
  Replace `"medium"` with `"tiny"`, `"base"`, `"small"`, or `"large"`.

- **Contributions:**
  Contributions are welcome! Please open an issue or submit a pull request for improvements.

---

## License

This project is licensed under the MIT License. Feel free to use and modify it as needed.