"""Run a command-line program for the hidden tests of a Dodam benchmark task, one case at a time.

The judge copies this file next to itself (as /tests/lib/dodam_cli.py in the verifier) and the hidden
tests of a command-line task import it. A test never starts the program itself:

    from dodam_cli import case

    def test_converts_a_file():
        with case(files={"in.csv": "a,b\\n1,2\\n"}) as c:
            r = c.run(["in.csv", "out.json"])
            assert r.exit_code == 0 and r.out == ""
            assert c.read("out.json") is not None

A case is one test function. It gets a fresh folder holding three folders: `work`, where the program is
started and where the case's input files are; `outside`, a neighbour the program could reach if it left
its working folder; and a home and temporary folder of its own. The test sees the program only from
outside: its standard output and error, its exit code and the files it left.

Every case has limits. In the verifier each is a bound that holds whatever the program does:

- time, added up over the runs of the case: the judge ends the program when it is up;
- output, standard output plus standard error: the judge stops reading and ends the program;
- processes and threads: the kernel refuses one more than the limit, for the program's user alone;
- files: the case's folder lives in a memory-backed folder that the container caps at the limit, so
  no more can be written, whether in many files, deep trees or files deleted while open; one file
  cannot be made larger than the limit either, not even a sparse one;
- memory: the judge measures, as often as it gets to run and at most every two thousandths of a
  second, everything the kernel counts for the container beyond what was there when the run began,
  memory moved out to swap included, and ends the program above the limit. Between two
  measurements the program can be above it. After the run the judge also reads the highest value
  the kernel itself kept for the container. That value counts the cache of files on disk, so the
  judge takes out of a new high everything the container read from disk during the run; what is
  left above the limit fails the case even when no measurement saw it. A program that only reads
  files, cached or not, never fails this way. The program can never be above the container's memory cap,
  where the kernel ends it first, and that fails the case as well. Memory that would outlive the
  program's processes cannot be taken: the calls for it are refused.

A case that broke a limit fails, whatever the test asserts and even when the test would have been
left unjudged; so does a program that leaves processes behind, and a case whose folder cannot be
removed. After every run nothing of the program is left running, and after every case its folder
is removed.

In the verifier the program runs as a user of its own that owns nothing, in no group, and that cannot
gain privileges; the judge has taken away its write access everywhere but the case's folder. Without
that separation (only for the project's own prepared solutions) the program runs as the judge's user
and only the time, output and file-size limits hold. A fault of the judge itself is written to the
fault file the judge names and is never reported as a failure of the program.
"""
from __future__ import annotations

import errno
import json
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import dodam_judge as _judge   # in the verifier the judge sits next to this file under that name
except ImportError:
    try:
        import judge as _judge
    except ImportError:
        from dodambench import judge as _judge   # imported as part of the package

resource = _judge.resource
CONFIG_ENV = "DODAM_CLI_CONFIG"
def _ended_by_the_kernel(killed_before: int, killed_now: int) -> bool:
    """The container's memory ran out during the run and the kernel ended a process for it."""
    return killed_now > killed_before


def _above_in_a_new_high(high_before: int, high_now: int, taken_before: int, allowed: int) -> int:
    """How far a new high of the container's memory lies above what the case may take in memory
    and files together, counted from the memory that could not be given back when the run began;
    0 when the run made no new high, or one within that.

    The kernel keeps the high whatever happens between two measurements. But it counts the cache
    of files into it, which is no memory of the program's and which grows in ways the kernel
    counts nowhere for the container: by reading a file that is on disk, by reading one that has
    holes or room that was never written, by writing. What the kernel kept cannot be told apart
    afterwards. So this number is the most the program can have held at once, not what it held;
    it is written into the result and fails no case. A case fails for memory by what the judge
    measured, or because the kernel ended a process. The high never goes down, so a run that
    stays below an earlier high of the container gives 0 here whatever it took."""
    if high_now <= high_before:
        return 0
    return max(high_now - taken_before - allowed, 0)


