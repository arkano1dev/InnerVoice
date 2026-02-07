# Whisper API - ROCm optimized for eGPU
FROM python:3.11-slim

# Install system dependencies (rocm-smi optional - may need full ROCm runtime)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch from ROCm index, then Whisper/Flask from PyPI
RUN pip install --no-cache-dir \
    torch torchvision torchaudio --index-url https://download.pytorch.org/whl/rocm6.0 && \
    pip install --no-cache-dir openai-whisper flask

WORKDIR /app

COPY whisper_server.py /app/

EXPOSE 9000

ENV WHISPER_MODEL=medium
ENV PYTHONUNBUFFERED=1

CMD ["python", "whisper_server.py"]
