import importlib.util
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients"))
import desktop


class WaylandDesktopTests(unittest.TestCase):
    def test_setup_leaves_shortcut_binding_to_desktop(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            with (
                patch.object(desktop, "CONFIG", config),
                patch.dict(os.environ, {"XDG_SESSION_TYPE": "wayland"}),
                patch.object(desktop, "check_gateway") as check,
                patch("builtins.input", side_effect=["https://100.100.10.2:9000", "", "", "", "", ""]) as prompt,
            ):
                desktop.setup()
            saved = json.loads(config.read_text())
            self.assertNotIn("hotkey", saved)
            self.assertEqual(saved["url"], "https://100.100.10.2:9000")
            check.assert_called_once_with(saved)
            self.assertEqual(prompt.call_count, 6)

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
