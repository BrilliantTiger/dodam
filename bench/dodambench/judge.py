"""Judge one step of a Dodam benchmark task.

Runs inside the verifier container (copied there as /tests/lib/dodam_judge.py) and also on a
developer machine to vet a task. It only uses the Python standard library and pytest.

Steps: build the program, start it with a fresh data folder, run the hidden tests of this step
(earlier steps' tests too, unless superseded), restart the program before every test file that
needs a fresh start, and write reward.json plus details.json.

A test that skips is unjudged: a security test skips when the feature it attacks does not work,
because it then can say nothing about security. Unjudged tests are neither passes nor
vulnerabilities, and the full reward needs every test to pass.

In the verifier the judge runs as root and builds and starts the program as a plain user
(--run-as, or DODAM_APP_USER). The build and the program are the agent's code. Before any of it runs
the judge closes the tests and the results to everyone but root, hands /app to that user and relinks
the dependency folders to the image's pinned copies. The build runs confined: it cannot gain rights,
it is the first to go when the container runs out of memory, and it cannot make anything in the
kernel that outlives it. Afterwards the judge ends every process of that user and replaces, never
follows, whatever sits where the results go. Nothing about the judging is on a command line, which
every user of the container can read.

A command-line task has no program to keep running. After the build the judge runs each hidden test
file, and the tests run the program once per case through dodam_cli.py, which sits next to this
file. In the verifier the program then runs as a user of its own that owns nothing: the judge takes
write access away from every place others could write and leaves the program only its case's
folder, which lives in a memory-backed folder the container caps. The judge refuses to start when
the container has no cap on memory or processes, when its memory cap cannot hold on the machine, or
when that folder is missing; the caps are checked for every kind of task. When the judge itself
cannot do its work it writes judge-fault.json instead of a result and ends with exit code 3, so a
fault of the judge never looks like a failure of the program.

The outcome of every case is written down when the case ends. When the process that runs the cases
is ended, by the kernel or by anything else, the case that was running fails with that reason, the
cases before it keep their outcome and the cases after it are run by a new process. Whatever the
program's side left as files is handled by routines that take one step per entry, hold two folders
open at a time, follow no link and refuse a tree of more than TREE_ENTRIES entries; all of them
together have TREE_SECONDS in a step.

Without a user to run the program as nothing separates the program from the judge. That is only for
the project's own prepared solutions: it must be asked for, it is refused for root, and its result
says "separated": 0.0 (and its details false).
"""
from __future__ import annotations

import argparse
import ast
import ctypes
import errno
import grp
import json
import os
import platform
import pwd
import re
import secrets
import select
import shlex
import shutil
import signal
import stat
import struct
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path, PurePosixPath


def read_env_file(path: Path) -> dict[str, str]:
    env = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value
    return env


def select_tests(manifest: dict, steps: list[str], step: str) -> list[dict]:
    upto = steps.index(step)
    chosen = []
    for t in manifest.get("test", []):
        if t.get("step") in steps and steps.index(t["step"]) <= upto and step not in t.get("superseded_in", []):
            t = dict(t)
            t.setdefault("fresh_app", t.get("kind") == "security")
            chosen.append(t)
    return chosen


def port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_port(port: int, timeout: float, proc: subprocess.Popen) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def restore_deps(app_dir: Path, deps_dir: Path, links: list[str]) -> None:
    """Point each dependency folder of the app at the pinned copy, dropping what the app brought."""
    for name in links:
        target = app_dir / name
        remove_whatever(target)
        target.symlink_to(deps_dir / name, target_is_directory=True)


def give_app_to(app_dir: Path, user: str) -> int:
    """Make the plain user own the app folder so its build can write there; links are not followed.
    Says how many entries the folder holds."""
    entry = pwd.getpwnam(user)
    os.lchown(app_dir, entry.pw_uid, entry.pw_gid)
    return walk_tree(app_dir, lambda folder, name, info: os.chown(name, entry.pw_uid, entry.pw_gid, dir_fd=folder,
                                                           follow_symlinks=False))


def user_pids(uid: int, proc_root: Path = Path("/proc")) -> list[int]:
    """Running processes with uid as their real, effective, saved or file-system user. One that has
    ended and only waits to be collected runs nothing and is not counted."""
    found = []
    for entry in proc_root.iterdir() if proc_root.is_dir() else []:
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        ended = False
        for line in status.splitlines():
            if line.startswith("State:"):
                ended = line.split()[1] in ("Z", "X")
            if line.startswith("Uid:"):
                if str(uid) in line.split()[1:] and not ended:
                    found.append(int(entry.name))
                break
    return found


