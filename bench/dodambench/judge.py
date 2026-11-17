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
when that folder is missing. When the judge itself cannot do its work it writes judge-fault.json
instead of a result and ends with exit code 3, so a fault of the judge never looks like a failure
of the program.

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
import xml.etree.ElementTree as ET
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
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        target.symlink_to(deps_dir / name, target_is_directory=True)


def give_app_to(app_dir: Path, user: str) -> None:
    """Make the plain user own the app folder so its build can write there; links are not followed."""
    entry = pwd.getpwnam(user)
    os.lchown(app_dir, entry.pw_uid, entry.pw_gid)
    for root, dirs, files in os.walk(app_dir):
        for name in dirs + files:
            os.lchown(os.path.join(root, name), entry.pw_uid, entry.pw_gid)


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


def kill_user(user: str) -> None:
    """Kill everything the plain user still runs, including daemons that left the process group."""
    uid = pwd.getpwnam(user).pw_uid
    for _ in range(3):
        pids = user_pids(uid)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.2)


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
JUDGE_ROOM_MB = 256                # what the judge and the language's runtime need beside the case's own limits
CASE_ENTRIES = 20000               # how many files and folders a case may hold


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


def remove_tree(top: str | Path, keep_top: bool = False) -> None:
    """Remove a folder and everything in it, whatever a program made there: any depth, any names,
    links (never followed), closed permissions, special files. It holds two folders open at a time
    and remembers only the names on the way down, so its cost is one step per entry."""
    top = str(top)
    here = os.open(top, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    names: list[str] = []
    try:
        while True:
            went_down = False
            with os.scandir(here) as entries:
                for entry in entries:
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


def container_memory() -> dict[str, int]:
    """What the kernel counts for this container, in bytes: all memory taken, and what of it only
    caches files on disk. Memory in shared memory and in memory-backed folders is not such a cache."""
    current = int(Path("/sys/fs/cgroup/memory.current").read_text())
    stat_lines = dict(line.split()[:2] for line in Path("/sys/fs/cgroup/memory.stat").read_text().splitlines())
    return {"taken": current - int(stat_lines["file"]) + int(stat_lines["shmem"])}


def killed_for_memory() -> int:
    """How many processes the kernel has ended in this container because its memory ran out."""
    for line in Path("/sys/fs/cgroup/memory.events").read_text().splitlines():
        if line.startswith("oom_kill "):
            return int(line.split()[1])
    return 0


def container_caps(disk_mb: int | None = None, group: Path = Path("/sys/fs/cgroup"),
                   machine_info: Path = Path("/proc/meminfo")) -> dict:
    """The caps the kernel holds this container to. A judge fault when one is missing or cannot hold
    on this machine, since then nothing bounds what the program's side takes."""
    def number(name: str) -> int | None:
        try:
            text = (group / name).read_text().strip()
        except OSError:
            return None
        return int(text) if text.isdigit() else None

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
    if processes is None:
        raise refuse("processes", "the container has no cap on processes, so a build that forks without end "
                                  "is not bounded")
    if refusing_filter() is None:
        raise refuse("machine", f"no call filter is known for a {platform.machine()} machine")
    caps = {"memory_mb": memory // 2**20, "processes": processes}
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


def run_build(command: str, app_dir: Path, side: Side | None, env: dict[str, str], timeout: float) -> tuple[bool, str]:
    """Run the build and say whether it worked, with the end of what it printed. The build is the
    agent's code: with a side it runs confined as that user, what it prints is read and dropped but
    for its end, and when it ends nothing of it is left running."""
    try:
        proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=app_dir, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=side.environment(env) if side else env,
                                preexec_fn=side.entering() if side else None, start_new_session=side is None)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"the build could not be started: {exc}"
    tail = b""
    deadline = time.monotonic() + timeout
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
    if side:
        side.end()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        raise JudgeFault("the build did not end after it was killed") from None
    proc.stdout.close()
    if timed_out:
        return False, "build timed out"
    return proc.returncode == 0, tail.decode("utf-8", errors="replace")[-2000:]


CLI_LIMITS = {"timeout_sec": "CASE_TIMEOUT_SEC", "memory_mb": "CASE_MEMORY_MB", "output_kb": "CASE_OUTPUT_KB",
              "processes": "CASE_PROCESSES", "disk_mb": "CASE_DISK_MB"}
FIRST_CASE_USER_ID, LAST_CASE_USER_ID = 200_000, 2_000_000_000
NO_FILES = {"proc", "sysfs", "cgroup", "cgroup2", "devpts", "securityfs", "debugfs", "tracefs", "pstore", "bpf"}
# What a file of hidden tests may take besides the time of its cases: starting Python and the judge's own work.
FILE_SECONDS, CASE_SECONDS = 30, 2
BUILD_SECONDS = 900
ARGS_ENV = "DODAM_JUDGE_ARGS"


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


def writable_by_others(mounts: Path = Path("/proc/self/mounts")) -> list[str]:
    """Every folder and file any user may write, on every file system mounted here."""
    tops = []
    for line in mounts.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] not in NO_FILES:
            tops.append(parts[1].replace("\\040", " "))
    found: list[str] = []
    for top in tops:
        res = subprocess.run(["find", top, "-xdev", "(", "-type", "d", "-o", "-type", "f", ")", "-perm", "-0002",
                              "-print0"], capture_output=True)
        found += [p.decode("utf-8", errors="surrogateescape") for p in res.stdout.split(b"\0") if p]
    return sorted(set(found))


