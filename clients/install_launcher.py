"""Install the per-user freedesktop launcher and application icon."""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def install_file(path, contents):
    with NamedTemporaryFile(dir=path.parent, delete=False) as pending:
        temporary = Path(pending.name)
        try:
            pending.write(contents)
            pending.close()
            temporary.chmod(0o644)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def main():
    app_id = "io.github.arkano1dev.InnerVoice"
    clients = Path(__file__).resolve().parent
    fallback = Path.home() / ".local/share"
    data = Path(os.environ.get("XDG_DATA_HOME") or fallback)
    if not data.is_absolute():
        data = fallback
    applications = data / "applications"
    icons = data / "icons/hicolor/128x128/apps"
    applications.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)

    launcher = str(clients.parent / "bin/innervoice-desktop")
    if any(ord(char) < 32 or ord(char) == 127 for char in launcher):
        raise SystemExit("Move the checkout to a path without control characters.")
    # Desktop Entry field codes, Exec quoting, then generic string escaping.
    command = launcher.replace("%", "%%")
    for char in ('\\', '"', '`', '$'):
        command = command.replace(char, '\\' + char)
    command = command.replace('\\', '\\\\')
    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=InnerVoice\n"
        "Comment=Record and transcribe speech\n"
        f'Exec="{command}" open\n'
        f"Icon={app_id}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;\n"
    )
    install_file(icons / f"{app_id}.png", (clients / "assets/innervoice.png").read_bytes())
    install_file(applications / f"{app_id}.desktop", entry.encode())


if __name__ == "__main__":
    main()
