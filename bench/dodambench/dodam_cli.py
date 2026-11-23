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

Every case has limits. In the verifier each has a line that holds whatever the program does:

- time, added up over the runs of the case: the judge ends the program when it is up;
- output, standard output plus standard error: the judge stops reading and ends the program;
- processes and threads: the kernel refuses one more than the limit, for the program's user alone;
- files: the case's folder lives in a memory-backed folder that the container caps at the limit, so
  no more can be written, whether in many files, deep trees or files deleted while open; one file
  cannot be made larger than the limit either, not even a sparse one;
- memory: the judge measures, as often as it gets to run and at most every two thousandths of a
  second, everything the kernel counts for the container, memory moved out to swap included,
  less three things, and ends the program above the limit. Left out are the pages of files that
  stand on the kernel's two lists for pages of files, so a program that only reads files never
  fails this way by how much it reads; a page of a file on neither list counts. Left out are the kernel's records of
  names and files, up to a quarter of the limit and at most 16 MB; they count beyond that, those
  of the different files a program reads too. Left out are the cases' files, which have a limit
  of their own, and this runner's own memory. A
  page on the two lists that something holds is left out although the kernel cannot give it
  back. The ways of holding one that are known are closed (see the end of this entry); it is
  not shown that there is no other, and for one the container's memory cap is the only bound.
  A case has one starting figure, read before its first run, and is counted from the lowest
  figure it reached since. Between two measurements the program can be above its limit. The
  highest value the kernel itself kept for the container is read before and after every run and
  written into the result; it fails no case. The program can never be above the container's
  memory cap, where the kernel ends it first, and that fails the case. Memory that would outlive the program's processes cannot be
  taken, memory cannot be locked, pages cannot be handed to a pipe or a socket without
  copying, and neither kind of asynchronous input and output nor the cryptography sockets can
  be used: the calls for these are refused or answer that it cannot be done. A case's user can
  write only to memory-backed places.

A case that broke a limit fails, whatever the test asserts and even when the test would have been
left unjudged; so does a case whose program was ended by a signal, a program that leaves processes
behind, and a case whose folder cannot be removed. After every run nothing of the program is left
running, and after every case its folder is removed. All removing in a case runner is done on the
one time a step gives all walking and removing of what the program's side left.

In the verifier the program runs as a user of its own that owns nothing, in no group, and that cannot
gain privileges; the judge has taken away its write access everywhere but the case's folder. Without
that separation (only for the project's own prepared solutions) the program runs as the judge's user
and only the time, output and file-size limits hold. A fault of the judge itself is written to the
fault file the judge names and is never reported as a failure of the program.
"""
from __future__ import annotations

import errno
import json
import math
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
    and files together, counted from the container's count when the run began;
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


def _records_left_out_at_most(memory_mb: int) -> int:
    """How much of the kernel's records of names and files is left out of a case's memory at most:
    a quarter of the case's memory limit, and never more than 16 MB.

    The kernel keeps a record of every name and file that was looked at and of every page of the
    cache of files, and gives those records back when memory is needed, as it does the cache. But
    it cannot give back a record while something holds on to what it describes: an open file, a
    mapping, a file in the case's folder. From the kernel's figures the two cannot be told apart.
    So a bounded amount is left out, which a program that looks at files in the ordinary way
    stays within, and the rest counts."""
    return min(memory_mb * 2**20 // 4, _judge.RECORDS_LEFT_OUT_MB * 2**20)


def _records_grown(lowest: int | None, now: int) -> tuple[int, int]:
    """The lowest figure the kernel's records reached since the case began, with the figure read
    now, and how far the figure now lies above it. Counted from the lowest, a fall below where the
    case began makes no room: when the kernel gives back older records, newer ones that are held
    count. Records that rise and fall again with what a program does, such as files it makes and
    removes or processes it starts and waits for, are not added up: what counts is how far they
    stand above the lowest now."""
    lowest = now if lowest is None else min(lowest, now)
    return lowest, now - lowest


def _records_counted(grown: int, left_out_at_most: int) -> int:
    """What of the growth of the kernel's records during a case counts as the case's memory."""
    return max(grown - left_out_at_most, 0)


def _own_memory(status: str = "/proc/self/status") -> int:
    """The ordinary memory of this process, the case runner, in memory and moved out to swap; 0
    where the kernel does not say. It is taken out of every figure of a case, so that what the
    runner itself holds from one run to the next, the input it feeds and the output it has read,
    is no memory of the program's. Pages of files are not in it: they are left out already."""
    own = 0
    try:
        with open(status, encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith(("RssAnon:", "VmSwap:")):
                    own += int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return 0
    return own


def _lowest(lowest: int | None, figure: int) -> int:
    """The lowest figure a case has reached, with the figure read now. A case's memory is counted
    from there: from one starting figure for all its runs, and when memory that was there at the
    start goes away during the case, from the lower figure, so that its going makes no room."""
    return figure if lowest is None else min(lowest, figure)


# How often the judge looks at the program: whether it ended, and how much memory the container holds.
TICK = 0.002
WATCH = 0.002
PR_SET_CHILD_SUBREAPER = 36


class JudgeFault(Exception):
    """The judge could not do its own work. Not a failure of the program."""


class LimitBroken(AssertionError):
    """The program broke a limit of the case, or was ended by a signal, so the case fails."""


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
    """Record a fault of the judge where the judge will find it, and return the error to raise. Before
    the runner has its settings, that is where it writes down the outcomes of its cases."""
    path = (_config or {}).get("fault_file") or os.environ.get(_judge.PROGRESS_ENV)
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"fault": text, "test": os.environ.get("PYTEST_CURRENT_TEST", "")}) + "\n")
        except OSError:
            pass
    return JudgeFault(text)


