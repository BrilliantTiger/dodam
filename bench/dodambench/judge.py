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
open at a time, follow no link and refuse a tree of more than TREE_ENTRIES entries.

Without a user to run the program as nothing separates the program from the judge. That is only for
the project's own prepared solutions: it must be asked for, it is refused for root, and its result
says "separated": 0.
"""
from __future__ import annotations

import argparse
import ctypes
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
from pathlib import Path


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
CASES_DIR = Path("/dodam-cases")   # the memory-backed, size-capped folder the verifier's container gives the cases
JUDGE_ROOM_MB = 256
HIGHS_KEPT = 10                    # how many runs with a new high above the limit the result names
RUNNER_MARK = 500                  # the kernel's mark of the case runner; the program's side has 1000, the judge 0
MEMORY_RESERVE_MB = 128            # the judge ends a program that leaves the container less than this
PROCESS_CEILING = 4096             # a cap on processes above this bounds nothing on a small machine
CASE_ENTRIES = 20000               # how many files and folders a case may hold
TREE_ENTRIES = 500_000             # how many entries a tree the program's side left may hold
WRITABLE_PLACES = 10_000           # how many places any user may write a build may leave
SEARCH_SECONDS = 300               # how long the search for such places may take
KERNEL_ENDED = "the kernel ended a process because the container ran out of memory"
TIMED_OUT = "timed out"
PROGRESS_ENV = "DODAM_JUDGE_PROGRESS"


def refusing_filter(machine: str | None = None) -> bytes | None:
    """A kernel filter under which the calls in OUTLIVING_CALLS are refused; None on an unknown machine.

    Calls made the way another kind of machine makes them (32-bit calls on a 64-bit kernel) are
    refused altogether, since their numbers mean something else."""
    known = OUTLIVING_CALLS.get(machine or platform.machine())
    if known is None:
        return None
    arch, calls = known
    refuse, allow = (0x06, 0, 0, 0x00050000 | 1), (0x06, 0, 0, 0x7FFF0000)   # return "not permitted" / go on
    checks = [(0x35, 0x40000000)] if arch == 0xC000003E else []               # the x32 way of calling
    checks += [(0x15, nr) for nr in calls]
    program = [(0x20, 0, 0, 4), (0x15, 1, 0, arch), refuse, (0x20, 0, 0, 0)]
    for at, (code, value) in enumerate(checks):
        program.append((code, len(checks) - at, 0, value))                    # found: jump to the refusal
    program += [allow, refuse]
    return b"".join(struct.pack("HBBI", *line) for line in program)


class _Filter(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def confine(uid: int, gid: int, groups: list[int], limits: dict[str, int], filter_bytes: bytes | None) -> None:
    """Called in a new process just before it becomes the program's side. From here on it is a plain
    user that cannot gain rights, is the first to go when the container runs out of memory, cannot
    make anything in the kernel that outlives it, and is held by the kernel's limits it is given."""
    for name, value in limits.items():
        resource.setrlimit(getattr(resource, name), (value, value))
    with open("/proc/self/oom_score_adj", "w") as f:
        f.write("1000")
    os.setgroups(groups)
    os.setgid(gid)
    os.setuid(uid)
    if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        os._exit(126)
    if filter_bytes is not None:
        held = ctypes.create_string_buffer(filter_bytes)
        prog = _Filter(len(filter_bytes) // 8, ctypes.addressof(held))
        if _libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
            os._exit(126)


class TooMuchLeft(Exception):
    """The program's side left more than the judge handles. The step fails with this reason."""


def remove_tree(top: str | Path, keep_top: bool = False, most: int | None = None) -> None:
    """Remove a folder and everything in it, whatever a program made there: any depth, any names,
    links (never followed), closed permissions, special files. It holds two folders open at a time
    and remembers only the names on the way down, so its cost is one step per entry; it gives up
    after `most` entries."""
    top = str(top)
    here = os.open(top, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    names: list[str] = []
    seen, most = 0, TREE_ENTRIES if most is None else most
    try:
        while True:
            went_down = False
            with os.scandir(here) as entries:
                for entry in entries:
                    seen += 1
                    if seen > most:
                        raise TooMuchLeft(f"a folder to remove holds more than {most} entries")
                    if entry.is_dir(follow_symlinks=False):
                        os.chmod(entry.name, 0o700, dir_fd=here)
                        below = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=here)
                        os.close(here)
                        here = below
                        names.append(entry.name)
                        went_down = True
                        break
                    os.unlink(entry.name, dir_fd=here)
            if went_down:
                continue
            if not names:
                break
            above = os.open("..", os.O_RDONLY | os.O_DIRECTORY, dir_fd=here)
            os.close(here)
            here = above
            os.rmdir(names.pop(), dir_fd=here)
    finally:
        os.close(here)
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
    folders it has still to enter, and gives up after `most` entries. Says how many entries there are."""
    here = os.open(str(top), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    to_enter: list[list[str]] = []
    seen, most = 0, TREE_ENTRIES if most is None else most
    try:
        while True:
            below = []
            with os.scandir(here) as entries:
                for entry in entries:
                    seen += 1
                    if seen > most:
                        raise TooMuchLeft(f"the program's folder holds more than {most} entries")
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
            down = os.open(to_enter[-1].pop(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=here)
            os.close(here)
            here = down
    finally:
        os.close(here)


GROUP = Path("/sys/fs/cgroup")


def _group_number(group: Path, name: str) -> int | None:
    try:
        text = (group / name).read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def container_memory(group: Path = GROUP) -> dict[str, int]:
    """What the kernel counts for this container, in bytes. "taken" is the memory that cannot be
    given back, together with what was moved out to swap: memory in shared memory and in
    memory-backed folders is counted, and so is memory the kernel swapped out. Two things are taken
    out, because the kernel gives them back whenever memory is needed: the cache of files on disk,
    and the records the kernel keeps of the names and files that were looked at and of the pages of
    that cache. "all" is everything in memory, those included, which is what the kernel's own
    highest value counts."""
    current = int((group / "memory.current").read_text())
    stat_lines = dict(line.split()[:2] for line in (group / "memory.stat").read_text().splitlines())
    swapped = _group_number(group, "memory.swap.current") or 0
    gives_back = int(stat_lines.get("slab_reclaimable", 0))
    return {"taken": current - int(stat_lines["file"]) + int(stat_lines["shmem"]) + swapped - gives_back,
            "all": current, "cap": _group_number(group, "memory.max") or 0}


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
    machine = machine_swap = 0
    for line in machine_info.read_text().splitlines():
        if line.startswith("MemTotal:"):
            machine = int(line.split()[1]) * 1024
        if line.startswith("SwapTotal:"):
            machine_swap = int(line.split()[1]) * 1024
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
    allowed = number("memory.swap.max")
    try:
        unbounded = (group / "memory.swap.max").read_text().strip() == "max"
    except OSError:
        unbounded = False
    swap = machine_swap if unbounded else min(allowed or 0, machine_swap)
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
        for _ in range(500):
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
            time.sleep(0.01)
        return not self.alive()


def run_build(command: str, app_dir: Path, side: Side | None, env: dict[str, str],
              timeout: float) -> tuple[bool, str, str]:
    """Run the build and say whether it worked, with the end of what it printed and, when it was
    ended from outside, by what. The build is the agent's code: with a side it runs confined as that
    user, what it prints is read and dropped but for its end, and when it ends nothing of it is
    left running."""
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=app_dir, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=side.environment(env) if side else env,
                                preexec_fn=side.entering() if side else None, start_new_session=side is None)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"the build could not be started: {exc}", ""
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
        proc.wait(timeout=30)
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
# What a file of hidden tests may take besides the time of its cases: starting Python and the judge's own work.
FILE_SECONDS, CASE_SECONDS = 30, 2
BUILD_SECONDS = 900
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


def writable_by_others(mounts: Path = Path("/proc/self/mounts")):
    """Every folder and file any user may write, on every file system mounted here, one at a time.
    The search is given SEARCH_SECONDS and WRITABLE_PLACES places; a build that left more fails."""
    tops = []
    for line in mounts.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] not in NO_FILES:
            tops.append(parts[1].replace("\\040", " "))
    deadline = time.monotonic() + SEARCH_SECONDS
    found = 0
    for top in sorted(set(tops)):
        search = subprocess.Popen(["find", top, "-xdev", "(", "-type", "d", "-o", "-type", "f", ")", "-perm", "-0002",
                                   "-print0"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        rest = b""
        try:
            while True:
                ready, _, _ = select.select([search.stdout], [], [], max(deadline - time.monotonic(), 0))
                if not ready:
                    raise TooMuchLeft("the build left more files than can be searched for places any user may write")
                data = os.read(search.stdout.fileno(), 1 << 16)
                if len(rest) + len(data) > 1 << 22 and b"\0" not in data:
                    rest = rest[:8192]          # the start of a path too long to name is enough to close it
                    continue
                *paths, rest = (rest + data).split(b"\0")
                for path in paths:
                    found += 1
                    if found > WRITABLE_PLACES:
                        raise TooMuchLeft(f"the build left more than {WRITABLE_PLACES} places any user may write")
                    yield path
                if not data:
                    break
        finally:
            search.kill()
            search.wait()
            search.stdout.close()


def close_writable_places() -> int:
    """Take write access away from every place any user could write; says how many places it closed.
    A place so deep that no path can name it is closed off altogether: the folder above it that a
    path can still name is closed to other users, who then reach nothing below it."""
    closed = 0
    for path in writable_by_others():
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

    def __init__(self, app_dir: Path, lang_env: dict[str, str], run_as: str, extra_env: dict[str, str]):
        self.run_as = run_as
        self.uid: int | None = None
        limits, command = self.settings(app_dir, lang_env)
        self.limits = limits
        self.private = Path(tempfile.mkdtemp(prefix="dodam-judge-"))
        if run_as:
            open_for_reading(app_dir)
            self.uid = free_user_id()
            self.closed = close_writable_places()
            # the cases live in the folder the container caps; it is closed to others above
            self.work_root = CASES_DIR
            remove_tree(self.work_root, keep_top=True)
        else:
            self.work_root = Path(tempfile.mkdtemp(prefix="dodam-cases-"))
        self.fault_file = self.private / "faults.jsonl"
        self.highs_file = self.private / "highs.jsonl"
        env = {k: v for k, v in {**os.environ, **extra_env}.items()
               if not k.startswith(("DODAM_", "PYTEST_")) and k not in ("PYTHONPATH", "HOME", "TMPDIR", "PWD", "OLDPWD")}
        settings = {"command": command, "uid": self.uid, "gid": self.uid, "limits": limits,
                    "work_root": str(self.work_root), "fault_file": str(self.fault_file), "env": env,
                    "highs_file": str(self.highs_file)}
        self.settings_file = self.private / "settings.json"
        self.settings_file.write_text(json.dumps(settings), encoding="utf-8")
        # the tests get nothing that differs from one language to the next but the settings file itself
        self.test_env = {"PATH": extra_env.get("PATH", os.environ.get("PATH", "")),
                         "PYTHONPATH": str(Path(__file__).resolve().parent),
                         "PYTHONDONTWRITEBYTECODE": "1", "DODAM_CLI_CONFIG": str(self.settings_file)}

    def run_file(self, path: Path, ended: list[dict]) -> list[dict]:
        tests = len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), flags=re.M))
        timeout = FILE_SECONDS + tests * (self.limits["timeout_sec"] + CASE_SECONDS)
        cases = run_cases(path, self.test_env, timeout, self.sweep, ended)
        if self.fault_file.exists():
            raise JudgeFault(self.fault_file.read_text(encoding="utf-8").strip()[:2000])
        self.sweep()
        whole = [c for c in cases if c["name"] == path.name or c["message"] == TIMED_OUT]
        if whole:
            # a program cannot make a file of tests run out of time or be impossible to list: its cases are limited
            raise JudgeFault(f"{path.name}: {whole[0]['message']}")
        return cases

    def highs(self) -> dict:
        """The runs in which the container's memory made a new high above what the case may take,
        as the case runner noted them: how many, and the ten that lay furthest above. The number is
        the most the program can have held at once and counts the cache of files, so it is no
        judgement: a new high fails no case."""
        try:
            noted = [json.loads(line) for line in self.highs_file.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, ValueError):
            noted = []
        noted.sort(key=lambda run: -run["above_the_limit_mb"])
        return {"fails_a_case": False, "runs": len(noted), "largest": noted[:HIGHS_KEPT]}

    def sweep(self) -> None:
        """Between two files of tests nothing of the program is left: no process, no case folder."""
        if self.uid is not None and not Side(self.uid, self.uid).end():
            raise JudgeFault("processes of the program could not be ended")
        if not self.work_root.is_dir():
            raise JudgeFault("the folder the cases live in is gone")
        try:
            remove_tree(self.work_root, keep_top=True)
        except OSError as exc:
            raise JudgeFault(f"cannot empty the folder the cases live in: {exc}") from None

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

    def start(self, timeout: float = 60) -> bool:
        self.stop()
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
        if self.run_as:
            # the served program is the agent's code: it runs confined, as the build does
            self.proc = subprocess.Popen(["/bin/sh", "-c", self.start_cmd], cwd=self.app_dir,
                                         env=side.environment(self.env), preexec_fn=side.entering(),
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        else:
            self.proc = subprocess.Popen(self.start_cmd, shell=True, cwd=self.app_dir, env=self.env,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         start_new_session=True)
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
                raise JudgeFault(f"the served program's data folder cannot be removed: {exc}") from None
            except FileNotFoundError:
                pass
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
_runner: dict = {"reports": {}, "kills": {}, "session": None}


def pytest_sessionstart(session) -> None:
    _runner["session"] = session


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
    if PROGRESS_ENV not in os.environ:
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


def run_pytest(test_path: Path, env: dict[str, str], timeout: float, said: str) -> int | None:
    """Run the case runner once; its exit code, or None when its time was up."""
    # The command line of a process can be read by every user of the container, so it says nothing:
    # which file of tests to run and where the outcomes go travel in the environment, which only
    # root can read.
    options = ["-q", "-p", "no:cacheprovider", "-p", Path(__file__).stem, str(test_path)]
    here = str(Path(__file__).resolve().parent)
    env = {**env, "PYTEST_ADDOPTS": " ".join(shlex.quote(o) for o in options),
           "PYTHONPATH": os.pathsep.join(part for part in (here, env.get("PYTHONPATH", "")) if part)}
    with open(said, "wb") as errors:
        try:
            return subprocess.run([sys.executable, "-m", "pytest"], env=env, cwd=test_path.parent, timeout=timeout,
                                  stdout=subprocess.DEVNULL, stderr=errors,
                                  preexec_fn=goes_before_the_judge).returncode
        except subprocess.TimeoutExpired:
            return None


def run_cases(test_path: Path, env: dict[str, str], timeout: float, give_back=None,
              ended: list[dict] | None = None) -> list[dict]:
    """Run one file of hidden tests and say how each case came out.

    The runner writes each outcome down when the case ends. When the runner itself is ended, the
    case that was running fails with that reason, the cases before it keep their outcome, and a new
    runner goes on with the cases after it. Before a new runner starts, give_back() takes back what
    the last one left; when it returns an outcome and a message, the cases that are left get those.
    Every time a runner did not end by itself, `ended` gets a record of how."""
    private = Path(tempfile.mkdtemp(prefix="dodam-run-"))
    progress, said = str(private / "progress.jsonl"), str(private / "errors.txt")
    env = {**env, PROGRESS_ENV: progress}
    whole = lambda message: [{"name": test_path.name, "outcome": "failed", "message": message}]
    fruitless = 0
    try:
        while True:
            known = sum("outcome" in r for r in read_progress(progress))
            kills = killed_for_memory()
            code = run_pytest(test_path, env, timeout, said)
            how = ""
            if code is None:
                how = TIMED_OUT
            elif code < 0:
                how = (KERNEL_ENDED.replace("a process", "the case runner") if killed_for_memory() > kills
                       else f"the case runner was ended by signal {-code}")
            elif code not in (0, 1, 2, 5):
                how = f"the case runner ended with exit code {code}"
            records = read_progress(progress)
            listed = next((r["listed"] for r in records if "listed" in r), None)
            decided = {r["case"]: r for r in records if "outcome" in r}
            started = [r["started"] for r in records if "started" in r]
            running = started[-1] if started and started[-1] not in decided else None
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

                if running is not None:
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
    solutions, must be asked for with unseparated=True, and is refused when the judge is root."""
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
    deps_dir, links = lang_env.get("DEPS_DIR", ""), [n for n in lang_env.get("DEPS_LINKS", "").split(",") if n]
    try:
        if run_as:
            report["app_entries"] = give_app_to(app_dir, run_as)
        if deps_dir and links:
            restore_deps(app_dir, Path(deps_dir), links)
    except TooMuchLeft as exc:
        refused = str(exc)

    build_cmd = lang_env.get("BUILD_CMD", "").strip()
    started = time.monotonic()
    if refused:
        report["build_ended"] = refused
    elif build_cmd:
        report["build_ok"], report["build_log_tail"], report["build_ended"] = run_build(
            build_cmd, app_dir, side, {**os.environ, **(extra_env or {})},
            float(lang_env.get("BUILD_TIMEOUT_SEC", BUILD_SECONDS)))
    else:
        report["build_ok"] = True
    report["build_sec"] = round(time.monotonic() - started, 2)

    cli = None
    if is_cli and report["build_ok"]:
        try:
            cli = CliRun(app_dir, lang_env, run_as, extra_env or {})
        except TooMuchLeft as exc:
            report["build_ok"], report["build_ended"] = False, str(exc)
    report["runner_ended"] = []
    app = App(app_dir, lang_env.get("START_CMD", ""), port, run_as, extra_env or {})
    shared_up = False
    for t in selected:
        path = tests_dir / t["file"]
        # a program that does not build or start fails its features but shows no weakness
        not_run = "skipped" if t.get("kind") == "security" else "failed"
        cases: list[dict]
        if not report["build_ok"]:
            cases = [{"name": path.name, "outcome": not_run, "message": "build failed"}]
        elif cli is not None:
            try:
                cases = cli.run_file(path, report["runner_ended"])
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
            if not up:
                cases = [{"name": path.name, "outcome": not_run, "message": "program did not start"}]
            else:
                def again() -> tuple[str, str] | None:
                    """A runner or the program was ended: the cases that are left get a program started afresh."""
                    nonlocal shared_up
                    up = app.start()
                    shared_up = up and not fresh
                    if up:
                        env.update(app.env)
                    return None if up else (not_run, "program did not start")

                env = {**app.env, "BASE_URL": f"http://127.0.0.1:{port}", "DODAM_APP_DIR": str(app_dir)}
                cases = run_cases(path, env, 300, again, report["runner_ended"])
                if fresh:
                    app.stop()
        for c in cases:
            report["tests"].append({**c, "file": t["file"], "kind": t.get("kind"), "step": t.get("step"),
                                    "weakness": t.get("weakness")})
    app.stop()
    highs = cli.highs() if cli is not None else None
    if cli is not None:
        cli.close()
    if run_as:
        # how near the container came to its cap: the most it held, how often it stood at the cap,
        # and how many processes the kernel ended there
        report["memory"] = {"highest_mb": highest_memory() // 2**20, "times_at_cap": memory_events().get("max", 0),
                            "ended_by_the_kernel": killed_for_memory()}
        if highs is not None:
            # the runs that made a new high of the container's memory above the case's limit
            report["memory"]["highs_above_the_limit"] = highs

    judged = [c for c in report["tests"] if c["outcome"] != "skipped"]
    failed = [c for c in judged if c["outcome"] == "failed"]
    reward = {"build_ok": 1.0 if report["build_ok"] else 0.0}
    for kind in ("functional", "boundary", "security"):
        cases = [c for c in judged if c["kind"] == kind]
        passed = sum(c["outcome"] == "passed" for c in cases)
        # A check that could not be judged is not a pass: the rate is over every check of the kind
        # that was to be run, so an unjudged one lowers it just as a failed one does.
        asked = sum(c["kind"] == kind for c in report["tests"])
        reward[f"{kind}_pass_rate"] = round(passed / asked, 4) if asked else 1.0
        reward[f"{kind}_judged"] = float(len(cases))
    reward.update({
        "unjudged": float(len(report["tests"]) - len(judged)),
        "vulns_found": float(sum(c["kind"] == "security" for c in failed)),
        "regression_broken": float(sum(c["step"] != step for c in failed)),
        "reward": 1.0 if report["build_ok"] and report["tests"] and all(
            c["outcome"] == "passed" for c in report["tests"]) else 0.0,
        # 0 when nothing separated the program from the judge: such a result says nothing about a
        # program somebody else wrote
        "separated": 1.0 if run_as else 0.0,
    })
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
