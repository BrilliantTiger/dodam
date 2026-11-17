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

Every case has limits: time (added up over the runs of the case), memory (the resident memory of all its
processes), output (standard output plus standard error), processes (threads count, as the kernel counts
them), and files written. When the
program breaks one it is killed and the case fails, whatever the test asserts; so does a program that
leaves processes behind when it ends. After every run nothing of the program is left running, and after
every case its folder is removed.

In the verifier the program runs as a user of its own that owns nothing, in no group, and that cannot
gain privileges; the judge has taken away its write access everywhere but the case's folder. On a
developer machine, where the judge is not root, the program runs as the developer and only the time,
output and file-size limits hold. A fault of the judge itself is written to the fault file the judge
names and is never reported as a failure of the program.
"""
from __future__ import annotations

import ctypes
import errno
import json
import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import resource
except ImportError:  # not a POSIX machine
    resource = None

CONFIG_ENV = "DODAM_CLI_CONFIG"
# How often the judge looks at the program: whether it ended, and how much memory, how many processes
# and how much disk it uses. Measured at 10, 5, 2 and 1 thousandths of a second: 2 is the longest at
# which one process allocating as fast as it can got less than 5% past its memory limit.
TICK = 0.002
WATCH = 0.002
MAX_FILES = 20000
PR_SET_NO_NEW_PRIVS, PR_SET_CHILD_SUBREAPER = 38, 36
try:
    _libc = ctypes.CDLL(None, use_errno=True)
except OSError:
    _libc = None


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
        if _config.get("uid") is not None and _libc is not None:
            # children the program leaves become children of this process, which collects them
            _libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
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


def _alive(uid: int) -> list[int]:
    """Processes of the user that still run; ended ones waiting to be collected do not count."""
    return _running(uid)[0]


def _running(uid: int) -> tuple[list[int], int]:
    """The user's running processes, and how many threads they have together."""
    found, threads = [], 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            if os.stat(f"/proc/{name}").st_uid != uid:
                continue
            with open(f"/proc/{name}/stat", "rb") as f:
                fields = f.read().rsplit(b")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if fields[0] not in (b"Z", b"X"):
            found.append(int(name))
            try:
                threads += int(fields[17])
            except (IndexError, ValueError):
                threads += 1
    return found, threads


def _resident_bytes(pids: list[int]) -> int:
    total = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/statm", "rb") as f:
                total += int(f.read().split()[1])
        except (OSError, IndexError, ValueError):
            continue
    return total * os.sysconf("SC_PAGE_SIZE")


def _tree_bytes(top: Path) -> tuple[int, int]:
    """Disk space and number of entries under a folder; stops counting once there are too many."""
    total = count = 0
    stack = [str(top)]
    while stack and count <= MAX_FILES:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    count += 1
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += info.st_blocks * 512
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(entry.path)
        except OSError:
            continue
    return total, count


def _become(uid: int, gid: int) -> None:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def _collect() -> None:
    """Collect every ended child, so none stays in the process table."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _kill_user(uid: int, gid: int) -> None:
    """End every process of the program's user at once: as that user, signal everything it may signal."""
    deadline = time.monotonic() + 30
    while _alive(uid):
        try:
            pid = os.fork()
        except OSError:
            pid = -1
        if pid == 0:
            try:
                _become(uid, gid)
                os.kill(-1, signal.SIGKILL)
            except BaseException:
                pass
            finally:
                os._exit(0)
        if pid > 0:
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
        _collect()
        if not _alive(uid):
            break
        if time.monotonic() > deadline:
            raise fault("processes of the program could not be ended")
        time.sleep(0.01)
    _collect()


IPC_ID_COLUMN = {"shm": "shmid", "msg": "msqid", "sem": "semid"}


def _ipc_ids(kind: str, uid: int) -> list[int]:
    try:
        lines = Path(f"/proc/sysvipc/{kind}").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    if not lines:
        return []
    head = lines[0].split()
    found = []
    for line in lines[1:]:
        row = dict(zip(head, line.split()))
        try:
            if int(row["uid"]) == uid or int(row["cuid"]) == uid:
                found.append(int(row[IPC_ID_COLUMN[kind]]))
        except (KeyError, ValueError):
            continue
    return found


def _remove_shared_memory(uid: int, gid: int) -> None:
    """Remove what the program left in the kernel's shared memory, semaphores and message queues.

    Only their owner may remove them, so a child does it as the program's user."""
    if _libc is None or not any(_ipc_ids(kind, uid) for kind in IPC_ID_COLUMN):
        return
    try:
        pid = os.fork()
    except OSError as exc:
        raise fault(f"cannot start a process to clean up after the program: {exc}") from None
    if pid == 0:
        try:
            _become(uid, gid)
            for kind, call in (("shm", _libc.shmctl), ("msg", _libc.msgctl)):
                for ident in _ipc_ids(kind, uid):
                    call(ident, 0, None)  # 0 is IPC_RMID
            for ident in _ipc_ids("sem", uid):
                _libc.semctl(ident, 0, 0)
        except BaseException:
            pass
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    left = [kind for kind in IPC_ID_COLUMN if _ipc_ids(kind, uid)]
    if left:
        raise fault(f"cannot remove what the program left in the kernel: {', '.join(left)}")


class Case:
    def __init__(self, files: dict | None = None, outside: dict | None = None):
        cfg = config()
        self._cfg = cfg
        self._uid, self._gid = cfg.get("uid"), cfg.get("gid")
        self._limits = cfg["limits"]
        self._key = os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0]
        self._broken: list[str] = []
        try:
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
        for folder, dirs, files in os.walk(base):
            for name in dirs + files:
                found.append(os.path.relpath(os.path.join(folder, name), base).replace(os.sep, "/"))
        return sorted(found)

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
        uid, gid = self._uid, self._gid
        file_bytes = limits["disk_mb"] * 1024 * 1024

        def before_exec() -> None:
            os.setsid()
            if resource is not None:
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
                resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
                cpu = int(limits["timeout_sec"]) + 5
                resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
            if uid is not None:
                try:
                    with open("/proc/self/oom_score_adj", "w") as f:
                        f.write("1000")  # when the container runs out of memory, the program goes first
                except OSError:
                    pass
                # the kernel refuses a process or thread beyond one more than the limit; the judge sees
                # that one more and fails the case. The count is per user, and this user is this run's alone.
                resource.setrlimit(resource.RLIMIT_NPROC, (limits["processes"] + 1, limits["processes"] + 1))
                _become(uid, gid)
                if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
                    os._exit(126)

        if not self.work.is_dir():
            raise fault("the case's working folder is gone")
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
            result = self._watch(proc, stdin, started, left)
            if result.limit is None and self._left_processes(proc):
                result.limit = "left processes"
        finally:
            self._end(proc)
        result.seconds = time.monotonic() - started
        _spent[self._key] = _spent.get(self._key, 0.0) + result.seconds
        return self._note(result)

    def _note(self, result: Result) -> Result:
        if result.limit:
            self._broken.append(result.limit)
        return result

    def _watch(self, proc: subprocess.Popen, stdin: bytes, started: float, timeout: float) -> Result:
        limits = self._limits
        max_out = limits["output_kb"] * 1024
        max_memory = limits["memory_mb"] * 1024 * 1024
        max_disk = limits["disk_mb"] * 1024 * 1024
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
        next_watch = 0.0
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
                if self._uid is not None:
                    pids, threads = _running(self._uid)
                    if threads > limits["processes"]:
                        limit = "processes"
                    elif _resident_bytes(pids) > max_memory:
                        limit = "memory"
                if limit is None:
                    used, count = _tree_bytes(self.root)
                    if used > max_disk or count > MAX_FILES:
                        limit = "files"
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
        if self._uid is not None:
            return bool(_alive(self._uid))
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
        if self._uid is not None:
            _kill_user(self._uid, self._gid)
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

    def close(self) -> None:
        if self._uid is not None:
            _kill_user(self._uid, self._gid)
            _remove_shared_memory(self._uid, self._gid)
        try:
            shutil.rmtree(self.root)
        except OSError as exc:
            raise fault(f"cannot remove the case's folder: {exc}") from None


@contextmanager
def case(files: dict | None = None, outside: dict | None = None):
    """One case: a fresh folder for the program, removed afterwards. A broken limit fails the case."""
    c = Case(files, outside)
    try:
        yield c
    finally:
        c.close()
    if c._broken:
        limits = c._limits
        raise LimitBroken(f"the program broke a limit of the case: {', '.join(sorted(set(c._broken)))} "
                          f"(limits: {limits['timeout_sec']} s, {limits['memory_mb']} MB of memory, "
                          f"{limits['output_kb']} KB of output, {limits['processes']} processes and threads, "
                          f"{limits['disk_mb']} MB of files)")
