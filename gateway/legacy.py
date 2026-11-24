"""Internal compatibility listener for the existing Compose bot."""

import os
from threading import BoundedSemaphore

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024
worker_url = os.environ.get("WHISPER_WORKER_URL", "http://whisper:9000").rstrip("/")
legacy_slot = BoundedSemaphore(1)


def forward(path, *, timeout):
    headers = {}
    body = None
    if request.method == "POST":
        headers["Content-Type"] = request.content_type or "application/octet-stream"
        body = request.get_data(cache=False)

    try:
        upstream = requests.request(
            request.method,
            f"{worker_url}{path}",
            params=request.args,
            data=body,
            headers=headers,
            timeout=(2, timeout),
            allow_redirects=False,
        )
    except requests.RequestException:
        return jsonify(error="worker_unavailable"), 502

    return Response(
        upstream.content,
        status=upstream.status_code,
        content_type=upstream.headers.get("Content-Type", "application/octet-stream"),
    )


@app.post("/transcribe")
def transcribe():
    if not legacy_slot.acquire(blocking=False):
        return jsonify(error="gpu_busy", message="Whisper is processing audio. Try again when free."), 503
    try:
        return forward("/transcribe", timeout=590)
    finally:
        legacy_slot.release()


@app.get("/can-transcribe")
def can_transcribe():
    if not legacy_slot.acquire(blocking=False):
        return jsonify(can_transcribe=False, message="Whisper is processing audio. Try again when free."), 503
    try:
        return forward("/can-transcribe", timeout=7)
    finally:
        legacy_slot.release()


@app.get("/health")
def health():
    return forward("/health", timeout=2)


@app.get("/gpu-check")
def gpu_check():
    return forward("/gpu-check", timeout=7)
