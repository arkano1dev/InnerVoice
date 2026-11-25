"""Owner-scoped recording sessions; only the contiguous prefix is schedulable."""
import hashlib
import hmac
import io
import os
import time
import uuid
import wave
from flask import request

import jobs
import telemetry


STREAM_SECONDS = 2
STREAM_BLOCK = STREAM_SECONDS * 32000
TASKS = ("translate", "transcribe")


def schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, closed INTEGER DEFAULT 0, last INTEGER);
    CREATE TABLE IF NOT EXISTS fragments(job TEXT, idx INTEGER, digest TEXT, size INTEGER, PRIMARY KEY(job,idx));
    CREATE TABLE IF NOT EXISTS session_options(
        id TEXT PRIMARY KEY, task TEXT NOT NULL, streaming INTEGER NOT NULL,
        preview_until INTEGER NOT NULL DEFAULT 0, preview TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS fragment_arrivals(
        job TEXT, idx INTEGER, received REAL NOT NULL, PRIMARY KEY(job, idx)
    );
    """)


def lookup(db,ident):
    return db.execute("""SELECT j.*,s.closed,s.last,o.task,o.streaming,o.preview
                         FROM sessions s JOIN jobs j ON s.id=j.id
                         LEFT JOIN session_options o ON o.id=j.id
                         WHERE j.id=? AND j.owner=?""",(ident,request.environ["innervoice.owner"])).fetchone()


def routes(api):
    @api.post("/v1/sessions")
    def create():
        owner=request.environ["innervoice.owner"]
        key=request.headers.get("Idempotency-Key","")
        if not 1<=len(key)<=128:
            return jobs.failure("invalid_key",400)
        options = request.get_json(silent=True) if request.is_json else {}
        if not isinstance(options, dict):
            return jobs.failure("invalid_session_options",400)
        task = options.get("task", "translate")
        streaming = options.get("streaming", False)
        if task not in TASKS or type(streaming) is not bool:
            return jobs.failure("invalid_session_options",400)
        with jobs.admission,jobs.database() as db:
            old=db.execute("SELECT id,state,digest FROM jobs WHERE replay=? AND owner=?",(key,owner)).fetchone()
            if old:
                if old["digest"]!="capture":
                    return jobs.failure("idempotency_conflict",409)
                if old["state"]=="expired":
                    return jobs.failure("expired",410)
                saved = db.execute("SELECT task,streaming FROM session_options WHERE id=?", (old["id"],)).fetchone()
                if (tuple(saved) if saved else ("translate", 0)) != (task, streaming):
                    return jobs.failure("idempotency_conflict",409)
                return {"session_id":old["id"]},200
            if jobs.paused():
                return jobs.failure("admission_disabled",503)
            counts=db.execute("SELECT count(*),sum(owner=?) FROM jobs WHERE finished IS NULL",(owner,)).fetchone()
            if (counts[1] or 0)>=jobs.setting("jobs_per_owner",4) or counts[0]>=jobs.setting("jobs",8):
                return jobs.failure("queue_capacity",429 if (counts[1] or 0)>=jobs.setting("jobs_per_owner",4) else 503)
            ident=uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,owner,replay,digest,profile,state,total,created) VALUES(?,?,?,'capture',?,'queued',0,?)",
                       (ident,owner,key,jobs.PROFILE,time.time()))
            db.execute("INSERT INTO sessions(id) VALUES(?)",(ident,))
            db.execute("INSERT INTO session_options(id,task,streaming) VALUES(?,?,?)",
                       (ident,task,streaming))
            return {"session_id":ident},201

    @api.get("/v1/sessions/<ident>")
    def progress(ident):
        with jobs.database() as db:
            db.execute("BEGIN")
            row=lookup(db,ident)
            if not row:
                return jobs.failure("unknown_session",404)
            if row["state"]=="expired":
                return jobs.failure("expired",410)
            indexes=[r[0] for r in db.execute("SELECT idx FROM fragments WHERE job=? ORDER BY idx",(ident,))]
            parts=[dict(index=r[0],text=r[1]) for r in db.execute("SELECT idx,text FROM segments WHERE job=? ORDER BY idx",(ident,))]
            body = dict(session_id=ident,state=row["state"],received=indexes,
                        segments=parts,closed=bool(row["closed"]))
            if row["streaming"]:
                body["preview"] = row["preview"]
            return body

    @api.put("/v1/sessions/<ident>/fragments/<int:index>")
    def upload(ident,index):
        began = time.monotonic()
        if index>=(jobs.PCM_LIMIT+STREAM_BLOCK-1)//STREAM_BLOCK or request.content_length is None or request.content_length>jobs.BLOCK+4096:
            return jobs.failure("fragment_limit",413)
        data=request.get_data(cache=False)
        try:
            with wave.open(io.BytesIO(data),"rb") as wav:
                if (wav.getframerate(),wav.getsampwidth(),wav.getnchannels())!=(16000,2,1):
                    return jobs.failure("unsupported_audio",415)
                pcm=wav.readframes(wav.getnframes())
                if not pcm or len(pcm)!=wav.getnframes()*2 or len(pcm)>jobs.BLOCK:
                    return jobs.failure("invalid_fragment",422)
        except (wave.Error,EOFError):
            return jobs.failure("invalid_fragment",422)
        digest=hmac.new((jobs.ROOT/"fingerprint.key").read_bytes(),data,hashlib.sha256).hexdigest()
        with jobs.admission,jobs.database() as db:
            row=lookup(db,ident)
            if not row:
                return jobs.failure("unknown_session",404)
            if row["state"]=="expired":
                return jobs.failure("expired",410)
            block = STREAM_BLOCK if row["streaming"] else jobs.BLOCK
            if len(pcm)>block or index>=(jobs.PCM_LIMIT+block-1)//block:
                return jobs.failure("fragment_limit",413)
            old=db.execute("SELECT digest FROM fragments WHERE job=? AND idx=?",(ident,index)).fetchone()
            if old:
                if old[0]!=digest:
                    return jobs.failure("fragment_conflict",409)
                return {"index":index},200
            if row["closed"] or row["finished"]:
                return jobs.failure("session_closed",409)
            duration_bytes=db.execute("SELECT coalesce(sum(size),0) FROM fragments WHERE job=?",(ident,)).fetchone()[0]
            if row["streaming"] and db.execute(
                """SELECT 1 FROM fragments WHERE job=? AND
                   ((idx<? AND size<?) OR (idx>? AND ?<?)) LIMIT 1""",
                (ident,index,block,index,len(pcm),block),
            ).fetchone():
                return jobs.failure("invalid_fragment",422)
            if duration_bytes+len(pcm)>jobs.PCM_LIMIT:
                return jobs.failure("duration_limit",413)
            if not jobs.space_available(len(pcm)):
                return jobs.failure("storage_capacity",503)
            path=jobs.AUDIO/f"{ident}.{index}.pcm"
            temp=path.with_suffix(".part")
            with temp.open("wb") as stream:
                stream.write(pcm)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(path)
            jobs.sync_dir(jobs.AUDIO)
            db.execute("INSERT INTO fragments VALUES(?,?,?,?)",(ident,index,digest,len(pcm)))
            db.execute("INSERT INTO fragment_arrivals VALUES(?,?,?)",(ident,index,time.time()))
            total=row["total"]
            while db.execute("SELECT 1 FROM fragments WHERE job=? AND idx=?",(ident,total)).fetchone():
                total+=1
            db.execute("UPDATE jobs SET total=? WHERE id=?",(total,ident))
        telemetry.emit("fragment_received", ident, index=index,
                       receive_ms=telemetry.ms(began), audio_ms=len(pcm)/32)
        return {"index":index},201

    @api.post("/v1/sessions/<ident>/finalize")
    def finalize(ident):
        began = time.monotonic()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jobs.failure("invalid_last_index", 400)
        last=body.get("last_index")
        if type(last) is not int or not 0<=last<(jobs.PCM_LIMIT+STREAM_BLOCK-1)//STREAM_BLOCK:
            return jobs.failure("invalid_last_index",400)
        with jobs.admission,jobs.database() as db:
            db.execute("BEGIN IMMEDIATE")
            row=lookup(db,ident)
            if not row:
                return jobs.failure("unknown_session",404)
            if row["state"]=="expired":
                return jobs.failure("expired",410)
            if row["closed"]:
                if row["last"]!=last:
                    return jobs.failure("finalize_conflict",409)
                return jobs.accepted(row)
            if row["finished"]:
                return jobs.failure("session_closed",409)
            block = STREAM_BLOCK if row["streaming"] else jobs.BLOCK
            if last >= (jobs.PCM_LIMIT+block-1)//block:
                return jobs.failure("invalid_last_index",400)
            have={r[0] for r in db.execute("SELECT idx FROM fragments WHERE job=?",(ident,))}
            missing=sorted(set(range(last+1))-have)
            if missing:
                return {"error":{"code":"missing_fragments","missing":missing}},409
            if any(i>last for i in have):
                if body.get("recover_prefix") is not True or last+1!=row["total"]:
                    return jobs.failure("finalize_conflict",409)
                for idx in have:
                    if idx>last:
                        (jobs.AUDIO/f"{ident}.{idx}.pcm").unlink(missing_ok=True)
                db.execute("DELETE FROM fragments WHERE job=? AND idx>?",(ident,last))
                db.execute("DELETE FROM fragment_arrivals WHERE job=? AND idx>?",(ident,last))
            db.execute("UPDATE sessions SET closed=1,last=? WHERE id=?",(last,ident))
            already_inferred = row["done"] == last + 1
            if already_inferred:
                jobs.publish(db,ident)
        telemetry.emit("session_finalized", ident, duration_ms=telemetry.ms(began),
                       fragments=last+1, result_available=already_inferred)
        return jobs.accepted(row)
