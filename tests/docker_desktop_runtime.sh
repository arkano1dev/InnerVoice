#!/usr/bin/env bash
# Isolated compositor and synthetic Pulse sink, with no microphone, GPU, or network.
set -euo pipefail
umask 077
image=${1:?desktop image}
temp=$(mktemp -d)
export XDG_RUNTIME_DIR="$temp/runtime"
mkdir -m 700 "$XDG_RUNTIME_DIR" "$temp/home"
weston --backend=headless-backend.so --socket=iv-wayland --idle-time=0 >"$temp/weston.log" 2>&1 &
compositor=$!
export PULSE_SERVER="unix:$temp/pulse.sock"
pulseaudio -n --daemonize=no --exit-idle-time=-1 \
    -L "module-native-protocol-unix socket=$temp/pulse.sock auth-anonymous=1" \
    -L "module-null-sink sink_name=iv" >"$temp/pulse.log" 2>&1 &
audio=$!
cleanup() {
    kill "$compositor" "$audio" 2>/dev/null || true
    wait "$compositor" "$audio" 2>/dev/null || true
    rm -rf "$temp"
}
trap cleanup EXIT
for _ in {1..100}; do
    [[ -S "$XDG_RUNTIME_DIR/iv-wayland" && -S "$temp/pulse.sock" ]] && break
    sleep .1
done
[[ -S "$XDG_RUNTIME_DIR/iv-wayland" && -S "$temp/pulse.sock" ]] || { cat "$temp/weston.log" "$temp/pulse.log"; exit 1; }
pactl set-default-source iv.monitor
pactl set-default-sink iv

timeout 50 docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --user "$(id -u):$(id -g)" --tmpfs /tmp --tmpfs "/run/runtime:uid=$(id -u),gid=$(id -g),mode=0700" \
    -e XDG_RUNTIME_DIR=/run/runtime -e WAYLAND_DISPLAY=/run/iv/wayland -e XDG_SESSION_TYPE=wayland \
    -e QT_QPA_PLATFORM=wayland -e PULSE_SERVER=unix:/run/iv/pulse -e HOME=/home/recorder \
    -v "$XDG_RUNTIME_DIR/iv-wayland:/run/iv/wayland:ro" -v "$temp/pulse.sock:/run/iv/pulse:ro" \
    -v "$PWD/tests/test_desktop_wayland.py:/app/test_desktop_wayland.py:ro" \
    -v "$temp/home:/home/recorder" --entrypoint /opt/innervoice/bin/python "$image" - <<'PY'
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
import desktop

assert os.geteuid() and os.getegid()
desktop.CONFIG.parent.mkdir(parents=True)
desktop.CONFIG.write_text('{"microphone": ""}')
ui = subprocess.Popen([os.sys.executable, "/app/desktop.py", "run"])
try:
    for _ in range(100):
        if ui.poll() is not None:
            raise RuntimeError(f"Recorder exited with {ui.returncode}")
        if (desktop.HOME / "control.sock").exists() and (desktop.HOME / "indicator").exists():
            break
        time.sleep(.1)
    else:
        raise RuntimeError("The recorder did not open on Wayland")
    desktop.send_toggle(b"show")
    directory = desktop.HOME / "fixture"
    directory.mkdir()
    events = queue.Queue()
    stop = threading.Event()
    recorder = threading.Thread(target=desktop.capture, args=(directory, stop, events, "", threading.Event()))
    recorder.start()
    try:
        while True:
            event, value = events.get(timeout=12)
            assert event != "capture_error", value
            if event == "waveform":
                assert value and all(0 <= peak <= 1 for peak in value)
                break
    finally:
        stop.set()
        recorder.join(5)
    assert not recorder.is_alive()
    assert (directory / "000.raw").stat().st_size >= 2
    desktop.play_audio(directory, threading.Event(), events)
    while True:
        event, value = events.get(timeout=1)
        assert event not in ("capture_error", "play_error"), (event, value)
        if event == "play_end":
            break
    print("Wayland window, capture and playback passed", flush=True)
finally:
    ui.send_signal(signal.SIGTERM)
    assert ui.wait(timeout=10) == 0

for case in ("test_editor_records_and_stops_from_shortcuts_then_copies_result",
             "test_transport_retry_and_reprocess_controls"):
    subprocess.run([os.sys.executable, "-m", "unittest", f"test_desktop_wayland.WaylandDesktopTests.{case}"],
                   check=True, cwd="/app", env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
PY
