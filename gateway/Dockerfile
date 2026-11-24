FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY access.py legacy.py jobs.py telemetry.py sessions.py server.py ./
USER 65534:65534
CMD ["python", "server.py"]
