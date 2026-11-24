# Supply the validated ROCm runtime digest for release builds.
ARG ROCM_BASE=rocm/pytorch:latest
FROM ${ROCM_BASE}

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-service.txt /tmp/requirements-service.txt
RUN python -c "from importlib.metadata import version; print('torch==' + version('torch'))" > /tmp/torch-constraint.txt && \
    python -m pip install --no-cache-dir -c /tmp/torch-constraint.txt -r /tmp/requirements-service.txt

WORKDIR /app
COPY whisper_server.py /app/

EXPOSE 9000
ENV WHISPER_MODEL=medium
ENV PYTHONUNBUFFERED=1

# Use Gunicorn for production WSGI (Flask dev server is single-threaded and slower)
ENV HOME=/tmp XDG_CACHE_HOME=/tmp/cache
USER 65534:65534
CMD ["gunicorn", "--bind", "0.0.0.0:9000", "--workers", "1", "--threads", "4", "--timeout", "600", "whisper_server:app"]
