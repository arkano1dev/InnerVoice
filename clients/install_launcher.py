"""Install the per-user freedesktop launcher and application icon."""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from desktop_icons import APP_ID, PHASES, icon_path


def install_file(path, contents):
    if path.is_file() and path.read_bytes() == contents:
        return
    with NamedTemporaryFile(dir=path.parent, delete=False) as pending:
        temporary = Path(pending.name)
        try:
            pending.write(contents)
            pending.close()
            temporary.chmod(0o644)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def data_home():
    fallback = Path.home() / ".local/share"
    data = Path(os.environ.get("XDG_DATA_HOME") or fallback)
    if not data.is_absolute():
        data = fallback
    return data


def entry_path():
    return data_home() / "applications" / f"{APP_ID}.desktop"


def installed_icon(phase):
    return data_home() / "icons/hicolor/128x128/apps" / (APP_ID + ("-" + phase.lower() if phase in PHASES else "") + ".png")


def update_icon(phase):
    path = entry_path()
    if path.is_file():
        lines = path.read_text().splitlines(keepends=True)
        icon = icon_field(phase)
        install_file(path, "".join(icon if line.startswith("Icon=") else line for line in lines).encode())


def icon_field(phase):
    path = str(installed_icon(phase)).replace("\\", "\\\\")
    return f"Icon={path}\n"


def main():
    clients = Path(__file__).resolve().parent
    data = data_home()
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
    try:
        phase = (Path.home()/".local/state/innervoice-desktop/indicator").read_text().strip()
    except OSError:
        phase = None
    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=InnerVoice\n"
        "Comment=Record and transcribe speech\n"
        f'Exec="{command}" open\n'
        f"{icon_field(phase)}"
        f"StartupWMClass={APP_ID}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;\n"
    )
    for phase in (None, *PHASES):
        install_file(installed_icon(phase), icon_path(phase).read_bytes())
    install_file(entry_path(), entry.encode())


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--clear-status"]:
        update_icon(None)
    else:
        main()
