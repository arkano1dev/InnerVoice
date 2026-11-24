FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY legacy.py .

USER 65534:65534
CMD ["gunicorn", "--bind", "0.0.0.0:9000", "--workers", "1", "--threads", "8", "legacy:app"]
