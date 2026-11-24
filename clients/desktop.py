#!/usr/bin/env python3
"""Ubuntu graphical client. Capture, network, and UI use independent threads."""
import fcntl
import getpass
import io
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import warnings
import wave

import requests

from provider import Client, atomic, gateway_url, read_bearer

HOME=Path.home()/".local/state/innervoice-desktop"
CONFIG=Path.home()/".config/innervoice-desktop/settings.json"
UNIT="innervoice-desktop.service"
BLOCK=960000


def check_gateway(cfg, bearer):
    caps = Client(cfg, bearer=bearer).call("GET", "/v1/capabilities", timeout=8)
    if cfg.get("role") == "agent":
        if not isinstance(caps, dict) or caps.get("api_version") != 1 or not caps.get("jobs"):
            raise ValueError("This agent credential requires the jobs API.")
        return
    if not isinstance(caps, dict) or caps.get("api_version") != 1 or not caps.get("sessions") or caps.get("fragment_seconds") != 30:
        raise ValueError("Gateway needs the session API and 30-second fragment support")


def wayland_session():
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or os.environ.get("QT_QPA_PLATFORM") == "wayland"


def ask(label, default=""):
    suffix = f" [{default}]" if default != "" else ""
    return input(f"  {label}{suffix}: ").strip() or default


def choose_certificate(current):
    print("  For the Gateway's self-signed certificate, use the public certificate copied from the server.")
    print(f"  Suggested local certificate: {CONFIG.with_name('ca.crt')}")
    print("  Type 'system' to use the system trust store for a CA-issued certificate.")
    while True:
        value = ask("Public certificate file", current)
        if value == "system":
            return ""
        if value:
            path = Path(value).expanduser().resolve()
            if path.is_file() and os.access(path, os.R_OK):
                return str(path)
        print("  Certificate file missing or unreadable. Copy it into the desktop configuration directory first.")


def choose_bearer(current):
    print("  Use the token registered on the server for this desktop.")
    print("  Enter a token file path or type 'paste' to enter the token without displaying it.")
    while True:
        value = ask("Bearer token file or paste", current or "paste")
        if value == "paste":
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    bearer = getpass.getpass("  Paste bearer token (hidden): ").strip()
            except getpass.GetPassWarning:
                print("  Open an interactive terminal or enter the path to a token file.")
                continue
            if len(bearer) >= 32 and bearer.isascii() and all(char.isprintable() and not char.isspace() for char in bearer):
                return str(CONFIG.with_name("bearer")), bearer, True
            print("  Enter one token of at least 32 characters.")
        else:
            path = Path(value).expanduser().resolve()
            try:
                return str(path), read_bearer(str(path)), False
            except (ValueError, OSError) as error:
                print(f"  {error}")


def connection_error(error):
    if isinstance(error, requests.exceptions.SSLError):
        return "TLS verification failed. Check the trust certificate and use the exact server address it was issued for."
    if isinstance(error, requests.HTTPError):
        status = error.response.status_code
        return {
            401: "The Gateway rejected the token. Use the credential registered for this desktop.",
            403: "The Gateway denied this source IP. Add the desktop's private IP to Allowed clients on the server.",
            404: "The server did not expose the desktop session API. Check the Gateway HTTPS port.",
        }.get(status, f"The Gateway returned HTTP {status}. Check its status on the server.")
    if isinstance(error, requests.exceptions.Timeout):
        return "The connection timed out. Check the desktop's private-network route, server port and firewall."
    if isinstance(error, requests.exceptions.ConnectionError):
        return "Could not connect. Check the Gateway is running and the desktop can reach its private HTTPS port."
    if isinstance(error, requests.exceptions.JSONDecodeError):
        return "The server did not return Gateway API data. Check the server address and port."
    return str(error)


