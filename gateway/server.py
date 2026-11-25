"""Both listening sockets share one process and inference scheduler."""
import fcntl
import ipaddress
import os
import secrets
import ssl
import threading

from cheroot.wsgi import Server
from cheroot.ssl.builtin import BuiltinSSLAdapter
import jobs
from private_memory import require_private_memory
import legacy
import sessions


def restrict_to_networks(app, setting):
    networks = tuple(ipaddress.IPv4Network(value.strip()) for value in setting.split(",") if value.strip())
    if not networks:
        raise ValueError("Private listener requires allowed client networks")

    def application(environ, start_response):
        try:
            peer = ipaddress.IPv4Address(environ.get("REMOTE_ADDR", ""))
        except ipaddress.AddressValueError:
            peer = None
        if peer is not None and any(peer in network for network in networks):
            return app(environ, start_response)
        start_response("403 Forbidden", [("Content-Type", "application/json"), ("Cache-Control", "no-store")])
        return [b'{"error":{"code":"client_network_denied","retryable":false}}']

    return application


def wait_for_activation(app):
    def application(environ, start_response):
        if environ["REQUEST_METHOD"] in ("POST", "PUT", "PATCH", "DELETE") and (jobs.ROOT / "startup-pending").exists():
            start_response("503 Service Unavailable", [("Content-Type", "application/json"), ("Cache-Control", "no-store")])
            return [b'{"error":{"code":"admission_disabled","retryable":true}}']
        return app(environ, start_response)

    return application


def main():
    os.umask(0o077)
    if os.getuid() == 0 or os.getgid() == 0:
        raise RuntimeError("The gateway requires a non-root identity")
    require_private_memory("/tmp")
    telegram = os.environ.get("IV_TELEGRAM") == "1"
    if telegram:
        internal = Server("/run/telegram-socket/gateway.sock", wait_for_activation(legacy.app), numthreads=4)
        internal.max_request_body_size = 8 * 1024**2
    if os.environ.get("PRIVATE_API") != "1":
        raise RuntimeError("Start the private Gateway using bin/innervoice")
    private_app = restrict_to_networks(wait_for_activation(jobs.api), os.environ.get("IV_ALLOWED_NETWORKS", ""))
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
        external = Server(("0.0.0.0",9443),private_app,numthreads=12)
        external.max_request_body_size = jobs.LIMIT + 65536
        external.ssl_adapter = BuiltinSSLAdapter("/run/innervoice/tls.crt","/run/innervoice/tls.key")
        external.ssl_adapter.context.minimum_version = ssl.TLSVersion.TLSv1_2
        external.prepare()
        if telegram:
            internal.prepare()
        threading.Thread(target=jobs.run,daemon=True).start()
        if telegram:
            threading.Thread(target=internal.serve,daemon=True).start()
        external.serve()


if __name__ == "__main__":
    main()
