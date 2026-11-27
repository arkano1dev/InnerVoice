from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients"))
import desktop


class WaylandDesktopTests(unittest.TestCase):
    def test_native_resize_regions(self):
        for x, horizontal in ((0, "left"), (70, ""), (139, "right")):
            for y, vertical in ((1, "top"), (32, ""), (63, "bottom")):
                self.assertEqual(desktop.resize_region(x, y, 140, 64), (horizontal, vertical))
        self.assertEqual(desktop.resize_region(8, 8, 140, 64), ("", ""))

    def test_topmost_preference_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "always-on-top"
            self.assertFalse(desktop.pin_requested(path))
            for value, expected in ((b"1", True), (b"0", False), (b"\xff", False)):
                path.write_bytes(value)
                self.assertEqual(desktop.pin_requested(path), expected)

    def test_user_corrections_survive_preview_and_final_revisions(self):
        raw = "Meet Ana at noon"
        edited = "Meet Anna at noon"
        next_raw = "Meet Ana at noon tomorrow"
        merged = desktop.revised_draft(raw, edited, next_raw)
        self.assertEqual(merged, "Meet Anna at noon tomorrow")
        self.assertEqual(desktop.revised_draft(next_raw, merged, "Meet Ana at noon tomorrow."),
                         "Meet Anna at noon tomorrow.")
        self.assertEqual(desktop.revised_draft("Call Ana", "Call Ángela", "Call Ana tomorrow"),
                         "Call Ángela tomorrow")

    def test_desktop_build_context_includes_copy_sources(self):
        clients = ROOT / "clients"
        rules = (clients / "Dockerfile.desktop.dockerignore").read_text().splitlines()
        self.assertEqual(rules[0], "*")
        included = {rule[1:] for rule in rules if rule.startswith("!")}
        for line in (clients / "Dockerfile.desktop").read_text().splitlines():
            if line.startswith("COPY ") and "--from=" not in line:
                for source in shlex.split(line)[1:-1]:
                    self.assertIn(source, included)
                    self.assertTrue((clients / source).is_file())

    def test_setup_validates_connection_and_saves_hidden_token(self):
        token = "t" * 40
        caps = {"api_version": 1, "sessions": True, "fragment_seconds": 30}
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            certificate = config.with_name("ca.crt")
            certificate.write_text("test certificate")
            output = io.StringIO()

            def request(session, method, url, **options):
                self.assertEqual(method, "GET")
                self.assertEqual(url, "https://gateway.example.invalid:9000/v1/capabilities")
                self.assertEqual(session.headers["Authorization"], "Bearer " + token)
                self.assertEqual(session.verify, str(certificate))
                self.assertFalse(session.trust_env)
                self.assertFalse(config.exists())
                self.assertFalse(config.with_name("bearer").exists())
                response = MagicMock(status_code=200)
                response.json.return_value = caps
                return response

            with (
                patch.object(desktop, "CONFIG", config),
                patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}),
                patch.object(requests.Session, "request", autospec=True, side_effect=request),
                patch("builtins.input", side_effect=["", "https://gateway.example.invalid:9000", "", "", "", ""]),
                patch.object(desktop.getpass, "getpass", side_effect=["", token]),
                redirect_stdout(output),
            ):
                desktop.setup()
            saved = json.loads(config.read_text())
            self.assertNotIn("hotkey", saved)
            self.assertNotIn(token, json.dumps(saved))
            self.assertNotIn(token, output.getvalue())
            self.assertIn("Connected", output.getvalue())
            self.assertEqual(Path(saved["token_file"]).read_text().strip(), token)
            self.assertEqual(Path(saved["token_file"]).stat().st_mode & 0o777, 0o600)

    def test_setup_can_retry_a_denied_client_without_changing_saved_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            bearer = config.with_name("bearer")
            bearer.write_text("t" * 40)
            saved = {"url": "https://gateway.example.invalid:9000", "ca": "", "token_file": str(bearer), "microphone": "USB"}
            config.write_text(json.dumps(saved))
            output = io.StringIO()
            response = requests.Response()
            response.status_code = 403
            error = requests.HTTPError("403", response=response)
            with (
                patch.object(desktop, "CONFIG", config),
                patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}),
                patch.object(desktop, "check_gateway", side_effect=[error, None]) as check,
                patch("builtins.input", side_effect=["", "", "", "y", "", "", "", "n"]),
                redirect_stdout(output),
            ):
                with self.assertRaisesRegex(SystemExit, "not saved"):
                    desktop.setup()
            self.assertIn("Add the desktop's private IP", output.getvalue())
            self.assertEqual(check.call_count, 2)
            self.assertEqual(json.loads(config.read_text()), saved)
            self.assertEqual(bearer.read_text(), "t" * 40)

    def test_agent_reuses_saved_credential_without_reentry(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/"settings.json"
            bearer = config.with_name("bearer")
            bearer.write_text("a" * 40)
            config.write_text(json.dumps({"url":"https://gateway.example.invalid", "ca":"", "token_file":str(bearer)}))
            with (
                patch.object(desktop, "CONFIG", config),
                patch.object(desktop.Client, "call", return_value={"api_version":1, "jobs":True, "sessions":False}),
                patch.object(desktop.getpass, "getpass", side_effect=AssertionError("unexpected credential prompt")),
                patch("builtins.input", return_value=""),
                redirect_stdout(io.StringIO()),
            ):
                desktop.setup(agent=True)
            saved = json.loads(config.read_text())
            self.assertEqual(saved["role"], "agent")
            self.assertEqual(Path(saved["token_file"]).read_text(), "a" * 40)
            self.assertNotIn("a" * 40, config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_empty_token_file_path_reports_error_instead_of_opening_working_directory(self):
        with self.assertRaisesRegex(ValueError, "bearer token file"):
            desktop.Client({"url": "https://gateway.example.invalid:9000", "token_file": ""})

    def test_local_command_sends_only_toggle_and_handles_stopped_client(self):
        with patch.object(desktop.socket, "socket") as create_socket:
            client = create_socket.return_value.__enter__.return_value
            desktop.send_toggle()
            client.sendto.assert_called_once_with(b"toggle", str(desktop.HOME / "control.sock"))
            client.sendto.side_effect = FileNotFoundError
            with self.assertRaisesRegex(SystemExit, "not ready"):
                desktop.send_toggle()

    def test_receiver_passes_only_known_messages_to_the_ui(self):
        listener = MagicMock()
        listener.recv.side_effect = [b"unknown", b"toggle", b"toggle", BlockingIOError]
        events = queue.Queue()
        desktop.read_shortcuts(listener, events)
        kind, pressed = events.get_nowait()
        self.assertEqual(kind, "toggle")
        self.assertIsInstance(pressed, float)
        kind, pressed = events.get_nowait()
        self.assertEqual(kind, "toggle")
        self.assertIsInstance(pressed, float)
        self.assertTrue(events.empty())

    def test_installed_launcher_tracks_state_with_one_identity(self):
        import install_launcher
        from desktop_icons import APP_ID, PHASES, icon_path
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, XDG_DATA_HOME=tmp, HOME=tmp):
            install_launcher.main()
            entry = Path(tmp, "applications", APP_ID + ".desktop")
            for phase in (*PHASES, None):
                install_launcher.update_icon(phase)
                fields = dict(line.split("=", 1) for line in entry.read_text().splitlines() if "=" in line)
                self.assertEqual(fields["Name"], "InnerVoice")
                self.assertEqual(fields["StartupWMClass"], APP_ID)
                self.assertEqual(Path(fields["Icon"]).read_bytes(), icon_path(phase).read_bytes())
                stamp = entry.stat().st_ino
                install_launcher.update_icon(phase)
                self.assertEqual(entry.stat().st_ino, stamp)
            self.assertEqual(list(entry.parent.glob("*.desktop")), [entry])

    def test_launcher_prints_absolute_shortcut_and_requires_wayland(self):
        launcher = ROOT / "bin" / "innervoice-desktop"
        with tempfile.TemporaryDirectory() as directory:
            shortcut = subprocess.check_output([launcher, "shortcut"], text=True, cwd=directory)
            self.assertEqual(shlex.split(shortcut), [str(launcher), "toggle"])
            env = dict(os.environ, XDG_SESSION_TYPE="x11", DISPLAY=":0", XDG_RUNTIME_DIR=directory)
            result = subprocess.run([launcher, "setup"], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("local Wayland desktop session", result.stderr)
            docker = Path(directory) / "docker"
            docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            docker.chmod(0o700)
            env["PATH"] = f"{directory}:{env['PATH']}"
            command = subprocess.check_output([launcher, "toggle"], env=env, text=True, cwd=directory)
            self.assertEqual(command.splitlines()[-6:],
                             ["exec", "-T", "desktop", "/opt/innervoice/bin/python", "/app/desktop.py", "toggle"])

    def test_app_launcher_cold_start_leaves_pill_and_warm_open_shows_editor(self):
        launcher = ROOT / "bin" / "innervoice-desktop"
        with tempfile.TemporaryDirectory() as tmp:
            # Exercise the open dispatcher while standing in for its child invocations.
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            dispatcher = bindir / "open-dispatcher"
            dispatcher.write_bytes(launcher.read_bytes())
            child = bindir / "innervoice-desktop"
            child.write_text("""#!/bin/sh
echo "$1" >> "$HOME/calls"
case "$1" in
    show) test -f "$HOME/running" ;;
    start) touch "$HOME/running" ;;
esac
""")
            child.chmod(0o700)
            env = dict(os.environ, HOME=tmp)
            calls = Path(tmp) / "calls"
            subprocess.run(["bash", dispatcher, "open"], env=env, check=True)
            self.assertEqual(calls.read_text().splitlines(), ["show", "start"])
            subprocess.run(["bash", dispatcher, "open"], env=env, check=True)
            self.assertEqual(calls.read_text().splitlines(), ["show", "start", "show"])

    def test_host_network_is_an_explicit_compose_override(self):
        launcher = ROOT / "bin" / "innervoice-desktop"
        with tempfile.TemporaryDirectory() as directory:
            docker = Path(directory) / "docker"
            docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            docker.chmod(0o700)
            env = dict(os.environ, PATH=f"{directory}:{os.environ['PATH']}")
            default = subprocess.check_output([launcher, "status"], env=env | {"INNERVOICE_DESKTOP_HOST_NETWORK": "0"}, text=True)
            host = subprocess.check_output([launcher, "status"], env=env | {"INNERVOICE_DESKTOP_HOST_NETWORK": "1"}, text=True)
            override = str(ROOT / "compose.desktop.host-network.yml")
            self.assertNotIn(override, default.splitlines())
            self.assertIn(override, host.splitlines())
            self.assertEqual(host.splitlines()[-3:], ["ps", "-a", "desktop"])

    def test_audio_quota_preserves_existing_and_partial_takes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "audio-limit-mib").write_text("1")
            old = home / "old"
            old.mkdir()
            existing = old / "000.raw"
            existing.write_bytes(bytes(1024 ** 2 - 4))
            current = home / "new"
            current.mkdir()
            device = MagicMock()
            mic = device.RawInputStream.return_value.__enter__.return_value
            mic.read.return_value = (b"1234", False)
            events = queue.Queue()
            with patch.dict(sys.modules, sounddevice=device):
                desktop.capture(current, threading.Event(), events, "", threading.Event())
            mic.read.assert_called_once_with(2)
            self.assertEqual((current / "000.raw").read_bytes(), b"1234")
            self.assertEqual(existing.stat().st_size, 1024 ** 2 - 4)
            self.assertIn("audio limit", dict(events.queue)["capture_error"])

    def test_storage_cleanup_preview_and_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".local/state/innervoice-desktop"
            home.mkdir(parents=True)
            for i in range(4):
                folder = home / str(i)
                folder.mkdir()
                (folder / "editor.json").write_text(json.dumps({"created":i, "ready":i != 1}))
                (folder / "000.raw").write_bytes(b"00")
            args = [ROOT / "bin/innervoice-desktop", "storage", "clean", "--keep", "2"]
            env = dict(os.environ, HOME=tmp)
            preview = subprocess.check_output(args, env=env, text=True)
            self.assertIn(str(home / "0"), preview)
            self.assertEqual(len(list(home.glob("*/*.raw"))), 4)
            subprocess.check_output([*args, "--apply"], env=env)
            self.assertEqual({p.parent.name for p in home.glob("*/*.raw")}, {"1", "2", "3"})
            self.assertEqual(len(list(home.glob("*/editor.json"))), 4)

    def test_pause_preview_and_resume_preserve_samples(self):
        stop, pause = threading.Event(), threading.Event()
        events = queue.Queue()
        device = MagicMock()
        mic = device.RawInputStream.return_value.__enter__.return_value
        first, second = b"\x01\x00" * 4, b"\x02\x00" * 3

        def read(_):
            if mic.read.call_count == 1:
                pause.set()
                return first, False
            stop.set()
            return second, False

        mic.read.side_effect = read
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, sounddevice=device):
            directory = Path(tmp)
            worker = threading.Thread(target=desktop.capture, args=(directory, stop, events, "", pause))
            worker.start()
            try:
                while events.get(timeout=2)[0] != "paused":
                    pass
                self.assertEqual((directory / "000.raw").read_bytes(), first)
                desktop.play_audio(directory, threading.Event(), events)
                out = device.RawOutputStream.return_value.__enter__.return_value
                out.write.assert_called_once_with(first)
                pause.clear()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual((directory / "000.raw").read_bytes(), first + second)
            finally:
                stop.set()
                worker.join(2)

    @unittest.skipUnless(importlib.util.find_spec("PySide6" if os.environ.get("IV_DESKTOP_QT") == "6" else "PyQt5"),
                         "Qt bindings are required for the editor test")
    def test_editor_records_and_stops_from_shortcuts_then_copies_result(self):
        if os.environ.get("IV_DESKTOP_QT") == "6":
            from PySide6 import QtCore, QtWidgets
        else:
            from PyQt5 import QtCore, QtWidgets

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "settings.json"
            config.write_text('{"microphone": "", "streaming": true}')
            archived = home / "previous"
            archived.mkdir()
            metadata = archived / "editor.json"
            metadata.write_text('{"text":"Previous take","applied":1,"ready":true}')
            os.utime(metadata, (1, 1))
            (archived / "000.raw").write_bytes(b"\x01\x00" * 8)
            (home / "current").write_text("previous")
            transcript_only = home / "cleaned"
            transcript_only.mkdir()
            (transcript_only / "editor.json").write_text(
                '{"created":2,"text":"Saved after audio cleanup","ready":true,"task":"transcribe"}'
            )
            for i in range(10):
                older = home / f"older-{i}"
                older.mkdir()
                (older / "editor.json").write_text('{"created":0,"ready":true}')
                (older / "000.raw").write_bytes(b"00")
            runtime = home / "runtime"
            runtime.mkdir(mode=0o700)

            def capture(directory, stop, events, device, pause, block):
                (directory / "000.raw").write_bytes(b"\x01\x00" * 8)
                stop.wait(2)
                events.put(("stopped", None))

            user_edited = threading.Event()
            attempts = []
            def network(_, directory, ___, events, finished):
                attempts.append(directory)
                events.put(("session", "sample"))
                events.put(("preview", (0, "Draft words")))
                if len(attempts) == 1 and user_edited.wait(1):
                    events.put(("preview", (0, "Draft words continued")))
                if finished.wait(2):
                    if len(attempts) == 1:
                        events.put(("retry", ("Synthetic gateway failure", True)))
                        return
                    events.put(("segments", [{"index": 0, "text": "Test note"}]))
                    events.put(("ready", "Ready"))

            observed = {}
            listener = MagicMock()
            messages = iter([b"toggle", None, b"toggle"])
            def receive(_):
                message = next(messages, None)
                if message is None:
                    raise BlockingIOError
                return message
            listener.recv.side_effect = receive

            def inspect_editor():
                for widget in app.topLevelWidgets():
                    editor = widget.findChild(QtWidgets.QTextEdit)
                    if editor is not None and "startup" not in observed:
                        observed["startup"] = (widget.isVisible() and not editor.isVisible() and widget.height() == 64
                                               and bool(widget.windowFlags() & QtCore.Qt.FramelessWindowHint)
                                               and not editor.toPlainText()
                                               and editor.placeholderText() == "Ready when you are")
                        observed["ordinary"] = (
                            widget.windowType() == QtCore.Qt.Window
                            and not widget.windowFlags() & QtCore.Qt.WindowStaysOnTopHint
                        )
                    if editor is not None and editor.toPlainText() == "Draft words":
                        observed["inline"] = not editor.isReadOnly()
                        if not user_edited.is_set():
                            editor.selectAll()
                            editor.insertPlainText("Correct words")
                            user_edited.set()
                    if editor is not None and editor.toPlainText() == "Correct words continued":
                        observed["corrected"] = True
                    if editor is not None and not observed.get("retried"):
                        labels = [label.text() for label in widget.findChildren(QtWidgets.QLabel)]
                        if "Synthetic gateway failure" in labels:
                            observed["retried"] = True
                            button = next(b for b in widget.findChildren(QtWidgets.QPushButton) if b.accessibleName() == "Record")
                            button.click()
                            button.click()
                    if editor is None or editor.toPlainText() != "Test note":
                        continue
                    copy = next(button for button in widget.findChildren(QtWidgets.QPushButton) if button.accessibleName() == "Copy")
                    observed["auto"] = app.clipboard().text()
                    app.sendEvent(editor.viewport(), QtCore.QEvent(QtCore.QEvent.Enter))
                    def check_after_copy_delay():
                        observed["after_auto"] = widget.isVisible() and not widget.isMinimized()
                        glyph = copy.findChild(QtWidgets.QLabel, "glyph")
                        observed["centered"] = (glyph.geometry().center() - copy.rect().center()).manhattanLength() <= 2
                        def control(name):
                            return next(b for b in widget.findChildren(QtWidgets.QPushButton)
                                        if b.accessibleName() == name)
                        normal_size = widget.size()
                        observed["compact_delivery"] = normal_size.height() == 64 and not editor.isVisible()
                        observed["hidden_metric"] = "first_text" not in [call.args[0] for call in metrics.call_args_list]
                        observed["passive"] = not activate.called
                        actions = control("More controls").menu()
                        actions.aboutToShow.emit()
                        observed["overflow"] = {"Copy", "Play recording", "Local recording history",
                                                "Task and live text"} <= {a.text() for a in actions.actions()}
                        pin = control("Always on top")
                        pin.click()
                        observed["pin"] = ((home / "always-on-top").read_bytes() == b"1"
                                           and bool(widget.windowFlags() & QtCore.Qt.WindowStaysOnTopHint)
                                           and widget.size() == normal_size)

                        control("Expand editor").click()
                        app.processEvents()
                        expanded_size = widget.size()
                        control("Collapse editor").click()
                        app.processEvents()
                        observed["maximized"] = (expanded_size.width() == 520 and expanded_size.height() == 280
                                                  and widget.height() == normal_size.height())
                        widget.resize(600, 220)
                        app.processEvents()
                        observed["manual"] = editor.isVisible()
                        editor.setPlainText("Long dictation with wrapping. " * 400)
                        widget.showNormal()
                        for _ in range(5):
                            app.processEvents()
                        observed["bounded"] = widget.size() == QtCore.QSize(600, 220) and editor.verticalScrollBar().maximum() > 0
                        editor.selectAll()
                        selected = editor.textCursor().selectedText()
                        widget.resize(600, 64)
                        app.processEvents()
                        observed["width_only"] = not editor.isVisible()
                        control("Expand editor").click()
                        app.processEvents()
                        observed["restored"] = (widget.size() == QtCore.QSize(600, 220)
                                                and bool(selected) and editor.textCursor().selectedText() == selected)
                        widget.close()
                        observed["closed"] = not widget.isVisible()
                        widget.show()
                        app.processEvents()
                        observed["separate"] = (
                            editor.mapTo(widget, editor.rect().bottomLeft()).y()
                            < copy.mapTo(widget, copy.rect().topLeft()).y()
                            and widget.rect().contains(copy.mapTo(widget, copy.rect().bottomRight()))
                        )
                        task_button = widget.findChild(QtWidgets.QPushButton, "task")
                        task_button.menu().actions()[0].trigger()
                        observed["live"] = (home / "live-preview").read_text()
                        editor.setPlainText("Edited note")
                        copy.click()
                        observed["clipboard"] = app.clipboard().text()
                        observed["after_copy"] = widget.isVisible() and not widget.isMinimized()
                        history = next(b.menu() for b in widget.findChildren(QtWidgets.QPushButton)
                                       if b.accessibleName() == "Local recording history")
                        history.aboutToShow.emit()
                        observed["reprocess"] = any(a.text() == "Reprocess saved audio" for a in history.actions())
                        observed["count"] = sum(not a.isSeparator() and a.text() != "Reprocess saved audio"
                                                for a in history.actions())
                        observed["cleaned"] = any(
                            action.text().startswith("Saved after audio cleanup\n") and action.text().endswith("ES")
                            for action in history.actions()
                        )
                        previous = next((a for a in history.actions() if "Previous take" in a.text()), None)
                        if previous:
                            previous.trigger()
                            observed["history"] = editor.toPlainText()
                            observed["playable"] = any(
                                b.isVisible() and b.accessibleName() == "Play recording"
                                for b in widget.findChildren(QtWidgets.QPushButton)
                            )
                        app.exit(0)
                    QtCore.QTimer.singleShot(2500, check_after_copy_delay)
                    return
                QtCore.QTimer.singleShot(30, inspect_editor)

            env = {"XDG_SESSION_TYPE": "wayland", "QT_QPA_PLATFORM": "offscreen", "XDG_RUNTIME_DIR": str(runtime)}
            with patch.dict(os.environ, env):
                app = QtWidgets.QApplication([])
                QtCore.QTimer.singleShot(30, inspect_editor)
                watchdog = QtCore.QTimer()
                watchdog.setSingleShot(True)
                watchdog.timeout.connect(lambda: app.exit(0))
                watchdog.start(11000)
                with (
                    patch.object(QtWidgets, "QApplication", return_value=app),
                    patch.object(desktop, "HOME", home),
                    patch.object(desktop, "CONFIG", config),
                    patch.object(QtWidgets.QWidget, "activateWindow") as activate,
                    patch.object(desktop, "metric") as metrics,
                    patch.object(desktop, "capture", side_effect=capture),
                    patch.object(desktop, "network", side_effect=network),
                ):
                    desktop.graphical(listener)
                watchdog.stop()
            self.assertTrue(observed.get("retried"))
            self.assertEqual(len(set(attempts)), 2)
            self.assertEqual(observed.get("auto"), "Test note")
            for condition in ("startup", "ordinary", "after_auto", "after_copy", "compact_delivery", "manual", "pin",
                              "hidden_metric", "passive", "overflow", "width_only", "restored"):
                self.assertTrue(observed.get(condition), condition)
            self.assertEqual(observed.get("clipboard"), "Edited note")
            self.assertEqual(observed.get("history"), "Previous take")
            self.assertTrue(observed.get("playable"))
            self.assertEqual(observed["count"], 11)
            self.assertTrue(observed["cleaned"])
            self.assertTrue(observed["separate"])
            self.assertEqual(observed["live"], "0")
            self.assertTrue(observed["inline"])
            self.assertTrue(observed.get("corrected"))
            self.assertTrue(observed["centered"])
            self.assertTrue(observed["bounded"])
            self.assertTrue(observed["maximized"])
            self.assertTrue(observed["reprocess"])
            self.assertTrue(observed["closed"])
            saved = attempts[-1]
            self.assertTrue((saved / "000.raw").is_file())
            record = json.loads((saved / "editor.json").read_text())
            self.assertEqual(record["text"], "Edited note")
            self.assertTrue(record["ready"])
            self.assertEqual(record["applied"], 1)

    @unittest.skipUnless(os.environ.get("IV_DESKTOP_QT") == "6", "desktop container uses Qt 6")
    def test_transport_retry_and_reprocess_controls(self):
        from PySide6 import QtCore, QtWidgets, QtTest

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config = home / "settings.json"
            (home / "always-on-top").write_bytes(b"1")
            config.write_text('{"microphone":"","ui_poll_ms":100}')
            app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
            listener = MagicMock()
            listener.recv.side_effect = BlockingIOError
            failures = []
            attempts = []

            def capture(directory, stop, events, device, pause):
                (directory / "000.raw").write_bytes(bytes(32000))
                while not stop.wait(.01):
                    if pause.is_set():
                        events.put(("paused", None))
                        while pause.is_set() and not stop.wait(.01):
                            pass
                events.put(("stopped", None))

            def playback(directory, cancel, events):
                cancel.wait(5)
                events.put(("play_end", None))

            def network(cfg, directory, key, events, finished):
                attempts.append(key)
                if finished.wait(8):
                    if len(attempts) == 1:
                        events.put(("retry", ("Retry this take", False)))
                    elif len(attempts) == 2:
                        events.put(("segments", [{"index": 0, "text": "Saved words"}]))
                        events.put(("ready", "Ready"))
                    else:
                        events.put(("replacement", [{"text": "Reprocessed words"}]))
                        events.put(("ready", "Ready"))

            existing = set(app.topLevelWidgets())

            def scenario():
                window = next(w for w in app.topLevelWidgets()
                              if w not in existing and w.objectName() == "recorderWindow")
                editor = window.findChild(QtWidgets.QTextEdit)
                def button(label):
                    return next((b for b in window.findChildren(QtWidgets.QPushButton)
                                 if b.isVisible() and b.accessibleName() == label), None)
                def wait_for(predicate):
                    for _ in range(100):
                        value = predicate()
                        if value:
                            return value
                        yield
                    self.fail("Timed out waiting for the recorder control or result")

                pin = button("Always on top")
                self.assertTrue(pin.isChecked())
                self.assertTrue(window.windowFlags() & QtCore.Qt.WindowStaysOnTopHint)
                sheet = window.findChild(QtWidgets.QWidget, "surface")
                handle = MagicMock()
                with patch.object(window, "windowHandle", return_value=handle):
                    for x, horizontal in ((1, QtCore.Qt.LeftEdge), (100, None),
                                          (sheet.width() - 2, QtCore.Qt.RightEdge)):
                        for y, vertical in ((1, QtCore.Qt.TopEdge), (10, None),
                                            (sheet.height() - 2, QtCore.Qt.BottomEdge)):
                            point = QtCore.QPoint(x, y)
                            QtTest.QTest.mouseClick(sheet, QtCore.Qt.LeftButton, pos=point)
                            if horizontal is None and vertical is None:
                                handle.startSystemMove.assert_called_once_with()
                            else:
                                edges = horizontal or vertical
                                if horizontal and vertical:
                                    edges = horizontal | vertical
                                handle.startSystemResize.assert_called_once_with(edges)
                            handle.reset_mock()
                button("Expand editor").click()
                task = window.findChild(QtWidgets.QPushButton, "task")
                next(a for a in task.menu().actions() if a.text() == "Transcribe Spanish").trigger()
                self.assertIn("ES", task.text())
                button("Record").click()
                size = window.size()
                pin.click()
                self.assertEqual(window.size(), size)
                self.assertEqual((home / "always-on-top").read_bytes(), b"0")
                self.assertIsNone(button("Play recording"))
                button("Pause recording").click()
                play = yield from wait_for(lambda: button("Play recording"))
                play.click()
                self.assertFalse(button("Resume recording").isEnabled())
                button("Stop playback").click()
                yield from wait_for(lambda: button("Resume recording").isEnabled())
                button("Resume recording").click()
                self.assertIsNone(button("Play recording"))
                button("Stop recording").click()
                retry = yield from wait_for(lambda: button("Retry sync"))
                retry.click()
                yield from wait_for(lambda: editor.toPlainText() == "Saved words")
                history = button("Local recording history").menu()
                history.aboutToShow.emit()
                next(a for a in history.actions() if a.text() == "Reprocess saved audio").trigger()
                self.assertEqual(editor.toPlainText(), "Saved words")
                yield from wait_for(lambda: editor.toPlainText() == "Reprocessed words")
                self.assertEqual(app.clipboard().text(), "Reprocessed words")
                self.assertEqual(len(attempts), 3)

            steps = scenario()
            def tick():
                try:
                    next(steps)
                except StopIteration:
                    app.exit(0)
                except Exception as error:
                    failures.append(error)
                    app.exit(0)
                else:
                    QtCore.QTimer.singleShot(30, tick)

            with (patch.object(QtWidgets, "QApplication", return_value=app),
                  patch.object(desktop, "CONFIG", config), patch.object(desktop, "HOME", home),
                  patch.object(desktop, "capture", side_effect=capture),
                  patch.object(desktop, "play_audio", side_effect=playback),
                  patch.object(desktop, "network", side_effect=network)):
                QtCore.QTimer.singleShot(30, tick)
                desktop.graphical(listener)
            if failures:
                raise failures[0]
