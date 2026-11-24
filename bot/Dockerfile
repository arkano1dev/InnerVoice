FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-bot.txt .
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN pip install --no-cache-dir -r requirements-bot.txt && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" && chmod -R a+rX /opt/tiktoken
COPY bot.py .
USER 65534:65534
CMD ["python", "bot.py"]
