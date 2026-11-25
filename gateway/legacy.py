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


class WorkerFault(RuntimeError):
    pass


def fault_marker():
    from jobs import ROOT
    return ROOT / "worker-fault"


@contextmanager
def guarded_dispatch():
    # The caller owns the shared inference lane. Set the latch before releasing it.
    marker = fault_marker()
    if marker.exists():
        raise WorkerFault("worker_protection_active")
    try:
        yield
    except (requests.ConnectionError, requests.Timeout, WorkerFault) as error:
        from jobs import sync_dir
        marker.touch()
        sync_dir(marker.parent)
        print("InnerVoice: worker inference interrupted; dispatch paused until gpu-resume", flush=True)
        raise WorkerFault("worker_protection_active") from error


def check_dispatch(response):
    if response.status_code >= 500 and response.status_code != 503:
        raise WorkerFault("worker_error")
    if response.status_code == 503:
        try:
            reason = response.json().get("error")
        except (ValueError, AttributeError):
            reason = None
        if reason == "gpu_oom":
            raise WorkerFault("gpu_oom")


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
        if path == "/transcribe":
            check_dispatch(upstream)
    except requests.RequestException:
        if path == "/transcribe":
            raise
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
        with inference_lane("legacy"), guarded_dispatch():
            return forward("/transcribe", timeout=590)
    except WorkerFault:
        return jsonify(error="worker_protection_active"), 503
    finally:
        legacy_slot.release()


@app.get("/can-transcribe")
def can_transcribe():
    if fault_marker().exists():
        return jsonify(can_transcribe=False, error="worker_protection_active"), 503
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
