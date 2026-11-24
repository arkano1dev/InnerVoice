FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip && \
    python -m pip install --no-cache-dir -r requirements.txt && \
    python -m pip uninstall -y setuptools wheel pip
COPY access.py legacy.py jobs.py telemetry.py sessions.py server.py ./
USER 65534:65534
CMD ["python", "server.py"]
