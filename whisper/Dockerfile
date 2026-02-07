# Whisper API - ROCm optimized for eGPU
FROM python:3.11-slim

# Install system dependencies (rocm-smi optional - may need full ROCm runtime)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install official Whisper from OpenAI with ROCm PyTorch
RUN pip install --no-cache-dir \
    openai-whisper \
    flask \
    torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm6.0

WORKDIR /app

COPY whisper_server.py /app/

EXPOSE 9000

ENV WHISPER_MODEL=medium
ENV PYTHONUNBUFFERED=1

CMD ["python", "whisper_server.py"]
