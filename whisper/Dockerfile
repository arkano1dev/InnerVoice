# Whisper API - GPU (AMD ROCm). Uses prebuilt rocm/pytorch image.
# latest tag can be more stable on some hosts than a specific 7.2 tag
FROM rocm/pytorch:latest

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir openai-whisper flask

WORKDIR /app
COPY whisper_server.py /app/

EXPOSE 9000
ENV WHISPER_MODEL=medium
ENV PYTHONUNBUFFERED=1

CMD ["python", "whisper_server.py"]
