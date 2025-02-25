# Inervoice

Inervoice is a Telegram bot that transcribes and translates voice messages using OpenAI's Whisper model. Built with [aiogram](https://docs.aiogram.dev) and other Python libraries, the bot processes incoming voice messages, converts them to WAV format via `ffmpeg`, and then uses Whisper to generate both a transcription and a translation.

---

## Table of Contents

- [Overview](#overview)
- [How the Bot Works](#how-the-bot-works)
- [Features](#features)
- [Requirements](#requirements)
- [System Requirements](#system-requirements)
- [Installation & Deployment](#installation--deployment)
- [Usage](#usage)
- [Customization & Contributing](#customization--contributing)


---

## Overview

Inervoice enables Telegram users to simply send a voice message and receive:

- A **transcription** of the audio.
- A **translation** of the spoken content.

The bot is designed for reproducibility and ease of deployment on various servers. You can clone or download the repository and follow the instructions below to set up your environment.

---

## How the Bot Works

1. **Environment & Configuration:**
   - Reads configuration details (e.g., the `BOT_TOKEN`) from an `.env` file.
   - Logs activities and errors in `bot.log`.

2. **Message Handling:**
   - Responds to the `/start` command with a welcome message and instructions.
   - Downloads incoming voice messages into an `audios/` directory.

3. **Audio Processing:**
   - Converts the OGG audio file to WAV using `ffmpeg` combined with `ionice` for lower CPU priority.
   - Processes audio files sequentially using a queue system to efficiently manage system resources.

4. **Transcription & Translation:**
   - Uses the Whisper model (default: "medium") to transcribe the WAV file.
   - Generates a translation of the transcription.
   - Returns both outputs along with processing metrics such as time, CPU usage, and memory usage.

5. **Resource Management:**
   - Checks CPU usage before processing to prevent system overload.
   - Deletes temporary audio files after processing to conserve disk space.

---

## Features

- **Voice-to-Text Transcription:** Converts audio messages to text.
- **Translation:** Provides a translation of the transcribed text.
- **Resource-Aware Processing:** Monitors CPU usage and delays processing if necessary.
- **Logging:** Detailed logs in `bot.log` aid in debugging and performance monitoring.

---

# Requirements

The following is an example of a complete `requirements.txt` file:

```
aiogram
python-dotenv
openai-whisper
psutil
```

### System Dependencies

- **ffmpeg:**
    
    - **Installation:**
        - **Ubuntu/Debian:**
            
            ```bash
            sudo apt update
            sudo apt install ffmpeg
            ```
            
        - **macOS (Homebrew):**
            
            ```bash
            brew install ffmpeg
            ```
            
        - **Windows:** Download the binary from the [official website](https://ffmpeg.org/download.html) and add it to your system's PATH.
    - **Usage:**  
        Required to convert OGG audio files to WAV format before transcription.
- **ionice:**
    
    - **Installation:**
        - **Ubuntu/Debian:**
            
            ```bash
            sudo apt update
            sudo apt install util-linux
            ```
            
    - **Usage:**  
        Ensures `ffmpeg` runs with lower CPU priority to manage system resources efficiently.

---

### System Requirements

The performance of Inervoice depends on the Whisper model variant selected. See the guide below:

|**Model Variant**|**CPU Requirements**|**Memory (RAM)**|**GPU (Optional)**|**Notes**|
|---|---|---|---|---|
|**Tiny**|2+ cores|≥ 2 GB|Not required|Fastest response; lower accuracy. Ideal for low-resource devices.|
|**Base**|2+ cores|≥ 2–3 GB|Not required|Improved accuracy over Tiny; minimal resource use.|
|**Small**|4+ cores|≥ 4 GB|Beneficial: ~2–3 GB VRAM if using GPU|Balances speed and accuracy well.|
|**Medium**|4–8 cores|≥ 8 GB|Recommended: at least 4 GB VRAM for GPU use|Better accuracy; default model used in Inervoice.|
|**Large**|8+ cores|≥ 16 GB|Strongly recommended: high-end GPU (≥ 8 GB VRAM)|Highest accuracy; most resource intensive.|

> **System Tool Note:**
> 
> - **ffmpeg:** Must be installed on your server for audio conversion.
> - **ionice:** Typically included in the util-linux package on Linux systems.

---

## Installation & Deployment

### 1. Clone or Download the Repository

Since this is a public repository, you can clone it via Git:

```bash
git clone https://github.com/arkano1dev/InnerVoice.git
````

Alternatively, you can download the ZIP file directly from the GitHub page.

### 2. Install Python & Dependencies

- Ensure you have Python 3.8 or newer installed.
    
- Install the required packages:
    
    ```bash
    pip install -r requirements.txt
    ```
    

### 3. Set Up the Environment

- Create a `.env` file in the repository root by copying from `.env.example`.
- Add your Telegram Bot API key and any other required configuration.

### 4. Run the Bot

Start the bot with:

```bash
python3 bot.py
```

#### Running in the Background with nohup

To keep the bot running after closing your terminal:

```bash
nohup python3 bot.py > bot.log 2>&1 &
```

This command will:

- Run the bot in the background.
- Redirect both standard output and error to `bot.log`.

#### Managing the Bot

- **Activate the Virtual Environment:**
    
    ```bash
    source venv/bin/activate
    ```
    
- **Restarting the Bot:**
    
    1. Stop the bot:
        
        ```bash
        pkill -f bot.py
        ```
        
    2. Activate the virtual environment (if not already active):
        
        ```bash
        source venv/bin/activate
        ```
        
    3. Restart the bot:
        
        ```bash
        nohup python3 bot.py > bot.log 2>&1 &
        ```
        
- **Viewing Logs:**
    
    You can view the log file in real-time using:
    
    ```bash
    tail -f bot.log
    ```
    

---

## Usage

1. **Starting the Bot:**  
    Run `python3 bot.py` after setting up your environment.
    
2. **Interacting via Telegram:**
    
    - Send the `/start` command to receive a welcome message.
    - Send a voice message. The bot will:
        - Download and convert the audio.
        - Process the audio using the Whisper model.
        - Return the transcription and translation along with processing metrics (time, CPU usage, and memory usage).
3. **Logging:**  
    Refer to `bot.log` for detailed logs and troubleshooting information.
    

---

## Customization & Contributing

- **Change Model Variant:**  
    To select a different Whisper model, modify the following line in `bot.py`:
    
    ```python
    model = whisper.load_model("medium")
    ```
    
    Replace `"medium"` with `"tiny"`, `"base"`, `"small"`, or `"large"` based on your resource availability and accuracy needs.
    
- **Contributions:**  
    Contributions are welcome! Please open an issue or submit a pull request if you have improvements or bug fixes.
    