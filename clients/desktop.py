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

from recording_storage import audio_bytes, audio_fragments, limit_bytes, recordings
from provider import Client, atomic, gateway_url, read_bearer

HOME=Path.home()/".local/state/innervoice-desktop"
CONFIG=Path.home()/".config/innervoice-desktop/settings.json"
UNIT="innervoice-desktop.service"
BLOCK=960000
STREAM_SECONDS = 2
TASK_LABELS = {"translate": "Translate to English", "transcribe": "Transcribe Spanish"}


def metric(event, session, **values):
    print("IV_METRIC " + json.dumps(dict(event=event, session=session, **values)), flush=True)


def elapsed_ms(start, end=None):
    if start is None:
        return None
    return round(((time.monotonic() if end is None else end) - start) * 1000, 1)


def delivery_intervals(cfg):
    poll = cfg.get("poll_seconds", 1)
    if type(poll) not in (int, float) or not .1 <= poll <= 1:
        poll = 1
    ui = cfg.get("ui_poll_ms", 700)
    if type(ui) is not int or not 100 <= ui <= 700:
        ui = 700
    return poll, ui


def capture_options(state):
    streaming = state.get("streaming") is True
    return streaming, state.get("task", "translate"), STREAM_SECONDS * 32000 if streaming else BLOCK


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
    print("Have the Gateway HTTPS address, its public certificate and a scoped token ready.")
    print("For Meshnet, use the private endpoint covered by the certificate.")

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
        capability = "job" if agent else "recording session"
        print(f"  Checking TLS, the credential and {capability} support…", flush=True)
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


