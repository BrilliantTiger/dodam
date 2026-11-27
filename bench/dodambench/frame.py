"""The frame through which every container on this computer is started.

    python -m dodambench.frame run [--worktree PATH]... -- COMMAND...

runs COMMAND as one batch. In code, a command that makes a container goes through `container`:

    subprocess.run(frame.container(["docker", "create", ...]), ...)

What the frame does for a batch:
- before it starts, it reads free disk, memory pressure and fseventsd's memory, and refuses under a line;
- it holds a lock, so that one batch runs at a time: a batch that waits is told after a minute which process
  holds it, and is refused;
- every container it makes carries the batch's mark (a label with the batch's run id) and writes its id to a
  file of the batch;
- every two minutes it reads again, and past a line it removes its containers and stops;
- at its end, also after a failure or a signal, it ends the programs it started, removes exactly the containers
  it made, by their ids, and the named volumes made through it, and its temporary folder; a batch that was
  killed is cleaned up so by the next one, once its process is dead;
- it refuses to mount the sensitive folders of this computer: what may be mounted is the batch's own temporary
  folder and work trees named to it;
- at the end it gives space back by trimming the disk of the container machine, only when no container at all
  runs. It never prunes, never removes an image, and never starts or stops the container machine.

Where it runs is told by the platform. The readings belong to this computer (macOS); elsewhere, as in the
automatic check, they are not made, and everything else is the same.

A process that starts a container without an outer batch opens one of its own at its first container and keeps
it until it ends. A process inside a `frame run` joins that batch: the run must still be alive and be one of the
process's own ancestors, so that naming a run in the environment lets no other process in.

Only the standard library is used: the Gemini job loads this file by its path."""
from __future__ import annotations

import atexit
import contextlib
import fcntl
import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

PLATFORM = sys.platform
LABEL = "dodam.frame.run"
GIB = 2**30

# The lines. The two lines of the disk are the project's own. The owner is still to decide the memory line (the
# pressure level, and perhaps swap in use beside it) and the way fseventsd is measured: those are in PENDING.
SETTINGS: dict[str, object] = {
    "free_disk_gib": 35,       # free disk of this computer under which a batch does not start, and stops
    "disk_drop_gib": 10,       # how far free disk may fall below what it was when the batch started
    "pressure_level": 2,       # the memory pressure level at which a batch does not start, and stops
    "swap_used_gb": None,      # swap in use, in GiB, at which a batch stops; not read while it is None
    "fseventsd_gb": 3,         # fseventsd's memory as top reports it, in GiB, over which a batch stops
    "every_seconds": 120,      # how often a running batch reads again
    "lock_wait_seconds": 60,   # how long a batch waits for the lock before it is refused
    "trim_seconds": 30,        # the bound of time for giving space back
    "retry_seconds": 1,        # a reading that fails is tried once more after this; a second failure stops
    "end_seconds": 10,         # between asking the programs of a batch to end and ending them
    "command_seconds": 60,     # the bound of time for one call of the container command
}
PENDING = ("pressure_level", "swap_used_gb", "fseventsd_gb")

# Folders of the container machine, not of this computer, that a caller may name for one container: a judged
# container's folder of the kernel's, and a file of the machine's memory settings. The caller names each one
# exactly (the judge's tests give their helper two of them, named in those tests only), and they are given only
# to a container without network and without any folder of this computer.
MACHINE_PATHS = (re.compile(r"/sys/fs/cgroup/(docker/[0-9a-f]{64}|system\.slice/docker-[0-9a-f]{64}\.scope)"),
                 re.compile(r"/proc/sys/vm/[a-z_]+"))
# Folders under the home folder that are never mounted, nor a folder that holds one of them.
SENSITIVE = (".dodam", ".dodam-desk", ".ssh", ".gnupg", ".aws", ".config", ".docker", ".colima", ".claude",
             ".codex", ".gemini", ".kube", ".netrc", "Library")

SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
RUN_RE = re.compile(r"[0-9a-f]{32}")
ID_RE = re.compile(r"[0-9a-f]{64}")


def default_root() -> Path:
    """One folder for every session of this user: not the temporary folder of a session, which may differ."""
    return Path("/tmp").resolve() / f"dodam-frame-{os.getuid()}"


ROOT = default_root()


class FrameRefused(Exception):
    """A batch or a container the frame does not start. `said` is the diagnostic, JSON first."""

    def __init__(self, said: dict):
        super().__init__(json.dumps(said))
        self.said = said


class FrameStopped(FrameRefused):
    """The batch stopped itself at a line and starts no more containers."""


def _refuse(reason: str, message: str, **more) -> FrameRefused:
    return FrameRefused({"refused": True, "reason": reason, "message": message, **more})


