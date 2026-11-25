FROM cgr.dev/chainguard/wolfi-base AS runtime
RUN apk add --no-cache ca-certificates python-3.13 ffmpeg-9.1
ENV PATH=/opt/innervoice/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

FROM runtime AS dependencies
RUN apk add --no-cache py3.13-pip && python3.13 -m venv --without-pip /opt/innervoice
COPY gateway/requirements.txt .
RUN /usr/bin/python3.13 -m pip --python /opt/innervoice install --no-cache-dir -r requirements.txt

FROM runtime
COPY --from=dependencies /opt/innervoice /opt/innervoice
COPY gateway/access.py gateway/legacy.py gateway/jobs.py gateway/telemetry.py \
     gateway/sessions.py gateway/server.py private_memory.py ./
USER 65534:65534
CMD ["python", "server.py"]