def setup(agent=False):
    cfg = json.loads(CONFIG.read_bytes()) if CONFIG.exists() else {}
    for key, filename in (("ca", "ca.crt"), ("token_file", "bearer")):
        if key == "ca" and cfg.get(key) == "":
            continue
        path = CONFIG.with_name(filename)
        if not Path(cfg.get(key) or ".").is_file() and path.is_file():
            cfg[key] = str(path)
    if agent:
        cfg["role"] = "agent"
    cfg.setdefault("source_ip", "")
    cfg.setdefault("source_port", 0)
    cfg.setdefault("microphone", "")
    print("InnerVoice agent setup" if agent else "InnerVoice desktop setup")
    print("Press Enter to keep a value in brackets. Ctrl+C cancels without saving.\n")

    while True:
        print("1/3  Gateway address")
        while True:
            try:
                cfg["url"] = gateway_url(ask("HTTPS URL (for example https://SERVER-IP:9000)", cfg.get("url", "")))
                break
            except ValueError:
                print("  Enter the Gateway's full HTTPS URL, without a path.")

        print("\n2/3  Secure connection")
        cfg["ca"] = choose_certificate((cfg["ca"] or "system") if "ca" in cfg else "")
        identity = "telegram-agent" if agent else "desktop-hotkey"
        print(f"  Register this identity once on the server: ./bin/innervoice credential --identity {identity} --scope {'jobs' if agent else 'sessions'}")
        cfg["token_file"], bearer, save_token = choose_bearer(cfg.get("token_file", ""))
        if not agent and not wayland_session():
            cfg["hotkey"] = ask("Recording shortcut", cfg.get("hotkey", "<cmd>+<shift>+<f8>"))
        print("\n3/3  Verify and save")
        print("  Checking the Gateway's TLS, credential and desktop session support…", flush=True)
        try:
            check_gateway(cfg, bearer)
        except (requests.RequestException, ValueError, OSError) as error:
            print(f"  {connection_error(error)}")
            if ask("Edit connection settings and retry? (y/N)").lower() in ("y", "yes"):
                # The pasted token only exists in memory until a successful save.
                if save_token and not Path(cfg["token_file"]).is_file():
                    cfg.pop("token_file")
                continue
            raise SystemExit("Setup cancelled. Settings were not saved.") from None
        break

    print(f"  Connected to {cfg['url']}")
    if ask("Save local settings? (Y/n)", "y").lower() not in ("y", "yes"):
        raise SystemExit("Setup cancelled. Settings were not saved.")
    CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if Path(cfg["token_file"]) != CONFIG.with_name("bearer"):
        cfg["token_file"] = str(CONFIG.with_name("bearer"))
        save_token = True
    if agent:
        cfg.setdefault("state_dir", str(CONFIG.parent/"state"))
    if save_token:
        token_path = Path(cfg["token_file"])
        previous = token_path.read_bytes() if token_path.exists() else None
        atomic(token_path, (bearer + "\n").encode())
        try:
            atomic(CONFIG, json.dumps(cfg).encode())
        except (OSError, KeyboardInterrupt):
            if previous is None:
                token_path.unlink(missing_ok=True)
            else:
                atomic(token_path, previous)
            raise
    else:
        atomic(CONFIG, json.dumps(cfg).encode())
    if agent:
        print(f"Agent settings saved. Use clients/provider.py --config {CONFIG} INPUT OUTPUT.")
        return
    print("Desktop settings saved. Start the client to test a recording.")