# How often the judge looks at the program: whether it ended, and how much memory the container holds.
TICK = 0.002
WATCH = 0.002
PR_SET_CHILD_SUBREAPER = 36


class JudgeFault(Exception):
    """The judge could not do its own work. Not a failure of the program."""


class LimitBroken(AssertionError):
    """The program broke a limit of the case, so the case fails."""


@dataclass
class Result:
    exit_code: int | None  # None when the program did not end by itself with an exit code
    stdout: bytes
    stderr: bytes
    limit: str | None      # the limit the program broke: time, memory, output, processes, files, left processes
    signal: int | None     # the signal that ended the program, when one did
    seconds: float

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")

    def __repr__(self) -> str:
        return (f"Result(exit_code={self.exit_code}, limit={self.limit!r}, signal={self.signal}, "
                f"stdout={self.stdout[:200]!r}, stderr={self.stderr[:200]!r})")


def need(condition: bool, what: str) -> None:
    """Leave a security test unjudged when the feature it attacks does not work."""
    if not condition:
        import pytest
        pytest.skip(f"precondition failed: {what}")


_config: dict | None = None
_spent: dict[str, float] = {}


def config() -> dict:
    global _config
    if _config is None:
        path = os.environ.get(CONFIG_ENV)
        if not path:
            raise JudgeFault(f"{CONFIG_ENV} is not set: the judge did not say how to run the program")
        try:
            _config = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise fault(f"cannot read the judge's settings: {exc}") from None
        if _config.get("uid") is not None:
            # children the program leaves become children of this process, which collects them
            _judge._libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    return _config


def fault(text: str) -> JudgeFault:
    """Record a fault of the judge where the judge will find it, and return the error to raise."""
    path = (_config or {}).get("fault_file") or os.environ.get("DODAM_CLI_FAULT_FILE")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"fault": text, "test": os.environ.get("PYTEST_CURRENT_TEST", "")}) + "\n")
        except OSError:
            pass
    return JudgeFault(text)


