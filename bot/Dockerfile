FROM cgr.dev/chainguard/wolfi-base AS runtime
RUN apk add --no-cache ca-certificates python-3.13 ffmpeg-9.1
ENV PATH=/opt/innervoice/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

FROM runtime AS dependencies
RUN apk add --no-cache py3.13-pip && python3.13 -m venv --without-pip /opt/innervoice
COPY requirements-bot.txt .
RUN /usr/bin/python3.13 -m pip --python /opt/innervoice install --no-cache-dir -r requirements-bot.txt
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" && chmod -R a+rX /opt/tiktoken

FROM runtime
COPY --from=dependencies /opt/innervoice /opt/innervoice
COPY --from=dependencies /opt/tiktoken /opt/tiktoken
ENV TIKTOKEN_CACHE_DIR=/opt/tiktoken
COPY bot.py .
USER 65534:65534
CMD ["python", "bot.py"]