def capture(directory,stop,events,device):
    try:
        import sounddevice as sd
        from array import array
        with sd.RawInputStream(samplerate=16000,channels=1,dtype="int16",device=device or None) as mic:
            for index in range(120):
                path=directory/f"{index:03d}.raw"
                with path.open("xb") as out:
                    while out.tell()<BLOCK and not stop.is_set():
                        audio,overflow=mic.read(min(4000,(BLOCK-out.tell())//2))
                        if overflow:
                            raise ValueError("Capture overflow")
                        samples = array("h", bytes(audio))
                        events.put(("level", min(100, max(map(abs, samples), default=0) * 100 // 16000)))
                        out.write(bytes(audio))
                        out.flush()
                        os.fsync(out.fileno())
                if stop.is_set():
                    break
    except Exception:
        events.put(("notice","Capture interrupted. Saved audio can be recovered."))
    finally:
        stop.set()
        events.put(("stopped",None))


def network(cfg, directory, key, events, finished):
    try:
        client = Client(cfg)
    except (OSError, ValueError, KeyError):
        events.put(("retry", ("Check the desktop connection settings and credential file.", False)))
        return

    path = None
    failures = 0
    while True:
        try:
            done = finished.is_set()
            files = sorted(directory.glob("*.raw"))
            ready = [p for p in files if p.stat().st_size == BLOCK or (done and p.stat().st_size >= 2)]
            if path is None and not ready:
                if done:
                    events.put(("ready", "Empty recording"))
                    return
                finished.wait(1)
                continue
            if path is None:
                session = client.call("POST", "/v1/sessions", headers={"Idempotency-Key": key})
                path = "/v1/sessions/" + session["session_id"]
            progress = client.call("GET", path)
            events.put(("segments", progress["segments"]))
            if progress["state"] == "succeeded":
                events.put(("ready", "Ready"))
                return
            if progress["state"] == "failed":
                events.put(("retry", ("Gateway could not finish this capture. Retry to reprocess the saved audio.", True)))
                return
            received = set(progress["received"])
            for fragment in ready:
                index = int(fragment.stem)
                if index in received:
                    continue
                with io.BytesIO() as buf:
                    with wave.open(buf, "wb") as wav:
                        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                        data = fragment.read_bytes()
                        wav.writeframes(data[:len(data) // 2 * 2])
                    client.call("PUT", path + f"/fragments/{index}", data=buf.getvalue(),
                                headers={"Content-Type": "audio/wav"})
            if done and ready and not progress["closed"]:
                client.call("POST", path + "/finalize", json={"last_index": int(ready[-1].stem)})
            failures = 0
            time.sleep(1)
        except requests.HTTPError as error:
            status = error.response.status_code
            if status in (429, 502, 503, 504) and failures < 2:
                failures += 1
                events.put(("notice", "Gateway busy or unavailable. Retrying saved fragments."))
                time.sleep(3)
                continue
            restart = status == 410 or (status == 404 and path is not None)
            events.put(("retry", (f"Gateway returned HTTP {status}. Saved audio is available to retry.", restart)))
            return
        except (requests.exceptions.SSLError, requests.exceptions.InvalidURL, ValueError, KeyError):
            events.put(("retry", ("Check the Gateway endpoint, TLS trust and configuration.", False)))
            return
        except (requests.Timeout, requests.ConnectionError):
            failures += 1
            if failures >= 3:
                events.put(("retry", ("Gateway offline. Retry this audio or record another.", False)))
                return
            events.put(("notice", "Connection interrupted. Retrying saved fragments."))
            time.sleep(3)
        except (requests.RequestException, OSError):
            events.put(("retry", ("Could not sync saved audio. Check the client state and retry.", False)))
            return


def send_toggle(message=b"toggle"):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.sendto(message, str(HOME/"control.sock"))
    except OSError:
        sys.exit("The desktop client is not ready. Start it and check its status.")


def read_shortcuts(listener, events):
    while True:
        try:
            message = listener.recv(64)
        except BlockingIOError:
            return
        if message == b"show":
            events.put(("show", None))
        if message == b"toggle":
            events.put(("toggle", None))


def run_graphical():
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (HOME/"desktop.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("The desktop client is already running.")
        control = HOME/"control.sock"
        control.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
            listener.bind(str(control))
            listener.setblocking(False)
            try:
                graphical(listener)
            finally:
                control.unlink(missing_ok=True)


def graphical(listener):
    from PyQt5.QtCore import Qt, QSignalBlocker, QTimer, QSocketNotifier
    from PyQt5.QtGui import QTextCursor, QIcon, QPixmap, QColor, QPainter
    from PyQt5.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget, QProgressBar, QSystemTrayIcon, QMenu

    cfg = json.loads(CONFIG.read_bytes())
    wayland = wayland_session()
    if wayland:
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")
    pointer=HOME/"current"
    for old in HOME.iterdir():
        record=old/"editor.json"
        if record.is_file() and time.time()-record.stat().st_mtime>86400:
            if pointer.exists() and pointer.read_text()==old.name:
                pointer.unlink()
            for item in old.iterdir():
                if item.suffix in (".json",".raw"):
                    item.unlink()
            if not any(old.iterdir()):
                old.rmdir()
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    class RecorderWindow(QWidget):
        def closeEvent(self, event):
            # Keep shortcut capture available through a normal task-switcher window.
            event.ignore()
            self.showMinimized()

    root = RecorderWindow()
    root.setWindowTitle("InnerVoice")
    root.resize(520, 300)
    root.setMinimumSize(360, 220)
    root.setStyleSheet("""
        QWidget {background:#191b20;color:#edeef2;font-size:14px}
        QLabel#status {color:#bfc7d2}
        QLabel#elapsed {color:#ff8792;font-weight:600}
        QTextEdit {background:#23262d;border:1px solid #474d58;border-radius:9px;
                   padding:10px;font-size:16px;selection-background-color:#376467}
        QTextEdit:focus, QPushButton:focus {border:1px solid #96d9cf}
        QPushButton {background:#30353d;border:1px solid #4a505b;border-radius:7px;
                     padding:6px 10px}
        QPushButton:hover {background:#414752}
        QPushButton:pressed {background:#272c33}
        QPushButton:disabled {color:#969ba3}
        QPushButton#record {background:#a1dfd3;color:#132521;font-weight:600}
        QPushButton#record:hover {background:#baeee4}
        QPushButton#record[recording="true"] {background:#ff9aa5;color:#351319}
        QProgressBar {background:#30353d;border:0;border-radius:2px}
        QProgressBar::chunk {background:#ff8792;border-radius:2px}
    """)
    layout = QVBoxLayout(root)
    layout.setContentsMargins(18, 14, 18, 14)
    layout.setSpacing(10)
    header = QHBoxLayout()
    status = QLabel("Ready to record")
    status.setObjectName("status")
    status.setWordWrap(True)
    header.addWidget(status, 1)
    elapsed = QLabel()
    elapsed.setObjectName("elapsed")
    header.addWidget(elapsed)
    layout.addLayout(header)
    meter = QProgressBar()
    meter.setTextVisible(False)
    meter.setFixedHeight(5)
    layout.addWidget(meter)
    editor = QTextEdit()
    editor.setAcceptRichText(False)
    editor.setPlaceholderText("Your transcript appears here")
    editor.setAccessibleName("Transcript")
    layout.addWidget(editor, 1)
    buttons = QHBoxLayout()
    layout.addLayout(buttons)
    tray = QSystemTrayIcon(app)
    menu = QMenu()
    tray.setContextMenu(menu)
    def reveal():
        if root.isMinimized():
            root.setWindowState(root.windowState() & ~Qt.WindowMinimized)
        root.show()
        root.raise_()
        root.activateWindow()

    def indicator(phase):
        pix = QPixmap(32, 32)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor({"Recording":"#ff354e", "Processing":"#efbe67"}.get(phase, "#75d9cf")))
        painter.drawEllipse(4, 4, 24, 24)
        painter.end()
        icon = QIcon(pix)
        tray.setIcon(icon)
        root.setWindowIcon(icon)
        tray.setToolTip("InnerVoice · " + phase)
        atomic(HOME/"indicator", phase.encode())

    indicator("Ready")
    tray.show()
    started = 0.
    events=queue.Queue()
    directory=None
    state={}
    stop=threading.Event()
    finished=threading.Event()
    working=False
    transfer_active = False
    replace_session = False

    def save():
        if directory:
            state["text"]=editor.toPlainText()
            atomic(directory/"editor.json",json.dumps(state).encode())

    editor.textChanged.connect(save)

    def transfer():
        nonlocal transfer_active
        transfer_active = True
        retry_button.hide()
        key = state.get("session_key", directory.name)
        threading.Thread(target=network, args=(cfg, directory, key, events, finished), daemon=True).start()

    def retry():
        nonlocal replace_session
        if transfer_active or not finished.is_set() or not directory or state.get("ready"):
            return
        if replace_session:
            state["session_key"] = secrets.token_hex(16)
            replace_session = False
            state["replace_session"] = False
            save()
        indicator("Processing")
        status.setText("Syncing saved audio")
        transfer()

    def toggle():
        nonlocal directory,state,working,stop,finished,started,replace_session
        if working:
            if not stop.is_set():
                stop.set()
                status.setText("Finishing recording")
            return
        if transfer_active:
            status.setText("Processing saved audio")
            return
        replace_session = False
        retry_button.hide()
        timer.setInterval(100)
        started = time.monotonic()
        indicator("Recording")
        set_recording(True)
        directory=HOME/secrets.token_hex(16)
        directory.mkdir(mode=0o700)
        state={"applied":0,"ready":False}
        editor.clear()
        save()
        atomic(pointer,directory.name.encode())
        stop=threading.Event()
        finished=threading.Event()
        working=True
        status.setText("Recording")
        threading.Thread(target=capture,args=(directory,stop,events,cfg["microphone"]),daemon=True).start()
        transfer()

    def poll():
        nonlocal working, transfer_active, replace_session
        if working and not stop.is_set():
            seconds = int(time.monotonic() - started)
            elapsed.setText(f"●  {seconds // 60:02}:{seconds % 60:02}")
        while not events.empty():
            kind,value=events.get()
            if kind=="toggle":
                toggle()
            elif kind=="show":
                reveal()
            elif kind=="stopped":
                finished.set()
                working = False
                timer.setInterval(700)
                elapsed.clear()
                meter.setValue(0)
                set_recording(False)
                indicator("Processing" if transfer_active else "Ready")
                if transfer_active:
                    status.setText("Processing saved audio")
                else:
                    retry_button.show()
            elif kind=="level":
                meter.setValue(value)
            elif kind=="segments":
                for part in value:
                    if part["index"]==state["applied"]:
                        if part["text"]:
                            with QSignalBlocker(editor):
                                tail = QTextCursor(editor.document())
                                tail.movePosition(QTextCursor.End)
                                tail.insertText((" " if editor.toPlainText() else "") + part["text"])
                        state["applied"]+=1
                        save()
            elif kind=="retry":
                transfer_active = False
                message, replace_session = value
                state["replace_session"] = replace_session
                save()
                if finished.is_set():
                    indicator("Ready")
                status.setText(message)
                if finished.is_set():
                    retry_button.show()
            elif kind=="ready":
                transfer_active = False
                working=False
                state["ready"]=True
                save()
                status.setText(value)
                retry_button.hide()
                for fragment in directory.glob("*.raw"):
                    try:
                        fragment.unlink(missing_ok=True)
                    except OSError:
                        status.setText("Text saved. Local audio cleanup is pending.")
                indicator("Ready")
                if editor.toPlainText().strip():
                    copy_result()
            else:
                status.setText(value)

    def copy_result():
        text = editor.toPlainText()
        if not text:
            return
        clipboard = app.clipboard()
        clipboard.setText(text)
        if clipboard.text() != text:
            status.setText("Clipboard unavailable. Select and copy the text here.")
            return
        status.setText("Copied to clipboard")

    start_button = QPushButton("Record")
    start_button.setObjectName("record")

    def set_recording(active):
        start_button.setText("Stop" if active else "Record")
        start_button.setProperty("recording", active)
        start_button.style().unpolish(start_button)
        start_button.style().polish(start_button)

    start_button.clicked.connect(toggle)
    retry_button = QPushButton("Retry sync")
    retry_button.hide()
    retry_button.clicked.connect(retry)
    copy_button = QPushButton("Copy")
    copy_button.clicked.connect(copy_result)
    buttons.addWidget(start_button)
    buttons.addWidget(retry_button)
    buttons.addStretch()
    buttons.addWidget(copy_button)
    recovery = QMenu(root)
    recover_button = QPushButton("Recover")
    recover_button.setMenu(recovery)
    buttons.addWidget(recover_button)

    def recover(saved):
        nonlocal directory, state, replace_session
        if working or transfer_active:
            return
        directory = saved.parent
        state = json.loads(saved.read_bytes())
        replace_session = state.get("replace_session", False)
        atomic(pointer, directory.name.encode())
        with QSignalBlocker(editor):
            editor.setPlainText(state.get("text", ""))
        stop.set()
        finished.set()
        retry()

    def list_recoverable():
        recovery.clear()
        if not working and not transfer_active:
            for saved in sorted(HOME.glob("*/editor.json"), key=lambda f: f.stat().st_mtime, reverse=True):
                if not json.loads(saved.read_bytes()).get("ready"):
                    label = time.strftime("%b %d · %H:%M:%S", time.localtime(saved.stat().st_mtime))
                    recovery.addAction(label, lambda checked=False, file=saved: recover(file))
        if not recovery.actions():
            recovery.addAction("No available recordings").setEnabled(False)
    recovery.aboutToShow.connect(list_recoverable)
    if pointer.exists():
        directory=HOME/pointer.read_text()
        state=json.loads((directory/"editor.json").read_bytes())
        editor.setPlainText(state.get("text", ""))
        if state.get("intent")=="pending":
            status.setText("Insertion outcome unknown. Check the destination.")
        elif not state["ready"]:
            replace_session = state.get("replace_session", False)
            stop.set()
            finished.set()
            status.setText("Recovering saved recording")
            retry()
    menu.addAction("Record / Stop", toggle)
    menu.addAction("Show editor", reveal)
    tray.activated.connect(lambda reason: reveal() if reason == QSystemTrayIcon.Trigger else None)
    hotkeys = None
    if not wayland:
        from pynput.keyboard import GlobalHotKeys
        hotkeys = GlobalHotKeys({cfg.get("hotkey", "<cmd>+<shift>+<f8>"): lambda: events.put(("toggle", None))})
        hotkeys.start()
    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(700)
    handlers = {sig: signal.signal(sig, lambda *_: app.quit()) for sig in (signal.SIGTERM, signal.SIGINT)}
    notifier = None
    if isinstance(listener, socket.socket):
        notifier = QSocketNotifier(listener.fileno(), QSocketNotifier.Read)
        notifier.activated.connect(lambda _: (read_shortcuts(listener, events), poll()))
    else:
        timer.timeout.connect(lambda: read_shortcuts(listener, events))
    root.show()
    try:
        app.exec_()
    finally:
        stop.set()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if hotkeys:
            hotkeys.stop()
        if tray:
            tray.hide()
        (HOME/"indicator").unlink(missing_ok=True)


def main():
    os.umask(0o077)
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action in ("setup", "agent-setup"):
        global CONFIG
        if action == "agent-setup":
            CONFIG = Path.home()/".config/innervoice-agent/settings.json"
        try:
            setup(agent=action == "agent-setup")
        except (KeyboardInterrupt, EOFError):
            sys.exit("Setup cancelled.")
        except (OSError, ValueError) as error:
            sys.exit(f"Could not complete desktop setup: {error}")
    elif action=="power":
        running = subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT]).returncode == 0
        os.execv(sys.executable, [sys.executable, __file__, "stop" if running else "start"])
    elif action=="start":
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            sys.exit("Start in a graphical desktop session with microphone access.")
        cmd=["systemd-run","--user","--unit="+UNIT,"--collect","-p","MemoryMax=384M","-p","CPUQuota=100%","-p","TasksMax=96"]
        for key in ("DISPLAY","XAUTHORITY","DBUS_SESSION_BUS_ADDRESS","XDG_RUNTIME_DIR","AT_SPI_BUS_ADDRESS", "XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
            if key in os.environ:
                cmd.append("--setenv="+key+"="+os.environ[key])
        subprocess.run(cmd+[sys.executable,str(Path(__file__).resolve()),"run"],check=True)
    elif action in ("stop","status"):
        subprocess.run(["systemctl","--user",action,UNIT],check=True)
    elif action=="run":
        run_graphical()
    elif action=="check":
        cfg = json.loads(CONFIG.read_bytes())
        try:
            check_gateway(cfg, read_bearer(cfg["token_file"]))
        except (ValueError, KeyError, OSError, requests.RequestException) as error:
            sys.exit("Disconnected · " + connection_error(error))
        print("Connected · Gateway authenticated")
    elif action in ("toggle", "show"):
        send_toggle(action.encode())
    else:
        sys.exit("desktop setup|start|status|stop|run|toggle")


if __name__=="__main__":
    main()
