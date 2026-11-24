"""Host indicator for a sandboxed Wayland recorder. Only reads its state label."""
import os
from pathlib import Path
import socket
import sys

from PyQt5.QtCore import QFileSystemWatcher, Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import QApplication, QMenu, QSystemTrayIcon

state = Path.home()/".local/state/innervoice-desktop"
app = QApplication(sys.argv)
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
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    paint = QPainter(pix)
    paint.setRenderHint(QPainter.Antialiasing)
    paint.setPen(Qt.NoPen)
    paint.setBrush(QColor({"Recording":"#ff354e", "Ready":"#75d9cf", "Processing":"#efbe67"}.get(label, "#808088")))
    paint.drawEllipse(4, 4, 24, 24)
    paint.end()
    tray.setIcon(QIcon(pix))
    tray.setToolTip("InnerVoice · " + label)


menu.addAction("Record / Stop", lambda: command(b"toggle"))
menu.addAction("Show editor", lambda: command(b"show"))
tray.setContextMenu(menu)
tray.activated.connect(lambda reason: command(b"show") if reason == tray.Trigger else None)
watch = QFileSystemWatcher([str(state)])
watch.directoryChanged.connect(refresh)
refresh()
tray.show()
sys.exit(app.exec_())