def _collect() -> None:
    """Collect every ended child, so none stays in the process table."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _files_held(folder) -> int:
    """Bytes held by the memory-backed folder the cases live in, files deleted while open included."""
    room = os.statvfs(folder)
    return (room.f_blocks - room.f_bfree) * room.f_frsize


class Case:
    def __init__(self, files: dict | None = None, outside: dict | None = None):
        cfg = config()
        self._cfg = cfg
        self._uid, self._gid = cfg.get("uid"), cfg.get("gid")
        self._limits = cfg["limits"]
        self._key = os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0]
        self._broken: list[str] = []
        # how the judge measured this case: how often, and the longest time between two measurements
        self.measured = {"measurements": 0, "longest_gap": 0.0}
        self._side = _judge.Side(self._uid, self._gid) if self._uid is not None else None
        try:
            # cases run one after another, so whatever is in the cases' folder now is not this case's
            _judge.remove_tree(cfg["work_root"], keep_top=True)
            self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="case-", dir=cfg["work_root"])))
            os.chmod(self.root, 0o755)
            self.work, self.outside, self.home = self.root / "work", self.root / "outside", self.root / "home"
            for folder in (self.work, self.outside, self.home, self.home / "tmp"):
                folder.mkdir()
                self._give(folder)
        except OSError as exc:
            raise fault(f"cannot make the case's folder: {exc}") from None
        for rel, data in (files or {}).items():
            self.write(rel, data)
        for rel, data in (outside or {}).items():
            self.write_outside(rel, data)

    # --- the case's files, as the test puts them there and as the program left them ---

    def _give(self, path: Path) -> None:
        if self._uid is not None:
            os.lchown(path, self._uid, self._gid)

    def _place(self, base: Path, rel: str) -> Path:
        """Where a file of the case goes; never through a link the program left."""
        path = base / rel
        parent = path.parent
        made = []
        while not parent.exists() and not parent.is_symlink():
            made.append(parent)
            parent = parent.parent
        for folder in reversed(made):
            folder.mkdir()
            self._give(folder)
        real = os.path.realpath(path.parent)
        if os.path.commonpath([real, os.path.realpath(self.root)]) != os.path.realpath(self.root):
            raise AssertionError(f"the program left a link where the case's file {rel} goes")
        if path.is_symlink() or path.is_file():
            path.unlink()
        return path

    def _write(self, base: Path, rel: str, data) -> Path:
        path = self._place(base, rel)
        if isinstance(data, str):
            data = data.encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        self._give(path)
        return path

    def write(self, rel: str, data) -> Path:
        """Put a file into the working folder."""
        return self._write(self.work, rel, data)

    def write_outside(self, rel: str, data) -> Path:
        """Put a file next to the working folder, where the program has no business."""
        return self._write(self.outside, rel, data)

    def sparse(self, rel: str, size: int) -> Path:
        """Put a file of the given size into the working folder that takes no disk space."""
        path = self._write(self.work, rel, b"")
        os.truncate(path, size)
        return path

    def mkdir(self, rel: str) -> Path:
        """Put a folder into the working folder."""
        return self._place(self.work, rel + "/-").parent

    def symlink(self, rel: str, target) -> Path:
        """Put a symbolic link into the working folder."""
        path = self._place(self.work, rel)
        os.symlink(str(target), path)
        self._give(path)
        return path

    def _read(self, base: Path, rel: str) -> bytes | None:
        path = base / rel
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
            real, root = os.path.realpath(path), os.path.realpath(self.root)
            if os.path.commonpath([real, root]) != root:
                return None
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except (OSError, ValueError):
            return None
        with os.fdopen(fd, "rb") as f:
            return f.read()

    def read(self, rel: str) -> bytes | None:
        """The content of a regular file in the working folder; None when there is none. Links are not followed."""
        return self._read(self.work, rel)

    def read_outside(self, rel: str) -> bytes | None:
        return self._read(self.outside, rel)

    def _names(self, base: Path) -> list[str]:
        found = []
        try:
            for folder, dirs, files in os.walk(base, onerror=self._unreadable):
                for name in dirs + files:
                    found.append(os.path.relpath(os.path.join(folder, name), base).replace(os.sep, "/"))
        except RecursionError:
            raise AssertionError("the program left folders too deep to list") from None
        return sorted(found)

    @staticmethod
    def _unreadable(error: OSError) -> None:
        raise AssertionError(f"the program left something that cannot be listed: {error.strerror}")

    def files(self) -> list[str]:
        """Everything in the working folder, as paths relative to it."""
        return self._names(self.work)

    def files_outside(self) -> list[str]:
        return self._names(self.outside)

    # --- running the program ---

    def run(self, args: list[str], stdin: bytes | str = b"") -> Result:
        """Run the program once in the working folder with these arguments and this standard input."""
        if isinstance(stdin, str):
            stdin = stdin.encode("utf-8")
        argv = list(self._cfg["command"]) + [str(a) for a in args]
        limits = self._limits
        left = limits["timeout_sec"] - _spent.get(self._key, 0.0)
        if left <= 0:
            return self._note(Result(None, b"", b"", "time", None, 0.0))
        env = {**self._cfg.get("env", {}), "HOME": str(self.home), "TMPDIR": str(self.home / "tmp"),
               "PWD": str(self.work)}
        file_bytes = limits["disk_mb"] * 1024 * 1024
        # no file larger than the limit, and no dump of the program's memory when it crashes
        kernel_limits = {"RLIMIT_CORE": 0, "RLIMIT_FSIZE": file_bytes}
        if self._side is not None:
            # the kernel refuses a process or thread beyond the limit; the count is per user, and
            # this user is this judging's alone
            before_exec = self._side.entering({**kernel_limits, "RLIMIT_NPROC": limits["processes"]})
        else:
            def before_exec() -> None:
                os.setsid()
                if resource is not None:
                    for name, value in kernel_limits.items():
                        resource.setrlimit(getattr(resource, name), (value, value))

        if not self.work.is_dir():
            raise fault("the case's working folder is gone")
        # What the run starts from is read before the program exists, so that nothing the program
        # does in its first moments is in it.
        separated = self._side is not None
        killed_before = _judge.killed_for_memory() if separated else 0
        high_before = _judge.highest_memory() if separated else 0
        held_before = _judge.container_memory() if separated else {"taken": 0, "all": 0}
        memory_before = held_before["taken"] - _files_held(self._cfg["work_root"]) if separated else 0
        started = time.monotonic()
        try:
            proc = subprocess.Popen(argv, cwd=self.work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, preexec_fn=before_exec, close_fds=True)
        except subprocess.SubprocessError as exc:
            raise fault(f"cannot start the program as its own user: {exc}") from None
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.ENOMEM):
                raise fault(f"cannot start a process for the program: {exc}") from None
            # the program is not there or cannot be run as built: the program's failure
            return self._note(Result(None, b"", str(exc).encode(), None, None, 0.0))
        try:
            result = self._watch(proc, stdin, started, left, memory_before)
            if result.limit is None and self._left_processes(proc):
                result.limit = "left processes"
        finally:
            self._end(proc)
        if separated and _ended_by_the_kernel(killed_before, _judge.killed_for_memory()):
            result.limit, result.exit_code = "memory", None
        high_now = _judge.highest_memory() if separated else 0
        above = _above_in_a_new_high(high_before, high_now, held_before["taken"],
                                     (limits["memory_mb"] + limits["disk_mb"]) * 2**20)
        self.measured.update(high_before=high_before, high_now=high_now, taken_before=held_before["taken"],
                             held_before=held_before["all"], above_the_limit=above)
        if above:
            # written into the result as a number; the judgement of the case is what the measuring gave
            self._record_high(high_before, high_now, held_before, above)
        result.seconds = time.monotonic() - started
        _spent[self._key] = _spent.get(self._key, 0.0) + result.seconds
        return self._note(result)

    def _record_high(self, high_before: int, high_now: int, held_before: dict, above: int) -> None:
        """Hand the judge the numbers of a run in which the container's memory made a new high
        above what the case may take."""
        path = self._cfg.get("highs_file")
        if not path:
            return
        mb = 2**20
        record = {"case": self._key, "limit_mb": self._limits["memory_mb"], "files_mb": self._limits["disk_mb"],
                  "high_before_mb": high_before // mb, "high_mb": high_now // mb,
                  "taken_before_mb": held_before["taken"] // mb, "held_before_mb": held_before["all"] // mb,
                  "above_the_limit_mb": above // mb}
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass

    def _note(self, result: Result) -> Result:
        if result.limit:
            self._broken.append(result.limit)
        return result

    def _watch(self, proc: subprocess.Popen, stdin: bytes, started: float, timeout: float,
               memory_before: int) -> Result:
        limits = self._limits
        max_out = limits["output_kb"] * 1024
        max_memory = limits["memory_mb"] * 1024 * 1024
        work_root = self._cfg["work_root"]

        def memory_taken() -> int:
            """Everything the kernel counts for the container but the cases' files."""
            return _judge.container_memory()["taken"] - _files_held(work_root)

        def memory_runs_out() -> bool:
            """The container is about to run out of memory, whatever the case's limit says. Ending the
            program here keeps the kernel from having to choose what to end."""
            now = _judge.container_memory()
            return bool(now["cap"]) and now["taken"] > now["cap"] - _judge.MEMORY_RESERVE_MB * 1024 * 1024

        chunks = {proc.stdout: [], proc.stderr: []}
        seen = 0
        limit = None
        sel = selectors.DefaultSelector()
        for stream in (proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
            sel.register(stream, selectors.EVENT_READ)
        to_write = memoryview(stdin)
        if to_write:
            os.set_blocking(proc.stdin.fileno(), False)
            sel.register(proc.stdin, selectors.EVENT_WRITE)
        else:
            proc.stdin.close()
        deadline = started + timeout
        next_watch = last_watch = 0.0
        open_streams = 2

        def take(stream) -> bool:
            """Read what is there; False once the stream has ended."""
            nonlocal seen
            while True:
                try:
                    data = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    return True
                except OSError:
                    return False
                if not data:
                    return False
                seen += len(data)
                if seen <= max_out:
                    chunks[stream].append(data)
                else:
                    return True

        while limit is None:
            now = time.monotonic()
            ended = proc.poll() is not None
            if ended:
                break
            if now >= deadline:
                limit = "time"
                break
            for key, _ in sel.select(min(TICK, max(deadline - now, 0))):
                stream = key.fileobj
                if stream is proc.stdin:
                    try:
                        sent = os.write(stream.fileno(), to_write[:65536])
                        to_write = to_write[sent:]
                    except BlockingIOError:
                        pass
                    except OSError:
                        to_write = to_write[:0]  # the program closed its input
                    if not to_write:
                        sel.unregister(stream)
                        try:
                            stream.close()
                        except OSError:
                            pass
                elif not take(stream):
                    sel.unregister(stream)
                    open_streams -= 1
            if seen > max_out:
                limit = "output"
            elif now >= next_watch:
                next_watch = now + WATCH
                if last_watch:
                    self.measured["longest_gap"] = max(self.measured["longest_gap"], now - last_watch)
                last_watch = now
                self.measured["measurements"] += 1
                if self._side is not None and (memory_taken() - memory_before > max_memory or memory_runs_out()):
                    limit = "memory"
        if limit is not None:
            self._end(proc)
        # what the program wrote before it ended is still in the pipes
        for stream in (proc.stdout, proc.stderr):
            if not stream.closed:
                take(stream)
        if limit is None and seen > max_out:
            limit = "output"
        sel.close()
        code = proc.poll()
        sig = -code if code is not None and code < 0 else None
        if limit is None and sig == getattr(signal, "SIGXFSZ", None):
            limit = "files"
        if limit is None and sig == getattr(signal, "SIGXCPU", None):
            limit = "time"
        exit_code = code if limit is None and code is not None and code >= 0 else None
        return Result(exit_code, b"".join(chunks[proc.stdout]), b"".join(chunks[proc.stderr]), limit, sig, 0.0)

    def _left_processes(self, proc: subprocess.Popen) -> bool:
        if self._side is not None:
            return bool(self._side.alive())
        try:
            os.killpg(proc.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def _end(self, proc: subprocess.Popen) -> None:
        """Leave nothing of the program running."""
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if self._side is not None and not self._side.end():
            raise fault("processes of the program could not be ended after its run")
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            raise fault("the program did not end after it was killed") from None
        for stream in (proc.stdin,):
            try:
                stream.close()
            except OSError:
                pass
        if self._uid is not None:
            _collect()

    def broken(self) -> LimitBroken:
        limits = self._limits
        return LimitBroken(f"the program broke a limit of the case: {', '.join(sorted(set(self._broken)))} "
                           f"(limits: {limits['timeout_sec']} s, {limits['memory_mb']} MB of memory, "
                           f"{limits['output_kb']} KB of output, {limits['processes']} processes and threads, "
                           f"{limits['disk_mb']} MB of files)")

    def close(self) -> None:
        """Give back what the case took. What cannot be removed fails the case: the program made it."""
        if self._side is not None and not self._side.end():
            raise fault("processes of the program could not be ended when its case closed")
        try:
            _judge.remove_tree(self.root)
        except OSError as exc:
            self._broken.append(f"left what cannot be removed ({exc.strerror})")


@contextmanager
def case(files: dict | None = None, outside: dict | None = None):
    """One case: a fresh folder for the program, removed afterwards. A broken limit fails the case,
    also when the test itself would have passed or would have been left unjudged."""
    c = Case(files, outside)
    try:
        yield c
    except BaseException:
        c.close()
        if c._broken:
            raise c.broken() from None
        raise
    c.close()
    if c._broken:
        raise c.broken()
