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
import unittest
from unittest.mock import MagicMock, patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients"))
import desktop


class WaylandDesktopTests(unittest.TestCase):
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
                self.assertEqual(url, "https://100.100.10.2:9000/v1/capabilities")
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
                patch("builtins.input", side_effect=["", "https://100.100.10.2:9000", "", "", "", ""]),
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
            saved = {"url": "https://100.100.10.2:9000", "ca": "", "token_file": str(bearer), "microphone": "USB"}
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

    def test_empty_token_file_path_reports_error_instead_of_opening_working_directory(self):
        with self.assertRaisesRegex(ValueError, "bearer token file"):
            desktop.Client({"url": "https://100.100.10.2:9000", "token_file": ""})

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
        self.assertEqual(events.get_nowait(), ("toggle", None))
        self.assertEqual(events.get_nowait(), ("toggle", None))
        self.assertTrue(events.empty())

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

    @unittest.skipUnless(importlib.util.find_spec("PyQt5"), "PyQt5 is required for the editor test")
    def test_editor_records_and_stops_from_shortcuts_then_copies_result(self):
        from PyQt5 import QtCore, QtWidgets

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / "settings.json"
            config.write_text('{"microphone": ""}')
            runtime = home / "runtime"
            runtime.mkdir(mode=0o700)

            def capture(_, stop, events, __):
                stop.wait(2)
                events.put(("stopped", None))

            def network(_, __, ___, events, finished):
                if finished.wait(2):
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
                    if editor is None or editor.toPlainText() != "Test note":
                        continue
                    copy = next(button for button in widget.findChildren(QtWidgets.QPushButton) if button.text() == "Copy")
                    copy.click()
                    observed["clipboard"] = app.clipboard().text()
                    app.quit()
                    return
                QtCore.QTimer.singleShot(30, inspect_editor)

            env = {"XDG_SESSION_TYPE": "wayland", "QT_QPA_PLATFORM": "offscreen", "XDG_RUNTIME_DIR": str(runtime)}
            with patch.dict(os.environ, env):
                app = QtWidgets.QApplication([])
                QtCore.QTimer.singleShot(30, inspect_editor)
                QtCore.QTimer.singleShot(3000, app.quit)
                with (
                    patch.object(QtWidgets, "QApplication", return_value=app),
                    patch.object(desktop, "HOME", home),
                    patch.object(desktop, "CONFIG", config),
                    patch.object(desktop, "capture", side_effect=capture),
                    patch.object(desktop, "network", side_effect=network),
                ):
                    desktop.graphical(listener)
            self.assertEqual(observed.get("clipboard"), "Test note")
            record = json.loads((home / (home / "current").read_text() / "editor.json").read_text())
            self.assertEqual(record["text"], "Test note")
            self.assertTrue(record["ready"])
            self.assertEqual(record["applied"], 1)
