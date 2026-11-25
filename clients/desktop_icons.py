"""Freedesktop identity and artwork shared by the recorder and host launcher."""
from pathlib import Path

APP_ID = "io.github.arkano1dev.InnerVoice"
PHASES = ("Ready", "Recording", "Processing", "Paused")


def icon_path(phase=None):
    suffix = "-" + phase.lower() if phase in PHASES else ""
    return Path(__file__).with_name("assets") / f"innervoice{suffix}.png"
