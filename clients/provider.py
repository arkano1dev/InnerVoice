#!/usr/bin/env python3
"""Command-based STT provider. Python 3.11+, requests."""
import argparse
import contextlib
import re
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import random
import secrets
import sys
import tempfile
import time
try:
    import tomllib
except ImportError:  # Python 3.10 on Ubuntu 22.04
    import tomli as tomllib
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter


def atomic(path,data):
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(out.name,path)
    fd=os.open(path.parent,os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Client:
    def __init__(self,cfg):
        self.base=cfg["url"].rstrip("/")
        url=urlsplit(self.base)
        if url.scheme!="https" or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
            raise ValueError("Invalid private HTTPS endpoint")
        self.session=requests.Session()
        self.session.trust_env=False
        self.session.verify=cfg.get("ca") or True
        self.session.headers["Authorization"]="Bearer "+Path(cfg["token_file"]).read_text().strip()
        address=(cfg.get("source_ip") or "",int(cfg.get("source_port") or 0))
        if any(address):
            class Bound(HTTPAdapter):
                def init_poolmanager(self,*args,**kwargs):
                    super().init_poolmanager(*args,source_address=address,**kwargs)
            self.session.mount("https://",Bound())

    def call(self,method,path,timeout=30,**kwargs):
        response=self.session.request(method,self.base+path,timeout=timeout,allow_redirects=False,**kwargs)
        if response.status_code not in (200,201,202):
            raise requests.HTTPError(str(response.status_code),response=response)
        return response.json()


def run(args):
    with open(args.config,"rb") as stream:
        cfg=tomllib.load(stream)["client"]
    client=Client(cfg)
    root=Path(cfg["state_dir"])
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    # Keep state only for the configured replay horizon. Files are owner-only.
    horizon=cfg.get("state_retention_seconds",86400)
    for old in root.glob("*.json"):
        if old.stat().st_mtime < time.time()-horizon:
            with old.with_suffix(".lock").open("a") as guard:
                try:
                    fcntl.flock(guard,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if old.exists() and old.stat().st_mtime < time.time()-horizon:
                    old.unlink()
    identity=os.environ.get("INNERVOICE_EVENT_ID") or str(args.output.resolve())
    with (root/"identity.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        key=root/"identity.key"
        if not key.exists():
            atomic(key,secrets.token_bytes(32))
        ident=hmac.new(key.read_bytes(),identity.encode(),hashlib.sha256).hexdigest()
    record=root/(ident+".json")
    deadline=time.monotonic()+cfg.get("deadline",300)
    with (root/(ident+".lock")).open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=json.loads(record.read_bytes()) if record.exists() else {"key":secrets.token_urlsafe(32)}
        if state.get("publishing"):
            raise ValueError("Ambiguous delivery")
        atomic(record,json.dumps(state).encode())
        delay=.5
        while time.monotonic()<deadline:
            timeout=max(.1,min(30,deadline-time.monotonic()))
            try:
                if "job" not in state:
                    with args.input.open("rb") as audio:
                        job=client.call("POST","/v1/jobs",timeout,headers={"Idempotency-Key":state["key"]},
                                        files={"audio":("note",audio,"application/octet-stream")})
                    if not re.fullmatch(r"[a-f0-9]{32}",job["job_id"]):
                        raise ValueError("Invalid job identity")
                    state["job"]=job["job_id"]
                    atomic(record,json.dumps(state).encode())
                job=client.call("GET","/v1/jobs/"+state["job"],timeout)
                if job["state"]=="failed":
                    raise ValueError("Gateway job failed")
                if job["state"]=="succeeded":
                    result=job["result"]
                    if result["language"]!="en" or not isinstance(result["text"],str) or result["speech_detected"] is not bool(result["text"].strip()):
                        raise ValueError("Invalid result")
                    state["publishing"]=True
                    atomic(record,json.dumps(state).encode())
                    atomic(args.output,result["text"].encode())
                    return
            except requests.HTTPError as error:
                if error.response.status_code not in (429,502,503,504):
                    raise
            except (requests.Timeout,requests.ConnectionError):
                pass
            time.sleep(min(max(0,deadline-time.monotonic()),delay*random.uniform(.8,1.2)))
            delay=min(2,delay*2)
    raise TimeoutError()


if __name__=="__main__":
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",required=True)
    parser.add_argument("input",type=Path)
    parser.add_argument("output",type=Path)
    try:
        run(parser.parse_args())
    except (OSError,ValueError,KeyError,requests.RequestException):
        sys.exit("InnerVoice transcription unavailable or delivery requires review")