def close_writable_places() -> int:
    """Take write access away from every place any user could write; says how many places it closed."""
    closed = 0
    for path in writable_by_others():
        try:
            os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & ~0o002)
            closed += 1
        except OSError as exc:
            if exc.errno != 30:  # a read-only file system cannot be written anyway
                raise JudgeFault(f"cannot close {path} to the program: {exc}") from None
    return closed


def open_for_reading(app_dir: Path) -> None:
    """After the build: everyone may read and run the program, nobody but its owner may change it,
    and no file of it lends its owner's rights to whoever runs it."""
    for root, dirs, files in os.walk(app_dir):
        for name in [""] + dirs + files:
            path = os.path.join(root, name) if name else root
            try:
                if os.path.islink(path):
                    continue
                mode = stat.S_IMODE(os.stat(path).st_mode)
                mode = (mode | 0o004 | (0o001 if mode & 0o100 or os.path.isdir(path) else 0)) & ~0o6022
                os.chmod(path, mode)
            except OSError:
                pass


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
        os.chmod(self.work_root, 0o711)
        self.fault_file = self.private / "faults.jsonl"
        env = {k: v for k, v in {**os.environ, **extra_env}.items()
               if not k.startswith(("DODAM_", "PYTEST_")) and k not in ("PYTHONPATH", "HOME", "TMPDIR", "PWD", "OLDPWD")}
        settings = {"command": command, "uid": self.uid, "gid": self.uid, "limits": limits,
                    "work_root": str(self.work_root), "fault_file": str(self.fault_file), "env": env}
        self.settings_file = self.private / "settings.json"
        self.settings_file.write_text(json.dumps(settings), encoding="utf-8")
        # the tests get nothing that differs from one language to the next but the settings file itself
        self.test_env = {"PATH": extra_env.get("PATH", os.environ.get("PATH", "")),
                         "PYTHONPATH": str(Path(__file__).resolve().parent),
                         "PYTHONDONTWRITEBYTECODE": "1", "DODAM_CLI_CONFIG": str(self.settings_file)}

    def run_file(self, path: Path, junit: Path) -> list[dict]:
        tests = len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), flags=re.M))
        timeout = FILE_SECONDS + tests * (self.limits["timeout_sec"] + CASE_SECONDS)
        cases = run_pytest(path, self.test_env, junit, timeout)
        if self.fault_file.exists():
            raise JudgeFault(self.fault_file.read_text(encoding="utf-8").strip()[:2000])
        self.sweep()
        whole = [c for c in cases if c["name"] == path.name]
        if whole:
            # a program cannot make a file of tests run out of time or end without a report: its cases are limited
            raise JudgeFault(f"{path.name}: {whole[0]['message']}")
        return cases

    def sweep(self) -> None:
        """Between two files of tests nothing of the program is left: no process, no case folder."""
        if self.uid is not None:
            Side(self.uid, self.uid).end()
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
            shutil.rmtree(self.work_root, ignore_errors=True)
        shutil.rmtree(self.private, ignore_errors=True)