def _remove(path, keep_top: bool = False) -> None:
    """Remove what the program's side left, on the one time a step gives all walking and removing of
    it; the runner notes what it used, so that the judge takes it back."""
    _judge.note_tree_clock(removing=True)
    try:
        _judge.remove_tree(path, keep_top=keep_top)
    finally:
        _judge.note_tree_clock()


def _not_removed(exc: BaseException) -> str | None:
    """Why what the program's side left was not removed, when that is the program's doing: it cannot
    be removed whatever the judge does, or the step's time for removing is spent. None otherwise."""
    if isinstance(exc, _judge.TooMuchLeft):
        return str(exc)
    if isinstance(exc, OSError) and exc.errno in _judge.UNREMOVABLE:
        return exc.strerror or str(exc)
    return None


def _parts(rel) -> tuple[str, ...] | None:
    """The names on the way to a place in one of the case's folders; None for a path that does not
    name a place inside it."""
    parts = PurePosixPath(str(rel)).parts
    if not parts or parts[0] == "/" or any(part in ("", ".", "..") for part in parts):
        return None
    return parts


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
        self._signals: list[int] = []      # the signals that ended the program in a run of this case
        # how the judge measured this case: how often, and the longest time between two measurements
        self.measured = {"measurements": 0, "longest_gap": 0.0}
        # the kernel's records: the lowest figure since the case began, and the most they stood above it
        self._records_lowest: int | None = None
        self._records_grown = 0
        self._left_out_at_most = _records_left_out_at_most(self._limits["memory_mb"])
        # what the container held besides the cases' files and the runner's own memory: the lowest
        # figure since the case's first run began, from which the case's memory is counted
        self._lowest: int | None = None
        self._side = _judge.Side(self._uid, self._gid) if self._uid is not None else None
        try:
            # cases run one after another, so whatever is in the cases' folder now is not this case's
            _remove(cfg["work_root"], keep_top=True)
        except (OSError, _judge.TooMuchLeft) as exc:
            why = _not_removed(exc)
            if why is None or not os.path.isdir(cfg["work_root"]):
                raise fault(f"cannot make the case's folder: {exc}") from None
            # what an earlier case left cannot be taken back: this case is not judged on it
            _judge.leave_undecided(self._key, f"the folder the cases live in cannot be emptied: {why}")
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

    def _folder(self, base: Path, parts: tuple[str, ...], make: bool) -> int:
        """Open the folder that `parts` name below `base`, one name at a time and never through a
        link: where a folder of the way is a link or no folder, it raises. With `make`, a folder that
        is not there is made and given to the program's user."""
        here = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in parts:
                try:
                    below = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=here)
                except FileNotFoundError:
                    if not make:
                        raise
                    os.mkdir(name, dir_fd=here)
                    if self._uid is not None:
                        os.chown(name, self._uid, self._gid, dir_fd=here, follow_symlinks=False)
                    below = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=here)
                os.close(here)
                here = below
        except BaseException:
            os.close(here)
            raise
        return here

    def _place(self, base: Path, rel: str) -> tuple[int, str]:
        """The open folder a file of the case goes into, and its name there; never through a link the
        program left. What the program left with that name is removed when it is a link or a file."""
        parts = _parts(rel)
        if parts is None:
            raise AssertionError(f"{rel} names no place in the case's folder")
        try:
            folder = self._folder(base, parts[:-1], make=True)
        except OSError as exc:
            raise AssertionError(f"the program left a link or no folder on the way to the case's file {rel}: "
                                 f"{exc.strerror}") from None
        try:
            try:
                info = os.stat(parts[-1], dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None:
                if not (stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise AssertionError(f"the program left something that is no file where the case's file {rel} goes")
                os.unlink(parts[-1], dir_fd=folder)
        except BaseException:
            os.close(folder)
            raise
        return folder, parts[-1]

    def _write(self, base: Path, rel: str, data, size: int | None = None) -> Path:
        folder, name = self._place(base, rel)
        if isinstance(data, str):
            data = data.encode("utf-8")
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=folder)
        finally:
            os.close(folder)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            if size is not None:
                f.truncate(size)
            if self._uid is not None:
                os.fchown(f.fileno(), self._uid, self._gid)
        return base / rel

    def write(self, rel: str, data) -> Path:
        """Put a file into the working folder."""
        return self._write(self.work, rel, data)

    def write_outside(self, rel: str, data) -> Path:
        """Put a file next to the working folder, where the program has no business."""
        return self._write(self.outside, rel, data)

    def sparse(self, rel: str, size: int) -> Path:
        """Put a file of the given size into the working folder that takes no disk space."""
        return self._write(self.work, rel, b"", size)

    def mkdir(self, rel: str) -> Path:
        """Put a folder into the working folder."""
        parts = _parts(rel)
        if parts is None:
            raise AssertionError(f"{rel} names no place in the case's folder")
        try:
            os.close(self._folder(self.work, parts, make=True))
        except OSError as exc:
            raise AssertionError(f"the program left a link or no folder on the way to the case's folder {rel}: "
                                 f"{exc.strerror}") from None
        return self.work / rel

    def symlink(self, rel: str, target) -> Path:
        """Put a symbolic link into the working folder."""
        folder, name = self._place(self.work, rel)
        try:
            os.symlink(str(target), name, dir_fd=folder)
            if self._uid is not None:
                os.chown(name, self._uid, self._gid, dir_fd=folder, follow_symlinks=False)
        finally:
            os.close(folder)
        return self.work / rel

    def _read(self, base: Path, rel: str) -> bytes | None:
        parts = _parts(rel)
        if parts is None:
            return None
        try:
            folder = self._folder(base, parts[:-1], make=False)
        except OSError:
            return None
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
        except OSError:
            return None
        finally:
            os.close(folder)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None     # a folder, a named pipe or anything else that is no file
            with os.fdopen(fd, "rb", closefd=False) as f:
                return f.read()
        finally:
            os.close(fd)

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
        held_before = self._container() if separated else {"taken": 0, "all": 0}
        if separated:
            # one starting figure for the whole case, read before its first run, when the test's
            # own files are written; a later run of the case is measured against it as well
            self._lowest = _lowest(self._lowest, self._figure(held_before))
            self._records(held_before)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(argv, cwd=self.work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, preexec_fn=before_exec, close_fds=True)
        except subprocess.SubprocessError as exc:
            raise fault(f"cannot start the program confined as its own user: {exc}") from None
        except OSError as exc:
            if exc.filename != argv[0] or exc.errno in (errno.EAGAIN, errno.ENOMEM):
                # not the program's command that failed to run: the judge could not start it
                raise fault(f"cannot start a process for the program: {exc}") from None
            # the program is not there or cannot be run as built: the program's failure
            return self._note(Result(None, b"", str(exc).encode(), None, None, 0.0))
        try:
            result = self._watch(proc, stdin, started, left)
            # ended by a signal no limit of the case explains
            signalled = result.limit is None and result.signal is not None
            if result.limit is None and self._left_processes(proc):
                result.limit = "left processes"
        finally:
            self._end(proc)
        if separated and _ended_by_the_kernel(killed_before, _judge.killed_for_memory()):
            result.limit, result.exit_code = "memory", None
        if signalled and result.limit != "memory":
            # the program the case runner started was ended by a signal: the case fails, whatever
            # its test checks afterwards, and says which signal also when it left processes behind
            self._signals.append(result.signal)
        high_now = _judge.highest_memory() if separated else 0
        above = _above_in_a_new_high(high_before, high_now, held_before["taken"],
                                     (limits["memory_mb"] + limits["disk_mb"]) * 2**20)
        if separated:
            self.measured["lowest"] = self._lowest
        self.measured.update(records_grown=self._records_grown,
                             records_left_out=min(self._records_grown, self._left_out_at_most),
                             records_counted=_records_counted(self._records_grown, self._left_out_at_most))
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
                  # less than 1 MB above is written down with its size, not as nothing
                  "above_the_limit_mb": math.ceil(above * 100 / mb) / 100, "above_the_limit_bytes": above}
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass

    def _note(self, result: Result) -> Result:
        if result.limit:
            self._broken.append(result.limit)
        return result

    def _records(self, now: dict) -> int:
        """Take note of the kernel's records as they are now; what they stand above their lowest
        figure in the case, beyond the amount left out, counts as memory."""
        self._records_lowest, grown = _records_grown(self._records_lowest, now.get("records", 0))
        self._records_grown = max(self._records_grown, grown)
        return _records_counted(grown, self._left_out_at_most)

    def _container(self) -> dict:
        """One reading of the container, the cases' files read with it so that the parts belong
        together."""
        return _judge.container_memory(outside=lambda: _files_held(self._cfg["work_root"]))

    def _figure(self, now: dict) -> int:
        """The container's count, less the cases' files, which have a limit of
        their own, and less the case runner's own memory."""
        files = now["files"] if "files" in now else _files_held(self._cfg["work_root"])
        return now["taken"] - files - _own_memory()

    def _watch(self, proc: subprocess.Popen, stdin: bytes, started: float, timeout: float) -> Result:
        limits = self._limits
        max_out = limits["output_kb"] * 1024
        max_memory = limits["memory_mb"] * 1024 * 1024

        def above_a_line() -> bool:
            """One measurement. The case's memory is the container's count (what
            the kernel counts for it, less the pages of files on the kernel's two lists and the
            records it keeps), less the cases' files and the runner's own memory, counted
            from the lowest such figure since the case's first run began, and of the kernel's
            records what grew beyond the amount that is left out. Above the limit the program is
            ended. It is ended as well when the container is about to run out of memory, whatever
            the case's limit says: that keeps the kernel from having to choose what to end."""
            now = self._container()
            records = self._records(now)
            figure = self._figure(now)
            self._lowest = _lowest(self._lowest, figure)
            taken = figure - self._lowest + records
            self.measured["memory_highest"] = max(self.measured.get("memory_highest", 0), taken)
            # how far the parts of the reading the figure was made of moved apart
            self.measured["moved_most"] = max(self.measured.get("moved_most", 0), now.get("moved", 0))
            runs_out = bool(now["cap"]) and now["taken"] + records > now["cap"] - _judge.MEMORY_RESERVE_MB * 1024 * 1024
            return taken > max_memory or runs_out

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
                if self._side is not None and above_a_line():
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

    def _note_records(self) -> None:
        """Hand the judge what this case did to the kernel's records: how much they grew, how much
        of that was left out of the case's memory and how much was counted."""
        path = self._cfg.get("records_file")
        if not path or self._side is None:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"case": self._key, "grown": self._records_grown,
                                    "left_out": min(self._records_grown, self._left_out_at_most),
                                    "counted": _records_counted(self._records_grown, self._left_out_at_most)}) + "\n")
        except OSError:
            pass

    def failing(self) -> bool:
        """Whether the case fails whatever its test says."""
        return bool(self._broken or self._signals)

    def broken(self) -> LimitBroken:
        limits, said = self._limits, []
        if self._signals:
            said.append("the program was ended by signal " + ", ".join(str(n) for n in sorted(set(self._signals))))
        if self._broken:
            said.append(f"the program broke a limit of the case: {', '.join(sorted(set(self._broken)))} "
                        f"(limits: {limits['timeout_sec']} s, {limits['memory_mb']} MB of memory, "
                        f"{limits['output_kb']} KB of output, {limits['processes']} processes and threads, "
                        f"{limits['disk_mb']} MB of files)")
        return LimitBroken("; ".join(said))

    def close(self) -> None:
        """Give back what the case took. What cannot be removed fails the case: the program made it."""
        if self._side is not None and not self._side.end():
            raise fault("processes of the program could not be ended when its case closed")
        self._note_records()
        try:
            _remove(self.root)
        except (OSError, _judge.TooMuchLeft) as exc:
            why = _not_removed(exc)
            if why is None:
                raise fault(f"cannot remove the case's folder: {exc}") from None
            what = ("left what could not be removed in the step's time" if isinstance(exc, _judge.TooMuchLeft)
                    else "left what cannot be removed")
            self._broken.append(f"{what} ({why})")


@contextmanager
def case(files: dict | None = None, outside: dict | None = None):
    """One case: a fresh folder for the program, removed afterwards. A broken limit fails the case,
    also when the test itself would have passed or would have been left unjudged."""
    c = Case(files, outside)
    try:
        yield c
    except BaseException:
        c.close()
        if c.failing():
            raise c.broken() from None
        raise
    c.close()
    if c.failing():
        raise c.broken()
