"""The host process table, watched for the token while Harbor runs.

Any process of the same user can read the command line of every host process. The patched Harbor keeps the token
off command lines; this watch is the second line. It reads the command line of every process (`ps -A -ww -o
pid=,args=`) once before Harbor starts, every `interval` seconds while it runs and once after it ends. When the token
is in one, or the table cannot be read, it stops every process the pilot started (Harbor and the docker commands
under it). Neither the token nor a command line that held it is kept: the record has counts and process ids.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Callable, Iterable

PS_ARGS = ("ps", "-A", "-ww", "-o", "pid=,args=")
PS_TREE = ("ps", "-A", "-o", "pid=,ppid=,stat=")


def process_table() -> bytes:
    """The pid and full command line of every process, one per line."""
    result = subprocess.run(PS_ARGS, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    if result.returncode != 0:
        raise OSError(f"ps exited with status {result.returncode}")
    return result.stdout


def token_pids(table: bytes, token: str) -> list[int]:
    """The pids of the lines whose command line holds the token (-1 when the pid cannot be read)."""
    needle = token.encode("utf-8")
    pids = []
    for line in table.splitlines():
        if needle in line:
            head = line.split(None, 1)
            pids.append(int(head[0]) if head and head[0].isdigit() else -1)
    return pids


def _rows() -> list[tuple[int, int, str]]:
    """(pid, parent pid, state) of every process."""
    result = subprocess.run(PS_TREE, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def child_pids(root: int) -> list[int]:
    """Every live (not zombie) descendant of `root`, from one reading of the process table."""
    children: dict[int, list[int]] = {}
    for pid, ppid, state in _rows():
        if not state.startswith("Z"):
            children.setdefault(ppid, []).append(pid)
    found, todo = [], [root]
    while todo:
        for pid in children.get(todo.pop(), []):
            if pid not in found:
                found.append(pid)
                todo.append(pid)
    return found


def _signal(pids: Iterable[int], sig: int) -> list[int]:
    sent = []
    for pid in pids:
        try:
            os.kill(pid, sig)
            sent.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    return sent


def stop_children(grace: float = 5.0) -> list[int]:
    """SIGTERM every descendant of this process, then SIGKILL those still alive after `grace` seconds.
    Returns the pids that got the first signal."""
    signalled = _signal(child_pids(os.getpid()), signal.SIGTERM)
    end = time.monotonic() + grace
    while True:
        live = {pid for pid, _, state in _rows() if not state.startswith("Z")}
        left = [pid for pid in signalled if pid in live]
        if not left or time.monotonic() >= end:
            break
        time.sleep(0.1)
    _signal(left, signal.SIGKILL)
    return signalled


class ProcessTableWatch:
    """Use as `with ProcessTableWatch(token) as watch:`, and start Harbor inside only when `watch.clean`."""

    def __init__(self, token: str, sampler: Callable[[], bytes] = process_table, interval: float = 0.5,
                 stop: Callable[[], list[int]] = stop_children, clock: Callable[[], float] = time.time):
        self._token, self.sampler, self.interval, self._stop, self.clock = token, sampler, interval, stop, clock
        self.times: list[float] = []
        self.hit_samples = 0
        self.hit_pids: list[int] = []
        self.error: str | None = None
        self.stopped_harbor = False
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def clean(self) -> bool:
        return self.hit_samples == 0 and self.error is None

    def sample(self) -> bool:
        """Read the table once; False when the token was in it or it could not be read."""
        try:
            table = self.sampler()
        except Exception as err:  # the reason is kept, never the token
            self.error = f"{type(err).__name__}: {err}".replace(self._token, "[token]")
            return False
        self.times.append(self.clock())
        pids = token_pids(table, self._token)
        if pids:
            self.hit_samples += 1
            self.hit_pids = sorted(set(self.hit_pids) | set(pids))
            return False
        return True

    def _run(self) -> None:
        while not self._done.wait(self.interval):
            if not self.sample():
                if not self._done.is_set():
                    self.stopped_harbor = True
                    self._stop()
                return

    def __enter__(self) -> "ProcessTableWatch":
        if self.sample():
            self._thread = threading.Thread(target=self._run, name="process-table-watch", daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._done.set()
        if self._thread is not None:
            self._thread.join()
            self.sample()

    def count_within(self, windows: Iterable[tuple[float, float]]) -> int:
        windows = list(windows)
        return sum(1 for t in self.times if any(start <= t <= end for start, end in windows))

    def record(self) -> dict:
        return {"command": " ".join(PS_ARGS), "interval_seconds": self.interval, "samples": len(self.times),
                "hit_samples": self.hit_samples, "hit_pids": self.hit_pids, "error": self.error,
                "clean": self.clean, "stopped_harbor": self.stopped_harbor}