def write_output(path: Path, text: str) -> None:
    """Write through a new file and rename it into place, replacing a planted link instead of following it."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


class JudgeFault(Exception):
    """The judge could not do its own work; no result is written."""


# ---------------------------------------------------------------------------------------------
# The program's side: everything the agent's work can make run, the build as well as the program.
# ---------------------------------------------------------------------------------------------

try:
    _libc = ctypes.CDLL(None, use_errno=True)
except OSError:
    _libc = None
try:
    import resource
except ImportError:  # not a POSIX machine
    resource = None

PR_SET_SECCOMP, SECCOMP_MODE_FILTER, PR_SET_NO_NEW_PRIVS = 22, 2, 38
# Calls that make things in the kernel that stay when every process that made them has ended, so that
# memory taken through them is not given back by ending the program: System V shared memory, message
# queues and semaphores, and POSIX message queues. Per machine: the kernel's name for it, then the calls.
OUTLIVING_CALLS = {
    "x86_64": (0xC000003E, (29, 30, 31, 64, 65, 66, 67, 68, 69, 70, 71, 220, 240, 241, 242, 243, 244, 245)),
    "aarch64": (0xC00000B7, tuple(range(180, 198))),
}
# Calls that hand pages to a pipe or a socket without copying them: sendfile, splice, tee, vmsplice.
# A page of a file that a pipe or a socket holds this way cannot be given back by the kernel, and no
# figure of the kernel says how many there are. For a judged program these calls answer "invalid
# argument", which is what the kernel itself answers for a kind of file whose pages it cannot move:
# the answer on which the libraries of the runtimes copy instead.
PAGE_MOVING_CALLS = {"x86_64": (40, 275, 276, 278), "aarch64": (71, 76, 77, 75)}
# Calls that register buffers and files with the kernel for asynchronous work, on every kind of
# machine, and the call that makes a socket, which is refused for the kernel's cryptography sockets:
# both hold pages for a program in the same way.
ASYNC_CALLS = (425, 426, 427)
SOCKET_CALL = {"x86_64": 41, "aarch64": 198}
CRYPTO_SOCKETS = 38
INVALID_ARGUMENT = 22              # the error number the page-moving calls are answered with
# The call that makes a ring of the kernel's older asynchronous input and output. The pages of such
# a ring are charged as pages of a file and stand on the kernel's two lists for pages of files,
# but cannot be given back while the ring is there. The call answers that the kernel has no such
# call, as a kernel built without it does.
OLDER_ASYNC_CALL = {"x86_64": 206, "aarch64": 0}
NO_SUCH_CALL = 38                  # the error number of a call the kernel does not have
# Where a pair of terminals is made. What such a pair buffers is memory the kernel charges to no
# container, so no figure of the container shows it.
TERMINAL_DEVICES = (Path("/dev/pts/ptmx"), Path("/dev/ptmx"))
CASES_DIR = Path("/dodam-cases")   # the memory-backed, size-capped folder the verifier's container gives the cases
MEMORY_FOLDER = Path("/dev/shm")   # the other memory-backed folder of a container; what a build leaves there is memory
JUDGE_ROOM_MB = 256
RECORDS_LEFT_OUT_MB = 16           # the most of the kernel's records that is left out of a case's memory
HIGHS_KEPT = 10                    # how many runs with a new high above the limit the result names
RUNNER_MARK = 500                  # the kernel's mark of the case runner; the program's side has 1000, the judge 0
MEMORY_RESERVE_MB = 128            # the judge ends a program that leaves the container less than this
# One figure of the container is made of several of the kernel's files, read one after another while
# the program runs. What can be given back is read before and after the container's total, and a
# reading whose two halves differ by more than TOGETHER_BYTES, or that took longer than
# TOGETHER_SECONDS, is read again, up to TOGETHER_TRIES times.
TOGETHER_BYTES = 2**20
TOGETHER_SECONDS = 0.001
TOGETHER_TRIES = 8
# A reading whose every try took longer than TOGETHER_SECONDS was not read in one piece, and one that
# still moved more than TOGETHER_BYTES can say only that the figure lies within what moved. When it
# cannot say on which side of a line the figure lies, it ends nothing, and its case cannot pass: a case
# that would pass, or would be left unjudged, is UNMEASURED. What its test decided otherwise stands.
UNMEASURED = "unmeasured"
# every outcome the judge gives a case: pytest's three, and the judge's own for a case not judged for measuring
OUTCOMES = ("passed", "failed", "skipped", UNMEASURED)
NOT_JUDGED_FOR_MEASURING = ("not judged: the judge could not take a reading of the container's memory that says "
                            "on which side of a line it lies")
MACHINE_INFO = Path("/proc/meminfo")   # what the machine has, its swap included
PROCESS_CEILING = 4096             # a cap on processes above this bounds nothing on a small machine
CASE_ENTRIES = 20000               # how many files and folders a case may hold
TREE_ENTRIES = 500_000             # how many entries a tree the program's side left may hold
TREE_SECONDS = 120                 # how long all walking and removing of what it left may take in one step
WRITABLE_PLACES = 10_000           # how many places any user may write a build may leave
SEARCH_SECONDS = 300               # how long the searches for such places and for what the image does not hold may take
LATER_NS = 86_400 * 10**9          # how far after the judge sets them the access times of what a case may read lie
KERNEL_ENDED = "the kernel ended a process because the container ran out of memory"
# The kind of the entry that says the step failed for what the program's side left and the judge
# could not take back. No task gives a test this kind, and no rate counts it.
LEFT_BEHIND = "left-behind"
TIMED_OUT = "timed out"
PROGRESS_ENV = "DODAM_JUDGE_PROGRESS"
# What is left of the step's time for walking and removing when a case runner starts, and a mark of
# that start; the runner draws on the time and notes what it used, so that the judge takes it back.
TREE_ENV = "DODAM_JUDGE_TREE"
# What a removal is refused with when something cannot be removed whatever the judge does: the
# program's side left it so. A removal that fails in any other way is a fault of the judge.
UNREMOVABLE = (errno.EPERM, errno.EACCES, errno.EBUSY, errno.ENOTEMPTY)


def for_the_programs_side(env: dict[str, str]) -> dict[str, str]:
    """The environment every start of the program's side gets, the build, a served program and a
    case's program alike: the one place where the judge's own settings are taken out, with its
    temporary folder and the folders it was in."""
    return {key: value for key, value in env.items()
            if not key.startswith(("DODAM_", "PYTEST_")) and key not in ("PYTHONPATH", "TMPDIR", "PWD", "OLDPWD")}


def refusing_filter(machine: str | None = None) -> bytes | None:
    """A kernel filter for the program's side; None on an unknown machine. Under it the calls in
    OUTLIVING_CALLS and ASYNC_CALLS are refused as not permitted, the calls in PAGE_MOVING_CALLS
    answer "invalid argument", the call in OLDER_ASYNC_CALL answers that there is no such call,
    and a cryptography socket cannot be made.

    Calls made the way another kind of machine makes them (32-bit calls on a 64-bit kernel) are
    refused altogether, since their numbers mean something else."""
    machine = machine or platform.machine()
    known = OUTLIVING_CALLS.get(machine)
    if known is None:
        return None
    arch, calls = known
    refuse, allow = (0x06, 0, 0, 0x00050000 | 1), (0x06, 0, 0, 0x7FFF0000)   # return "not permitted" / go on
    cannot_move = (0x06, 0, 0, 0x00050000 | INVALID_ARGUMENT)
    # each check: the comparison, the value, and how far behind the last check its answer lies
    no_such_call = (0x06, 0, 0, 0x00050000 | NO_SUCH_CALL)
    to_refusal, to_cannot_move, to_no_such_call, to_socket = 5, 0, 1, 2
    checks = [(0x35, 0x40000000, to_refusal)] if arch == 0xC000003E else []   # the x32 way of calling
    checks += [(0x15, nr, to_refusal) for nr in (*calls, *ASYNC_CALLS)]
    checks += [(0x15, nr, to_cannot_move) for nr in PAGE_MOVING_CALLS[machine]]
    checks += [(0x15, OLDER_ASYNC_CALL[machine], to_no_such_call)]
    checks += [(0x15, SOCKET_CALL[machine], to_socket)]
    program = [(0x20, 0, 0, 4), (0x15, 1, 0, arch), refuse, (0x20, 0, 0, 0)]
    for at, (code, value, behind) in enumerate(checks):
        program.append((code, len(checks) - at + behind, 0, value))           # found: jump to its answer
    program += [(0x05, 0, 0, 4),                # no check found the call: go on
                cannot_move,
                no_such_call,
                (0x20, 0, 0, 16),               # a socket: of which family
                (0x15, 1, 0, CRYPTO_SOCKETS),
                allow, refuse]
    return b"".join(struct.pack("HBBI", *line) for line in program)


class _Filter(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


class NotConfined(Exception):
    """Raised in a new process that could not be confined, before anything of the program's side
    runs in it. The process that started it learns of it as a failed start, never as an exit code a
    program could give itself."""


def confine(uid: int, gid: int, groups: list[int], limits: dict[str, int], filter_bytes: bytes | None) -> None:
    """Called in a new process just before it becomes the program's side. From here on it is a plain
    user that cannot gain rights, is the first to go when the container runs out of memory, cannot
    make anything in the kernel that outlives it, and is held by the kernel's limits it is given."""
    # no memory can be locked: locked pages of files are pages the kernel cannot give back
    for name, value in {"RLIMIT_MEMLOCK": 0, **limits}.items():
        resource.setrlimit(getattr(resource, name), (value, value))
    with open("/proc/self/oom_score_adj", "w") as f:
        f.write("1000")
    os.setgroups(groups)
    os.setgid(gid)
    os.setuid(uid)
    if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise NotConfined("the kernel's mark that the process can gain no rights could not be set")
    if filter_bytes is not None:
        held = ctypes.create_string_buffer(filter_bytes)
        prog = _Filter(len(filter_bytes) // 8, ctypes.addressof(held))
        if _libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
            raise NotConfined("the filter of calls could not be set")


class TooMuchLeft(Exception):
    """The program's side left more than the judge handles. The step fails with this reason."""


class TreeClock:
    """How long every walk and removal of what the program's side left may still take in this step,
    all together. judge() gives it TREE_SECONDS when a step begins and takes it away when the step
    ends; outside a judging nothing is timed."""

    def __init__(self) -> None:
        self.left: float | None = None

    def deadline(self) -> float | None:
        return None if self.left is None else time.monotonic() + self.left

    def spent(self, began: float) -> None:
        if self.left is not None:
            self.left = max(self.left - (time.monotonic() - began), 0.0)


TREE_CLOCK = TreeClock()


def in_time(deadline: float | None) -> None:
    """Give up when the step's time for walking and removing what the program's side left is spent."""
    if deadline is not None and time.monotonic() >= deadline:
        raise TooMuchLeft(f"walking and removing what the program's side left took all of the {TREE_SECONDS} "
                          "seconds a step gives it")


def _enter(here: int, name: str) -> int | None:
    """Open a folder that was listed in the open folder `here`, never through a link, and open it up
    for removing through what was opened; None when what has the name is no folder any more. A
    folder closed to a judge that is not root is opened up by its name first, never through a link
    either."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    for opened_up in (False, True):
        try:
            below = os.open(name, flags, dir_fd=here)
            break
        except FileNotFoundError:
            return None
        except PermissionError:
            if opened_up:
                raise
            try:
                os.chmod(name, 0o700, dir_fd=here, follow_symlinks=False)
            except (NotImplementedError, ValueError):  # the name is a link now: what it points at is not touched
                return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                return None
            raise
    try:
        os.fchmod(below, 0o700)
    except BaseException:
        os.close(below)
        raise
    return below


def remove_tree(top: str | Path, keep_top: bool = False, most: int | None = None) -> None:
    """Remove a folder and everything in it, whatever a program made there: any depth, any names,
    links (never followed), closed permissions, special files. It holds two folders open at a time.
    It reads each folder once, removing its files and remembering the folders in it, and once more
    when those are gone, so its cost is one step per entry; it remembers no more names than walking
    the tree does. A folder it listed is opened before its permissions are changed, through what was
    opened; when a link or a file has taken its place by then, that is removed and not followed. It
    gives up after `most` entries, and when the step's time for walking and removing is spent."""
    top = str(top)
    here = os.open(top, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    names: list[str] = []              # the folders on the way down from top to here
    waiting: list[list[str]] = [[]]    # for top and for each of them: the folders in it still to remove
    seen, most = 0, TREE_ENTRIES if most is None else most
    began, deadline = time.monotonic(), TREE_CLOCK.deadline()
    try:
        while True:
            if waiting[-1]:
                in_time(deadline)
                name = waiting[-1].pop()
                below = _enter(here, name)
                if below is None:
                    # no folder any more: whatever has its name is removed, a link without being followed
                    try:
                        os.unlink(name, dir_fd=here)
                    except FileNotFoundError:
                        pass
                    continue
                os.close(here)
                here = below
                names.append(name)
                waiting.append([])
                continue
            # read the folder: its files go at once, the folders in it one after another
            with os.scandir(here) as entries:
                for entry in entries:
                    seen += 1
                    if seen > most:
                        raise TooMuchLeft(f"a folder to remove holds more than {most} entries")
                    in_time(deadline)
                    if entry.is_dir(follow_symlinks=False):
                        waiting[-1].append(entry.name)
                    else:
                        os.unlink(entry.name, dir_fd=here)
            if waiting[-1]:
                continue
            # nothing is left in it
            if not names:
                break
            above = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=here)
            os.close(here)
            here = above
            waiting.pop()
            os.rmdir(names.pop(), dir_fd=here)
    finally:
        os.close(here)
        TREE_CLOCK.spent(began)
    if not keep_top:
        os.rmdir(top)


def remove_whatever(path: str | Path) -> None:
    """Remove what sits at a path, whatever it is: a link is removed and not followed, a folder goes with all in it."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        remove_tree(path)
    else:
        os.unlink(path)


def walk_tree(top: str | Path, visit, most: int | None = None) -> int:
    """Call visit(folder, name, info) for every entry below top: folder is the open folder the entry
    is in, info what the entry itself is (a link is not followed). It reaches every entry at any
    depth and with any name, holds two folders open at a time, remembers only the names of the
    folders it has still to enter, and gives up after `most` entries, and when the step's time for
    walking and removing is spent. Says how many entries there are."""
    here = os.open(str(top), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    to_enter: list[list[str]] = []
    seen, most = 0, TREE_ENTRIES if most is None else most
    began, deadline = time.monotonic(), TREE_CLOCK.deadline()
    try:
        while True:
            below = []
            with os.scandir(here) as entries:
                for entry in entries:
                    seen += 1
                    if seen > most:
                        raise TooMuchLeft(f"the program's folder holds more than {most} entries")
                    in_time(deadline)
                    info = entry.stat(follow_symlinks=False)
                    visit(here, entry.name, info)
                    if stat.S_ISDIR(info.st_mode):
                        below.append(entry.name)
            to_enter.append(below)
            while not to_enter[-1]:
                to_enter.pop()
                if not to_enter:
                    return seen
                above = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=here)
                os.close(here)
                here = above
            in_time(deadline)
            down = os.open(to_enter[-1].pop(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=here)
            os.close(here)
            here = down
    finally:
        os.close(here)
        TREE_CLOCK.spent(began)


GROUP = Path("/sys/fs/cgroup")


def _group_number(group: Path, name: str) -> int | None:
    try:
        text = (group / name).read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _given_back(group: Path) -> tuple[int, int]:
    """Of what the kernel counts for the container: the pages of files on disk that stand on its two
    lists for pages of files, and its records of names, files and pages of that cache."""
    stat_lines = dict(line.split()[:2] for line in (group / "memory.stat").read_text().splitlines())
    return (int(stat_lines.get("inactive_file", 0)) + int(stat_lines.get("active_file", 0)),
            int(stat_lines.get("slab_reclaimable", 0)))


def _swap_of(group: Path, machine_info: Path) -> int:
    """How much the container may move out to swap, in bytes: its own allowance, and no more than
    the machine has; all the machine has when the container has no allowance of its own; none on a
    kernel that does not count swap for a container, and none on a machine without swap."""
    machine_swap = 0
    try:
        for line in machine_info.read_text().splitlines():
            if line.startswith("SwapTotal:"):
                machine_swap = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        machine_swap = 0
    allowed = _group_number(group, "memory.swap.max")
    try:
        unbounded = (group / "memory.swap.max").read_text().strip() == "max"
    except OSError:
        unbounded = False
    return machine_swap if unbounded else min(allowed or 0, machine_swap)


_SWAP_ALLOWED: dict[tuple[str, str], int] = {}


def swap_allowed(group: Path = GROUP) -> int:
    """The swap the container may use, read once: it does not change while the container runs."""
    key = (str(group), str(MACHINE_INFO))
    if key not in _SWAP_ALLOWED:
        _SWAP_ALLOWED[key] = _swap_of(group, MACHINE_INFO)
    return _SWAP_ALLOWED[key]


def container_memory(group: Path = GROUP, outside=None) -> dict[str, int]:
    """What the kernel counts for this container, in bytes. "taken" is the container's count:
    everything in memory together with what was moved out to swap, less two things. Memory in
    shared memory and in memory-backed folders is counted, and so is memory the kernel swapped
    out. Taken out are the pages of files on disk that stand on the kernel's two lists for pages
    of files, and the records the kernel keeps of the names and files that were looked at and of
    the pages of that cache: the kernel gives these back when memory is needed, as long as
    nothing holds them. A page of a file that stands on neither list counts, whatever keeps it
    off them. A page on the lists that something holds is taken out all the same; the ways of
    holding one that are known are refused where the program's side is confined, and it is not
    shown that there is no other. "all" is everything in
    memory, those included, which is what the kernel's own highest value counts. "cap" is the
    memory cap and the swap the container may use: the room it has. `outside`, when given, says
    how much the cases' files hold ("files").

    The parts are files read one after another, while a program may read files, write them or
    remove them. So what can be given back, and the cases' files, are read before and after the
    total, and the smaller of the two is taken: a page or a file that came or went while the total
    was read neither lowers the figure nor raises it by more than what moved. A reading whose two
    halves differ by more than TOGETHER_BYTES, or that took longer than TOGETHER_SECONDS, is read
    again, up to TOGETHER_TRIES times; then a reading that took no longer than that comes before one
    that did, and of those the one whose halves moved least is taken. What came and went while a reading
    was cut by time no half shows, so such a reading is "late": it says nothing of either side."""
    best = None
    for _ in range(TOGETHER_TRIES):
        began = time.monotonic()
        file_before, records_before = _given_back(group)
        files_before = outside() if outside is not None else 0
        swapped_before = _group_number(group, "memory.swap.current") or 0
        current = int((group / "memory.current").read_text())
        swapped_after = _group_number(group, "memory.swap.current") or 0
        file_after, records_after = _given_back(group)
        files_after = outside() if outside is not None else 0
        took = time.monotonic() - began
        moved = (abs(file_after - file_before) + abs(records_after - records_before) + abs(files_after - files_before)
                 + abs(swapped_after - swapped_before))
        # one value of the records for the figure and for their own count, so that nothing is counted twice
        reading = (took > TOGETHER_SECONDS, moved, took, current, max(swapped_before, swapped_after),
                   min(file_before, file_after), min(records_before, records_after), min(files_before, files_after))
        if best is None or reading[:3] < best[:3]:
            best = reading
        if moved <= TOGETHER_BYTES and took <= TOGETHER_SECONDS:
            break
    late, moved, took, current, swapped, can_be_given_back, records, files = best
    memory_max = _group_number(group, "memory.max") or 0
    return {"taken": current - can_be_given_back + swapped - records,
            "all": current, "cap": memory_max + swap_allowed(group) if memory_max else 0, "records": records,
            "files": files, "moved": moved, "late": int(late)}


# the kernel's own part of a container, as memory.stat names it
KERNEL_PARTS = ("kernel_stack", "pagetables", "sec_pagetables", "percpu", "sock", "vmalloc", "slab_unreclaimable")


def kernel_part(group: Path = GROUP) -> int:
    """What the kernel holds for the container of its own: the stacks and page tables of its
    processes, memory for each processor, sockets, and the records it cannot give back. Part of
    what it kept of processes that ended goes a while after them; 0 where it says nothing."""
    try:
        stat = dict(line.split()[:2] for line in (group / "memory.stat").read_text().splitlines() if line.strip())
        return sum(int(stat.get(part, 0)) for part in KERNEL_PARTS)
    except (OSError, ValueError):
        return 0


def highest_memory(group: Path = GROUP) -> int:
    """The most memory this container ever held, as the kernel keeps it; 0 where it keeps none.
    It never goes down, so it shows only a new high, and it does not count what was swapped out."""
    return _group_number(group, "memory.peak") or 0


def memory_events(group: Path = GROUP) -> dict[str, int]:
    """The kernel's counts for this container's memory; empty where it keeps none."""
    try:
        return {name: int(count) for name, count in
                (line.split() for line in (group / "memory.events").read_text().splitlines())}
    except (OSError, ValueError):
        return {}


def killed_for_memory(group: Path = GROUP) -> int:
    """How many processes the kernel has ended in this container because its memory ran out."""
    return memory_events(group).get("oom_kill", 0)


def container_caps(disk_mb: int | None = None, group: Path = GROUP,
                   machine_info: Path = Path("/proc/meminfo")) -> dict:
    """The caps the kernel holds this container to. A judge fault when one is missing or cannot hold
    on this machine, since then nothing bounds what the program's side takes. It also says how much
    the container may move out to swap: the judge counts that as memory."""
    def number(name: str) -> int | None:
        return _group_number(group, name)

    def refuse(key: str, message: str) -> JudgeFault:
        return JudgeFault(json.dumps({"severity": "error", "file": "container", "key": key, "message": message}))

    memory, processes = number("memory.max"), number("pids.max")
    machine = 0
    for line in machine_info.read_text().splitlines():
        if line.startswith("MemTotal:"):
            machine = int(line.split()[1]) * 1024
    if memory is None:
        raise refuse("memory", "the container has no memory cap, so nothing bounds the memory the program takes")
    if memory > machine:
        raise refuse("memory", f"the container's memory cap of {memory // 2**20} MB cannot hold: the machine it "
                               f"runs on has {machine // 2**20} MB")
    if processes is None or processes > PROCESS_CEILING:
        raise refuse("processes", "the container has no cap on processes, or one above "
                                  f"{PROCESS_CEILING}, so a build that forks without end is not bounded")
    if refusing_filter() is None:
        raise refuse("machine", f"no call filter is known for a {platform.machine()} machine")
    # how much of the machine's swap this container may use: none when the machine has none
    swap = _swap_of(group, machine_info)
    caps = {"memory_mb": memory // 2**20, "processes": processes, "swap_mb": swap // 2**20}
    if disk_mb is not None:
        try:
            room = os.statvfs(CASES_DIR)
            mounted = os.path.ismount(CASES_DIR)
        except OSError:
            room, mounted = None, False
        size = room.f_blocks * room.f_frsize // 2**20 if room else 0
        if not mounted or size != disk_mb or room.f_files > CASE_ENTRIES:
            raise refuse("files", f"the container gives the cases no folder capped at {disk_mb} MB and "
                                  f"{CASE_ENTRIES} entries at {CASES_DIR}"
                                  + (f" (found {size} MB, {room.f_files} entries)" if mounted else ""))
        caps["files_mb"] = size
        # on such a file system one case could show the next what it read, whatever the judge does
        counting = where_every_read_counts()
        if counting:
            raise refuse("access times", "every read moves when it was last read on " + ", ".join(counting)
                         + ", so one case could show the next what it read")
    return caps


class Side:
    """The program's side as one plain user: starting something as it, and ending all of it at once."""

    def __init__(self, uid: int, gid: int, groups: list[int] | None = None, home: str = "", name: str = ""):
        self.uid, self.gid, self.groups, self.home, self.name = uid, gid, groups or [], home, name
        self.filter = refusing_filter()

    @classmethod
    def named(cls, user: str) -> "Side":
        entry = pwd.getpwnam(user)
        return cls(entry.pw_uid, entry.pw_gid, os.getgrouplist(user, entry.pw_gid), entry.pw_dir, user)

    def entering(self, limits: dict[str, int] | None = None):
        """What a new process does before it runs anything of the program's side."""
        uid, gid, groups, held, limits = self.uid, self.gid, self.groups, self.filter, dict(limits or {})

        def enter() -> None:
            os.setsid()
            confine(uid, gid, groups, limits, held)
        return enter

    def environment(self, env: dict[str, str]) -> dict[str, str]:
        """The environment `su` would give: the user's own home and name."""
        return {**env, "HOME": self.home or "/", "USER": self.name, "LOGNAME": self.name, "SHELL": "/bin/sh"}

    def alive(self) -> list[int]:
        return user_pids(self.uid)

    def end(self) -> bool:
        """End every process of this user: as that user, signal everything it may signal. Needs no
        new process when none can be made: then the judge signals them one by one, as root."""
        for _ in range(ENDING_ROUNDS):
            if not self.alive():
                return True
            try:
                pid = os.fork()
            except OSError:
                pid = -1
            if pid == 0:
                try:
                    os.setgroups([])
                    os.setgid(self.gid)
                    os.setuid(self.uid)
                    os.kill(-1, signal.SIGKILL)
                except BaseException:
                    pass
                finally:
                    os._exit(0)
            if pid > 0:
                os.waitpid(pid, 0)
            else:
                # No process can be made: the container is at its cap. First stop them all, since a
                # stopped process makes no new ones, then end them.
                stopped: set[int] = set()
                for _ in range(50):
                    fresh = set(self.alive()) - stopped
                    if not fresh:
                        break
                    for other in fresh:
                        try:
                            os.kill(other, signal.SIGSTOP)
                        except ProcessLookupError:
                            pass
                    stopped |= fresh
                for other in stopped:
                    try:
                        os.kill(other, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            time.sleep(ENDING_PAUSE)
        return not self.alive()


def run_build(command: str, app_dir: Path, side: Side | None, env: dict[str, str],
              timeout: float) -> tuple[bool, str, str]:
    """Run the build and say whether it worked, with the end of what it printed and, when it was
    ended from outside, by what. The build is the agent's code: with a side it runs confined as that
    user, what it prints is read and dropped but for its end, and when it ends nothing of it is
    left running. Whatever stops the judge from starting it is the judge's fault, never a failure
    of the build."""
    env = for_the_programs_side(env)
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=app_dir, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=side.environment(env) if side else env,
                                preexec_fn=side.entering() if side else None, start_new_session=side is None)
    except subprocess.SubprocessError as exc:
        raise JudgeFault(f"cannot start the build confined as the build's user: {exc}") from None
    except OSError as exc:
        raise JudgeFault(f"cannot start the build: {exc}") from None
    tail = b""
    deadline = time.monotonic() + timeout
    killed_before = killed_for_memory()
    os.set_blocking(proc.stdout.fileno(), False)
    timed_out = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        ready, _, _ = select.select([proc.stdout], [], [], min(left, 0.05))
        if not ready:
            if proc.poll() is not None:
                break
            continue
        try:
            data = os.read(proc.stdout.fileno(), 1 << 20)
        except BlockingIOError:
            continue
        if not data:
            if proc.poll() is not None:
                break
            time.sleep(0.02)   # a process the build left holds the pipe's other end no more, yet the build runs
            continue
        tail = (tail + data)[-4000:]
    if timed_out or proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if side and not side.end():
        raise JudgeFault("processes of the build could not be ended")
    try:
        proc.wait(timeout=BUILD_ENDING_SECONDS)
    except subprocess.TimeoutExpired:
        raise JudgeFault("the build did not end after it was killed") from None
    proc.stdout.close()
    if timed_out:
        return False, "build timed out", "the build was ended at its time limit"
    text = tail.decode("utf-8", errors="replace")[-2000:]
    if killed_for_memory() > killed_before:
        # whatever the build went on to do, a part of it was ended: what it made is not what it builds
        return False, text, KERNEL_ENDED.replace("a process", "a process of the build")
    if proc.returncode < 0:
        return False, text, f"the build was ended by signal {-proc.returncode}"
    return proc.returncode == 0, text, ""


CLI_LIMITS = {"timeout_sec": "CASE_TIMEOUT_SEC", "memory_mb": "CASE_MEMORY_MB", "output_kb": "CASE_OUTPUT_KB",
              "processes": "CASE_PROCESSES", "disk_mb": "CASE_DISK_MB"}
FIRST_CASE_USER_ID, LAST_CASE_USER_ID = 200_000, 2_000_000_000
NO_FILES = {"proc", "sysfs", "cgroup", "cgroup2", "devpts", "securityfs", "debugfs", "tracefs", "pstore", "bpf"}
# What a case runner may take besides the time of its cases, starting Python and the judge's own work, and what
# the judge's work around one case may take.
FILE_SECONDS, CASE_SECONDS = 30, 2
BUILD_SECONDS = 900
BUILD_ENDING_SECONDS = 30          # how long the judge waits for a build it has ended
ENDING_ROUNDS, ENDING_PAUSE = 500, 0.01   # ending a user's processes: at most so many rounds, each with this pause


def names_of_tests(source: bytes | str) -> list[str]:
    """The tests of a file of tests: the functions at the top of the file whose names begin with test.
    A file that is no Python holds none."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    return [node.name for node in tree.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test")]


def tests_in(source: bytes | str) -> int:
    """How many tests pytest collects from a file of tests of a command-line task, which the format
    checker accepts: one for each of its tests. The checker refuses whatever would make pytest collect
    another number."""
    return len(names_of_tests(source))


def reward_of(cases: list[dict], build_ok: bool, step: str, separated: bool) -> dict:
    """The reward of a judging at `step`, from its cases: what the judge writes, and what the tool
    that checks prepared solutions compares a judging's reward with."""
    judged = [c for c in cases if c["outcome"] in ("passed", "failed")]
    failed = [c for c in judged if c["outcome"] == "failed"]
    reward = {"build_ok": 1.0 if build_ok else 0.0}
    for kind in ("functional", "boundary", "security"):
        of_kind = [c for c in judged if c["kind"] == kind]
        passed = sum(c["outcome"] == "passed" for c in of_kind)
        # A check that could not be judged is not a pass: the rate is over every check of the kind
        # that was to be run, so an unjudged one lowers it just as a failed one does.
        asked = sum(c["kind"] == kind for c in cases)
        reward[f"{kind}_pass_rate"] = round(passed / asked, 4) if asked else 1.0
        reward[f"{kind}_judged"] = float(len(of_kind))
    reward.update({
        "unjudged": float(sum(c["outcome"] == "skipped" for c in cases)),
        # cases not judged for measuring: no pass, and no judgement of the program either
        "unmeasured": float(sum(c["outcome"] == UNMEASURED for c in cases)),
        "vulns_found": float(sum(c["kind"] == "security" for c in failed)),
        "regression_broken": float(sum(c["step"] != step for c in failed)),
        "reward": 1.0 if build_ok and cases and all(c["outcome"] == "passed" for c in cases) else 0.0,
        # 0 when nothing separated the program from the judge: such a result says nothing about a
        # program somebody else wrote
        "separated": 1.0 if separated else 0.0,
    })
    return reward


def build_seconds(lang_env: dict[str, str]) -> float:
    """The build's time limit: BUILD_SECONDS, unless the settings handed to the judge give another.
    The format checker refuses a task whose settings would."""
    return float(lang_env.get("BUILD_TIMEOUT_SEC", BUILD_SECONDS))


def runners_at_most(tests: int) -> int:
    """The most case runners one file of tests can need. A runner that ends without deciding a case
    is started again, and the second time in a row it costs the next case: so a case needs at most
    two runners, and a file whose cases cannot be listed needs two."""
    return max(2 * tests, 2)


def file_seconds(tests: int, case_seconds: int) -> int:
    """The time one file of tests is given, shared by every case runner it needs: each runner's start
    with the judge's work around it, and every case at its full time limit."""
    return runners_at_most(tests) * FILE_SECONDS + tests * (case_seconds + CASE_SECONDS)


def step_terms(tests_per_file: list[int], case_seconds: int) -> dict[str, float]:
    """Every wait and every time limit the judge has in one step of a command-line task, in seconds,
    term by term: what the format checker adds up."""
    ending, runners = ENDING_ROUNDS * ENDING_PAUSE, sum(runners_at_most(tests) for tests in tests_per_file)
    return {"ending what the working user left running": ending,
            "the build": BUILD_SECONDS,
            "ending the build's processes": ending,
            "waiting for the ended build": BUILD_ENDING_SECONDS,
            "the searches for places any user may write and for what the image does not hold": SEARCH_SECONDS,
            "walking and removing what the program's side left": TREE_SECONDS,
            "starting every case runner the files can need": runners * FILE_SECONDS,
            "every case at its full time limit": sum(tests_per_file) * (case_seconds + CASE_SECONDS),
            "taking back what every case runner left": runners * ending,
            "taking back at the end": ending}
ARGS_ENV = "DODAM_JUDGE_ARGS"
RESULT_FILES = ("reward.json", "reward.txt", "details.json", "judge-fault.json")


def free_user_id() -> int:
    """A user and group number nobody in this container has, so the user owns nothing and is in no group.

    It is drawn at random from a wide range: the kernel counts a user's processes over the whole
    machine, so two judges running side by side must not share the number."""
    users = {u.pw_uid for u in pwd.getpwall()} | {u.pw_gid for u in pwd.getpwall()}
    groups = {g.gr_gid for g in grp.getgrall()}
    while True:
        number = FIRST_CASE_USER_ID + secrets.randbelow(LAST_CASE_USER_ID - FIRST_CASE_USER_ID)
        if number not in users and number not in groups:
            return number


def _mounted(mounts: Path) -> list[tuple[str, str, set[str]]]:
    """Where each file system is mounted here, its kind and its options, the kernel's own aside."""
    found = []
    for line in mounts.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] not in NO_FILES:
            found.append((parts[1].replace("\\040", " "), parts[2], set(parts[3].split(","))))
    return found


