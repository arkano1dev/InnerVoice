"""Internal compatibility listener for the existing Compose bot."""

import os
import time
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


# Both callers submit bounded audio segments. A hung native thread can keep
# Gunicorn and its HTTP connection alive long after the GPU has reset.
INFERENCE_TIMEOUT = int(os.environ.get("IV_INFERENCE_TIMEOUT_SECONDS", "45"))
if not 5 <= INFERENCE_TIMEOUT <= 590:
    raise ValueError("IV_INFERENCE_TIMEOUT_SECONDS must be between 5 and 590")


class WorkerFault(RuntimeError):
    pass


def fault_marker():
    from jobs import ROOT
    return ROOT / "worker-fault"


def gpu_dump_present():
    # Inspect the directory entry only. Reading the dump can consume it.
    render = Path(os.environ.get("IV_RENDER_NODE", "/dev/dri/renderD128")).name
    device = Path("/sys/class/drm") / render / "device"
    if os.environ.get("PRIVATE_API") == "1" and not device.is_dir():
        raise WorkerFault("render_node_unavailable")
    return (device / "devcoredump/data").exists()


def require_dispatch():
    if fault_marker().exists():
        raise WorkerFault("worker_protection_active")
    if gpu_dump_present():
        raise WorkerFault("gpu_coredump")


@contextmanager
def guarded_dispatch(context=""):
    # The caller owns the shared inference lane. Set the latch before releasing it.
    marker = fault_marker()
    if marker.exists():
        raise WorkerFault("worker_protection_active")
    try:
        require_dispatch()
        yield
    except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, WorkerFault) as error:
        from jobs import sync_dir
        marker.touch()
        sync_dir(marker.parent)
        reason = "worker_timeout" if isinstance(error, requests.Timeout) else "worker_fault"
        if isinstance(error, WorkerFault):
            reason = str(error)
        detail = f"{reason} {context}".strip()
        marker.write_text(detail + "\n")
        print(f"InnerVoice: {detail}; GPU dispatch paused until gpu-resume", flush=True)
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


class DesktopPriority(Exception):
    pass


def desktop_active():
    from jobs import database
    # Reserve the gaps between uploads. Expire an abandoned open recording
    # after one transport interval plus six seconds of network jitter.
    with database() as db:
        return bool(db.execute("""
            SELECT 1 FROM jobs j JOIN sessions s ON s.id=j.id
            LEFT JOIN session_options o ON o.id=j.id
            WHERE j.state IN ('queued','processing') AND (
                s.closed OR
                coalesce((SELECT max(received) FROM fragment_arrivals WHERE job=j.id), j.created)
                >= ? - CASE WHEN o.streaming THEN 8 ELSE 36 END
            ) LIMIT 1
        """, (time.time(),)).fetchone())


@contextmanager
def inference_lane(lane):
    global active
    with condition:
        waiting[lane] += 1
        try:
            if lane == "jobs":
                condition.wait_for(lambda: active is None)
            else:
                # The bot gives each request 600 seconds. Keep the current
                # segment pending within that budget, including inference.
                deadline = time.monotonic() + max(0, 590 - INFERENCE_TIMEOUT)
                while True:
                    if fault_marker().exists():
                        raise WorkerFault("worker_protection_active")
                    if fault_marker().with_name("drain").exists():
                        raise DesktopPriority()
                    if active is None and not waiting["jobs"] and not desktop_active():
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DesktopPriority()
                    condition.wait(min(remaining, 0.5))
            active = lane
        finally:
            waiting[lane] -= 1
    try:
        yield
    finally:
        with condition:
            active = None
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

    upstream = upstream_client.request(
        request.method,
        f"{worker_url}{path}",
        params=request.args,
        data=body,
        headers=headers,
        timeout=(2, timeout),
        allow_redirects=False,
    )
    check_dispatch(upstream)

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
            return forward("/transcribe", timeout=INFERENCE_TIMEOUT)
    except DesktopPriority:
        return jsonify(error="background_wait"), 503
    except WorkerFault:
        return jsonify(error="worker_protection_active"), 503
    finally:
        legacy_slot.release()


def worker_probe(path, timeout):
    # Polls must not wait behind inference and accidentally time out their caller.
    global active
    if fault_marker().with_name("startup-pending").exists():
        return jsonify(can_transcribe=False, error="admission_disabled"), 503
    with condition:
        if fault_marker().exists():
            return jsonify(can_transcribe=False, error="worker_protection_active"), 503
        if active is not None or any(waiting.values()) or desktop_active():
            return jsonify(can_transcribe=False, error="gpu_busy"), 503
        active = "probe"
    try:
        with guarded_dispatch():
            return forward(path, timeout=timeout)
    except WorkerFault:
        return jsonify(can_transcribe=False, error="worker_protection_active"), 503
    finally:
        with condition:
            active = None
            condition.notify_all()


@app.get("/can-transcribe")
def can_transcribe():
    if fault_marker().with_name("telegram-pending-hold").exists():
        return jsonify(can_transcribe=False, error="pending_queue_held"), 503
    return worker_probe("/can-transcribe", 7)


@app.get("/health")
def health():
    return worker_probe("/health", 7)


@app.get("/gpu-check")
def gpu_check():
    return worker_probe("/gpu-check", 7)
