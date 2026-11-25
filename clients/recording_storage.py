"""Local audio retention and offline storage administration."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path

MIB = 1024 ** 2
DEFAULT_LIMIT_MIB = 2048


def limit_bytes(home):
    setting = home / "audio-limit-mib"
    limit = int(setting.read_text()) if setting.exists() else DEFAULT_LIMIT_MIB
    if limit <= 0:
        raise ValueError("Audio limit must be positive")
    return limit * MIB


def audio_bytes(home):
    return sum(p.stat().st_size for p in home.glob("*/*.raw"))


def recordings(home):
    entries = []
    for saved in home.glob("*/editor.json"):
        if saved.is_symlink() or saved.parent.is_symlink():
            continue
        try:
            entry = json.loads(saved.read_bytes())
            created = float(entry.get("created", saved.stat().st_mtime))
            if not math.isfinite(created):
                continue
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        entries.append((created, saved, entry))
    return sorted(entries, key=lambda row: (row[0], row[1]), reverse=True)


def cleanup_candidates(home, keep):
    for _, saved, entry in recordings(home):
        fragments = sorted(saved.parent.glob("[0-9][0-9][0-9].raw"))
        if not any(p.stat().st_size for p in fragments):
            continue
        if keep:
            keep -= 1
        elif entry.get("ready") is True:
            yield from fragments


def main():
    parser = argparse.ArgumentParser(description="Manage local InnerVoice recording storage")
    commands = parser.add_subparsers(dest="action")
    commands.add_parser("status")
    commands.add_parser("limit").add_argument("mib", type=int)
    clean = commands.add_parser("clean")
    clean.add_argument("--keep", type=int, default=10)
    clean.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    home = Path.home() / ".local/state/innervoice-desktop"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action in (None, "status"):
        print(f"{home}\nAudio: {audio_bytes(home) / MIB:.2f} MiB / {limit_bytes(home) // MIB} MiB")
        return
    if args.action == "clean" and args.keep < 0:
        parser.error("--keep must be zero or greater")
    if args.action == "limit" and args.mib <= 0:
        parser.error("limit must be positive")
    with (home / "desktop.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "Stop the desktop recorder before managing storage.\n")
        if args.action == "limit":
            temporary = home / "audio-limit-mib.tmp"
            temporary.write_text(str(args.mib))
            os.replace(temporary, home / "audio-limit-mib")
            print(f"Audio limit set to {args.mib} MiB")
            return
        files = list(cleanup_candidates(home, args.keep))
        total = sum(p.stat().st_size for p in files)
        for directory in sorted({p.parent for p in files}):
            print(directory)
        if args.apply:
            for fragment in files:
                fragment.unlink()
        print(f"{'Removed' if args.apply else 'Eligible'}: {total / MIB:.2f} MiB of completed audio")


if __name__ == "__main__":
    main()