def capture(directory, stop, events, device, pause, block=BLOCK):
    from array import array
    try:
        import sounddevice as sd
        remaining = limit_bytes(directory.parent) - audio_bytes(directory.parent)
        if remaining < 2:
            raise ValueError("Local audio limit reached. Manage space with the storage command.")
        index = 0
        mic_opened = voice_detected = False
        frame_limit = 120 * BLOCK // block
        while not stop.is_set() and index < frame_limit:
            with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", device=device or None) as mic:
                if not mic_opened:
                    mic_opened = True
                    events.put(("mic_opened", time.monotonic()))
                while not stop.is_set() and not pause.is_set() and index < frame_limit:
                    path = directory / f"{index:03d}.raw"
                    new_fragment = not path.exists()
                    with path.open("ab") as out:
                        if new_fragment:
                            directory_fd = os.open(directory, os.O_DIRECTORY)
                            try:
                                os.fsync(directory_fd)
                            finally:
                                os.close(directory_fd)
                        while out.tell() < block and not stop.is_set() and not pause.is_set():
                            frames = min(4000, (block - out.tell()) // 2, remaining // 2)
                            if frames <= 0:
                                raise ValueError("Local audio limit reached. Manage space with the storage command.")
                            audio, overflow = mic.read(frames)
                            read_at = time.monotonic()
                            out.write(bytes(audio))
                            remaining -= len(audio)
                            out.flush()
                            os.fsync(out.fileno())
                            if overflow:
                                raise ValueError("Microphone overflow")
                            samples = array("h", bytes(audio))
                            peak = max(map(abs, samples), default=0)
                            if not voice_detected and peak >= 1000:
                                voice_detected = True
                                onset = next(i for i, sample in enumerate(samples) if abs(sample) >= 1000)
                                events.put(("voice", read_at - (len(samples) - onset) / 16000))
                            events.put(("level", min(100, peak * 100 // 16000)))
                        if out.tell() == block:
                            index += 1
            if pause.is_set() and not stop.is_set():
                events.put(("paused", None))
                while pause.is_set() and not stop.wait(.05):
                    pass
    except Exception as error:
        events.put(("capture_error", str(error)))
    finally:
        stop.set()
        events.put(("stopped", time.monotonic()))


def play_audio(directory, cancel, events):
    try:
        import sounddevice as sd
        with sd.RawOutputStream(samplerate=16000, channels=1, dtype="int16") as speaker:
            for fragment in audio_fragments(directory):
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

    try:
        state = json.loads((directory / "editor.json").read_bytes())
        streaming, task, block = capture_options(state)
    except (OSError, ValueError, AttributeError):
        events.put(("retry", ("Could not read the saved recording settings.", False)))
        return
    poll_seconds, ui_poll_ms = delivery_intervals(cfg)

    path = None
    failures = 0
    known_received = set()
    last_result = None
    stop_seen = False
    last_poll = 0.0
    needs_sync = True
    next_index = 0
    final_files = None
    while True:
        try:
            done = finished.is_set()
            newly_stopped = done and not stop_seen
            stop_seen = done
            if done:
                if final_files is None:
                    final_files = [p for p in audio_fragments(directory) if p.stat().st_size >= 2]
                ready = final_files
            else:
                fragment = directory / f"{next_index:03d}.raw"
                ready = [fragment] if fragment.exists() and fragment.stat().st_size == block else []
            if path is None and not ready:
                if done:
                    events.put(("ready", "Empty recording"))
                    return
                finished.wait(.05 if streaming else poll_seconds)
                continue
            if path is None:
                start = time.monotonic()
                options = {}
                if streaming or task != "translate":
                    caps = client.call("GET", "/v1/capabilities")
                    if (streaming and caps.get("stream_fragment_seconds") != STREAM_SECONDS
                            or task not in caps.get("session_tasks", ())):
                        events.put(("retry", ("Update the Gateway to sync this saved recording.", False)))
                        return
                    options = {"json": {"streaming": streaming, "task": task}}
                session = client.call("POST", "/v1/sessions", headers={"Idempotency-Key": key}, **options)
                path = "/v1/sessions/" + session["session_id"]
                events.put(("session", session["session_id"]))
                metric("session_open", session["session_id"], duration_ms=elapsed_ms(start),
                       streaming=streaming, task=task, poll_ms=poll_seconds * 1000, ui_poll_ms=ui_poll_ms)
            session_id = path.rsplit("/", 1)[-1]
            if needs_sync or newly_stopped or time.monotonic() - last_poll >= poll_seconds:
                start = time.monotonic()
                progress = client.call("GET", path)
                last_poll = time.monotonic()
                needs_sync = False
                known_received.update(progress["received"])
                while next_index in known_received:
                    next_index += 1
                result = (progress["segments"], progress.get("preview", ""))
                if result != last_result:
                    events.put(("segments", result[0]))
                    if streaming:
                        events.put(("preview", (len(result[0]), result[1])))
                    if result[1] or any(part["text"] for part in result[0]):
                        metric("result_fetched", session_id, poll_request_ms=elapsed_ms(start, last_poll),
                               confirmed_segments=len(result[0]), final=progress["state"] == "succeeded")
                    last_result = result
                if progress["state"] == "succeeded":
                    events.put(("delivered", last_poll))
                    events.put(("ready", "Ready"))
                    return
                if progress["state"] == "failed":
                    events.put(("retry", ("Gateway could not finish this capture. Retry to reprocess the saved audio.", True)))
                    return
            for fragment in ready:
                index = int(fragment.stem)
                if index in known_received:
                    continue
                start = time.monotonic()
                with io.BytesIO() as buf:
                    with wave.open(buf, "wb") as wav:
                        wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                        data = fragment.read_bytes()
                        wav.writeframes(data[:len(data) // 2 * 2])
                    encoded = time.monotonic()
                    client.call("PUT", path + f"/fragments/{index}", data=buf.getvalue(),
                                headers={"Content-Type": "audio/wav"})
                known_received.add(index)
                while next_index in known_received:
                    next_index += 1
                metric("fragment_uploaded", session_id, index=index,
                       encode_ms=elapsed_ms(start, encoded), upload_ms=elapsed_ms(encoded))
            if done and ready and not progress["closed"]:
                start = time.monotonic()
                client.call("POST", path + "/finalize", json={"last_index": int(ready[-1].stem)})
                progress["closed"] = True
                metric("finalize_ack", session_id, request_ms=elapsed_ms(start))
            failures = 0
            interval = .05 if streaming and not done else poll_seconds
            if done:
                time.sleep(interval)
            else:
                finished.wait(interval)
        except requests.HTTPError as error:
            status = error.response.status_code
            needs_sync = True
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
            needs_sync = True
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
            events.put(("toggle", time.monotonic()))


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
    _, ui_poll_ms = delivery_intervals(cfg)
    wayland = wayland_session()
    if wayland:
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")
    pointer=HOME/"current"
    app = QApplication([])
    app.setApplicationName("InnerVoice")
    app.setDesktopFileName("io.github.arkano1dev.InnerVoice")
    app_icon = QIcon(str(Path(__file__).with_name("assets") / "innervoice.png"))
    app.setWindowIcon(app_icon)
    app.setQuitOnLastWindowClosed(False)
    class RecorderWindow(QWidget):
        def closeEvent(self, event):
            # Keep shortcut capture available through a normal task-switcher window.
            event.ignore()
            self.showMinimized()

        def showEvent(self, event):
            super().showEvent(event)
            QTimer.singleShot(0, restore_layout)

        def resizeEvent(self, event):
            super().resizeEvent(event)
            if wayland or not self.isVisible() or self.isMinimized():
                return
            area = self.screen().availableGeometry()
            frame = self.frameGeometry()
            if frame.bottom() > area.bottom():
                self.move(self.x(), max(area.top(), area.bottom() - frame.height() + 1))

        def mousePressEvent(self, event):
            if event.button() == Qt.LeftButton and self.windowHandle().startSystemMove():
                event.accept()
                return
            super().mousePressEvent(event)

    root = RecorderWindow(None, Qt.Window | Qt.FramelessWindowHint)
    root.setObjectName("recorderWindow")
    root.setWindowTitle("InnerVoice")
    root.setWindowIcon(app_icon)
    root.setStyleSheet("""
        QWidget#recorderWindow {background:#17171b}
        QWidget#pill {background:#19191d;border:1px solid #47474e;border-radius:26px}
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
        QPushButton:pressed {background:#494550}
        QPushButton:focus {border:1px solid #c9b8ff}
        QPushButton#task {background:#36323e;color:#e2d9f5;font-weight:bold;border-radius:10px}
        QPushButton#task:hover, QPushButton#task:focus {border-color:#b7a4d5}
        QPushButton#record {background:#35353d;border-color:#54545e}
        QPushButton#record:focus {border-color:#d4cedf}
        QPushButton#record[recording="true"] {background:#64353c;border-color:#a55d65}
        QPushButton::menu-indicator {image:none}
        QMenu {background:#242429;color:#e4e4e8;border:1px solid #55555d;padding:4px}
        QMenu::item {padding:8px 14px;border-radius:6px}
        QMenu::item:selected {background:#43434d}
        QProgressBar {background:#44444a;border:0;border-radius:2px}
        QProgressBar::chunk {background:#ff9aa5;border-radius:2px}
    """)
    outer = QVBoxLayout(root)
    outer.setContentsMargins(0, 0, 0, 0)
    sheet = QWidget()
    sheet.setObjectName("pill")
    sheet.setFixedWidth(min(424, app.primaryScreen().availableGeometry().width()))
    outer.addWidget(sheet)
    body = QVBoxLayout(sheet)
    body.setContentsMargins(14, 10, 14, 10)
    body.setSpacing(5)
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
    provisional = QLabel()
    provisional.setTextFormat(Qt.PlainText)
    provisional.setWordWrap(True)
    provisional.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
    provisional.setMaximumHeight(96)
    provisional.setAccessibleName("Provisional text")
    provisional.setStyleSheet("color:#d3c8e7;background:#302d37;border-left:3px solid #ad97ce;padding:4px 8px")
    provisional.hide()
    body.addWidget(provisional)

    def fit_text(*_):
        if root.isMinimized():
            return
        height = int(editor.document().size().height()) + 3
        screen = root.screen() or app.primaryScreen()
        available = screen.availableGeometry().height()
        controls = max(36, footer.sizeHint().height(), footer.heightForWidth(sheet.width() - 28))
        preview_height = min(96, provisional.heightForWidth(editor.width())) if not provisional.isHidden() else 0
        ceiling = max(24, available - controls - 16 - 2 - 48 - preview_height)
        editor.setFixedHeight(max(24, min(height, ceiling)))
        policy = Qt.ScrollBarAsNeeded if height > ceiling else Qt.ScrollBarAlwaysOff
        if editor.verticalScrollBarPolicy() != policy:
            editor.setVerticalScrollBarPolicy(policy)

    def restore_layout():
        if root.isMinimized():
            return
        body.invalidate()
        body.activate()
        fit_text()
        outer.activate()
        root.adjustSize()
        sheet.update()
        editor.viewport().update()

    outer.setSizeConstraint(QVBoxLayout.SetFixedSize)
    footer = QWidget(sheet)
    buttons = QHBoxLayout(footer)
    buttons.setContentsMargins(0, 0, 0, 0)
    buttons.setSpacing(4)
    body.addWidget(footer)
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
        button.setFixedSize(36, 36)
        button.setIconSize(QSize(20, 20))
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
        first_visible()

    def indicator(phase):
        status.setStyleSheet("color:" + {"Recording":"#ffa0aa", "Processing":"#edc280", "Ready":"#a2d9c7"}.get(phase, "#cac5df"))
        pix = app_icon.pixmap(32, 32)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor({"Recording":"#ff354e", "Processing":"#efbe67"}.get(phase, "#75d9cf")))
        painter.drawEllipse(21, 21, 8, 8)
        painter.end()
        icon = QIcon(pix)
        tray.setIcon(icon)
        tray.setToolTip("InnerVoice · " + phase)
        atomic(HOME/"indicator", phase.encode())

    indicator("Ready")
    if QSystemTrayIcon.isSystemTrayAvailable():
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
    task = cfg.get("task", "translate")
    if task not in TASK_LABELS:
        task = "translate"
    timing = {}

    def first_visible():
        if (timing.get("first") or root.isMinimized() or not root.isVisible()
                or not (provisional.text().strip() or editor.toPlainText().strip())):
            return
        timing["first"] = True
        if timing.get("session"):
            metric("first_text", timing["session"], start_to_visible_ms=elapsed_ms(timing.get("start")),
                   voice_to_visible_ms=elapsed_ms(timing.get("voice")))

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

    def toggle(pressed=None):
        nonlocal directory,state,working,stop,finished,started,replace_session,capture_thread,paused_at,capture_problem,timing
        pressed = pressed if type(pressed) is float else time.monotonic()
        if playback is not None:
            status.setText("Stop playback first")
            return
        if working:
            if not stop.is_set():
                timing["stop"] = pressed
                stop.set()
                status.setText("Finishing recording")
            return
        if transfer_active:
            status.setText("Processing saved audio")
            return
        new_directory = HOME / secrets.token_hex(16)
        new_state = {"applied": 0, "ready": False, "created": time.time(), "text": "",
                     "streaming": cfg.get("streaming") is True, "task": task}
        timing = {"start": pressed}
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
        provisional.clear()
        provisional.hide()
        streaming, _, block = capture_options(state)
        args = (directory, stop, events, cfg["microphone"], pause)
        capture_thread = threading.Thread(target=capture, args=args + (block,) if streaming else args)
        capture_thread.start()
        if streaming:
            reveal()
        transfer()

    def poll():
        nonlocal working, transfer_active, replace_session, paused_at, playback, capture_problem
        if working and not stop.is_set():
            seconds = int((paused_at or time.monotonic()) - started)
            elapsed.setText(f"{seconds // 60:02}:{seconds % 60:02}")
        while not events.empty():
            kind,value=events.get()
            if kind=="toggle":
                toggle(value)
            elif kind=="show":
                reveal()
            elif kind == "session":
                timing["session"] = value
            elif kind == "mic_opened":
                timing["mic_opened"] = value
            elif kind == "delivered":
                timing["delivery"] = value
            elif kind == "voice":
                timing["voice"] = value
            elif kind == "preview":
                confirmed, preview = value
                provisional.setText(preview if confirmed >= state["applied"] else "")
                provisional.setVisible(bool(provisional.text()))
                fit_text()
                first_visible()
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
                timing["captured"] = value
                finished.set()
                working = False
                timer.setInterval(ui_poll_ms)
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
                first_visible()
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
                provisional.clear()
                provisional.hide()
                fit_text()
                state["ready"]=True
                save()
                status.setText(value)
                retry_button.hide()
                indicator("Ready")
                if editor.toPlainText().strip():
                    copy_result()
                if timing.get("session"):
                    metric("final_text", timing["session"],
                           useful_text=bool(editor.toPlainText().strip()),
                           mic_open_ms=elapsed_ms(timing.get("start"), timing["mic_opened"])
                           if "mic_opened" in timing else None,
                           stop_to_capture_ms=elapsed_ms(timing.get("stop"), timing.get("captured")),
                           final_ui_ms=elapsed_ms(timing.get("delivery")),
                           capture_to_final_ms=elapsed_ms(timing.get("captured")),
                           stop_to_final_ms=elapsed_ms(timing.get("stop")),
                           start_to_final_ms=elapsed_ms(timing.get("start")))
                timer.setInterval(700)
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
    task_button = QPushButton()
    task_button.setObjectName("task")
    task_button.setFixedSize(40, 30)
    buttons.addWidget(task_button)
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
        provisional.clear()
        provisional.hide()
        timing.clear()
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
            transcript = " ".join(entry.get("text", "").split())
            if size < 2 and not transcript:
                continue
            duration = size // 32000
            count += 1
            stamp = time.strftime("%b %d, %H:%M", time.localtime(created))
            title = transcript[:42] + ("…" if len(transcript) > 42 else "")
            if not title:
                title = "Saved audio" if entry.get("ready") else "Awaiting transcription"
            details = [stamp]
            if size:
                details.append(f"{duration // 60}:{duration % 60:02}")
            if entry.get("task") in TASK_LABELS:
                details.append("ES" if entry["task"] == "transcribe" else "EN")
            label = title.replace("&", "&&") + "\n" + "  ·  ".join(details)
            recovery.addAction(label, lambda checked=False, file=saved: recover(file))
            if count == 10:
                break
        if not recovery.actions():
            recovery.addAction("No saved recordings").setEnabled(False)
    recovery.aboutToShow.connect(list_recoverable)
    menu.addAction("Record / Stop", toggle)
    menu.addAction("Show editor", reveal)
    task_menu = QMenu(task_button)
    live_setting = HOME / "live-preview"
    if live_setting.is_file():
        cfg["streaming"] = live_setting.read_text().strip() == "1"
    live_preview = task_menu.addAction("Live text while recording")
    live_preview.setCheckable(True)
    live_preview.setChecked(cfg.get("streaming") is True)

    def toggle_preview(enabled):
        try:
            atomic(live_setting, b"1" if enabled else b"0")
        except OSError:
            with QSignalBlocker(live_preview):
                live_preview.setChecked(cfg.get("streaming") is True)
            status.setText("Could not save live preview setting")
            return
        cfg["streaming"] = enabled
        status.setText("Live preview on next recording" if enabled else "Standard mode next recording")

    live_preview.toggled.connect(toggle_preview)
    task_menu.addSeparator()
    task_button.setMenu(task_menu)
    task_actions = {}

    def select_task(selected):
        nonlocal task
        task = selected
        task_button.setText("ES" if task == "transcribe" else "EN")
        description = "Next recording: " + TASK_LABELS[task]
        task_button.setAccessibleName(description)
        task_button.setToolTip(description)
        for key, action in task_actions.items():
            action.setChecked(key == selected)

    for key, label in TASK_LABELS.items():
        action = task_menu.addAction(label)
        action.setCheckable(True)
        action.triggered.connect(lambda checked=False, selected=key: select_task(selected))
        task_actions[key] = action
    select_task(task)
    tray.activated.connect(lambda reason: reveal() if reason == QSystemTrayIcon.Trigger else None)
    hotkeys = None
    if not wayland:
        from pynput.keyboard import GlobalHotKeys
        hotkeys = GlobalHotKeys({cfg.get("hotkey", "<cmd>+<shift>+<f8>"): lambda: events.put(("toggle", time.monotonic()))})
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
