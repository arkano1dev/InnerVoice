"""Compatibility entry point for the single-user Telegram adapter."""
from pathlib import Path
import runpy

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).parent / "bot" / "bot.py"), run_name="__main__")
