"""The host process table, watched for the token while Harbor runs.

Any process of the same user can read the command line of every host process. The patched Harbor keeps the token
off command lines; this watch is the second line. It reads the command line of every process (`/bin/ps -A -ww -o
pid=,args=`) once before Harbor starts, every `interval` seconds while it runs and once after it ends.

When the token is in a command line, or the table cannot be read while Harbor runs, the watch stops Harbor. The
runner hands it Harbor's process as soon as it starts. After a sighting, the sighted processes that belong to
Harbor's process group get SIGKILL at once, which ends the exposure within one reading. Then Harbor is stopped
through its own process group (Harbor runs in a session of its own), which needs no reading of the table: SIGINT,
on which Harbor removes its containers, then SIGTERM, then SIGKILL to whatever is left of the group. If the group
cannot be signalled, Harbor itself is killed. The stop is a function the watch is given: on its own it is
stop_group below; the pilot gives it its runner's stop, so that one stop owns Harbor's group. The record says what happened, and never holds the token or a
command line that held it: only counts, process ids and the signals the stop sent and its errors.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Callable, Iterable

PS_ARGS = ("/bin/ps", "-A", "-ww", "-o", "pid=,args=")
# The same patience as the runner's own stop at its time limit: Harbor removes its containers on SIGINT.
STOP_GRACE_SECONDS = 300.0
TERM_GRACE_SECONDS = 60.0
KILL_WAIT_SECONDS = 30.0


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


def stop_group(proc: subprocess.Popen, sent: list[str], grace: float = STOP_GRACE_SECONDS,
               term_grace: float = TERM_GRACE_SECONDS) -> None:
    """Stop Harbor and its process group without reading the process table: SIGINT, then SIGTERM, then SIGKILL to
    whatever is left of the group. Each signal is added to `sent` as it goes, so the list stays true when this
    raises (OSError when the group cannot be signalled, TimeoutExpired when Harbor is not collected in time)."""
    for sig, wait in ((signal.SIGINT, grace), (signal.SIGTERM, term_grace)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            if proc.poll() is not None:
                return  # a group of ended, uncollected processes: macOS refuses the signal, and nothing is left
            raise
        sent.append(sig.name)
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # whatever Harbor left running in its group
        sent.append("SIGKILL")
    except ProcessLookupError:
        pass
    except PermissionError:
        if proc.poll() is None:
            raise
    proc.wait(timeout=KILL_WAIT_SECONDS)


class ProcessTableWatch:
    """Use as `with ProcessTableWatch(token) as watch:`; start Harbor inside only when `watch.clean`, and hand its
    process to `watch.track` as soon as it starts."""

    def __init__(self, token: str, sampler: Callable[[], bytes] = process_table, interval: float = 0.5,
                 stop: Callable[[subprocess.Popen, list[str]], None] = stop_group,
                 clock: Callable[[], float] = time.time):
        self._token, self.sampler, self.interval, self._stop, self.clock = token, sampler, interval, stop, clock
        self.times: list[float] = []
        self.hit_samples = 0
        self.hits_after_end = 0
        self.hit_pids: list[int] = []
        self._sighted: list[int] = []  # the processes of the latest reading that held the token
        self.error: str | None = None
        # not-started: the reading before the start was not clean; not-stopped: the watch never stopped Harbor
        # (a clean run, no Harbor process was handed over, or the token was only seen after Harbor ended);
        # ended-before-stop: Harbor had ended by itself when the watch went to stop it, and no signal was sent;
        # stopped: the watch sent signals and Harbor ended; stop-failed: Harbor was still running after the stop.
        self.harbor = "not-stopped"
        self.stop_signals: list[str] = []
        self.stop_error: str | None = None
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._must_stop = False
        self._stopping = False
        self._ended = False
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._stopper: threading.Thread | None = None

    @property
    def clean(self) -> bool:
        return self.hit_samples == 0 and self.error is None

    @property
    def stopped_harbor(self) -> bool:
        return self.harbor == "stopped"

    def _mask(self, text: str) -> str:
        return text.replace(self._token, "[token]")

    def sample(self) -> bool:
        """Read the table once; False when the token was in it or it could not be read."""
        try:
            table = self.sampler()
        except Exception as err:  # the reason is kept, never the token
            self.error = self._mask(f"{type(err).__name__}: {err}")
            return False
        self.times.append(self.clock())
        pids = token_pids(table, self._token)
        if pids:
            self._sighted = pids
            self.hit_samples += 1
            self.hits_after_end += self._ended
            self.hit_pids = sorted(set(self.hit_pids) | set(pids))
            return False
        return True

    def track(self, proc: subprocess.Popen) -> None:
        """Called by the runner right after it starts Harbor. If a reading already failed, the stop begins at once,
        in a thread of its own: the stop may be the runner's, which must not wait on itself."""
        with self._lock:
            self._proc = proc
            must_stop = self._must_stop
        if must_stop:
            self._stopper = threading.Thread(target=self._stop_harbor, name="process-table-stop", daemon=True)
            self._stopper.start()

    def _stop_harbor(self) -> None:
        with self._lock:
            self._must_stop = True
            proc = self._proc
            if proc is None or self._stopping:
                return  # nothing handed over yet: track() stops it when it comes
            self._stopping = True
        errors = []
        sent = self.stop_signals  # the stop adds to the watch's own list, so it survives an error
        for pid in self._sighted:  # end the exposure first: the sighted processes of Harbor's group
            try:
                if pid > 0 and os.getpgid(pid) == proc.pid:
                    os.kill(pid, signal.SIGKILL)
                    sent.append(f"SIGKILL to sighted process {pid}")
            except ProcessLookupError:
                pass
            except Exception as err:
                errors.append(f"killing sighted process {pid} failed: {type(err).__name__}: {err}")
        try:
            self._stop(proc, sent)
        except Exception as err:
            errors.append(f"stopping Harbor's process group failed: {type(err).__name__}: {err}")
        if proc.poll() is None:
            try:
                proc.kill()
                sent.append("SIGKILL to Harbor alone")
                proc.wait(timeout=KILL_WAIT_SECONDS)
            except Exception as err:
                errors.append(f"killing Harbor failed: {type(err).__name__}: {err}")
            if errors:
                errors.append("processes Harbor started may still be running")
        if errors:
            self.stop_error = self._mask("; ".join(errors))
        ended = proc.poll() is not None
        if not sent:
            self.harbor = "ended-before-stop" if ended else "stop-failed"
        else:
            self.harbor = "stopped" if ended else "stop-failed"

    def _run(self) -> None:
        while not self._done.wait(self.interval):
            if not self.sample():
                self._stop_harbor()
                return

    def __enter__(self) -> "ProcessTableWatch":
        if self.sample():
            self._thread = threading.Thread(target=self._run, name="process-table-watch", daemon=True)
            self._thread.start()
        else:
            self.harbor = "not-started"
            self._must_stop = True
        return self

    def __exit__(self, *exc) -> None:
        self._done.set()
        if self._stopper is not None:
            self._stopper.join()
        if self._thread is not None:
            self._thread.join()
            self._ended = True
            self.sample()

    def count_within(self, windows: Iterable[tuple[float, float]]) -> int:
        windows = list(windows)
        return sum(1 for t in self.times if any(start <= t <= end for start, end in windows))

    def outcome(self) -> str:
        """What happened to Harbor, in words, for messages."""
        if self.harbor == "not-started":
            return "Harbor was not started"
        if self.harbor == "stopped":
            return "Harbor was stopped" + (f" ({self.stop_error})" if self.stop_error else "")
        if self.harbor == "stop-failed":
            return f"Harbor could not be stopped ({self.stop_error})"
        if self.harbor == "ended-before-stop":
            return "Harbor had already ended when the watch went to stop it"
        if self.hit_samples and self.hit_samples == self.hits_after_end:
            return "the token was seen only after Harbor had ended"
        return "Harbor was not stopped by the watch"

    def record(self) -> dict:
        return {"command": " ".join(PS_ARGS), "interval_seconds": self.interval, "samples": len(self.times),
                "hit_samples": self.hit_samples, "hits_after_end": self.hits_after_end, "hit_pids": self.hit_pids,
                "error": self.error, "clean": self.clean, "harbor": self.harbor,
                "stopped_harbor": self.stopped_harbor, "stop_signals": self.stop_signals,
                "stop_error": self.stop_error}
