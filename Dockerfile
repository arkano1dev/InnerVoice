# CPU-only version (default) - Faster build, no GPU needed
FROM python:3.10-slim

# For GPU support, uncomment the following and comment the line above:
# FROM pytorch/pytorch:2.0.1-cuda11.7-cudnn8-runtime

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies (CPU-only - much faster!)
RUN pip install --no-cache-dir -r requirements.txt

# For GPU: If using GPU base image above, install with CUDA support:
# RUN pip install --no-cache-dir torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu117
# RUN pip install --no-cache-dir openai-whisper aiogram python-dotenv tiktoken

# Copy the rest of the application
COPY . .

# Create directory for audio files
RUN mkdir -p audios && \
    chmod 777 audios

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "bot.py"]