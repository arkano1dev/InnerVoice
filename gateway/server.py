"""Both listening sockets share one process and inference scheduler."""
import fcntl
import os
import secrets
import ssl
import threading

from cheroot.wsgi import Server
from cheroot.ssl.builtin import BuiltinSSLAdapter
import jobs
import legacy
import sessions


def main():
    os.umask(0o077)
    internal = Server(("0.0.0.0",9000), legacy.app,numthreads=8)
    if os.environ.get("PRIVATE_API") != "1":
        internal.start()
        return
    jobs.ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (jobs.ROOT / "gateway.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        key = jobs.ROOT / "fingerprint.key"
        if not key.exists():
            temp = key.with_suffix(".part")
            with temp.open("wb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(key)
            jobs.sync_dir()
        jobs.initialize()
        sessions.routes(jobs.api)
        external = Server(("0.0.0.0",9443),jobs.api,numthreads=12)
        external.max_request_body_size = jobs.LIMIT + 65536
        external.ssl_adapter = BuiltinSSLAdapter("/run/innervoice/tls.crt","/run/innervoice/tls.key")
        external.ssl_adapter.context.minimum_version = ssl.TLSVersion.TLSv1_2
        external.prepare()
        internal.prepare()
        threading.Thread(target=jobs.run,daemon=True).start()
        threading.Thread(target=internal.serve,daemon=True).start()
        external.serve()


if __name__ == "__main__":
    main()