def _search(mounts: Path, condition, most: int, what: str, deadline: float | None):
    """Every entry on every file system mounted here that `condition` of `find` picks, one at a
    time; `condition` is a list, or says one for each kind of file system. It gives up after `most`
    of them, and at `deadline` (SEARCH_SECONDS from now by default)."""
    deadline = time.monotonic() + SEARCH_SECONDS if deadline is None else deadline
    found = 0
    for top, kind in sorted({(where, kind) for where, kind, _ in _mounted(mounts)}):
        picked = condition(kind) if callable(condition) else condition
        search = subprocess.Popen(["find", top, "-xdev", *picked, "-print0"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        rest = b""
        try:
            while True:
                ready, _, _ = select.select([search.stdout], [], [], max(deadline - time.monotonic(), 0))
                if not ready or time.monotonic() >= deadline:
                    raise TooMuchLeft(f"the build left more files than can be searched for {what}")
                data = os.read(search.stdout.fileno(), 1 << 16)
                if len(rest) + len(data) > 1 << 22 and b"\0" not in data:
                    rest = rest[:8192]          # the start of a path too long to name is enough to close it
                    continue
                *paths, rest = (rest + data).split(b"\0")
                for path in paths:
                    found += 1
                    if found > most:
                        raise TooMuchLeft(f"the build left more than {most} {what}")
                    yield path
                if not data:
                    break
        finally:
            search.kill()
            search.wait()
            search.stdout.close()


def writable_by_others(mounts: Path = Path("/proc/self/mounts"), deadline: float | None = None):
    """Every folder, file, named pipe and socket any user may write, on every file system mounted
    here, one at a time: every kind of file a user without rights can make. A link's own permissions
    mean nothing, and the devices of a container are made by its maker, not by a user.
    The search is given SEARCH_SECONDS and WRITABLE_PLACES places; a build that left more fails."""
    yield from _search(mounts, ["(", "-type", "d", "-o", "-type", "f", "-o", "-type", "p",
                                "-o", "-type", "s", ")", "-perm", "-0002"],
                       WRITABLE_PLACES, "places any user may write", deadline)


IMAGE_LAYERS = "overlay"           # the kind of file system that lays the container's own changes over the image's


def not_of_the_image(since: float, mounts: Path = Path("/proc/self/mounts"), deadline: float | None = None):
    """Every entry the image's own layers do not hold, on every file system mounted here: on the
    one laid over the image, whatever changed since `since` (seconds of the clock of the machine);
    on every other one, all of it. The image's own entries are read from its layers, which keep
    their times whatever is read. It shares the time of the search for places any user may write
    and is given TREE_ENTRIES entries."""
    yield from _search(mounts, lambda kind: ["-newerct", f"@{int(since) - 1}"] if kind == IMAGE_LAYERS else [],
                       TREE_ENTRIES, "entries the image does not hold", deadline)


def container_started(proc: Path = Path("/proc")) -> float | None:
    """When the container's first process started, in seconds of the clock of the machine; None
    where that cannot be read."""
    try:
        fields = (proc / "1/stat").read_text().rsplit(")", 1)[1].split()
        booted = next(int(line.split()[1]) for line in (proc / "stat").read_text().splitlines()
                      if line.startswith("btime "))
        return booted + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def where_every_read_counts(mounts: Path = Path("/proc/self/mounts"), devices: Path = Path("/dev")) -> list[str]:
    """The file systems mounted here on which every read of an entry moves when it was last read:
    neither `relatime` nor `noatime` nor read-only. The container's device folder is always mounted
    so, and so are the files of it laid over other places; they are left out here."""
    try:
        own = os.stat(devices).st_dev
    except OSError:
        own = None
    found = []
    for where, _, options in _mounted(mounts):
        if options & {"relatime", "noatime", "ro"}:
            continue
        try:
            if os.stat(where).st_dev == own:
                continue
        except OSError:
            pass
        found.append(where)
    return found


def settle_access_times(since: float, mounts: Path = Path("/proc/self/mounts"), deadline: float | None = None) -> int:
    """Nothing a case may read that the image's own layers do not hold shows the next case whether
    it was read: what the build or the working phase left, and what the container was given. Reading
    a file or listing a folder moves when it was last read only while that time is not later than
    when it was last changed; so every such entry, on the file system laid over the image whatever
    changed since `since` and on every other file system all of it, gets a time of last reading a day
    after both the moment this runs and its time of change. A time of change set in the future is
    set to the moment this runs. Says how many entries it settled."""
    now = time.time_ns()

    def settle(folder: int | None, name, info: os.stat_result | None = None) -> None:
        try:
            info = info or os.lstat(name)
            os.utime(name, ns=(now + LATER_NS, min(info.st_mtime_ns, now)), dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno != 30:  # a read-only file system keeps its times
                raise JudgeFault(f"cannot settle when what the build left was last read: {exc}") from None

    settled = 0
    for path in not_of_the_image(since, mounts, deadline):
        if len(path) < 4000:
            settle(None, path)
        else:
            # a place so deep that no path names it is closed to others above, as places others may write are
            above = path[:4000].rsplit(b"/", 1)[0]
            try:
                os.chmod(above, stat.S_IMODE(os.lstat(above).st_mode) & ~0o007)
            except OSError as exc:
                raise JudgeFault(f"cannot close {above!r} to the program: {exc}") from None
        settled += 1
    return settled


def close_writable_places(deadline: float | None = None) -> int:
    """Take write access away from every place any user could write; says how many places it closed.
    A place so deep that no path can name it is closed off altogether: the folder above it that a
    path can still name is closed to other users, who then reach nothing below it."""
    closed = 0
    for path in writable_by_others(deadline=deadline):
        try:
            if len(path) < 4000:
                os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o002)
            else:
                above = path[:4000].rsplit(b"/", 1)[0]
                os.chmod(above, stat.S_IMODE(os.lstat(above).st_mode) & ~0o007)
            closed += 1
        except OSError as exc:
            if exc.errno != 30:  # a read-only file system cannot be written anyway
                raise JudgeFault(f"cannot close a place any user may write to the program: {exc}") from None
    return closed


def close_terminals(devices: tuple[Path, ...] = TERMINAL_DEVICES) -> None:
    """No user but root can make a pair of terminals. The device that makes them is closed to
    every user; root, which the judge is, opens it all the same. A link is not followed, and a
    container that has no such device needs nothing."""
    for device in devices:
        try:
            if not device.is_symlink() and device.exists():
                os.chmod(device, 0)
        except OSError as exc:
            raise JudgeFault(f"cannot close {device} to the program: {exc}") from None


def open_for_reading(app_dir: Path) -> None:
    """After the build: everyone may read and run the program, nobody but its owner may change it,
    and no file of it lends its owner's rights to whoever runs it. Every entry is reached, at any
    depth; one that cannot be changed stops the judging."""
    def opened(folder: int | None, name: str, info: os.stat_result) -> None:
        if stat.S_ISLNK(info.st_mode):
            return
        mode = stat.S_IMODE(info.st_mode)
        wanted = (mode | 0o004 | (0o001 if mode & 0o100 or stat.S_ISDIR(info.st_mode) else 0)) & ~0o6022
        if wanted != mode:
            os.chmod(name, wanted, dir_fd=folder)

    try:
        opened(None, str(app_dir), os.lstat(app_dir))
        walk_tree(app_dir, opened)
    except OSError as exc:
        raise JudgeFault(f"cannot open the built program for reading: {exc}") from None


def keep_private(folder: Path) -> None:
    """Only root may enter the folder, and no user may rename or replace it from the folder above."""
    try:
        os.chown(folder, 0, 0)
        os.chmod(folder, 0o700)
    except OSError as exc:
        info = os.stat(folder)
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
            raise JudgeFault(f"cannot close {folder} to the program: {exc}") from None
    for parent in folder.parents:
        info = os.stat(parent)
        if info.st_uid != 0 or info.st_mode & 0o022:
            try:
                os.chown(parent, 0, -1)
                os.chmod(parent, stat.S_IMODE(info.st_mode) & ~0o022)
            except OSError as exc:
                if exc.errno != 30:
                    raise JudgeFault(f"cannot protect {parent}: {exc}") from None


# what a case runner's note of a case holds, each of its own type
NOTE_FIELDS = {"case": str, "unsettled": bool, "undecided": int, "grown": int, "left_out": int, "counted": int,
               "bound": int, "fell": int}


def _note_of(line: bytes) -> dict | None:
    """One case runner's note of a case, or None where the line is not one whole."""
    try:
        note = json.loads(line.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(note, dict):
        return None
    for name, kind in NOTE_FIELDS.items():
        value = note.get(name)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            return None
    return note


def _note_belongs(note: dict, path: Path, case: dict) -> bool:
    """Whether a note is of this case: of the same file of tests and the same name, as the judge names
    a case after its test."""
    parts = note["case"].split("::")
    return len(parts) > 1 and PurePosixPath(parts[0]).name == path.name and parts[-1] == case["name"]


class CliRun:
    """How the hidden tests of a command-line task reach the program: settings for dodam_cli.py."""

    @staticmethod
    def settings(app_dir: Path, lang_env: dict[str, str]) -> tuple[dict[str, int], list[str]]:
        try:
            limits = {name: int(lang_env[key]) for name, key in CLI_LIMITS.items()}
            command = [part.replace("{app}", str(app_dir)) for part in shlex.split(lang_env["RUN_CMD"])]
        except (KeyError, ValueError) as exc:
            raise JudgeFault(f"the task's settings for a command-line program are incomplete: {exc}") from None
        if not command:
            raise JudgeFault("the task's settings name no command to run the program")
        return limits, command

    def __init__(self, app_dir: Path, lang_env: dict[str, str], run_as: str, extra_env: dict[str, str],
                 since: float | None = None):
        self.run_as = run_as
        self.uid: int | None = None
        self.left = ""   # why no more files of tests are run: what the cases left cannot be taken back
        limits, command = self.settings(app_dir, lang_env)
        self.limits = limits
        self.private = Path(tempfile.mkdtemp(prefix="dodam-judge-"))
        if run_as:
            open_for_reading(app_dir)
            self.uid = free_user_id()
            # the search for places any user may write and the one for what the build changed share their time
            deadline = time.monotonic() + SEARCH_SECONDS
            self.closed = close_writable_places(deadline)
            # what the build left in the container's memory-backed folder would stay charged to the
            # container through every case: it is removed before the first case begins
            if MEMORY_FOLDER.is_dir() and not MEMORY_FOLDER.is_symlink():
                try:
                    remove_tree(MEMORY_FOLDER, keep_top=True)
                except TooMuchLeft as exc:
                    self.left = f"the container's memory-backed folder cannot be emptied after the build: {exc}"
                except OSError as exc:
                    if exc.errno not in UNREMOVABLE:
                        raise JudgeFault(f"cannot empty {MEMORY_FOLDER} after the build: {exc}") from None
                    # what cannot be removed whatever the judge does is what the build left
                    self.left = f"the container's memory-backed folder cannot be emptied after the build: {exc}"
            # the cases live in the folder the container caps; it is closed to others above
            self.work_root = CASES_DIR
            try:
                remove_tree(self.work_root, keep_top=True)
            except OSError as exc:
                # nothing of the program's side could write there yet
                raise JudgeFault(f"cannot empty the folder the cases live in before the first case: {exc}") from None
            # no case shows the next what it read of what the image does not hold: what the container
            # was given since it started, and what the working phase and the build left
            began = [moment for moment in (container_started(), since, time.time()) if moment is not None]
            self.settled = settle_access_times(min(began), deadline=deadline)
        else:
            self.work_root = Path(tempfile.mkdtemp(prefix="dodam-cases-"))
        self.fault_file = self.private / "faults.jsonl"
        self.highs_file = self.private / "highs.jsonl"
        self.records_file = self.private / "records.jsonl"
        self.records_highest = 0       # the most the kernel's records grew by in one case of this judging
        self.undecided = 0             # readings that could not say on which side of a line the figure lay
        # the case runner gives every case its own home, temporary folder and working folder
        env = {k: v for k, v in for_the_programs_side({**os.environ, **extra_env}).items() if k != "HOME"}
        settings = {"command": command, "uid": self.uid, "gid": self.uid, "limits": limits,
                    "work_root": str(self.work_root), "fault_file": str(self.fault_file), "env": env,
                    "highs_file": str(self.highs_file), "records_file": str(self.records_file)}
        if run_as:
            # the judge's own processes besides the case runner, the container's first and this one,
            # whose memory the runner takes out of every figure
            settings["judge_statuses"] = ["/proc/1/status", f"/proc/{os.getpid()}/status"]
        self.settings_file = self.private / "settings.json"
        self.settings_file.write_text(json.dumps(settings), encoding="utf-8")
        # the tests get nothing that differs from one language to the next but the settings file itself
        self.test_env = {"PATH": extra_env.get("PATH", os.environ.get("PATH", "")),
                         "PYTHONPATH": str(Path(__file__).resolve().parent),
                         "PYTHONDONTWRITEBYTECODE": "1", "DODAM_CLI_CONFIG": str(self.settings_file)}

    def run_file(self, path: Path, ended: list[dict], not_run: str = "failed") -> list[dict]:
        tests = tests_in(path.read_bytes())
        timeout = file_seconds(tests, self.limits["timeout_sec"])
        cases = run_cases(path, self.test_env, timeout, lambda: self.give_back(not_run), ended)
        self.note_records(path, cases)
        if self.fault_file.exists():
            raise JudgeFault(self.fault_file.read_text(encoding="utf-8").strip()[:2000])
        self.sweep()
        whole = [c for c in cases if c["name"] == path.name or c["message"] == TIMED_OUT]
        if whole:
            # a program cannot make a file of tests run out of time or be impossible to list: its cases are limited
            raise JudgeFault(f"{path.name}: {whole[0]['message']}")
        if len(cases) != tests:
            # the file's time was counted from its tests: other cases would be judged with other time
            raise JudgeFault(f"{path.name}: pytest listed {len(cases)} cases where the judge counted {tests} tests")
        return cases

    def note_records(self, path: Path, cases: list[dict]) -> None:
        """Write into each case of the file of tests at `path` how much of the kernel's records was
        left out of its memory and how much was counted, and how many of its readings could not
        decide, as the case runner noted it, so that a case that failed for memory shows why. A note
        belongs to the case of its own file and name. A case with a reading that could not say on
        which side of a line the figure lay is UNMEASURED when its test passed or left it unjudged:
        then the outcome depended on the reading. A case whose test failed stays failed. A note that
        cannot be read whole could be the one that marks a case, and which case it belonged to cannot
        be told: then every case of the file whose test passed or left it unjudged is UNMEASURED. The
        notes are removed on every path, so that the next file of tests starts with none."""
        unreadable = False
        try:
            lines = self.records_file.read_bytes().splitlines()
        except FileNotFoundError:
            lines = []
        except OSError:
            lines, unreadable = [], True
        finally:
            try:
                self.records_file.unlink(missing_ok=True)
            except OSError as exc:
                raise JudgeFault(f"cannot remove the case runners' notes: {exc}") from None
        noted = []
        for line in lines:
            if not line.strip():
                continue
            note = _note_of(line)
            if note is None:
                unreadable = True
            else:
                noted.append(note)
        for case in cases:
            mine = [n for n in noted if _note_belongs(n, path, case)]
            if mine:
                case["records_left_out_bytes"] = max(n["left_out"] for n in mine)
                case["records_counted_bytes"] = max(n["counted"] for n in mine)
                case["undecided_readings"] = sum(n["undecided"] for n in mine)
                # the most the case can have held, and how far its figure fell below where it began
                case["memory_bound_bytes"] = max(n["bound"] for n in mine)
                case["fell_below_start_bytes"] = max(n["fell"] for n in mine)
                self.records_highest = max(self.records_highest, max(n["grown"] for n in mine))
                self.undecided += case["undecided_readings"]
            if (unreadable or any(n["unsettled"] for n in mine)) and case["outcome"] in ("passed", "skipped"):
                case["outcome"], case["message"] = UNMEASURED, NOT_JUDGED_FOR_MEASURING

    def highs(self) -> dict:
        """The runs in which the container's memory made a new high above what the case may take,
        as the case runner noted them: how many, and the ten that lay furthest above. The number is
        the most the program can have held at once and counts the cache of files, so it is no
        judgement: a new high fails no case."""
        try:
            noted = [json.loads(line) for line in self.highs_file.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, ValueError):
            noted = []
        noted.sort(key=lambda run: -run.get("above_the_limit_bytes", run["above_the_limit_mb"] * 2**20))
        return {"fails_a_case": False, "runs": len(noted), "largest": noted[:HIGHS_KEPT]}

    def give_back(self, not_run: str) -> tuple[str, str] | None:
        """Between two case runners of a file: take back what the last one left. Where the cases'
        folder cannot be emptied, no runner starts on it: the cases that are left get `not_run`
        and the reason."""
        self.sweep()
        return (not_run, self.left) if self.left else None

    def sweep(self) -> None:
        """Between two files of tests nothing of the program is left: no process, no case folder.
        Where the cases' folder cannot be emptied, because what is in it cannot be removed or the
        step's time for walking and removing is spent, no more files of tests are run and `left`
        says why."""
        if self.uid is not None and not Side(self.uid, self.uid).end():
            raise JudgeFault("processes of the program could not be ended")
        if not self.work_root.is_dir():
            raise JudgeFault("the folder the cases live in is gone")
        try:
            remove_tree(self.work_root, keep_top=True)
        except TooMuchLeft as exc:
            # what the program's side left is its doing: the step fails with that reason
            self.left = self.left or f"the folder the cases live in cannot be emptied: {exc}"
        except OSError as exc:
            if exc.errno not in UNREMOVABLE:
                raise JudgeFault(f"cannot empty the folder the cases live in: {exc}") from None
            # what cannot be removed whatever the judge does is the program's doing, as above
            self.left = self.left or f"the folder the cases live in cannot be emptied: {exc}"

    def close(self) -> None:
        try:
            self.sweep()
        except JudgeFault:
            pass
        if self.uid is None:
            try:
                os.rmdir(self.work_root)
            except OSError:
                pass
        shutil.rmtree(self.private, ignore_errors=True)


def write_fault(out_dir: Path, text: str) -> None:
    """Say that the judge failed, and leave no result that could be read as a judgement of the program."""
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_results(out_dir)
    write_output(out_dir / "judge-fault.json", json.dumps({"judge_fault": text}, indent=1, ensure_ascii=False))


def clear_results(out_dir: Path) -> None:
    """Remove whatever sits where the judge's files go, a folder or a link as well. The judge does
    this before anything runs, so a judge that is ended half-way leaves nothing that reads as a result."""
    for name in RESULT_FILES:
        remove_whatever(out_dir / name)


class App:
    def __init__(self, app_dir: Path, start_cmd: str, port: int, run_as: str, extra_env: dict[str, str]):
        self.app_dir, self.start_cmd, self.port, self.run_as = app_dir, start_cmd, port, run_as
        self.extra_env = extra_env
        self.proc: subprocess.Popen | None = None
        self.data_dir: Path | None = None
        self.env: dict[str, str] = {}
        self.left = ""   # why the program is not started again: an earlier start left what cannot be removed

    def start(self, timeout: float = 60) -> bool:
        self.stop()
        if self.left:
            # the program is not started again on what an earlier start left
            return False
        # whatever still answers on the port is not this start's program
        deadline = time.monotonic() + 5
        while port_in_use(self.port):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.2)
        self.data_dir = Path(tempfile.mkdtemp(prefix="dodam-data-"))
        side = Side.named(self.run_as) if self.run_as else None
        if side:
            os.chown(self.data_dir, side.uid, side.gid)
        self.env = {**os.environ, **self.extra_env, "PORT": str(self.port),
                    "DB_PATH": str(self.data_dir / "app.db"), "APP_SECRET": secrets.token_hex(32)}
        program_env = for_the_programs_side(self.env)
        try:
            if self.run_as:
                # the served program is the agent's code: it runs confined, as the build does
                self.proc = subprocess.Popen(["/bin/sh", "-c", self.start_cmd], cwd=self.app_dir,
                                             env=side.environment(program_env), preexec_fn=side.entering(),
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
            else:
                self.proc = subprocess.Popen(self.start_cmd, shell=True, cwd=self.app_dir, env=program_env,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             start_new_session=True)
        except subprocess.SubprocessError as exc:
            # whatever stops the judge from starting the program is its own fault, never a program that did not start
            raise JudgeFault(f"cannot start the served program confined as its user: {exc}") from None
        except OSError as exc:
            raise JudgeFault(f"cannot start the served program: {exc}") from None
        return wait_for_port(self.port, timeout, self.proc)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.proc and self.run_as and not Side.named(self.run_as).end():
            # the program may have left its process group; nothing of it may run on
            raise JudgeFault("processes of the served program could not be ended")
        self.proc = None
        if self.data_dir is not None:
            # what one start of the program wrote is given back before the next
            try:
                remove_tree(self.data_dir)
            except TooMuchLeft as exc:
                # what the program wrote is the program's doing: the step fails with that reason
                self.left = f"the served program's data folder cannot be removed: {exc}"
            except FileNotFoundError:
                pass
            except OSError as exc:
                if exc.errno not in UNREMOVABLE:
                    raise JudgeFault(f"cannot remove the served program's data folder: {exc}") from None
                # what cannot be removed whatever the judge does is the program's doing, as above
                self.left = f"the served program's data folder cannot be removed: {exc}"
            self.data_dir = None


def read_progress(path: str) -> list[dict]:
    """What the case runner wrote down so far. A line it was ended in the middle of is left out."""
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return records


def _note_progress(record: dict) -> None:
    with open(os.environ[PROGRESS_ENV], "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# What follows up to run_cases runs inside the case runner: pytest loads this file as a plug-in. It
# writes every case's outcome down the moment the case ends, so that the outcomes do not depend on
# the runner living to the end of its file.
_runner: dict = {"reports": {}, "kills": {}, "session": None, "undecided": set(), "tree": None}


def pytest_sessionstart(session) -> None:
    _runner["session"] = session
    given = os.environ.get(TREE_ENV, "")
    if given:
        # what is left of the step's time for walking and removing: the cases' removals draw on it
        left, _runner["tree"] = given.split()
        TREE_CLOCK.left = float(left)


def note_tree_clock(removing: bool = False) -> None:
    """In a case runner: note what is left of the step's time for walking and removing, and with
    `removing` when a removal begins, so that the judge takes back what this runner used of it, also
    when the runner is ended in the middle of a removal."""
    if _runner["tree"] is None or PROGRESS_ENV not in os.environ:
        return
    record = {"tree": TREE_CLOCK.left, "runner": _runner["tree"]}
    if removing:
        record["at"] = time.monotonic()
    _note_progress(record)


def leave_undecided(case: str, why: str) -> None:
    """In a case runner: the cases' folder cannot be emptied for this case, so it is not judged
    here. The runner stops after it without an outcome for it, and the judge, which then cannot
    empty that folder either, gives it and the cases after it the reason."""
    import pytest
    _runner["undecided"].add(case)
    if PROGRESS_ENV in os.environ:
        _note_progress({"undecided": case, "why": why})
    if _runner["session"] is not None:
        _runner["session"].shouldstop = why
    pytest.skip(why)


def pytest_collectreport(report) -> None:
    if PROGRESS_ENV in os.environ and report.failed:
        _note_progress({"unlisted": str(report.longrepr)[-300:]})


def pytest_collection_modifyitems(session, config, items) -> None:
    if PROGRESS_ENV not in os.environ:
        return
    records = read_progress(os.environ[PROGRESS_ENV])
    if not any("listed" in r for r in records):
        _note_progress({"listed": [item.nodeid for item in items]})
    decided = {r["case"] for r in records if "outcome" in r}
    items[:] = [item for item in items if item.nodeid not in decided]


def pytest_runtest_logstart(nodeid, location) -> None:
    if PROGRESS_ENV in os.environ:
        _runner["kills"][nodeid] = killed_for_memory()
        _note_progress({"started": nodeid})


def pytest_runtest_logreport(report) -> None:
    _runner["reports"].setdefault(report.nodeid, []).append(report)


def pytest_runtest_logfinish(nodeid, location) -> None:
    if PROGRESS_ENV not in os.environ or nodeid in _runner["undecided"]:
        return
    outcome, message = "passed", ""
    for report in _runner["reports"].pop(nodeid, []):
        if report.failed:
            crash = getattr(report.longrepr, "reprcrash", None)
            said = crash.message if crash is not None else str(report.longrepr)
            outcome, message = "failed", message or said[:300]
        elif report.skipped and outcome == "passed":
            outcome = "skipped"
    stop = killed_for_memory() > _runner["kills"].pop(nodeid, 0)
    if stop:
        # Whatever the kernel ended while this case ran, the case fails. The runner stops here; the
        # judge gives back what the case took and runs the cases that are left with a new runner.
        outcome, message = "failed", KERNEL_ENDED
    _note_progress({"case": nodeid, "outcome": outcome, "message": message})
    if stop and _runner["session"] is not None:
        _runner["session"].shouldstop = KERNEL_ENDED


def goes_before_the_judge() -> None:
    """Called in the new process that becomes the case runner. When the container's memory runs
    out and nothing of the program's side is left to end, the kernel ends the case runner and not
    the judge: the case that was running fails, and the judge goes on with a new runner. Raising
    this mark needs no rights."""
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write(str(RUNNER_MARK))
    except OSError:
        pass


def tree_left(records: list[dict], runner: str, given: float, now: float) -> float:
    """What is left of the step's time for walking and removing after a case runner drew on it, as
    the runner noted: what was left after its last removal, or, when it noted the start of one and
    no end, what was left then less the time since."""
    mine = [r for r in records if r.get("runner") == runner and "tree" in r]
    if not mine:
        return given
    last = mine[-1]
    return max(last["tree"] - (now - last["at"] if "at" in last else 0.0), 0.0)


def run_pytest(test_path: Path, env: dict[str, str], timeout: float, said: str) -> int | None:
    """Run the case runner once; its exit code, or None when its time was up.

    The runner is given what is left of the step's time for walking and removing, and what it
    used is taken back. That time is not the file's: the runner's own time is longer by what it
    has used of it so far."""
    # The command line of a process can be read by every user of the container, so it says nothing:
    # which file of tests to run and where the outcomes go travel in the environment, which only
    # root can read.
    options = ["-q", "-p", "no:cacheprovider", "-p", Path(__file__).stem, str(test_path)]
    here = str(Path(__file__).resolve().parent)
    env = {**env, "PYTEST_ADDOPTS": " ".join(shlex.quote(o) for o in options),
           "PYTHONPATH": os.pathsep.join(part for part in (here, env.get("PYTHONPATH", "")) if part)}
    given, runner, progress = TREE_CLOCK.left, secrets.token_hex(8), env.get(PROGRESS_ENV, "")
    if given is not None:
        env[TREE_ENV] = f"{given!r} {runner}"

    def left_now() -> float:
        return tree_left(read_progress(progress), runner, given, time.monotonic()) if given is not None else 0.0

    with open(said, "wb") as errors:
        proc = subprocess.Popen([sys.executable, "-m", "pytest"], env=env, cwd=test_path.parent,
                                stdout=subprocess.DEVNULL, stderr=errors, preexec_fn=goes_before_the_judge)
        began = time.monotonic()
        try:
            while True:
                used = given - left_now() if given is not None else 0.0
                wait = began + timeout + used - time.monotonic()
                if wait <= 0:
                    proc.kill()
                    proc.wait()
                    return None
                try:
                    return proc.wait(timeout=min(wait, 0.5))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if given is not None:
                TREE_CLOCK.left = min(TREE_CLOCK.left, left_now())


def run_cases(test_path: Path, env: dict[str, str], timeout: float, give_back=None,
              ended: list[dict] | None = None) -> list[dict]:
    """Run one file of hidden tests and say how each case came out.

    The runner writes each outcome down when the case ends. When the runner itself is ended, the
    case that was running fails with that reason, the cases before it keep their outcome, and a new
    runner goes on with the cases after it. Before a new runner starts, give_back() takes back what
    the last one left; when it returns an outcome and a message, the cases that are left get those.
    The runners share `timeout`: one that is started again gets what is left of it. Every time a
    runner did not end by itself, `ended` gets a record of how."""
    private = Path(tempfile.mkdtemp(prefix="dodam-run-"))
    progress, said = str(private / "progress.jsonl"), str(private / "errors.txt")
    env = {**env, PROGRESS_ENV: progress}
    whole = lambda message: [{"name": test_path.name, "outcome": "failed", "message": message}]
    fruitless, spent = 0, 0.0
    try:
        while True:
            known = sum("outcome" in r for r in read_progress(progress))
            kills = killed_for_memory()
            began, tree_before = time.monotonic(), TREE_CLOCK.left
            code = run_pytest(test_path, env, timeout - spent, said) if spent < timeout else None
            # what the runner spent walking and removing is the step's time for that, not the file's
            used = tree_before - TREE_CLOCK.left if tree_before is not None and TREE_CLOCK.left is not None else 0.0
            spent += time.monotonic() - began - used
            how = ""
            if code is None:
                how = TIMED_OUT
            elif code < 0:
                how = (KERNEL_ENDED.replace("a process", "the case runner") if killed_for_memory() > kills
                       else f"the case runner was ended by signal {-code}")
            elif code not in (0, 1, 2, 5):
                how = f"the case runner ended with exit code {code}"
            records = read_progress(progress)
            faults = [r["fault"] for r in records if "fault" in r]
            if faults:
                # the runner met a fault of the judge's before it knew where faults go
                raise JudgeFault(f"{test_path.name}: {faults[0]}")
            listed = next((r["listed"] for r in records if "listed" in r), None)
            decided = {r["case"]: r for r in records if "outcome" in r}
            started = [r["started"] for r in records if "started" in r]
            running = started[-1] if started and started[-1] not in decided else None
            # a case its runner left undecided because the cases' folder could not be emptied for it
            since = max((at for at, r in enumerate(records) if r.get("started") == running), default=0)
            undecided = {r["undecided"] for r in records[since:] if "undecided" in r}
            if how and ended is not None:
                with open(said, "rb") as f:
                    f.seek(max(os.path.getsize(said) - 1500, 0))
                    tail = f.read().decode("utf-8", errors="replace")
                ended.append({"file": test_path.name, "how": how, "exit_code": code, "case": running,
                              "kernel_kills_before": kills, "kernel_kills_after": killed_for_memory(),
                              "runner_said": tail})
            unlisted = next((r["unlisted"] for r in records if "unlisted" in r), None)
            if unlisted is not None:
                return whole(f"the tests could not be listed: {unlisted}")
            if listed is None:
                fruitless += 1
                if code is None or not how or fruitless >= 2:
                    return whole(how or "no test report")
            elif not listed:
                return whole("no tests collected")
            else:
                def decide(case: str, outcome: str, message: str) -> None:
                    decided[case] = {"case": case, "outcome": outcome, "message": message}
                    _note_progress_to(progress, decided[case])

                if running is not None and running not in undecided:
                    decide(running, "failed", how or "the case runner ended while this case ran")
                left = [case for case in listed if case not in decided]
                if left and code is None:
                    for case in left:
                        decide(case, "failed", TIMED_OUT)
                    left = []
                fruitless = 0 if len(decided) > known else fruitless + 1
                if left and fruitless >= 2:
                    decide(left[0], "failed", "the case runner ended twice before this case could be judged"
                           + (f": {how}" if how else ""))
                    left, fruitless = left[1:], 0
                if not left:
                    return [{"name": case.split("::")[-1], "outcome": decided[case]["outcome"],
                             "message": decided[case]["message"]} for case in listed]
            verdict = give_back() if give_back is not None else None
            if verdict is not None and listed:
                outcome, message = verdict
                return [{"name": case.split("::")[-1], "outcome": decided.get(case, {}).get("outcome", outcome),
                         "message": decided.get(case, {}).get("message", message)} for case in listed]
    finally:
        shutil.rmtree(private, ignore_errors=True)


def _note_progress_to(path: str, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def judge(tests_dir: Path, app_dir: Path, step: str, out_dir: Path, run_as: str = "",
          lang_env: dict[str, str] | None = None, extra_env: dict[str, str] | None = None,
          unseparated: bool = False) -> dict:
    """Judge one step. With run_as the program's side is that plain user and the judge is root. Without
    it nothing separates the program from the judge: that is only for the project's own prepared
    solutions, must be asked for with unseparated=True, and is refused when the judge is root. All
    walking and removing of what the program's side left in the step shares TREE_SECONDS."""
    TREE_CLOCK.left = float(TREE_SECONDS)
    try:
        return judge_step(tests_dir, app_dir, step, out_dir, run_as, lang_env, extra_env, unseparated)
    finally:
        TREE_CLOCK.left = None


def judge_step(tests_dir: Path, app_dir: Path, step: str, out_dir: Path, run_as: str,
               lang_env: dict[str, str] | None, extra_env: dict[str, str] | None, unseparated: bool) -> dict:
    if not run_as:
        if not unseparated:
            raise JudgeFault("no user to run the program as: judging without separation must be asked for")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise JudgeFault("judging without separation is refused for root: the program would run as root")
    # pytest runs from each test's folder, so every folder handed on must be absolute
    tests_dir, app_dir, out_dir = tests_dir.resolve(), app_dir.resolve(), out_dir.resolve()
    lang_env = lang_env if lang_env is not None else read_env_file(tests_dir / "lang.env")
    manifest = tomllib.loads((tests_dir / "manifest.toml").read_text(encoding="utf-8"))
    steps = [s for s in lang_env.get("STEPS", "").split(",") if s] or [step]
    selected = select_tests(manifest, steps, step)
    port = int(lang_env.get("PORT", "8080"))
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"step": step, "build_ok": False, "separated": bool(run_as), "tests": []}
    is_cli = lang_env.get("INTERFACE") == "cli"
    side = None
    if run_as:
        # Before anything of the agent's runs: only root may enter the tests and the results.
        keep_private(tests_dir)
        keep_private(out_dir)
    clear_results(out_dir)
    refused = ""
    if run_as:
        # every kind of task: a container without caps that hold runs nothing of the program's side
        report["caps"] = container_caps(CliRun.settings(app_dir, lang_env)[0]["disk_mb"] if is_cli else None)
        side = Side.named(run_as)
        if not side.end():
            raise JudgeFault("processes the working user left running could not be ended")
        close_terminals()
        # the kernel's records of names and files, before anything of the program's side has run here
        records = {"at_start": container_memory()["records"]}
    deps_dir, links = lang_env.get("DEPS_DIR", ""), [n for n in lang_env.get("DEPS_LINKS", "").split(",") if n]
    try:
        if run_as:
            report["app_entries"] = give_app_to(app_dir, run_as)
        if deps_dir and links:
            restore_deps(app_dir, Path(deps_dir), links)
    except TooMuchLeft as exc:
        refused = str(exc)
    except OSError as exc:
        if exc.errno not in UNREMOVABLE:
            raise JudgeFault(f"cannot take over what the working phase left in the program's folder: {exc}") from None
        # what cannot be removed whatever the judge does is what the working phase left
        refused = f"what the working phase left in the program's folder cannot be removed: {exc}"

    build_cmd = lang_env.get("BUILD_CMD", "").strip()
    started, build_began = time.monotonic(), time.time()
    if refused:
        report["build_ended"] = refused
    elif build_cmd:
        report["build_ok"], report["build_log_tail"], report["build_ended"] = run_build(
            build_cmd, app_dir, side, {**os.environ, **(extra_env or {})},
            build_seconds(lang_env))
    else:
        report["build_ok"] = True
    report["build_sec"] = round(time.monotonic() - started, 2)
    if run_as:
        records["after_build"] = container_memory()["records"]

    cli = None
    if is_cli and report["build_ok"]:
        try:
            cli = CliRun(app_dir, lang_env, run_as, extra_env or {}, build_began)
        except TooMuchLeft as exc:
            report["build_ok"], report["build_ended"] = False, str(exc)
    report["runner_ended"] = []
    app = App(app_dir, lang_env.get("START_CMD", ""), port, run_as, extra_env or {})
    shared_up = False
    served: dict | None = None   # the last file of tests a program was started for or served
    for t in selected:
        path = tests_dir / t["file"]
        # a program that does not build or start fails its features but shows no weakness
        not_run = "skipped" if t.get("kind") == "security" else "failed"
        cases: list[dict]
        if not report["build_ok"]:
            cases = [{"name": path.name, "outcome": not_run, "message": "build failed"}]
        elif cli is not None and cli.left:
            # what the cases left could not be taken back: no case runs on it
            cases = [{"name": path.name, "outcome": not_run, "message": cli.left}]
        elif cli is not None:
            try:
                cases = cli.run_file(path, report["runner_ended"], not_run)
            except JudgeFault:
                cli.close()
                raise
        else:
            fresh = t.get("fresh_app")
            if fresh or not shared_up or not app.alive():
                up = app.start()
                shared_up = up and not fresh
            else:
                up = True
            served = t
            if not up:
                cases = [{"name": path.name, "outcome": not_run, "message": app.left or "program did not start"}]
            else:
                def again() -> tuple[str, str] | None:
                    """A runner or the program was ended: the cases that are left get a program started afresh."""
                    nonlocal shared_up
                    up = app.start()
                    shared_up = up and not fresh
                    if up:
                        env.update(app.env)
                    return None if up else (not_run, app.left or "program did not start")

                env = {**app.env, "BASE_URL": f"http://127.0.0.1:{port}", "DODAM_APP_DIR": str(app_dir)}
                cases = run_cases(path, env, 300, again, report["runner_ended"])
                if fresh:
                    app.stop()
        for c in cases:
            report["tests"].append({**c, "file": t["file"], "kind": t.get("kind"), "step": t.get("step"),
                                    "weakness": t.get("weakness")})
    app.stop()
    highs = cli.highs() if cli is not None else None
    if cli is not None and run_as:
        records["highest_in_a_case"] = cli.records_highest
        records["left_out_of_a_case_at_most"] = min(cli.limits["memory_mb"] * 2**20 // 4, RECORDS_LEFT_OUT_MB * 2**20)
    if cli is not None:
        cli.close()
    left = app.left or (cli.left if cli is not None else "")
    if left:
        # the reason the step failed: what the program's side left could not be taken back
        report["left_behind"] = left
    last = served if app.left else (selected[-1] if cli is not None and selected else None)
    if left and last is not None and all(c["message"] != left for c in report["tests"]):
        # the last stop of the step could not take back what the program's side left, and no file of
        # tests came after it: the step fails with that reason all the same. The entry is of this step
        # and of a kind of its own, so it counts neither as unjudged nor as a weakness or an earlier step broken
        report["tests"].append({"name": Path(last["file"]).name, "message": left, "file": last["file"],
                               "outcome": "failed", "kind": LEFT_BEHIND, "step": step, "weakness": None})
    if run_as:
        # how near the container came to its cap: the most it held, how often it stood at the cap,
        # and how many processes the kernel ended there
        report["memory"] = {"highest_mb": highest_memory() // 2**20, "times_at_cap": memory_events().get("max", 0),
                            "ended_by_the_kernel": killed_for_memory()}
        if cli is not None:
            # how many readings of the step's cases could not say on which side of a line the figure lay
            report["memory"]["undecided_readings"] = cli.undecided
        records["at_end"] = container_memory()["records"]
        report["memory"]["records_mb"] = {name: round(value / 2**20, 1) for name, value in records.items()}
        if highs is not None:
            # the runs that made a new high of the container's memory above the case's limit
            report["memory"]["highs_above_the_limit"] = highs

    # the cases not judged for measuring, by name: their count is in the reward
    report[UNMEASURED] = [f"{c['file']}::{c['name']}" for c in report["tests"] if c["outcome"] == UNMEASURED]
    reward = reward_of(report["tests"], report["build_ok"], step, bool(run_as))
    write_output(out_dir / "details.json", json.dumps(report, indent=1, ensure_ascii=False))
    write_output(out_dir / "reward.json", json.dumps(reward, indent=1))
    return {"reward": reward, "report": report}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tests", required=True, type=Path)
    p.add_argument("--app", required=True, type=Path)
    p.add_argument("--step", required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--run-as", default=os.environ.get("DODAM_APP_USER", ""))
    p.add_argument("--unseparated", action="store_true",
                   help="run the program as the judge's own user; only for the project's own prepared solutions")
    if argv is None and len(sys.argv) > 1:
        # Every user of the container can read a process's command line. Start again with nothing on
        # it; what was asked travels in the environment, which only root can read.
        os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)],
                  {**os.environ, ARGS_ENV: json.dumps(sys.argv[1:])})
    if argv is None:
        argv = json.loads(os.environ.pop(ARGS_ENV, "[]"))
    a = p.parse_args(argv)
    try:
        result = judge(a.tests, a.app, a.step, a.out, run_as=a.run_as, unseparated=a.unseparated)
    except JudgeFault as exc:
        write_fault(a.out.resolve(), str(exc))
        print(json.dumps({"judge_fault": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result["reward"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
