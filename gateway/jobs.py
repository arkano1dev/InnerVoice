"""Single-process durable speech-to-English job coordinator."""
import contextlib
import tempfile
import hashlib
import hmac
import io
import json
import math
import os
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import telemetry
import wave
from pathlib import Path

import requests
from flask import Flask, request
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.formparser import FormDataParser
from lingua import Language, LanguageDetectorBuilder

from access import authorize
import legacy

api = Flask(__name__)
def setting(name, default):
    return int(os.environ.get("IV_"+name.upper()) or default)


LIMIT = setting("upload_mib",128) * 1024**2
PCM_LIMIT = setting("max_seconds",3600) * 32000
BLOCK = 30 * 32000
api.config.update(MAX_CONTENT_LENGTH=LIMIT + 65536, MAX_FORM_PARTS=3)
ROOT = Path(os.environ.get("STATE_DIR", "/state"))
AUDIO = Path("/tmp/innervoice-audio")
PROFILE = os.environ.get("PROFILE_REVISION", "")
admission = threading.Lock()
rejection_lock = threading.Lock()
rejections = {}
uploads = set()
replay_slots = threading.BoundedSemaphore(2)
reserved = 0
last_owner = None
detector = LanguageDetectorBuilder.from_all_spoken_languages().build()


@contextlib.contextmanager
def database():
    with contextlib.closing(sqlite3.connect(ROOT / "jobs.db", timeout=3)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA secure_delete=ON")
        with db:
            yield db


def initialize():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    AUDIO.mkdir(mode=0o700, exist_ok=True)
    tempfile.tempdir=str(AUDIO)
    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        if db.execute("PRAGMA user_version").fetchone()[0] not in (0,1):
            raise RuntimeError("Unsupported database schema")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS bot_preferences(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, replay TEXT NOT NULL, digest TEXT,
            profile TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, finished REAL,
            total INTEGER NOT NULL, done INTEGER DEFAULT 0, retries INTEGER DEFAULT 0,
            ready REAL DEFAULT 0, error TEXT, result TEXT, UNIQUE(owner,replay));
        CREATE INDEX IF NOT EXISTS ready_jobs ON jobs(state,ready);
        CREATE TABLE IF NOT EXISTS segments(job TEXT, idx INTEGER, text TEXT, PRIMARY KEY(job,idx));
        PRAGMA user_version=1;
        """)
        import sessions
        sessions.schema(db)
        telemetry.schema(db)
        db.execute("UPDATE jobs SET state='queued' WHERE state='processing'")
        # Docker discards the RAM spool when the container stops.
        for row in db.execute("SELECT id,digest,done,total FROM jobs WHERE finished IS NULL").fetchall():
            if row["digest"] == "capture":
                required = [AUDIO / f"{row['id']}.{r[0]}.pcm" for r in db.execute(
                    "SELECT idx FROM fragments WHERE job=? AND idx>=?", (row["id"], row["done"]))]
            else:
                required = [AUDIO / (row["id"] + ".pcm")]
            if any(not path.is_file() for path in required):
                finish_error(db, row["id"], "input_missing")
        known = {row[0] for row in db.execute("SELECT id FROM jobs")}
        known.update(f"{r[0]}.{r[1]}" for r in db.execute("SELECT job,idx FROM fragments"))
    for file in AUDIO.iterdir():
        if file.suffix in {".upload", ".part"} or (file.suffix == ".pcm" and file.stem not in known):
            file.unlink()


def failure(code, status):
    if status in (400,401,409,413,415,422,429,503):
        # The caller may already hold a write transaction. Aggregate in memory.
        with rejection_lock:
            rejections[code] = rejections.get(code,0)+1
    return {"error": {"code": code, "retryable": status in (429, 503)}}, status, (
        {"Retry-After": "2"} if status in (429, 503) else {})


@api.before_request
def authenticate():
    secret = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if request.headers.get("Authorization") != "Bearer " + secret or not secret:
        return failure("unauthorized", 401)
    with open("/run/innervoice-access/clients.json") as stream:
        clients = json.load(stream)
    owner, scope, error = authorize(clients, secret, request.remote_addr, request.path)
    if error:
        return failure(error, 401 if error == "unauthorized" else 403)
    request.environ["innervoice.scope"] = scope
    request.environ["innervoice.owner"] = owner


@api.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def sync_dir(directory=ROOT):
    fd = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def accepted(row):
    return {"job_id": row["id"], "state": row["state"], "status_url": "/v1/jobs/" + row["id"]}, 202


def paused():
    return (os.environ.get("ENABLE_JOBS") != "1" or not PROFILE
            or (ROOT / "drain").exists() or (ROOT / "startup-pending").exists())


def space_available(budget):
    used = sum(p.stat().st_size for p in AUDIO.glob("*.pcm"))
    return used + reserved + budget <= setting("quota_mib",1024)*1024**2 and shutil.disk_usage(AUDIO).free >= reserved + budget + setting("free_floor_mib",64)*1024**2


def replay_existing(row):
    """Hash a bounded replay without creating another audio spool file."""
    if not replay_slots.acquire(blocking=False):
        return failure("replay_capacity", 503)

    class AudioDigest:
        def __init__(self):
            self.size = 0
            self.digest = hmac.new((ROOT / "fingerprint.key").read_bytes(), digestmod="sha256")

        def write(self, data):
            self.size += len(data)
            if self.size > LIMIT:
                raise RequestEntityTooLarge()
            self.digest.update(data)

        def seek(self, offset):
            if offset != 0:
                raise ValueError("Invalid multipart offset")

        def close(self):
            pass

    try:
        parser = FormDataParser(
            stream_factory=lambda **_: AudioDigest(),
            max_content_length=LIMIT + 65536,
            max_form_memory_size=api.config["MAX_FORM_MEMORY_SIZE"],
            max_form_parts=3,
        )
        _, fields, files = parser.parse(
            request.stream, request.mimetype, request.content_length, request.mimetype_params
        )
        if fields or set(files) != {"audio"} or len(files.getlist("audio")) != 1:
            return failure("invalid_fields", 400)
        signature = files["audio"].stream.digest.hexdigest()
        if not hmac.compare_digest(row["digest"], signature):
            return failure("idempotency_conflict", 409)
        return failure("expired", 410) if row["state"] == "expired" else accepted(row)
    except RequestEntityTooLarge:
        return failure("size_limit", 413)
    finally:
        replay_slots.release()


@api.post("/v1/jobs")
def submit():
    global reserved
    owner = request.environ["innervoice.owner"]
    key = request.headers.get("Idempotency-Key", "")
    if not 1 <= len(key) <= 128:
        return failure("invalid_key", 400)
    claim = (owner, key)
    budget = 2 * LIMIT + PCM_LIMIT + 65536
    with admission, database() as db:
        old = db.execute("SELECT * FROM jobs WHERE owner=? AND replay=?", claim).fetchone()
        if not old:
            if claim in uploads:
                return failure("submission_pending", 503)
            if paused():
                return failure("admission_disabled", 503)
            if any(client == owner for client, _ in uploads):
                return failure("client_capacity", 429)
            if len(uploads) >= setting("uploads", 2):
                return failure("upload_capacity", 503)
            if not space_available(budget):
                return failure("storage_capacity", 503)
            uploads.add(claim)
            reserved += budget
    if old:
        return replay_existing(old)
    ident = uuid.uuid4().hex
    started=time.time()
    source = AUDIO / (ident + ".upload")
    pcm = AUDIO / (ident + ".part")
    try:
        if set(request.files) != {"audio"} or len(request.files.getlist("audio")) != 1 or request.form:
            return failure("invalid_fields", 400)
        digest = hmac.new((ROOT / "fingerprint.key").read_bytes(), digestmod="sha256")
        with source.open("xb") as out:
            while chunk := request.files["audio"].stream.read(65536):
                if out.tell() + len(chunk) > LIMIT:
                    return failure("size_limit", 413)
                digest.update(chunk)
                out.write(chunk)
        with source.open("rb") as stream:
            head=stream.read(12)
        if not (head.startswith((b"OggS",b"RIFF",b"fLaC",b"ID3",b"\x1aE\xdf\xa3")) or
                head[4:8]==b"ftyp" or (len(head)>=2 and head[0]==255 and head[1]&224==224)):
            return failure("unsupported_audio",415)
        signature = digest.hexdigest()
        decode_start=time.monotonic()
        command = ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                   "-format_whitelist", "ogg,wav,mp3,flac,mov,matroska,webm,aac",
                   "-i", "pipe:0", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
                   "-f", "s16le", "-fs", str(PCM_LIMIT + 65536), "pipe:1"]
        with source.open("rb") as src, pcm.open("xb") as out:
            result = subprocess.run(command, stdin=src, stdout=out, stderr=subprocess.DEVNULL, timeout=180)
            size = out.tell()
            if size > PCM_LIMIT:
                return failure("duration_limit", 413)
            if result.returncode or not size:
                return failure("undecodable_audio", 422)
            os.fsync(out.fileno())
        with admission, database() as db:
            old = db.execute("SELECT * FROM jobs WHERE owner=? AND replay=?", (owner,key)).fetchone()
            if old:
                if old["digest"] != signature:
                    return failure("idempotency_conflict", 409)
                return failure("expired", 410) if old["state"] == "expired" else accepted(old)
            if paused():
                return failure("admission_disabled",503)
            counts = db.execute("SELECT count(*),sum(owner=?) FROM jobs WHERE state IN ('queued','processing')",(owner,)).fetchone()
            if (counts[1] or 0) >= setting("jobs_per_owner",4):
                return failure("client_capacity",429)
            if counts[0] >= setting("jobs",8):
                return failure("queue_capacity",503)
            pcm.rename(AUDIO / (ident + ".pcm"))
            sync_dir(AUDIO)
            db.execute("INSERT INTO jobs(id,owner,replay,digest,profile,state,created,total) VALUES(?,?,?,?,?,'queued',?,?)",
                       (ident,owner,key,signature,PROFILE,time.time(),math.ceil(size/BLOCK)))
            db.execute("INSERT INTO performance(id,owner,profile,created,state,total,audio_seconds,admission_seconds,decode_seconds) VALUES(?,?,?,?,'queued',?,?,?,?)",
                       (ident,owner,PROFILE,started,math.ceil(size/BLOCK),size/32000,time.time()-started,time.monotonic()-decode_start))
        return accepted({"id": ident,"state":"queued"})
    except subprocess.TimeoutExpired:
        return failure("decode_timeout",422)
    finally:
        try:
            source.unlink(missing_ok=True)
            pcm.unlink(missing_ok=True)
        finally:
            with admission:
                uploads.remove(claim)
                reserved -= budget


@api.get("/v1/jobs/<ident>")
def status(ident):
    with database() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (ident, request.environ["innervoice.owner"])).fetchone()
    if not row:
        return failure("unknown_job",404)
    if row["state"] == "expired":
        return failure("expired",410)
    body = dict(job_id=ident,state=row["state"],completed_segments=row["done"],total_segments=row["total"])
    if row["state"] == "succeeded":
        with database() as db:
            db.execute("UPDATE performance SET first_served=coalesce(first_served,?) WHERE id=?",(time.time(),ident))
            option = db.execute("SELECT task FROM session_options WHERE id=?", (ident,)).fetchone()
        language = "es" if option and option["task"] == "transcribe" else "en"
        body["result"] = dict(text=row["result"],language=language,speech_detected=bool(row["result"]))
    if row["error"]:
        body["error"] = dict(code=row["error"],retryable=False)
    return body


@api.get("/v1/capabilities")
def capabilities():
    scope = request.environ["innervoice.scope"]
    from sessions import STREAM_SECONDS, TASKS
    body = dict(api_version=1,jobs=scope in ("both","jobs"),sessions=scope in ("both","sessions"),
                fragment_seconds=30,output_language="en")
    if body["sessions"]:
        body.update(stream_fragment_seconds=STREAM_SECONDS, session_tasks=TASKS)
    return body


def finish_error(db, ident, reason):
    db.execute("UPDATE jobs SET state='failed',finished=?,error=? WHERE id=?", (time.time(),reason,ident))


def worker_timing(response, header):
    value = response.headers.get(header, "")
    return float(value) if len(value) <= 16 and value.replace(".", "", 1).isdigit() else None


def process(row):
    ident = row["id"]
    capture = row["digest"] == "capture"
    from sessions import STREAM_BLOCK

    began = time.monotonic()
    streaming = bool(row["streaming"])
    index = row["done"]
    window = BLOCK // STREAM_BLOCK if streaming else 1
    end = min(index + window, row["total"])
    preview = bool(row["is_preview"])
    if preview:
        end = row["full_until"]
    task = row["task"] or "translate"
    if capture:
        with database() as db:
            arrival = db.execute(
                "SELECT max(received) FROM fragment_arrivals WHERE job=? AND idx>=? AND idx<?",
                (ident,index,end),
            ).fetchone()[0]
        queue_ms = round(max(0, time.time() - arrival) * 1000, 1) if arrival is not None else None
    samples = bytearray()
    try:
        if capture:
            for part in range(index, end):
                samples.extend((AUDIO / f"{ident}.{part}.pcm").read_bytes())
        else:
            with (AUDIO / (ident + ".pcm")).open("rb") as src:
                src.seek(index * BLOCK)
                samples.extend(src.read(BLOCK))
    except FileNotFoundError:
        with database() as db:
            finish_error(db,ident,"input_missing")
        return
    block = io.BytesIO()
    with wave.open(block,"wb") as wav:
        wav.setparams((1,2,16000,0,"NONE","not compressed"))
        wav.writeframes(samples)
    preparation_ms = telemetry.ms(began)
    worker_ms = model_ready_ms = 0
    worker_request_ms = 0
    lane_wait_ms = 0
    worker_probe_ms = 0
    if not any(samples):
        segment=""
    else:
        lane_start = time.monotonic()
        with legacy.inference_lane("jobs"):
            lane_wait_ms = telemetry.ms(lane_start)
            if preview:
                with database() as db:
                    closed = db.execute("SELECT closed FROM sessions WHERE id=?", (ident,)).fetchone()[0]
                    if closed:
                        db.execute("UPDATE jobs SET state='queued' WHERE id=?", (ident,))
                        return
            probe_start = time.monotonic()
            ready = legacy.upstream_client.get(legacy.worker_url+"/can-transcribe",allow_redirects=False,timeout=(2,7))
            if ready.status_code==503 and not preview:
                # Busy after an interrupted response is occupancy, not a new inference failure.
                with database() as db:
                    db.execute("UPDATE jobs SET state='queued',ready=? WHERE id=?",(time.time()+2,ident))
                return
            ready.raise_for_status()
            worker_probe_ms = telemetry.ms(probe_start)
            attempt=secrets.token_hex(16)
            tags={"X-Dispatch-Job":ident,"X-Dispatch-Index":str(row["done"]),"X-Dispatch-Token":attempt,"X-Dispatch-Profile":PROFILE}
            options = {"task": task}
            if task == "transcribe":
                options["language"] = "es"
            start = time.monotonic()
            response = legacy.upstream_client.post(legacy.worker_url+"/transcribe",allow_redirects=False,files={"audio":("segment.wav",block.getvalue(),"audio/wav")},
                                     data=options,headers=tags,timeout=(2,590))
            worker_request_ms = telemetry.ms(start)
            worker_ms = worker_timing(response, "X-Inference-Ms")
            model_ready_ms = worker_timing(response, "X-Model-Ready-Ms")
            response.raise_for_status()
            if any(response.headers.get(k)!=v for k,v in tags.items()):
                raise ValueError("Unconfirmed worker dispatch")
        segment = response.json()["text"].strip()
    inference_finished = time.monotonic()
    retained = {word.strip().casefold() for word in os.getenv("IV_RETAINED_TERMS", "").split(",") if word.strip()}
    if (task == "translate" and not preview and segment.strip(" .!?").casefold() not in retained
            and segment and any(c.isalpha() for c in segment)
            and detector.detect_language_of(segment) != Language.ENGLISH):
        with database() as db:
            finish_error(db,ident,"english_output_unavailable")
        return
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        current=db.execute("SELECT state,profile,done,total FROM jobs WHERE id=?",(ident,)).fetchone()
        if not current or current["state"]!="processing" or current["done"]!=row["done"] or current["profile"]!=PROFILE:
            return
        db.execute("UPDATE performance SET inference_seconds=inference_seconds+? WHERE id=?",
                   (worker_request_ms / 1000,ident))
        if preview:
            db.execute("UPDATE session_options SET preview_until=?,preview=? WHERE id=?",
                       (end,segment,ident))
            db.execute("UPDATE jobs SET state='queued',ready=?,retries=0 WHERE id=?", (time.time(),ident))
        else:
            db.execute("INSERT INTO segments VALUES(?,?,?)",(ident,index // window,segment))
            db.execute("UPDATE jobs SET done=?,state='queued',ready=?,retries=0 WHERE id=?",
                       (end,time.time(),ident))
            if streaming:
                db.execute("UPDATE session_options SET preview_until=?,preview='' WHERE id=?", (end,ident))
            if end == current["total"]:
                if not capture or db.execute("SELECT closed FROM sessions WHERE id=?",(ident,)).fetchone()[0]:
                    publish(db,ident)
    if capture:
        telemetry.emit("result_available", ident, index=index, until=end, preview=preview,
                       queue_ms=queue_ms, preparation_ms=preparation_ms, lane_wait_ms=lane_wait_ms,
                       worker_probe_ms=worker_probe_ms, worker_request_ms=worker_request_ms,
                       inference_ms=worker_ms, model_ready_ms=model_ready_ms,
                       publish_ms=telemetry.ms(inference_finished), attempt_to_available_ms=telemetry.ms(began))



def publish(db,ident):
    result = " ".join(r[0] for r in db.execute("SELECT text FROM segments WHERE job=? ORDER BY idx",(ident,)) if r[0])
    db.execute("UPDATE jobs SET result=?,state='succeeded',finished=? WHERE id=?",(result,time.time(),ident))
    if not db.execute("SELECT 1 FROM sessions WHERE id=?",(ident,)).fetchone():
        db.execute("DELETE FROM segments WHERE job=?",(ident,))



def maintain():
    now = time.time()
    with admission, database() as db:
        with rejection_lock:
            for code,count in rejections.items():
                db.execute("INSERT INTO rejections VALUES(?,?) ON CONFLICT(code) DO UPDATE SET count=count+excluded.count",(code,count))
            rejections.clear()
        drained=ROOT/"drained"
        if (ROOT/"drain").exists() and not uploads and not db.execute("SELECT 1 FROM jobs WHERE finished IS NULL LIMIT 1").fetchone():
            if legacy.legacy_slot.acquire(blocking=False):
                try:
                    drained.touch()
                finally:
                    legacy.legacy_slot.release()
        else:
            drained.unlink(missing_ok=True)
        db.execute("INSERT OR REPLACE INTO heartbeat VALUES(1,?,?,?)",(now,PROFILE,int(not paused())))
        db.execute("DELETE FROM performance WHERE finished<?",(now-setting("telemetry_days",30)*86400,))
        db.execute("UPDATE jobs SET state='failed',finished=?,error='capture_expired' WHERE digest='capture' AND finished IS NULL AND created<?",(now,now-setting("failed_seconds",86400)))
        for row in db.execute("SELECT id FROM jobs WHERE finished IS NOT NULL"):
            for path in AUDIO.glob(row[0]+"*.pcm"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    db.execute("UPDATE performance SET cleanup='retry' WHERE id=?",(row[0],))
                    break
            else:
                db.execute("UPDATE performance SET cleanup='complete' WHERE id=?",(row[0],))
        db.execute("DELETE FROM segments WHERE job IN (SELECT id FROM jobs WHERE finished < ?)",(now-setting("result_seconds",3600),))
        db.execute("UPDATE jobs SET state='expired',result=NULL WHERE finished < ?",(now-setting("result_seconds",3600),))
        db.execute("""UPDATE session_options SET preview='' WHERE preview!='' AND
                      id IN (SELECT id FROM jobs WHERE state='expired')""")
        for table,key in (("fragments","job"),("fragment_arrivals","job"),("session_options","id"),("sessions","id")):
            db.execute(f"DELETE FROM {table} WHERE {key} IN (SELECT id FROM jobs WHERE finished < ?)",(now-setting("result_seconds",3600)-setting("tombstone_seconds",86400),))
        db.execute("DELETE FROM jobs WHERE finished < ?",(now-setting("result_seconds",3600)-setting("tombstone_seconds",86400),))
        db.execute("UPDATE jobs SET state='failed',error='profile_unavailable',finished=? WHERE state='queued' AND profile!=? AND created<?",(now,PROFILE,now-setting("failed_seconds",86400)))


def runnable(db):
    from sessions import STREAM_BLOCK

    # A short last upload is reserved for finalization. Refresh the preview after
    # the first piece, then grow the interval with the amount already decoded.
    return db.execute("""
        WITH candidates AS (
            SELECT j.*,s.closed,o.streaming,o.task,o.preview_until,
                   coalesce(o.streaming,0) AND NOT s.closed AND j.total-j.done<? AS is_preview,
                   j.total - EXISTS(
                       SELECT 1 FROM fragments f WHERE f.job=j.id AND
                       f.idx=j.total-1 AND f.size<?
                   ) AS full_until
            FROM jobs j LEFT JOIN sessions s ON s.id=j.id
            LEFT JOIN session_options o ON o.id=j.id
            WHERE j.state='queued' AND j.done<j.total AND j.profile=? AND j.ready<=?
        )
        SELECT * FROM candidates
        WHERE NOT coalesce(streaming,0) OR closed OR total-done>=?
           OR (full_until>done AND (preview_until=done OR full_until-preview_until>=max(2,preview_until-done)))
        ORDER BY is_preview,ready,created
    """, (BLOCK//STREAM_BLOCK,STREAM_BLOCK,PROFILE,time.time(),BLOCK//STREAM_BLOCK)).fetchall()


def run():
    global last_owner
    maintenance_at = 0
    while True:
        try:
            if time.monotonic() >= maintenance_at:
                maintain()
                maintenance_at = time.monotonic()+(1 if (ROOT/"drain").exists() else 15)
            with database() as db:
                rows = runnable(db)
                owners = sorted({r["owner"] for r in rows})
                if owners:
                    if not rows[0]["is_preview"]:
                        rows = [row for row in rows if not row["is_preview"]]
                        owners = sorted({row["owner"] for row in rows})
                    owner = next((o for o in owners if o > (last_owner or "")), owners[0])
                    last_owner = owner
                    row = next(r for r in rows if r["owner"] == owner)
                    db.execute("UPDATE performance SET attempts=attempts+1,first_started=coalesce(first_started,?) WHERE id=?",(time.time(),row["id"]))
                    db.execute("UPDATE jobs SET state='processing' WHERE id=?",(row["id"],))
            if not owners:
                time.sleep(.25)
                continue
            try:
                process(row)
            except (requests.RequestException, ValueError, KeyError, OSError):
                with database() as db:
                    if row["is_preview"]:
                        db.execute("UPDATE session_options SET preview_until=? WHERE id=?",
                                   (row["full_until"],row["id"]))
                        db.execute("UPDATE jobs SET state='queued' WHERE id=?", (row["id"],))
                        telemetry.emit("preview_failed", row["id"], until=row["full_until"])
                    elif row["retries"]+1 >= setting("attempts",3):
                        finish_error(db,row["id"],"worker_unavailable")
                    else:
                        db.execute("UPDATE jobs SET state='queued',retries=retries+1,ready=? WHERE id=?",(time.time()+2**(1+row["retries"]),row["id"]))
        except Exception:
            # Let Compose restart and reconcile any uncommitted segment.
            os._exit(1)
