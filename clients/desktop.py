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

from recording_storage import audio_bytes, limit_bytes, recordings
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


def capture(directory, stop, events, device, pause):
    from array import array
    try:
        import sounddevice as sd
        remaining = limit_bytes(directory.parent) - audio_bytes(directory.parent)
        if remaining < 2:
            raise ValueError("Local audio limit reached. Manage space with the storage command.")
        index = 0
        while not stop.is_set() and index < 120:
            with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", device=device or None) as mic:
                while not stop.is_set() and not pause.is_set() and index < 120:
                    path = directory / f"{index:03d}.raw"
                    new_fragment = not path.exists()
                    with path.open("ab") as out:
                        if new_fragment:
                            directory_fd = os.open(directory, os.O_DIRECTORY)
                            try:
                                os.fsync(directory_fd)
                            finally:
                                os.close(directory_fd)
                        while out.tell() < BLOCK and not stop.is_set() and not pause.is_set():
                            frames = min(4000, (BLOCK - out.tell()) // 2, remaining // 2)
                            if frames <= 0:
                                raise ValueError("Local audio limit reached. Manage space with the storage command.")
                            audio, overflow = mic.read(frames)
                            out.write(bytes(audio))
                            remaining -= len(audio)
                            out.flush()
                            os.fsync(out.fileno())
                            if overflow:
                                raise ValueError("Microphone overflow")
                            samples = array("h", bytes(audio))
                            events.put(("level", min(100, max(map(abs, samples), default=0) * 100 // 16000)))
                        if out.tell() == BLOCK:
                            index += 1
            if pause.is_set() and not stop.is_set():
                events.put(("paused", None))
                while pause.is_set() and not stop.wait(.05):
                    pass
    except Exception as error:
        events.put(("capture_error", str(error)))
    finally:
        stop.set()
        events.put(("stopped", None))


def play_audio(directory, cancel, events):
    try:
        import sounddevice as sd
        with sd.RawOutputStream(samplerate=16000, channels=1, dtype="int16") as speaker:
            for fragment in sorted(directory.glob("*.raw")):
                with fragment.open("rb") as source:
                    while not cancel.is_set():
                        block = source.read(3200)
                        if len(block) < 2:
                            break
                        speaker.write(block[:len(block) // 2 * 2])
                if cancel.is_set():
                    speaker.abort()
                    break
    except Exception as error:
        events.put(("play_error", str(error)))
    finally:
        events.put(("play_end", None))


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
    if os.environ.get('IV_DESKTOP_QT') == '6':
        from PySide6.QtCore import Qt, QSignalBlocker, QTimer, QSocketNotifier, QPoint, QSize
        from PySide6.QtGui import QTextCursor, QIcon, QPixmap, QColor, QPainter, QPalette, QPen, QPolygon, QCursor
        from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget, QProgressBar, QSystemTrayIcon, QMenu
    else:
        from PyQt5.QtCore import Qt, QSignalBlocker, QTimer, QSocketNotifier, QPoint, QSize
        from PyQt5.QtGui import QTextCursor, QIcon, QPixmap, QColor, QPainter, QPalette, QPen, QPolygon, QCursor
        from PyQt5.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget, QProgressBar, QSystemTrayIcon, QMenu

    cfg = json.loads(CONFIG.read_bytes())
    wayland = wayland_session()
    if wayland:
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")
    pointer=HOME/"current"
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    class RecorderWindow(QWidget):
        def closeEvent(self, event):
            # Keep shortcut capture available through a normal task-switcher window.
            event.ignore()
            self.showMinimized()

        def resizeEvent(self, event):
            super().resizeEvent(event)
            if self.isVisible() and event.oldSize().isValid() and not wayland:
                self.move(self.x(), self.y() + event.oldSize().height() - event.size().height())

        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton and self.windowHandle().startSystemMove():
                event.accept()
                return
            super().mousePressEvent(event)

    root = RecorderWindow(None, Qt.Window | Qt.FramelessWindowHint)
    root.setAttribute(Qt.WA_TranslucentBackground)
    root.setWindowTitle("InnerVoice")
    root.setStyleSheet("""
        QWidget#pill {background:rgba(22,22,25,226);border:1px solid #47474e;border-radius:26px}
        QLabel {color:#bfbfc7;font-size:12px;background:transparent}
        QTextEdit {background:transparent;color:#e3e3e7;border:0;padding:0;font-size:15px;
                   selection-background-color:#535065}
        QScrollBar:vertical {background:transparent;width:8px;margin:2px 0}
        QScrollBar::handle:vertical {background:#73737e;border-radius:3px;min-height:25px;margin:0 1px}
        QScrollBar::handle:vertical:hover {background:#a2a2ad}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {height:0}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {background:transparent}
        QPushButton {border:1px solid transparent;border-radius:15px;background:transparent}
        QPushButton:hover, QPushButton:focus {background:#393940;border-color:#64646d}
        QPushButton#record {background:#35353d;border-color:#54545e}
        QPushButton#record:focus {border-color:#d4cedf}
        QPushButton#record[recording="true"] {background:#64353c;border-color:#a55d65}
        QPushButton::menu-indicator {image:none}
        QMenu {background:#242429;color:#e4e4e8;border:1px solid #55555d;padding:4px}
        QMenu::item {padding:6px 12px}
        QMenu::item:selected {background:#43434d}
        QProgressBar {background:#44444a;border:0;border-radius:2px}
        QProgressBar::chunk {background:#ff9aa5;border-radius:2px}
    """)
    outer = QVBoxLayout(root)
    outer.setContentsMargins(0, 0, 0, 0)
    sheet = QWidget()
    sheet.setObjectName("pill")
    sheet.setFixedWidth(360)
    outer.addWidget(sheet)
    body = QVBoxLayout(sheet)
    body.setContentsMargins(14, 8, 14, 8)
    body.setSpacing(2)
    editor = QTextEdit()
    editor.setAcceptRichText(False)
    editor.setAccessibleName("Transcript")
    editor.setPlaceholderText("Ready when you are")
    editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    palette = editor.palette()
    palette.setColor(QPalette.PlaceholderText, QColor("#aaaab4"))
    editor.setPalette(palette)
    editor.document().setDocumentMargin(2)
    editor.setFixedHeight(24)
    body.addWidget(editor)

    def fit_text(*_):
        height = int(editor.document().size().height()) + 3
        screen = root.screen() or app.primaryScreen()
        available = screen.availableGeometry().height()
        controls = max(32, buttons.sizeHint().height())
        ceiling = max(24, available - controls - 16 - 2 - 48)
        editor.setFixedHeight(max(24, min(height, ceiling)))
        policy = Qt.ScrollBarAsNeeded if height > ceiling else Qt.ScrollBarAlwaysOff
        if editor.verticalScrollBarPolicy() != policy:
            editor.setVerticalScrollBarPolicy(policy)

    outer.setSizeConstraint(QVBoxLayout.SetFixedSize)
    buttons = QHBoxLayout()
    buttons.setSpacing(2)
    body.addLayout(buttons)
    status = QLabel("")
    status.setAlignment(Qt.AlignCenter)
    status.setWordWrap(True)
    status.setMinimumWidth(55)
    elapsed = QLabel()
    meter = QProgressBar()
    meter.setTextVisible(False)
    meter.setFixedSize(20, 3)
    meter.setRange(0, 100)
    meter.setValue(0)
    meter.setAccessibleName("Microphone level")
    meter.hide()

    def icon_button(name, symbol):
        button = QPushButton()
        button.setFixedSize(32, 32)
        button.setIconSize(QSize(18, 18))
        set_icon(button, name, symbol)
        return button

    def set_icon(button, name, symbol):
        pix = QPixmap(48, 48)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(2, 2)
        color = QColor("#efedf3")
        painter.setPen(QPen(color, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if symbol == "mic":
            painter.drawRoundedRect(9, 2, 6, 12, 3, 3)
            painter.drawArc(5, 5, 14, 13, 180 * 16, 180 * 16)
            painter.drawLine(12, 18, 12, 21)
            painter.drawLine(9, 21, 15, 21)
        elif symbol == "copy":
            painter.drawRoundedRect(8, 8, 12, 13, 2, 2)
            painter.drawLine(4, 16, 4, 4)
            painter.drawLine(4, 4, 15, 4)
        elif symbol == "history":
            painter.drawEllipse(3, 3, 18, 18)
            painter.drawLine(12, 6, 12, 12)
            painter.drawLine(12, 12, 16, 14)
        elif symbol == "retry":
            painter.drawArc(4, 4, 16, 16, 45 * 16, 290 * 16)
            painter.drawLine(18, 3, 18, 8)
            painter.drawLine(18, 8, 13, 8)
        else:
            painter.setBrush(color)
            if symbol == "pause":
                painter.drawRoundedRect(6, 5, 3, 14, 1, 1)
                painter.drawRoundedRect(15, 5, 3, 14, 1, 1)
            elif symbol == "play":
                painter.drawPolygon(QPolygon([QPoint(7, 4), QPoint(20, 12), QPoint(7, 20)]))
            else:
                painter.drawRoundedRect(6, 6, 12, 12, 2, 2)
        painter.end()
        button.setIcon(QIcon(pix))
        button.setAccessibleName(name)
        button.setToolTip(name)

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
        status.setStyleSheet("color:" + {"Recording":"#ffa0aa", "Processing":"#edc280", "Ready":"#a2d9c7"}.get(phase, "#cac5df"))
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
    paused_at = None
    pause = threading.Event()
    playback = None
    playback_cancel = threading.Event()
    capture_thread = None
    capture_problem = ""
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
            state["text"] = editor.toPlainText()
            try:
                atomic(directory / "editor.json", json.dumps(state).encode())
            except OSError as error:
                status.setText(f"Could not save transcript: {error}")

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
        nonlocal directory,state,working,stop,finished,started,replace_session,capture_thread,paused_at,capture_problem
        if playback is not None:
            status.setText("Stop playback first")
            return
        if working:
            if not stop.is_set():
                stop.set()
                status.setText("Finishing recording")
            return
        if transfer_active:
            status.setText("Processing saved audio")
            return
        new_directory = HOME / secrets.token_hex(16)
        new_state = {"applied": 0, "ready": False, "created": time.time(), "text": ""}
        try:
            new_directory.mkdir(mode=0o700)
            atomic(new_directory / "editor.json", json.dumps(new_state).encode())
            atomic(pointer, new_directory.name.encode())
        except OSError as error:
            status.setText(f"Cannot start local recording: {error}")
            return
        replace_session = False
        retry_button.hide()
        timer.setInterval(100)
        pause.clear()
        paused_at = None
        capture_problem = ""
        started = time.monotonic()
        indicator("Recording")
        set_recording(True)
        directory, state = new_directory, new_state
        with QSignalBlocker(editor):
            editor.clear()
        stop=threading.Event()
        finished=threading.Event()
        working=True
        status.setText("Recording")
        capture_thread = threading.Thread(target=capture, args=(directory, stop, events, cfg["microphone"], pause))
        capture_thread.start()
        transfer()

    def poll():
        nonlocal working, transfer_active, replace_session, paused_at, playback, capture_problem
        if working and not stop.is_set():
            seconds = int((paused_at or time.monotonic()) - started)
            elapsed.setText(f"{seconds // 60:02}:{seconds % 60:02}")
        while not events.empty():
            kind,value=events.get()
            if kind=="toggle":
                toggle()
            elif kind=="show":
                reveal()
            elif kind == "paused":
                if not stop.is_set():
                    paused_at = time.monotonic()
                    indicator("Paused")
                    status.setText("Paused")
                    meter.hide()
                    set_icon(pause_button, "Resume recording", "mic")
                    pause_button.setEnabled(True)
                    play_button.show()
            elif kind == "capture_error":
                capture_problem = "Capture interrupted: " + value
                status.setText(capture_problem)
            elif kind == "play_error":
                status.setText("Playback failed: " + value)
            elif kind == "play_end":
                playback = None
                set_icon(play_button, "Play recording", "play")
                if status.text() == "Playing":
                    status.setText("Paused" if working else "Ready")
                pause_button.setEnabled(True)
            elif kind=="stopped":
                finished.set()
                working = False
                timer.setInterval(700)
                elapsed.clear()
                meter.setValue(0)
                set_recording(False)
                indicator("Processing" if transfer_active else "Ready")
                if transfer_active:
                    status.setText(capture_problem or "Processing")
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
                indicator("Ready")
                if editor.toPlainText().strip():
                    copy_result()
                if capture_problem:
                    status.setText(capture_problem)
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

    def audio_available():
        return directory is not None and any(p.stat().st_size >= 2 for p in directory.glob("*.raw"))

    def pause_or_resume():
        nonlocal paused_at, started
        if not working or stop.is_set() or playback is not None:
            return
        if paused_at is None:
            pause_button.setEnabled(False)
            pause.set()
        else:
            started += time.monotonic() - paused_at
            paused_at = None
            play_button.hide()
            meter.show()
            indicator("Recording")
            status.setText("Recording")
            set_icon(pause_button, "Pause recording", "pause")
            pause.clear()

    def play_or_stop():
        nonlocal playback
        if playback is not None:
            playback_cancel.set()
            return
        if (working and paused_at is None) or not audio_available():
            return
        playback_cancel.clear()
        playback = threading.Thread(target=play_audio, args=(directory, playback_cancel, events))
        playback.start()
        pause_button.setEnabled(False)
        set_icon(play_button, "Stop playback", "stop")
        status.setText("Playing")

    start_button = icon_button("Record", "mic")
    start_button.setObjectName("record")
    pause_button = icon_button("Pause recording", "pause")
    play_button = icon_button("Play recording", "play")
    retry_button = icon_button("Retry sync", "retry")
    copy_button = icon_button("Copy", "copy")
    recover_button = icon_button("Local recording history", "history")
    recovery = QMenu(root)
    recover_button.setMenu(recovery)
    start_button.clicked.connect(toggle)
    pause_button.clicked.connect(pause_or_resume)
    play_button.clicked.connect(play_or_stop)
    retry_button.clicked.connect(retry)
    copy_button.clicked.connect(copy_result)
    for button in (start_button, pause_button, play_button, retry_button):
        buttons.addWidget(button)
    buttons.addWidget(status, 1)
    buttons.addWidget(meter)
    buttons.addWidget(elapsed)
    buttons.addWidget(copy_button)
    buttons.addWidget(recover_button)
    for button in (pause_button, play_button, retry_button):
        button.hide()

    def set_recording(active):
        meter.setVisible(active)
        pause_button.setVisible(active)
        pause_button.setEnabled(True)
        set_icon(pause_button, "Pause recording", "pause")
        play_button.setVisible(not active and audio_available())
        set_icon(start_button, "Stop recording" if active else "Record", "stop" if active else "mic")
        start_button.setProperty("recording", active)
        start_button.style().unpolish(start_button)
        start_button.style().polish(start_button)

    def recover(saved):
        nonlocal directory, state, replace_session
        if working or transfer_active or playback is not None:
            return
        directory = saved.parent
        state = json.loads(saved.read_bytes())
        replace_session = state.get("replace_session", False)
        atomic(pointer, directory.name.encode())
        with QSignalBlocker(editor):
            editor.setPlainText(state.get("text", ""))
        stop.set()
        finished.set()
        play_button.setVisible(audio_available())
        retry_button.setVisible(not state.get("ready"))
        status.setText("Local recording" if audio_available() else "Saved transcript")

    def list_recoverable():
        recovery.clear()
        if working or transfer_active or playback is not None:
            recovery.addAction("Finish the current operation to open history").setEnabled(False)
            return
        count = 0
        for created, saved, entry in recordings(HOME):
            size = sum(p.stat().st_size for p in saved.parent.glob("*.raw"))
            if size < 2:
                continue
            duration = size // 32000
            count += 1
            stamp = time.strftime("%b %d, %H:%M", time.localtime(created))
            preview = " ".join(entry.get("text", "").split())[:36].replace("&", "&&")
            label = f"{stamp}   {duration // 60}:{duration % 60:02}   {preview}"
            recovery.addAction(label, lambda checked=False, file=saved: recover(file))
            if count == 10:
                break
        if not recovery.actions():
            recovery.addAction("No saved recordings").setEnabled(False)
    recovery.aboutToShow.connect(list_recoverable)
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
    handlers = {sig: signal.signal(sig, lambda *_: app.exit(0)) for sig in (signal.SIGTERM, signal.SIGINT)}
    notifier = None
    if isinstance(listener, socket.socket):
        notifier = QSocketNotifier(listener.fileno(), QSocketNotifier.Read)
        notifier.activated.connect(lambda *_: (read_shortcuts(listener, events), poll()))
    else:
        timer.timeout.connect(lambda: read_shortcuts(listener, events))
    editor.document().documentLayout().documentSizeChanged.connect(lambda _: fit_text())
    root.adjustSize()
    screen = app.screenAt(QCursor.pos()) or app.primaryScreen()
    if screen:
        area = screen.availableGeometry()
        root.move(area.center().x() - root.width() // 2, area.bottom() - root.height() - 24)
    root.show()
    watched_screen = root.screen()

    def screen_changed(screen):
        nonlocal watched_screen
        if watched_screen is not None:
            watched_screen.availableGeometryChanged.disconnect(fit_text)
        watched_screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(fit_text)
            fit_text()

    watched_screen.availableGeometryChanged.connect(fit_text)
    root.windowHandle().screenChanged.connect(screen_changed)
    fit_text()
    try:
        if os.environ.get("IV_DESKTOP_QT") == "6":
            app.exec()
        else:
            app.exec_()
    finally:
        stop.set()
        playback_cancel.set()
        if capture_thread is not None:
            capture_thread.join()
        if playback is not None:
            playback.join()
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
