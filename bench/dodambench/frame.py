"""The frame through which every container on this computer is started. Not built yet: every name is here so
that its tests fail on what it does, not on a name."""
from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PLATFORM = sys.platform
SETTINGS: dict[str, object] = {"lock_wait_seconds": 60, "retry_seconds": 1}
PENDING: tuple[str, ...] = ()


def default_root() -> Path:
    return Path(tempfile.gettempdir()) / "dodam-frame"


ROOT = default_root()


class FrameRefused(Exception):
    def __init__(self, said: dict):
        super().__init__(str(said))
        self.said = said


class FrameStopped(FrameRefused):
    pass


def free_disk(path) -> int:
    return shutil.disk_usage(path).free


def read_top_memory(said: str) -> int | None:
    return None


class Batch:
    def __init__(self, worktrees=(), shim: bool = False, on_stop=None):
        self.run_id = ""
        self.folder = Path(tempfile.mkdtemp(prefix="dodam-frame-stub-"))
        self.tmp = self.folder
        self.stopped = None

    def open(self) -> "Batch":
        return self

    def close(self) -> None:
        pass

    def __enter__(self) -> "Batch":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def container(self, cmd, allow=()) -> list[str]:
        return list(cmd)

    def volume(self, name: str) -> list[str]:
        return ["docker", "volume", "create", name]

    def env(self) -> dict[str, str]:
        return {}


def current() -> Batch | None:
    return None


def container(cmd, allow=()) -> list[str]:
    return list(cmd)


def volume(name: str) -> list[str]:
    return ["docker", "volume", "create", name]


def close() -> None:
    pass


@contextlib.contextmanager
def batch(**options):
    yield Batch(**options)


def main(argv: list[str]) -> int:
    command = argv[argv.index("--") + 1:]
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
