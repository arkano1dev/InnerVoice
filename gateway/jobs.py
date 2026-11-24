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
from lingua import Language, LanguageDetectorBuilder

import legacy

api = Flask(__name__)
def setting(name, default):
    return int(os.environ.get("IV_"+name.upper()) or default)


LIMIT = setting("upload_mib",128) * 1024**2
PCM_LIMIT = setting("max_seconds",3600) * 32000
BLOCK = 30 * 32000
api.config.update(MAX_CONTENT_LENGTH=LIMIT + 65536, MAX_FORM_PARTS=3)
ROOT = Path(os.environ.get("STATE_DIR", "/state"))
PROFILE = os.environ.get("PROFILE_REVISION", "")
admission = threading.Lock()
rejection_lock = threading.Lock()
rejections = {}
uploads = set()
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
    tempfile.tempdir=str(ROOT)
    with database() as db:
        db.execute("PRAGMA journal_mode=WAL")
        if db.execute("PRAGMA user_version").fetchone()[0] not in (0,1):
            raise RuntimeError("Unsupported database schema")
        db.executescript("""
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
        known = {row[0] for row in db.execute("SELECT id FROM jobs")}
        known.update(f"{r[0]}.{r[1]}" for r in db.execute("SELECT job,idx FROM fragments"))
    for file in ROOT.iterdir():
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
    with open("/run/innervoice/clients.json") as stream:
        clients = json.load(stream)
    for owner, digest in clients.items():
        if hmac.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), digest):
            request.environ["innervoice.owner"] = owner
            return
    return failure("unauthorized", 401)


@api.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def sync_dir():
    fd = os.open(ROOT, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def accepted(row):
    return {"job_id": row["id"], "state": row["state"], "status_url": "/v1/jobs/" + row["id"]}, 202


def paused():
    return os.environ.get("ENABLE_JOBS") != "1" or not PROFILE or (ROOT / "drain").exists()


def space_available(budget):
    used = sum(p.stat().st_size for p in ROOT.glob("*.pcm"))
    return used + reserved + budget <= setting("quota_mib",1024)*1024**2 and shutil.disk_usage(ROOT).free >= reserved + budget + setting("free_floor_mib",64)*1024**2


def reserve(owner, ident):
    global reserved
    with admission, database() as db:
        if sum(o == owner for o, _ in uploads) >= 1:
            return failure("client_capacity", 429)
        if len(uploads) >= setting("uploads",2):
            return failure("upload_capacity", 503)
        budget = 2 * LIMIT + PCM_LIMIT + 65536
        if not space_available(budget):
            return failure("storage_capacity", 503)
        uploads.add((owner, ident))
        reserved += budget


@api.post("/v1/jobs")
def submit():
    global reserved
    owner = request.environ["innervoice.owner"]
    key = request.headers.get("Idempotency-Key", "")
    if not 1 <= len(key) <= 128:
        return failure("invalid_key", 400)
    if paused():
        return failure("admission_disabled", 503)
    ident = uuid.uuid4().hex
    denied = reserve(owner, ident)
    if denied:
        return denied
    started=time.time()
    source = ROOT / (ident + ".upload")
    pcm = ROOT / (ident + ".part")
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
        with database() as db:
            old = db.execute("SELECT * FROM jobs WHERE owner=? AND replay=?", (owner,key)).fetchone()
        if old:
            if old["digest"] != signature:
                return failure("idempotency_conflict", 409)
            return failure("expired", 410) if old["state"] == "expired" else accepted(old)
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
                return accepted(old) if old["digest"] == signature else failure("idempotency_conflict",409)
            if paused():
                return failure("admission_disabled",503)
            counts = db.execute("SELECT count(*),sum(owner=?) FROM jobs WHERE state IN ('queued','processing')",(owner,)).fetchone()
            if (counts[1] or 0) >= setting("jobs_per_owner",4):
                return failure("client_capacity",429)
            if counts[0] >= setting("jobs",8):
                return failure("queue_capacity",503)
            pcm.rename(ROOT / (ident + ".pcm"))
            sync_dir()
            db.execute("INSERT INTO jobs(id,owner,replay,digest,profile,state,created,total) VALUES(?,?,?,?,?,'queued',?,?)",
                       (ident,owner,key,signature,PROFILE,time.time(),math.ceil(size/BLOCK)))
            db.execute("INSERT INTO performance(id,owner,profile,created,state,total,audio_seconds,admission_seconds,decode_seconds) VALUES(?,?,?,?,'queued',?,?,?,?)",
                       (ident,owner,PROFILE,started,math.ceil(size/BLOCK),size/32000,time.time()-started,time.monotonic()-decode_start))
        return accepted({"id": ident,"state":"queued"})
    except subprocess.TimeoutExpired:
        return failure("decode_timeout",422)
    finally:
        source.unlink(missing_ok=True)
        pcm.unlink(missing_ok=True)
        with admission:
            uploads.remove((owner, ident))
            reserved -= 2 * LIMIT + PCM_LIMIT + 65536


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
        body["result"] = dict(text=row["result"],language="en",speech_detected=bool(row["result"]))
    if row["error"]:
        body["error"] = dict(code=row["error"],retryable=False)
    return body


@api.get("/v1/capabilities")
def capabilities():
    return dict(api_version=1,jobs=True,sessions=True,fragment_seconds=30,output_language="en")


def finish_error(db, ident, reason):
    db.execute("UPDATE jobs SET state='failed',finished=?,error=? WHERE id=?", (time.time(),reason,ident))


def process(row):
    ident = row["id"]
    capture = row["digest"] == "capture"
    path = ROOT / (f"{ident}.{row['done']}.pcm" if capture else ident + ".pcm")
    if not path.exists():
        with database() as db:
            finish_error(db,ident,"input_missing")
        return
    block = io.BytesIO()
    with path.open("rb") as src, wave.open(block,"wb") as wav:
        src.seek(0 if capture else row["done"] * BLOCK)
        wav.setparams((1,2,16000,0,"NONE","not compressed"))
        samples=src.read(BLOCK)
        wav.writeframes(samples)
    start=time.monotonic()
    if not any(samples):
        segment=""
    else:
        with legacy.inference_lane("jobs"):
            ready = requests.get(legacy.worker_url+"/can-transcribe",timeout=(2,7))
            if ready.status_code==503:
                # Busy after an interrupted response is occupancy, not a new inference failure.
                with database() as db:
                    db.execute("UPDATE jobs SET state='queued',ready=? WHERE id=?",(time.time()+2,ident))
                return
            ready.raise_for_status()
            attempt=secrets.token_hex(16)
            tags={"X-Dispatch-Job":ident,"X-Dispatch-Index":str(row["done"]),"X-Dispatch-Token":attempt,"X-Dispatch-Profile":PROFILE}
            start=time.monotonic()
            response = requests.post(legacy.worker_url+"/transcribe",files={"audio":("segment.wav",block.getvalue(),"audio/wav")},
                                     data={"task":"translate"},headers=tags,timeout=(2,590))
            response.raise_for_status()
            if any(response.headers.get(k)!=v for k,v in tags.items()):
                raise ValueError("Unconfirmed worker dispatch")
        segment = response.json()["text"].strip()
    retained={word.strip().casefold() for word in os.getenv("IV_RETAINED_TERMS","").split(",") if word.strip()}
    if segment.strip(" .!?").casefold() not in retained and segment and any(c.isalpha() for c in segment) and detector.detect_language_of(segment) != Language.ENGLISH:
        with database() as db:
            finish_error(db,ident,"english_output_unavailable")
        return
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        current=db.execute("SELECT state,profile,done,total FROM jobs WHERE id=?",(ident,)).fetchone()
        if not current or current["state"]!="processing" or current["done"]!=row["done"] or current["profile"]!=PROFILE:
            return
        db.execute("UPDATE performance SET inference_seconds=inference_seconds+? WHERE id=?",(time.monotonic()-start,ident))
        db.execute("INSERT INTO segments VALUES(?,?,?)",(ident,row["done"],segment))
        now = time.time()
        db.execute("UPDATE jobs SET done=done+1,state='queued',ready=?,retries=0 WHERE id=?", (now,ident))
        if row["done"]+1 == current["total"]:
            if not capture or db.execute("SELECT closed FROM sessions WHERE id=?",(ident,)).fetchone()[0]:
                publish(db,ident)


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
        for row in db.execute("SELECT id FROM jobs WHERE state='succeeded' OR finished < ?",(now-setting("failed_seconds",86400),)):
            for path in ROOT.glob(row[0]+"*.pcm"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    db.execute("UPDATE performance SET cleanup='retry' WHERE id=?",(row[0],))
                    break
            else:
                db.execute("UPDATE performance SET cleanup='complete' WHERE id=?",(row[0],))
        db.execute("DELETE FROM segments WHERE job IN (SELECT id FROM jobs WHERE finished < ?)",(now-setting("result_seconds",3600),))
        db.execute("UPDATE jobs SET state='expired',result=NULL WHERE finished < ?",(now-setting("result_seconds",3600),))
        for table,key in (("fragments","job"),("sessions","id")):
            db.execute(f"DELETE FROM {table} WHERE {key} IN (SELECT id FROM jobs WHERE finished < ?)",(now-setting("result_seconds",3600)-setting("tombstone_seconds",86400),))
        db.execute("DELETE FROM jobs WHERE finished < ?",(now-setting("result_seconds",3600)-setting("tombstone_seconds",86400),))
        db.execute("UPDATE jobs SET state='failed',error='profile_unavailable',finished=? WHERE state='queued' AND profile!=? AND created<?",(now,PROFILE,now-setting("failed_seconds",86400)))


def run():
    global last_owner
    maintenance_at = 0
    while True:
        try:
            if time.monotonic() >= maintenance_at:
                maintain()
                maintenance_at = time.monotonic()+(1 if (ROOT/"drain").exists() else 15)
            with database() as db:
                rows = db.execute("SELECT * FROM jobs WHERE state='queued' AND done<total AND profile=? AND ready<=? ORDER BY ready,created",
                                  (PROFILE,time.time())).fetchall()
                owners = sorted({r["owner"] for r in rows})
                if owners:
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
                    if row["retries"]+1 >= setting("attempts",3):
                        finish_error(db,row["id"],"worker_unavailable")
                    else:
                        db.execute("UPDATE jobs SET state='queued',retries=retries+1,ready=? WHERE id=?",(time.time()+2**(1+row["retries"]),row["id"]))
        except Exception:
            # Let Compose restart and reconcile any uncommitted segment.
            os._exit(1)
