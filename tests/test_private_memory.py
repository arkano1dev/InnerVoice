from pathlib import Path
import unittest
from unittest.mock import patch

from private_memory import require_private_memory


class PrivateMemoryTests(unittest.TestCase):
    def test_requires_effective_swap_limit_and_ram_backed_audio_mount(self):
        for limit, usage, filesystem, error in (
            ("0", "0", "tmpfs tmpfs rw,size=1024k", None),
            ("max", "0", "tmpfs tmpfs rw", "memory.swap.max=0"),
            ("1", "0", "tmpfs tmpfs rw", "memory.swap.max=0"),
            ("0", "1", "tmpfs tmpfs rw", "zero swap usage"),
            ("0", "0", "ext4 /dev/sda rw", "requires a tmpfs"),
        ):
            with self.subTest(limit=limit, usage=usage, filesystem=filesystem):
                files = {
                    "/sys/fs/cgroup/memory.swap.max": limit + "\n",
                    "/sys/fs/cgroup/memory.swap.current": usage + "\n",
                    "/proc/self/mountinfo": f"22 21 0:11 / /tmp rw - {filesystem}\n",
                }
                with patch.object(Path, "read_text", autospec=True, side_effect=lambda path: files[str(path)]):
                    if error:
                        with self.assertRaisesRegex(RuntimeError, error):
                            require_private_memory("/tmp")
                    else:
                        require_private_memory("/tmp")

    def test_missing_swap_controller_fails_closed(self):
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "cgroup v2 swap controller"):
                require_private_memory("/tmp")
