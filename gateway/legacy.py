"""Internal compatibility listener for the existing Compose bot."""

import os
import hmac
import json
from pathlib import Path
from contextlib import contextmanager
from threading import BoundedSemaphore, Condition

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
upstream_client = requests.Session()
upstream_client.trust_env = False
worker_url = os.environ.get("WHISPER_WORKER_URL", "http://whisper:9000").rstrip("/")
legacy_slot = BoundedSemaphore(1)


condition = Condition()
waiting = {"jobs": 0, "legacy": 0}
active = None
previous = None


@contextmanager
def inference_lane(lane):
    global active, previous
    other = "legacy" if lane == "jobs" else "jobs"
    with condition:
        waiting[lane] += 1
        condition.wait_for(lambda: active is None and not (waiting[other] and previous == lane))
        waiting[lane] -= 1
        active = lane
    try:
        yield
    finally:
        with condition:
            previous, active = lane, None
            condition.notify_all()


@app.before_request
def authenticate_bot():
    token = Path("/run/telegram/bearer").read_bytes().strip()
    supplied = request.headers.get("Authorization", "").encode()
    if not token or not hmac.compare_digest(supplied, b"Bearer " + token):
        return jsonify(error="unauthorized"), 401


@app.route("/preferences", methods=["GET", "PATCH"])
def preferences():
    from jobs import database
    choices = {"ui_language": ("en", "es"), "language": ("es","en","fr","nl","pt","it","ja","zh"),
               "mode": ("fast","full"), "timestamps": (True, False), "show_stats": (True, False)}
    changes = None
    if request.method == "PATCH":
        if request.content_length is None or request.content_length > 512:
            return jsonify(error="invalid_preferences"), 400
        changes = request.get_json(silent=True)
        if not isinstance(changes, dict) or any(
            key not in choices or type(value) is not type(choices[key][0]) or value not in choices[key]
            for key, value in changes.items()
        ):
            return jsonify(error="invalid_preferences"), 400
    with database() as db:
        if changes == {}:
            db.execute("DELETE FROM bot_preferences")
        elif changes:
            db.executemany("INSERT OR REPLACE INTO bot_preferences VALUES(?,?)",
                           [(key, json.dumps(value)) for key,value in changes.items()])
        return jsonify({key: json.loads(value) for key,value in db.execute("SELECT key,value FROM bot_preferences")})


def forward(path, *, timeout):
    headers = {}
    body = None
    if request.method == "POST":
        headers["Content-Type"] = request.content_type or "application/octet-stream"
        body = request.get_data(cache=False)

    try:
        upstream = upstream_client.request(
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
        if os.environ.get("ENABLE_JOBS") != "1" or (Path(os.environ.get("STATE_DIR","/state"))/"drain").exists():
            return jsonify(error="gpu_busy",message="Service draining"),503
        with inference_lane("legacy"):
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
