"""Verify the effective Linux protections for private server audio."""
from pathlib import Path


def require_private_memory(*audio_mounts):
    cgroup = Path("/sys/fs/cgroup")
    try:
        # Private tmpfs pages and process memory are charged to the service cgroup.
        swap_limit = (cgroup / "memory.swap.max").read_text().strip()
        swap_usage = (cgroup / "memory.swap.current").read_text().strip()
    except OSError as error:
        raise RuntimeError("Private audio requires the cgroup v2 swap controller") from error
    if swap_limit != "0" or swap_usage != "0":
        raise RuntimeError("Private audio requires memory.swap.max=0 and zero swap usage")
    mounts = {}
    for entry in Path("/proc/self/mountinfo").read_text().splitlines():
        mount, filesystem = entry.split(" - ", 1)
        mounts[mount.split()[4]] = filesystem.split()[0]
    for path in audio_mounts:
        if mounts.get(path) != "tmpfs":
            raise RuntimeError(f"Private audio requires a tmpfs at {path}")