def write_fault(out_dir: Path, text: str) -> None:
    """Say that the judge failed, and leave no result that could be read as a judgement of the program."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("reward.json", "reward.txt"):
        path = out_dir / name
        if path.is_symlink() or path.exists():
            path.unlink()
    write_output(out_dir / "judge-fault.json", json.dumps({"judge_fault": text}, indent=1, ensure_ascii=False))


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
        if self.run_as:
            subprocess.run(["chown", "-R", self.run_as, str(self.data_dir)], check=False)
        self.env = {**os.environ, **self.extra_env, "PORT": str(self.port),
                    "DB_PATH": str(self.data_dir / "app.db"), "APP_SECRET": secrets.token_hex(32)}
        if self.run_as:
            # the served program is the agent's code: it runs confined, as the build does
            side = Side.named(self.run_as)
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
        if self.proc and self.run_as:
            # the program may have left its process group
            kill_user(self.run_as)
        self.proc = None


def run_pytest(test_path: Path, env: dict[str, str], junit: Path, timeout: float) -> list[dict]:
    # The command line of a process can be read by every user of the container, so it says nothing:
    # which file of tests to run and where the report goes travel in the environment, which only
    # root can read.
    options = ["-q", "-p", "no:cacheprovider", f"--junitxml={junit}", str(test_path)]
    env = {**env, "PYTEST_ADDOPTS": " ".join(shlex.quote(o) for o in options)}
    try:
        subprocess.run([sys.executable, "-m", "pytest"], env=env, cwd=test_path.parent, timeout=timeout,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [{"name": test_path.name, "outcome": "failed", "message": "timed out"}]
    if not junit.is_file():
        return [{"name": test_path.name, "outcome": "failed", "message": "no test report"}]
    cases = []
    for case in ET.parse(junit).iter("testcase"):
        outcome = "passed"
        message = ""
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                outcome, message = "failed", (node.get("message") or "")[:300]
        if case.find("skipped") is not None:
            outcome = "skipped"
        cases.append({"name": case.get("name", ""), "outcome": outcome, "message": message})
    return cases or [{"name": test_path.name, "outcome": "failed", "message": "no tests collected"}]


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
        side = Side.named(run_as)
        side.end()
        give_app_to(app_dir, run_as)
        if is_cli:
            limits, _ = CliRun.settings(app_dir, lang_env)
            report["caps"] = container_caps(limits["disk_mb"])
    deps_dir, links = lang_env.get("DEPS_DIR", ""), [n for n in lang_env.get("DEPS_LINKS", "").split(",") if n]
    if deps_dir and links:
        restore_deps(app_dir, Path(deps_dir), links)

    build_cmd = lang_env.get("BUILD_CMD", "").strip()
    started = time.monotonic()
    if build_cmd:
        report["build_ok"], report["build_log_tail"] = run_build(
            build_cmd, app_dir, side, {**os.environ, **(extra_env or {})},
            float(lang_env.get("BUILD_TIMEOUT_SEC", BUILD_SECONDS)))
    else:
        report["build_ok"] = True
    report["build_sec"] = round(time.monotonic() - started, 2)

    cli = None
    if is_cli and report["build_ok"]:
        cli = CliRun(app_dir, lang_env, run_as, extra_env or {})
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
                cases = cli.run_file(path, out_dir / ("junit-" + t["file"].replace("/", "-") + ".xml"))
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
                env = {**app.env, "BASE_URL": f"http://127.0.0.1:{port}", "DODAM_APP_DIR": str(app_dir)}
                junit = out_dir / ("junit-" + t["file"].replace("/", "-") + ".xml")
                cases = run_pytest(path, env, junit, timeout=300)
                if fresh:
                    app.stop()
        for c in cases:
            report["tests"].append({**c, "file": t["file"], "kind": t.get("kind"), "step": t.get("step"),
                                    "weakness": t.get("weakness")})
    app.stop()
    if cli is not None:
        cli.close()
    if run_as:
        kill_user(run_as)

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
