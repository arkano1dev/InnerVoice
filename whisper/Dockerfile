# Whisper API on AMD iGPU (integrated GPU / APU)
# Uses official ROCm 7.2 PyTorch image - avoids libamdhip64 executable-stack issues
# Requires AMD APU or GPU with ROCm 7.x support
FROM rocm/pytorch:rocm7.2_ubuntu24.04_py3.12_pytorch_release_2.9.1

# ffmpeg for audio handling
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# openai-whisper uses existing PyTorch from base (ROCm)
RUN pip install --no-cache-dir openai-whisper flask

WORKDIR /app
COPY whisper_server.py /app/

EXPOSE 9000
ENV WHISPER_MODEL=medium
ENV PYTHONUNBUFFERED=1

CMD ["python", "whisper_server.py"]
