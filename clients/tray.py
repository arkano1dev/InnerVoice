"""Reflect the local recorder state in the panel and installed launcher."""
import os
from pathlib import Path
import socket
import sys

from desktop_icons import APP_ID, icon_path
from install_launcher import update_icon

from PyQt5.QtCore import QFileSystemWatcher
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication, QMenu, QSystemTrayIcon

state = Path.home()/".local/state/innervoice-desktop"
app = QApplication(sys.argv)
app.setApplicationName("InnerVoice")
app.setApplicationDisplayName("InnerVoice")
app.setDesktopFileName(APP_ID)
app.setQuitOnLastWindowClosed(False)
if not QSystemTrayIcon.isSystemTrayAvailable():
    sys.exit("Enable an AppIndicator/StatusNotifier tray in the desktop shell.")
tray = QSystemTrayIcon()
menu = QMenu()


def command(message):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as control:
            control.sendto(message, os.fspath(state/"control.sock"))
    except OSError:
        tray.showMessage("InnerVoice", "Recorder offline")


def refresh():
    try:
        label = (state/"indicator").read_text()
    except OSError:
        label = "Offline"
    tray.setIcon(QIcon(str(icon_path(label))))
    tray.setToolTip("InnerVoice")
    try:
        update_icon(label)
    except OSError as error:
        print(f"Launcher icon unavailable: {error.strerror}", file=sys.stderr)


menu.addAction("Record / Stop", lambda: command(b"toggle"))
menu.addAction("Show editor", lambda: command(b"show"))
tray.setContextMenu(menu)
tray.activated.connect(lambda reason: command(b"show") if reason == tray.Trigger else None)
watch = QFileSystemWatcher([str(state)])
watch.directoryChanged.connect(refresh)
refresh()
tray.show()
sys.exit(app.exec_())