# --- the commands this computer is read and the containers are handled with -----------------------------------------

def _run(args: list[str], seconds: float | None = None, env: dict | None = None) -> subprocess.CompletedProcess | None:
    """A command found on the path, or None when it is not there, did not end in time or could not start."""
    # the path first, so that a stand-in is found; then the system's folders, which a narrowed path may lack
    path = os.pathsep.join(filter(None, [(env or os.environ).get("PATH", ""), SYSTEM_PATH]))
    program = shutil.which(args[0], path=path)
    if program is None:
        return None
    try:
        return subprocess.run([program, *args[1:]], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=seconds or SETTINGS["command_seconds"], env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def _said(args: list[str], seconds: float | None = None) -> str | None:
    done = _run(args, seconds)
    return done.stdout if done is not None and done.returncode == 0 else None


def started_at(pid: int) -> str | None:
    """When the process started, as `ps` tells it; with its number, it names one process and no later one."""
    said = _said(["ps", "-o", "lstart=", "-p", str(pid)])
    return said.strip() or None if said else None


def _ancestors() -> set[int]:
    seen, pid = set(), os.getpid()
    for _ in range(64):
        said = _said(["ps", "-o", "ppid=", "-p", str(pid)])
        try:
            pid = int(said.strip()) if said else 0
        except ValueError:
            pid = 0
        if pid <= 1:
            break
        seen.add(pid)
    return seen


def _alive(state: dict) -> bool:
    pid = state.get("pid")
    return isinstance(pid, int) and pid > 0 and started_at(pid) == state.get("started")


# --- the readings of this computer --------------------------------------------------------------------------------

class _ReadingFailed(Exception):
    pass


def free_disk(path) -> int:
    return shutil.disk_usage(path).free


TOP_UNITS = {"B": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}


def read_top_memory(said: str) -> int | None:
    """top's figure of memory with its unit, sometimes followed by + or -."""
    found = re.fullmatch(r"\s*(\d+(?:\.\d+)?)([BKMGT])[+-]?\s*", said or "")
    return int(float(found.group(1)) * TOP_UNITS[found.group(2)]) if found else None


def _read_pressure() -> int:
    said = _said(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"])
    try:
        return int(said.strip())
    except (AttributeError, ValueError):
        raise _ReadingFailed("pressure") from None


def _read_swap_used() -> int:
    said = _said(["sysctl", "-n", "vm.swapusage"]) or ""
    found = re.search(r"used = (\d+(?:\.\d+)?)([KMG])", said)
    if not found:
        raise _ReadingFailed("swap")
    return int(float(found.group(1)) * TOP_UNITS[found.group(2)])


def _read_fseventsd() -> int | None:
    """The largest memory of the processes named fseventsd, by top; None when there is none."""
    done = _run(["pgrep", "-x", "fseventsd"])
    if done is None or done.returncode not in (0, 1):
        raise _ReadingFailed("fseventsd")
    pids = done.stdout.split()
    if not pids:
        return None
    figures = []
    for pid in pids:
        said = _said(["top", "-l", "1", "-pid", pid, "-stats", "mem"])
        lines = [line for line in (said or "").splitlines() if line.strip()]
        figure = read_top_memory(lines[-1]) if lines else None
        if figure is None:
            raise _ReadingFailed("fseventsd")
        figures.append(figure)
    return max(figures)


def _read_once() -> dict:
    readings = {"free_disk": free_disk(Path.home()), "pressure": _read_pressure(), "fseventsd": _read_fseventsd()}
    if SETTINGS["swap_used_gb"] is not None:
        readings["swap_used"] = _read_swap_used()
    return readings


def readings() -> dict:
    """The readings of this computer. A reading that fails is tried once more; a second failure is a reason
    to stop, said as such, and never passed over."""
    try:
        return _read_once()
    except (_ReadingFailed, OSError):
        time.sleep(float(SETTINGS["retry_seconds"]))
    try:
        return _read_once()
    except (_ReadingFailed, OSError) as failed:
        return {"failed": str(failed) or type(failed).__name__}


def crossed(read: dict, start_free: int | None) -> str | None:
    """The first line the readings cross, or None."""
    if "failed" in read:
        return "reading_failed"
    if read["free_disk"] < int(SETTINGS["free_disk_gib"]) * GIB:
        return "free_disk"
    if start_free is not None and start_free - read["free_disk"] > int(SETTINGS["disk_drop_gib"]) * GIB:
        return "disk_drop"
    if read["pressure"] >= int(SETTINGS["pressure_level"]):
        return "pressure"
    if SETTINGS["swap_used_gb"] is not None and read.get("swap_used", 0) >= float(SETTINGS["swap_used_gb"]) * GIB:
        return "swap_used"
    if read["fseventsd"] is not None and read["fseventsd"] > float(SETTINGS["fseventsd_gb"]) * GIB:
        return "fseventsd"
    return None


MESSAGES = {
    "free_disk": "free disk is under the line",
    "disk_drop": "free disk fell further below the start than the line allows",
    "pressure": "memory pressure reached the line",
    "swap_used": "swap in use reached the line",
    "fseventsd": "fseventsd holds more memory than the line",
    "reading_failed": "a reading of this computer could not be made twice in a row",
}


# --- the words the frame adds, and the mounts it refuses ----------------------------------------------------------

# options of `docker run` and `docker create` that take a value, and those that take none
VALUE_OPTIONS = {
    "--name", "--network", "--net", "--network-alias", "--user", "-u", "--memory", "-m", "--memory-swap",
    "--memory-reservation", "--memory-swappiness", "--cpus", "--cpu-shares", "-c", "--cpuset-cpus", "--cpuset-mems",
    "--cpu-period", "--cpu-quota", "--pids-limit", "--tmpfs", "-e", "--env", "--entrypoint", "--security-opt",
    "--cap-add", "--cap-drop", "--label", "-l", "--workdir", "-w", "--ulimit", "--shm-size", "--stop-timeout",
    "--stop-signal", "--platform", "--hostname", "-h", "--add-host", "--ipc", "--pid", "--cgroupns", "--restart",
    "--log-driver", "--log-opt", "--runtime", "--userns", "--uts", "--expose", "-p", "--publish", "--dns",
    "--dns-option", "--dns-search", "--group-add", "--health-cmd", "--health-interval", "--health-retries",
    "--health-timeout", "--health-start-period", "--sysctl", "--oom-score-adj", "--blkio-weight", "--gpus",
    "--mac-address", "--ip", "--ip6", "--attach", "-a", "--pull", "--storage-opt", "--domainname",
    "--volume", "-v", "--mount",
}
FLAG_OPTIONS = {"--rm", "--detach", "-d", "-i", "--interactive", "-t", "--tty", "--init", "--read-only",
                "--oom-kill-disable", "--no-healthcheck", "-P", "--publish-all", "-q", "--quiet"}
# options that hand the container a file, a device or the folders of this computer by another way
REFUSED_OPTIONS = {"--env-file", "--label-file", "--device", "--device-cgroup-rule", "--volumes-from",
                   "--privileged", "--cidfile"}
SHORT_FLAGS = set("dit") | {"P", "q"}
SHORT_VALUES = {"v": "--volume", "e": "--env", "u": "--user", "m": "--memory", "w": "--workdir", "h": "--hostname",
                "p": "--publish", "a": "--attach", "l": "--label", "c": "--cpu-shares"}
GLOBAL_FLAGS = {"-D", "--debug"}
GLOBAL_VALUES = {"-l", "--log-level"}


def _options(words: list[str]) -> list[tuple[str, str | None]]:
    """The options before the image, each with its value; refused when an option is not known."""
    found, i = [], 0
    while i < len(words):
        word = words[i]
        if word == "--" or not word.startswith("-") or word == "-":
            break
        if word.startswith("--"):
            name, eq, value = word.partition("=")
            if name in REFUSED_OPTIONS:
                raise _refuse("option", f"the frame does not pass {name}: it hands the container what is not its own")
            if name in FLAG_OPTIONS and not eq:
                found.append((name, None))
                i += 1
            elif name in VALUE_OPTIONS:
                if not eq:
                    if i + 1 >= len(words):
                        raise _refuse("option", f"{name} has no value")
                    value = words[i + 1]
                    i += 1
                found.append((name, value))
                i += 1
            else:
                raise _refuse("option", f"the frame does not know the option {name}")
            continue
        letters = word[1:]
        for at, letter in enumerate(letters):
            if letter in SHORT_FLAGS:
                found.append((f"-{letter}", None))
                continue
            if letter in SHORT_VALUES:
                value = letters[at + 1:].removeprefix("=")
                if not value:
                    if i + 1 >= len(words):
                        raise _refuse("option", f"-{letter} has no value")
                    value = words[i + 1]
                    i += 1
                found.append((SHORT_VALUES[letter], value))
                break
            raise _refuse("option", f"the frame does not know the option -{letter}")
        i += 1
    return found


def _within(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _work_tree(path) -> Path:
    """A work tree named to the frame: a folder with its history, not the home folder, a folder above it, or
    a sensitive folder or one inside it."""
    real = Path(os.path.realpath(path))
    home = Path(os.path.realpath(Path.home()))
    if not (real / ".git").exists() or _within(home, real):
        raise _refuse("mount", f"{path} is not a work tree the frame may mount")
    if any(_within(real, home / name) for name in SENSITIVE):
        raise _refuse("mount", f"{path} lies in a sensitive folder of this computer")
    return real


def _bind_allowed(source: str, allowed: list[Path]) -> bool:
    real = Path(os.path.realpath(source))
    try:
        if stat.S_ISSOCK(os.stat(real).st_mode):
            return False
    except OSError:
        pass
    home = Path(os.path.realpath(Path.home()))
    if any(_within(real, home / name) for name in SENSITIVE):
        return False
    return any(_within(real, folder) for folder in allowed)


def _check_mounts(options: list[tuple[str, str | None]], allowed: list[Path], machine=()) -> None:
    binds, given = [], []
    for name, value in options:
        if name == "--label" and value is not None and value.split("=", 1)[0] == LABEL:
            raise _refuse("option", "the mark of a batch is given by the frame only")
        if name == "--volume":
            source = value.split(":", 1)[0] if ":" in value else ""
            if not source or ("/" not in source and not source.startswith((".", "~"))):
                continue  # a named volume of the container machine, or an anonymous one
            kind, path = "bind", source
        elif name == "--mount":
            fields = dict(part.partition("=")[::2] for part in value.split(","))
            kind = fields.get("type", "volume")
            if kind in ("volume", "tmpfs"):
                continue
            if kind != "bind":
                raise _refuse("mount", f"the frame does not mount the kind {kind}")
            path = fields.get("source") or fields.get("src") or ""
        else:
            continue
        if path in machine and any(p.fullmatch(path) for p in MACHINE_PATHS) and posixpath.normpath(path) == path:
            given.append(path)
        elif _bind_allowed(path, allowed):
            binds.append(path)
        else:
            raise _refuse("mount", f"the frame does not mount {path}: only its own temporary folder and the work "
                                   "trees named to it")
    if given:
        network = [value for name, value in options if name in ("--network", "--net")]
        if network != ["none"] or binds:
            raise _refuse("mount", "the folders of the container machine are given only to a container without "
                                   "network and without any folder of this computer")


def _subcommand(cmd: list[str]) -> tuple[int, str]:
    """Where the words of the subcommand start (after the program's own options) and what it is."""
    i = 1
    while i < len(cmd) and cmd[i].startswith("-"):
        name = cmd[i].partition("=")[0]
        if name in GLOBAL_FLAGS:
            i += 1
        elif name in GLOBAL_VALUES:
            i += 1 if "=" in cmd[i] else 2
        else:
            raise _refuse("daemon_option", f"the frame does not pass {name}: the container command talks to the "
                                           "container machine of this computer only")
    sub = cmd[i] if i < len(cmd) else ""
    if sub == "container" and i + 1 < len(cmd):
        return i + 2, cmd[i + 1]
    return i + 1, sub


def frame_words(cmd: list[str], run_id: str, cids: Path, allowed: list[Path], machine=()) -> list[str]:
    """A command with the frame's words: for a command that makes a container, the mark and the file for its id
    right after the subcommand, once its mounts are checked; any other command as it was."""
    cmd = list(cmd)
    at, sub = _subcommand(cmd)
    if sub not in ("run", "create"):
        return cmd
    given = cmd[at:at + 4]
    if (len(given) == 4 and given[:3] == ["--label", f"{LABEL}={run_id}", "--cidfile"]
            and Path(given[3]).parent == cids):
        # framed already, in this batch, by the code that runs inside a frame run: its mounts are checked again
        _check_mounts(_options(cmd[at + 4:]), allowed, machine)
        return cmd
    _check_mounts(_options(cmd[at:]), allowed, machine)
    cidfile = cids / secrets.token_hex(8)
    return cmd[:at] + ["--label", f"{LABEL}={run_id}", "--cidfile", str(cidfile)] + cmd[at:]


def _approval(folder: Path, words: list[str]) -> Path:
    digest = hashlib.sha256(json.dumps(words[1:]).encode("utf-8")).hexdigest()
    return folder / "approved" / digest


def approve(folder: Path, words: list[str]) -> None:
    """A command that code of the batch framed with folders of the container machine: the container command of
    the batch passes that exact command once. Folders of the machine are never named to the container command."""
    place = _approval(folder, words)
    place.parent.mkdir(mode=0o700, exist_ok=True)
    place.write_text("", encoding="utf-8")


# --- the batch ------------------------------------------------------------------------------------------------------

def _safe_root() -> Path:
    root = Path(ROOT)
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError as err:
        raise _refuse("root_unsafe", f"the frame's state folder cannot be made: {err}") from None
    info = os.lstat(root)
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise _refuse("root_unsafe", f"the frame's state folder {root} is a link, not a folder, not this user's, or "
                                     "open to others")
    (root / "runs").mkdir(mode=0o700, exist_ok=True)
    return root


def _read_state(folder: Path) -> dict | None:
    try:
        state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _write_state(folder: Path, state: dict) -> None:
    part = folder / "state.json.part"
    part.write_text(json.dumps(state), encoding="utf-8")
    part.replace(folder / "state.json")


def _docker(args: list[str]) -> subprocess.CompletedProcess | None:
    return _run(["docker", *args])


def _label_of(ident: str) -> str | None:
    done = _docker(["inspect", "--format", '{{index .Config.Labels "' + LABEL + '"}}', ident])
    return done.stdout.strip() if done is not None and done.returncode == 0 else None


def _say(said: dict) -> None:
    try:
        print(json.dumps({"frame": said}), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def collect(folder: Path, run_id: str) -> bool:
    """Remove the containers and named volumes of one batch: the ids its files hold and the ids listed by its
    exact mark, each confirmed to carry that mark, removed by id; volumes by name, also after their mark is
    confirmed. True when nothing of the batch is left that could not be removed."""
    cids = folder / "cids"
    named = [p.read_text(encoding="utf-8").strip() for p in sorted(cids.glob("*"))] if cids.is_dir() else []
    volumes = [p.name for p in sorted((folder / "volumes").glob("*"))] if (folder / "volumes").is_dir() else []
    state = _read_state(folder) or {}
    if not named and not volumes and not state.get("made"):
        return True
    listed = _docker(["ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={LABEL}={run_id}"])
    if listed is None or listed.returncode != 0:
        _say({"collect": "the container command did not answer", "run": run_id})
        return False
    ids = sorted({i for i in named + listed.stdout.split() if ID_RE.fullmatch(i)})
    own = [ident for ident in ids if _label_of(ident) == run_id]
    whole = True
    if own:
        done = _docker(["rm", "--force", *own])
        whole = done is not None and done.returncode == 0
    for name in volumes:
        done = _docker(["volume", "inspect", "--format", '{{index .Labels "' + LABEL + '"}}', name])
        if done is not None and done.returncode == 0 and done.stdout.strip() == run_id:
            _docker(["volume", "rm", name])
    return whole


def end_group(pgid: int | None, leader_started: str | None, grace: float | None = None) -> None:
    """End the programs of one process group: asked first, then made to. A group whose leader is alive and is
    another program than the one recorded is left alone."""
    if not pgid or pgid <= 1:
        return
    leader = started_at(pgid)
    if leader is not None and leader_started is not None and leader != leader_started:
        return
    for sig, wait in ((signal.SIGTERM, float(SETTINGS["end_seconds"]) if grace is None else grace), (signal.SIGKILL, 2)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.05)


def clean_earlier(root: Path, own: str | None = None) -> None:
    """What earlier batches whose process is dead left: their programs, their containers, their folder."""
    for folder in sorted((root / "runs").glob("*")):
        if folder.name == own or not RUN_RE.fullmatch(folder.name):
            continue
        state = _read_state(folder)
        if state is None:
            if time.time() - folder.stat().st_mtime > 3600:
                shutil.rmtree(folder, ignore_errors=True)
            continue
        if _alive(state):
            continue
        end_group(state.get("pgid"), state.get("child_started"))
        if collect(folder, folder.name):
            shutil.rmtree(folder, ignore_errors=True)
            _say({"cleaned": folder.name})


def _shim_text(folder: Path, real: str | None) -> str:
    return (f"#!{sys.executable} -I\n"
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('dodam_frame_shim', {str(Path(__file__).resolve())!r})\n"
            "frame = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(frame)\n"
            f"sys.exit(frame.shim({str(folder)!r}, {real!r}, sys.argv[1:]))\n")


class Batch:
    """One batch: the lock, the readings, the mark, and its end."""

    def __init__(self, worktrees=(), shim: bool = False, on_stop=None):
        self.worktrees = [_work_tree(w) for w in worktrees]
        self.shim = shim
        self.on_stop = on_stop
        self.run_id = secrets.token_hex(16)
        self.folder = Path()
        self.tmp = Path()
        self.stopped: str | None = None
        self._refusing: str | None = None
        self.start_free: int | None = None
        self._fd: int | None = None
        self._ended = threading.Event()
        self._watch: threading.Thread | None = None
        self._closed = False

    # opening

    def _lock(self, root: Path) -> None:
        fd = os.open(root / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + float(SETTINGS["lock_wait_seconds"])
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    try:
                        holder = json.loads((root / "holder").read_text(encoding="utf-8")).get("pid")
                    except (OSError, ValueError, AttributeError):
                        holder = None
                    os.close(fd)
                    raise _refuse("lock_held", f"another batch holds the frame's lock: process {holder}",
                                  holder=holder) from None
                time.sleep(0.1)
        self._fd = fd
        (root / "holder").write_text(json.dumps({"pid": os.getpid(), "run": self.run_id}), encoding="utf-8")

    def open(self) -> "Batch":
        root = _safe_root()
        self._lock(root)
        try:
            clean_earlier(root)
            if PLATFORM == "darwin":
                read = readings()
                reason = crossed(read, None)
                if reason:
                    raise _refuse(reason, MESSAGES[reason], readings=read)
                self.start_free = read["free_disk"]
            self.folder = root / "runs" / self.run_id
            self.folder.mkdir(mode=0o700)
            _write_state(self.folder, {"run": self.run_id, "pid": os.getpid(), "started": started_at(os.getpid()),
                                       "worktrees": [str(w) for w in self.worktrees], "made": False})
            for name in ("cids", "tmp", "volumes", "bin"):
                (self.folder / name).mkdir(mode=0o700)
            self.tmp = self.folder / "tmp"
            if self.shim:
                real = shutil.which("docker")
                (self.folder / "bin" / "docker").write_text(_shim_text(self.folder, real), encoding="utf-8")
                (self.folder / "bin" / "docker").chmod(0o700)
            if PLATFORM == "darwin":
                self._watch = threading.Thread(target=self._measure, name="dodam-frame-readings", daemon=True)
                self._watch.start()
        except BaseException:
            self._release()
            if self.folder != Path() and self.folder.is_dir():
                shutil.rmtree(self.folder, ignore_errors=True)
            raise
        return self

    def __enter__(self) -> "Batch":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # while it runs

    def _measure(self) -> None:
        while not self._ended.wait(float(SETTINGS["every_seconds"])):
            read = readings()
            reason = crossed(read, self.start_free)
            if reason and not self._ended.is_set():
                self.stop(reason, read)
                return

    def stop(self, reason: str, read: dict | None = None) -> None:
        """Past a line: remove the batch's containers, start no more, and tell whoever runs the batch."""
        self._refusing = reason
        state = _read_state(self.folder)
        if state is not None:
            state["stopped"] = reason  # the container command of the batch's programs refuses from now on
            _write_state(self.folder, state)
        _say({"stopped": reason, "message": MESSAGES.get(reason, reason), "readings": read, "run": self.run_id})
        collect(self.folder, self.run_id)
        if self.on_stop is not None:
            self.on_stop(reason)
        self.stopped = reason

    def container(self, cmd, allow=(), machine=()) -> list[str]:
        self._refuse_if_stopped()
        allowed = [Path(os.path.realpath(self.tmp))] + self.worktrees + [_work_tree(p) for p in allow]
        words = frame_words(list(cmd), self.run_id, self.folder / "cids", allowed, tuple(machine))
        if words != list(cmd):
            self._made()
        if machine:
            approve(self.folder, words)
        return words

    def _made(self) -> None:
        state = _read_state(self.folder)
        if state is not None and not state.get("made"):
            state["made"] = True
            _write_state(self.folder, state)

    def _refuse_if_stopped(self) -> None:
        reason = getattr(self, "_refusing", None)
        if reason:
            raise FrameStopped({"refused": True, "reason": reason, "message": MESSAGES.get(reason, "")})

    def volume(self, name: str) -> list[str]:
        self._refuse_if_stopped()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise _refuse("option", f"{name!r} is not a name of a volume")
        (self.folder / "volumes" / name).write_text("", encoding="utf-8")
        return ["docker", "volume", "create", "--label", f"{LABEL}={self.run_id}", name]

    def env(self) -> dict[str, str]:
        """What the programs of the batch are given: the batch's run, its temporary folder and, with the
        container command, that first on the path."""
        env = {"DODAM_FRAME_RUN": self.run_id, "TMPDIR": str(self.tmp)}
        if (self.folder / "bin" / "docker").exists():
            env["PATH"] = f"{self.folder / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
        return env

    def record_group(self, pgid: int) -> None:
        state = _read_state(self.folder) or {}
        state.update(pgid=pgid, child_started=started_at(pgid))
        _write_state(self.folder, state)

    # its end

    def _release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def close(self) -> None:
        if self._closed or self._fd is None:
            return
        self._closed = True
        self._ended.set()
        try:
            state = _read_state(self.folder) or {}
            made = any((self.folder / "cids").glob("*"))
            end_group(state.get("pgid"), state.get("child_started"))
            whole = collect(self.folder, self.run_id)
            shutil.rmtree(self.tmp, ignore_errors=True)
            if whole:
                shutil.rmtree(self.folder, ignore_errors=True)
            if made:
                give_space_back()
        finally:
            self._release()


def give_space_back() -> None:
    """Trim the disk of the container machine, on this computer only, when the machine runs and no container
    at all runs on it. Never more: no pruning, no image removed, the machine neither started nor stopped."""
    if PLATFORM != "darwin":
        return
    status = _run(["colima", "status"], seconds=SETTINGS["trim_seconds"])
    if status is None or status.returncode != 0:
        return
    running = _docker(["ps", "--quiet"])
    if running is None or running.returncode != 0 or running.stdout.strip():
        return
    done = _run(["colima", "ssh", "--", "sudo", "fstrim", "-a"], seconds=SETTINGS["trim_seconds"])
    if done is None or done.returncode != 0:
        _say({"trim": "the container machine's disk was not trimmed"})


class Joined:
    """A process inside a frame run: its containers carry that run's mark and are collected by that run."""

    def __init__(self, folder: Path, state: dict):
        self.folder = folder
        self.run_id = folder.name
        self.tmp = folder / "tmp"
        self.worktrees = [Path(w) for w in state.get("worktrees", [])]
        self.stopped = None

    def _refuse_if_stopped(self) -> None:
        reason = (_read_state(self.folder) or {}).get("stopped")
        if reason:
            raise FrameStopped({"refused": True, "reason": reason, "message": MESSAGES.get(reason, "")})

    def container(self, cmd, allow=(), machine=()) -> list[str]:
        self._refuse_if_stopped()
        allowed = [Path(os.path.realpath(self.tmp))] + self.worktrees + [_work_tree(p) for p in allow]
        words = frame_words(list(cmd), self.run_id, self.folder / "cids", allowed, tuple(machine))
        if words != list(cmd):
            Batch._made(self)  # type: ignore[arg-type]
        if machine:
            approve(self.folder, words)
        return words

    def volume(self, name: str) -> list[str]:
        return Batch.volume(self, name)  # type: ignore[arg-type]

    def env(self) -> dict[str, str]:
        return Batch.env(self)  # type: ignore[arg-type]

    def close(self) -> None:
        pass


# --- the batch of a process ---------------------------------------------------------------------------------------

_current: Batch | Joined | None = None
_guard = threading.Lock()


def _outer() -> Joined | None:
    run = os.environ.get("DODAM_FRAME_RUN", "")
    if not RUN_RE.fullmatch(run):
        return None
    root = _safe_root()
    folder = root / "runs" / run
    state = _read_state(folder)
    if state is None or not _alive(state) or state.get("pid") not in _ancestors() | {os.getpid()}:
        return None
    return Joined(folder, state)


def _interrupt(reason: str) -> None:
    import _thread
    _thread.interrupt_main()


def _on_term(signum, frame_):
    raise SystemExit(128 + signum)


def current() -> Batch | Joined | None:
    return _current


def _process_batch(**options) -> Batch | Joined:
    global _current
    with _guard:
        if _current is None:
            joined = _outer()
            if joined is not None:
                _current = joined
            else:
                _current = Batch(on_stop=options.pop("on_stop", _interrupt), **options).open()
                atexit.register(close)
                if threading.current_thread() is threading.main_thread() and \
                        signal.getsignal(signal.SIGTERM) == signal.SIG_DFL:
                    signal.signal(signal.SIGTERM, _on_term)
        return _current


def container(cmd, allow=(), machine=()) -> list[str]:
    """The command with the frame's words, in this process's batch (opened at the first container). `allow`
    names work trees that may be mounted; `machine` names, exactly, folders of the container machine."""
    return _process_batch().container(cmd, allow=allow, machine=machine)


def volume(name: str) -> list[str]:
    """The command that makes a named volume with the batch's mark; the batch removes it at its end."""
    return _process_batch().volume(name)


def close() -> None:
    """End this process's batch, if it has one of its own."""
    global _current
    with _guard:
        held, _current = _current, None
    if held is not None:
        held.close()


@contextlib.contextmanager
def batch(**options):
    """A batch around a block of work: the batch already open in this process, the frame run this process is
    in, or a batch of its own that ends with the block."""
    global _current
    with _guard:
        held = _current
    if held is not None:
        yield held
        return
    joined = _outer()
    if joined is not None:
        yield joined
        return
    made = Batch(**options).open()
    with _guard:
        _current = made
    try:
        yield made
    finally:
        with _guard:
            if _current is made:
                _current = None
        made.close()


# --- the container command of a frame run ------------------------------------------------------------------------

def _shim_refuse(reason: str, message: str) -> int:
    print(json.dumps({"refused": True, "reason": reason, "message": message}), file=sys.stderr)
    return 2


def _targets(words: list[str]) -> list[str]:
    valued = {"-t", "--time", "-s", "--signal", "--format", "-f"}
    found, i = [], 0
    while i < len(words):
        word = words[i]
        if word.startswith("-"):
            i += 2 if word in valued else 1
            continue
        found.append(word)
        i += 1
    return found


def shim(folder: str, real: str | None, args: list[str]) -> int:
    """The container command that a frame run puts first on the path of its programs."""
    folder_path = Path(folder)
    state = _read_state(folder_path)
    if state is None or real is None:
        return _shim_refuse("no_batch", "the frame's batch or the container command is not there")
    run_id = folder_path.name
    cmd = [real, *args]
    try:
        at, sub = _subcommand(cmd)
    except FrameRefused as refused:
        return _shim_refuse(refused.said["reason"], refused.said["message"])
    words = cmd[at - 1:]
    second = cmd[at] if at < len(cmd) else ""
    if sub == "prune" or second == "prune":
        return _shim_refuse("prune", "nothing is ever pruned: the frame removes its own containers by their ids")
    if sub == "rmi" or (sub == "image" and second in ("rm", "remove")):
        return _shim_refuse("image_removal", "images are never removed")
    if sub in ("run", "create"):
        joined = Joined(folder_path, state)
        approved = _approval(folder_path, cmd)
        try:
            if approved.exists():
                approved.unlink()  # framed by code of the batch with folders of the machine: passed once
            else:
                cmd = joined.container(cmd)
        except FrameRefused as refused:
            return _shim_refuse(refused.said["reason"], refused.said["message"])
    elif sub in ("rm", "kill", "stop", "restart", "pause", "unpause", "update"):
        for target in _targets(words[1:]):
            done = _run([real, "inspect", "--format", '{{index .Config.Labels "' + LABEL + '"}}', target])
            if done is None or done.returncode != 0 or done.stdout.strip() != run_id:
                return _shim_refuse("not_own", f"{target} is not a container of this batch")
    elif sub == "volume" and second in ("rm", "remove"):
        for target in _targets(cmd[at + 1:]):
            done = _run([real, "volume", "inspect", "--format", '{{index .Labels "' + LABEL + '"}}', target])
            if done is None or done.returncode != 0 or done.stdout.strip() != run_id:
                return _shim_refuse("not_own", f"{target} is not a volume of this batch")
    os.execv(real, cmd)
    return 0


# --- the command --------------------------------------------------------------------------------------------------

def run(command: list[str], worktrees: list[str]) -> int:
    child: subprocess.Popen | None = None

    def stop_child(reason: str) -> None:
        if child is not None:
            end_group(child.pid, None)

    try:
        held = Batch(worktrees=worktrees, shim=True, on_stop=stop_child).open()
    except FrameRefused as refused:
        print(json.dumps(refused.said), file=sys.stderr)
        return 2
    stop = {"signal": None}

    def forward(signum, frame_):
        stop["signal"] = signum
        if child is not None:
            end_group(child.pid, None, grace=float(SETTINGS["end_seconds"]))

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)
    code = 1
    try:
        try:
            child = subprocess.Popen(command, env={**os.environ, **held.env()}, start_new_session=True)
        except OSError as err:
            print(json.dumps({"refused": True, "reason": "command", "message": str(err)}), file=sys.stderr)
            return 2
        held.record_group(child.pid)
        code = child.wait()
    finally:
        held.close()
    if held.stopped:
        print(json.dumps({"stopped": held.stopped, "message": MESSAGES.get(held.stopped, "")}), file=sys.stderr)
        return 3
    if stop["signal"] is not None:
        return 128 + stop["signal"]
    return code if code >= 0 else 128 - code


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "run" or "--" not in argv:
        print(json.dumps({"refused": True, "reason": "usage",
                          "message": "python -m dodambench.frame run [--worktree PATH]... -- COMMAND..."}),
              file=sys.stderr)
        return 2
    ours, command = argv[1:argv.index("--")], argv[argv.index("--") + 1:]
    worktrees, i = [], 0
    while i < len(ours):
        if ours[i] == "--worktree" and i + 1 < len(ours):
            worktrees.append(ours[i + 1])
            i += 2
        else:
            print(json.dumps({"refused": True, "reason": "usage", "message": f"unknown option {ours[i]}"}),
                  file=sys.stderr)
            return 2
    if not command:
        print(json.dumps({"refused": True, "reason": "usage", "message": "no command"}), file=sys.stderr)
        return 2
    try:
        return run(command, worktrees)
    except FrameRefused as refused:
        print(json.dumps(refused.said), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
