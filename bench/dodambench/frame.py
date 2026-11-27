"""The frame through which every container on this computer is started.

    python -m dodambench.frame run [--worktree PATH]... [--refuse-all] -- COMMAND...

runs COMMAND as one batch. In code, every command for the container tool goes through `run` or `popen`:

    frame.run(["docker", "create", ...], capture_output=True, check=True)

The frame enforces in one place. It reads the words into a structure, checks the structure against a closed
table of what the repository's own commands use, writes the command anew from the structure, and starts the
container tool itself: from a fixed path for each platform, with an environment from which every variable that
would point it at another daemon, context, configuration or certificate is removed. Whatever the table does not
have is refused, with a diagnostic in JSON, before anything reaches the tool.

What the frame does for a batch:
- before it starts, it reads free disk, memory pressure and fseventsd's memory, and refuses under a line;
- it holds a lock, so that one batch runs at a time: a batch that waits is told after a minute which process
  holds it, and is refused;
- every container it makes carries the batch's mark (a label with the batch's run id) and writes its id to a
  file of the batch; a volume it makes carries the mark too;
- a command that names a container or a volume (remove, stop, start, look at, copy) passes only when every object
  it names carries this batch's mark;
- every two minutes it reads again, and past a line it starts no more, tells whoever runs the batch, which stops
  its programs, and removes its containers;
- at its end, also after a failure or a signal, it ends the programs it started, removes exactly its containers,
  by their ids, and the volumes it made, and its temporary folder; a batch that was killed is cleaned up so by the
  next one, once its process is dead;
- what may be mounted or copied from this computer is the batch's own temporary folder, and a folder named on the
  call in code: one inside a git work tree, or one below the temporary area (never the temporary area itself);
- at the end it gives space back by trimming the disk of the container machine, only when the batch made a
  container and no container at all runs. It never prunes, never removes an image, and never starts or stops the
  container machine.

The command that a frame run puts first on the path of its programs goes through the same reading and checking,
without what only code may name (folders named on a call, folders of the container machine). In the automatic
check it is put there in a mode that refuses every call, so that a start in any spelling the frame's code does not
make is refused when it runs. That command is a guard for the project's sessions and their ordinary commands, not a
boundary: a program that calls the real tool by its full path is outside it.

Where it runs is told by the platform. The readings belong to this computer (macOS); elsewhere, as in the
automatic check, they are not made, and everything else is the same.

A process that gives a container command without an outer batch opens one of its own at its first command and keeps
it until it ends. A process inside a `frame run` joins that batch: the run must still be alive and be one of the
process's own ancestors, so that naming a run in the environment lets no other process in.

The one exception is the pilot: Harbor starts its containers through compose, from a fixed file the project's
converter writes, and that does not go through the table. The pilot's batch gives it the lock, the readings with the
stop, the removal of Harbor's containers while the lock is held, and giving space back at its end.

Only the standard library is used: the Gemini job loads this file by its path."""
from __future__ import annotations

import atexit
import contextlib
import fcntl
import json
import os
import posixpath
import pwd
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
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
    "command_seconds": 60,     # the bound of time for one call the frame makes itself
}
PENDING = ("pressure_level", "swap_used_gb", "fseventsd_gb")

RUN_RE = re.compile(r"[0-9a-f]{32}")
ID_RE = re.compile(r"[0-9a-f]{64}")


# --- the tools, at fixed paths --------------------------------------------------------------------------------------

# One fixed list of places for each platform; the first that holds the tool is taken. Nothing is looked up on the
# path, so that a folder put first on it cannot answer for the tool or for a reading.
CANDIDATES = {
    "darwin": {"docker": ("/opt/homebrew/bin/docker", "/usr/local/bin/docker"),
               "colima": ("/opt/homebrew/bin/colima", "/usr/local/bin/colima"),
               "sysctl": ("/usr/sbin/sysctl",), "pgrep": ("/usr/bin/pgrep",), "top": ("/usr/bin/top",),
               "ps": ("/bin/ps",)},
    "linux": {"docker": ("/usr/bin/docker",), "colima": (), "sysctl": ("/usr/sbin/sysctl", "/sbin/sysctl"),
              "pgrep": ("/usr/bin/pgrep",), "top": ("/usr/bin/top",), "ps": ("/usr/bin/ps", "/bin/ps")},
}


def _fixed_tools() -> dict[str, str]:
    places = CANDIDATES.get(sys.platform, CANDIDATES["linux"])
    return {name: next((p for p in paths if os.access(p, os.X_OK)), paths[0] if paths else "")
            for name, paths in places.items()}


TOOLS: dict[str, str] = _fixed_tools()


def use_tools(**paths: str) -> dict[str, str]:
    """Give the frame other tools by name (for tests: stand-ins); returns what it had, to give back."""
    before = dict(TOOLS)
    TOOLS.clear()
    TOOLS.update(_fixed_tools())
    TOOLS.update(paths)
    return before


def home() -> Path:
    """The account's home folder, from the account's record, not from the environment."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


# variables that would point the container tool at another daemon, context, configuration folder or certificates
STEERING = ("DOCKER_", "BUILDX_", "COMPOSE_")


def tool_env(env: dict | None = None) -> dict[str, str]:
    given = dict(os.environ if env is None else env)
    return {k: v for k, v in given.items() if not k.startswith(STEERING)}


class FrameRefused(Exception):
    """A batch or a command the frame does not start. `said` is the diagnostic, JSON first."""

    def __init__(self, said: dict):
        super().__init__(json.dumps(said))
        self.said = said


class FrameStopped(FrameRefused):
    """The batch stopped itself at a line and starts no more containers."""


def _refuse(reason: str, message: str, **more) -> FrameRefused:
    return FrameRefused({"refused": True, "reason": reason, "message": message, **more})


def _run(tool: str, args: list[str], seconds: float | None = None) -> subprocess.CompletedProcess | None:
    """One of the frame's own calls of a tool at its fixed path, or None when it is not there or did not end."""
    program = TOOLS.get(tool) or ""
    if not program or not os.access(program, os.X_OK):
        return None
    try:
        return subprocess.run([program, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=seconds or SETTINGS["command_seconds"], env=tool_env())
    except (OSError, subprocess.SubprocessError):
        return None


def _said(tool: str, args: list[str], seconds: float | None = None) -> str | None:
    done = _run(tool, args, seconds)
    return done.stdout if done is not None and done.returncode == 0 else None


def started_at(pid: int) -> str | None:
    """When the process started, as `ps` tells it; with its number, it names one process and no later one."""
    said = _said("ps", ["-o", "lstart=", "-p", str(pid)])
    return said.strip() or None if said else None


def _exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _ancestors() -> set[int]:
    seen, pid = set(), os.getpid()
    for _ in range(64):
        said = _said("ps", ["-o", "ppid=", "-p", str(pid)])
        try:
            pid = int(said.strip()) if said else 0
        except ValueError:
            pid = 0
        if pid <= 1:
            break
        seen.add(pid)
    return seen


def _alive(state: dict) -> bool:
    """A batch's process lives: its number is in use, and by the process that started when the record says. A
    record whose start could not be read counts as alive only while its number is in use."""
    pid = state.get("pid")
    if not isinstance(pid, int) or pid <= 0 or not _exists(pid):
        return False
    recorded = state.get("started")
    return recorded is None or started_at(pid) == recorded


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
    said = _said("sysctl", ["-n", "kern.memorystatus_vm_pressure_level"])
    try:
        return int(said.strip())
    except (AttributeError, ValueError):
        raise _ReadingFailed("pressure") from None


def _read_swap_used() -> int:
    said = _said("sysctl", ["-n", "vm.swapusage"]) or ""
    found = re.search(r"used = (\d+(?:\.\d+)?)([KMG])", said)
    if not found:
        raise _ReadingFailed("swap")
    return int(float(found.group(1)) * TOP_UNITS[found.group(2)])


def _read_fseventsd() -> int | None:
    """The largest memory of the processes named fseventsd, by top; None when there is none."""
    done = _run("pgrep", ["-x", "fseventsd"])
    if done is None or done.returncode not in (0, 1):
        raise _ReadingFailed("fseventsd")
    pids = done.stdout.split()
    if not pids:
        return None
    figures = []
    for pid in pids:
        said = _said("top", ["-l", "1", "-pid", pid, "-stats", "mem"])
        lines = [line for line in (said or "").splitlines() if line.strip()]
        figure = read_top_memory(lines[-1]) if lines else None
        if figure is None:
            raise _ReadingFailed("fseventsd")
        figures.append(figure)
    return max(figures)


def _read_once() -> dict:
    readings = {"free_disk": free_disk(home()), "pressure": _read_pressure(), "fseventsd": _read_fseventsd()}
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


# --- the closed table ------------------------------------------------------------------------------------------------

NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
ABSOLUTE = r"/[^\x00-\x1f\x7f]*"
IMAGE = r"[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?"

# The options of `run` and `create`: the name the frame writes, the names it reads for it, and the shape of the value
# (None: an option without a value). What is not here is refused; so is a value of another shape.
START_OPTIONS = {
    "--name": (("--name",), NAME),
    "--network": (("--network",), r"none"),
    "--user": (("--user",), r"0"),
    "--memory": (("--memory",), r"\d{1,6}[mg]"),
    "--memory-swap": (("--memory-swap",), r"\d{1,6}[mg]"),
    "--cpus": (("--cpus",), r"\d{1,3}(?:\.\d{1,3})?"),
    "--pids-limit": (("--pids-limit",), r"\d{1,7}"),
    "--tmpfs": (("--tmpfs",), r"/[A-Za-z0-9_./-]*(?::[a-z0-9=,_]+)?"),
    "--env": (("--env", "-e"), r"[A-Z_][A-Z0-9_]*=[^\n\r\x00]*"),
    "--volume": (("--volume", "-v"), r"[^:\s\x00]+:/[^:\s\x00]*(?::(?:ro|rw))?"),
    "--entrypoint": (("--entrypoint",), ABSOLUTE),
    "--security-opt": (("--security-opt",), r"seccomp=unconfined"),
    "--cap-add": (("--cap-add",), r"LINUX_IMMUTABLE"),
    "--label": (("--label",), r"(?!dodam\.frame)[A-Za-z0-9._-]+=[A-Za-z0-9._:/=-]*"),
    "--rm": (("--rm",), None),
    "--detach": (("--detach",), None),
}
READ_AS = {given: written for written, (names, _) in START_OPTIONS.items() for given in names}
ONCE = {"--name", "--network", "--user", "--memory", "--memory-swap", "--cpus", "--pids-limit", "--entrypoint",
        "--rm", "--detach"}
INFO_FORMATS = ("{{.MemTotal}}", "{{.CgroupDriver}}")
COMMANDS = ("run", "create", "start", "rm", "stop", "inspect", "cp", "ps", "info", "image inspect", "volume create",
            "volume rm")


def _value(option: str, value: str, shape: str) -> None:
    if not re.fullmatch(shape, value):
        raise _refuse("value", f"the frame does not pass {option} with the value {value!r}")


def read_command(words) -> dict:
    """The structure of a container command, from its words; refused when the table does not have it."""
    words = list(words)
    if not words or words[0] != "docker":
        raise _refuse("command", "a container command begins with the word docker")
    rest = words[1:]
    if rest and rest[0].startswith("-"):
        raise _refuse("daemon_option", f"the frame does not pass {rest[0]}: the container tool talks to the container "
                                       "machine of this computer only, with its own settings")
    head = " ".join(rest[:2]) if rest[:1] in (["image"], ["volume"]) else (rest[0] if rest else "")
    if head not in COMMANDS:
        raise _refuse("command", f"the frame does not pass the command {' '.join(rest[:2]) or '(none)'}")
    args = rest[len(head.split()):]
    if head in ("run", "create"):
        return _read_start(head, args)
    return _read_other(head, args)


def _read_start(head: str, args: list[str]) -> dict:
    options, i = [], 0
    while i < len(args) and args[i].startswith("-"):
        given = args[i]
        if given not in READ_AS:
            raise _refuse("option", f"the frame does not pass the option {given} in this form")
        written = READ_AS[given]
        shape = START_OPTIONS[written][1]
        if shape is None:
            options.append((written, None))
            i += 1
            continue
        if i + 1 >= len(args):
            raise _refuse("option", f"{given} has no value")
        _value(written, args[i + 1], shape)
        options.append((written, args[i + 1]))
        i += 2
    names = [name for name, _ in options]
    twice = sorted({n for n in names if n in ONCE and names.count(n) > 1})
    if twice:
        raise _refuse("option", f"the frame does not pass {twice[0]} twice")
    if i >= len(args):
        raise _refuse("value", "no image is named")
    _value("the image", args[i], IMAGE)
    return {"command": head, "options": options, "image": args[i], "args": args[i + 1:]}


def _flags_then(head: str, args: list[str], allowed: dict, at_least: int, at_most: int | None) -> tuple[list, list]:
    options, i = [], 0
    while i < len(args) and args[i].startswith("-") and args[i] != "--":
        given = args[i]
        if given not in allowed:
            raise _refuse("option", f"the frame does not pass {given} with {head}")
        shape = allowed[given]
        if shape is None:
            options.append((given, None))
            i += 1
        else:
            if i + 1 >= len(args):
                raise _refuse("option", f"{given} has no value")
            _value(given, args[i + 1], shape)
            options.append((given, args[i + 1]))
            i += 2
    if i < len(args) and args[i] == "--":
        i += 1
    rest = args[i:]
    if len(rest) < at_least or (at_most is not None and len(rest) > at_most):
        raise _refuse("value", f"{head} is not given that many names here")
    return options, rest


def _read_other(head: str, args: list[str]) -> dict:
    if head == "start":
        options, rest = _flags_then(head, args, {"--attach": None}, 1, 1)
        _names(rest)
        return {"command": head, "options": options, "names": rest}
    if head in ("rm", "stop"):
        allowed = {"--force": None, "-f": None} if head == "rm" else {}
        options, rest = _flags_then(head, args, allowed, 1, None)
        _names(rest)
        return {"command": head, "options": [("--force", None)] if options else [], "names": rest}
    if head == "inspect":
        options, rest = _flags_then(head, args, {"--type": r"container", "--format": r"\{\{\.Id\}\}"}, 1, 1)
        _names(rest)
        return {"command": head, "options": [], "names": rest}
    if head == "cp":
        _flags_then(head, args, {}, 2, 2)
        return {"command": head, "options": [], "from": args[0], "to": args[1]}
    if head == "ps":
        options, _ = _flags_then(head, args, {"--all": None, "-a": None, "--quiet": None, "-q": None,
                                              "--no-trunc": None,
                                              "--filter": r"label=[A-Za-z0-9._-]+(?:=[A-Za-z0-9._-]+)?"}, 0, 0)
        long = {"-a": "--all", "-q": "--quiet"}
        return {"command": head, "options": [(long.get(n, n), v) for n, v in options]}
    if head == "info":
        options, _ = _flags_then(head, args, {"--format": "|".join(re.escape(f) for f in INFO_FORMATS)}, 0, 0)
        if not options:
            raise _refuse("option", "info is passed with one of its two formats only")
        return {"command": head, "options": options}
    if head == "image inspect":
        _, rest = _flags_then(head, args, {}, 1, 1)
        _value("the image", rest[0], IMAGE)
        return {"command": head, "options": [], "names": rest}
    if head == "volume create":
        _, rest = _flags_then(head, args, {}, 1, 1)
        _names(rest)
        return {"command": head, "options": [], "names": rest}
    if head == "volume rm":
        options, rest = _flags_then(head, args, {"--force": None, "-f": None}, 1, None)
        _names(rest)
        return {"command": head, "options": [("--force", None)] if options else [], "names": rest}
    raise _refuse("command", f"the frame does not pass the command {head}")


def _names(names: list[str]) -> None:
    for name in names:
        if not re.fullmatch(NAME, name):
            raise _refuse("value", f"{name!r} is not a name the frame passes")


def write_command(structure: dict) -> list[str]:
    """The words of the command, written from its structure (without the program)."""
    head = structure["command"]
    if head == "inspect":
        return ["inspect", "--type", "container", "--format", "{{.Id}}", "--", *structure["names"]]
    words = head.split()
    for name, value in structure.get("options", []):
        words += [name] if value is None else [name, value]
    if head in ("run", "create"):
        return words + [structure["image"], *structure["args"]]
    if head == "cp":
        return words + [structure["from"], structure["to"]]
    if "names" in structure:
        return words + (["--"] if head in ("rm", "stop", "volume rm") else []) + structure["names"]
    return words


# --- what may be mounted or copied -----------------------------------------------------------------------------------

# Folders under the home folder that are never mounted or copied, nor a folder that holds one of them.
SENSITIVE = (".dodam", ".dodam-desk", ".ssh", ".gnupg", ".aws", ".config", ".docker", ".colima", ".claude",
             ".codex", ".gemini", ".kube", ".netrc", "Library", ".git-credentials")
# Beginnings of names that no folder named on a call may have on its way: other runs of the frame, the Gemini job's
# folders, other sessions' scratch, and the sensitive folders.
NEVER_ON_THE_WAY = ("dodam-frame", "dodam-gemini", "claude-", ".dodam", ".ssh", ".aws", ".gnupg", ".config",
                    "git-credentials")


def _within(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _temporary_roots() -> list[Path]:
    return sorted({Path(os.path.realpath(p)) for p in ("/tmp", "/private/tmp", tempfile.gettempdir())})


def named_folder(path) -> Path:
    """A folder named on a call in code: inside a git work tree, or below the temporary area; never the home
    folder, a folder above it, a sensitive folder, a temporary root, or a folder of another run, job or session."""
    real = Path(os.path.realpath(path))
    mine = home()
    if not real.is_dir() or real.stat().st_uid != os.getuid():
        raise _refuse("mount", f"{path} is not a folder of this account")
    if _within(mine, real) or any(_within(real, mine / name) for name in SENSITIVE):
        raise _refuse("mount", f"{path} is the home folder, above it, or a sensitive folder")
    if any(part.startswith(NEVER_ON_THE_WAY) for part in real.parts):
        raise _refuse("mount", f"{path} lies in a folder of another run, job or session, or a sensitive one")
    if _within(real, Path(os.path.realpath("/var/tmp"))):
        raise _refuse("mount", f"{path} lies below /var/tmp")
    roots = _temporary_roots()
    if real in roots:
        raise _refuse("mount", f"{path} is the temporary area itself")
    below_temporary = any(_within(real, root) for root in roots)
    in_work_tree = any((above / ".git").exists() and not _within(mine, above) for above in (real, *real.parents))
    if not (below_temporary or in_work_tree):
        raise _refuse("mount", f"{path} is neither inside a work tree nor below the temporary area")
    return real


def _host_path_allowed(path: str, allowed: list[Path]) -> Path:
    """A path of this computer that may be mounted or copied: in an allowed folder after its links are followed,
    not a socket, and in no sensitive folder."""
    real = Path(os.path.realpath(path))
    mine = home()
    try:
        if stat.S_ISSOCK(os.stat(real).st_mode):
            raise _refuse("mount", f"{path} is a socket")
    except OSError:
        pass
    if any(_within(real, mine / name) for name in SENSITIVE) or not any(_within(real, a) for a in allowed):
        raise _refuse("mount", f"the frame does not mount or copy {path}: only its own temporary folder and the "
                               "folders named on the call")
    return real


# The folders of the container machine that code may name for one container: a judged container's folder of the
# kernel's, and the machine's file for letting go of unused records.
MACHINE_GROUP = re.compile(r"/sys/fs/cgroup/(?:docker/([0-9a-f]{64})|system\.slice/docker-([0-9a-f]{64})\.scope)")
MACHINE_DROP = "/proc/sys/vm/" + "drop" + "_caches"


# --- the batch's state ------------------------------------------------------------------------------------------------

def default_root() -> Path:
    """One folder for every session of this user: not the temporary folder of a session, which may differ."""
    return Path("/tmp").resolve() / f"dodam-frame-{os.getuid()}"


ROOT = default_root()


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
    return _run("docker", args)


def _label_of(name: str) -> str | None:
    """The mark a container carries, asked of the tool as a container, never of any object of that name."""
    done = _docker(["inspect", "--type", "container", "--format", '{{index .Config.Labels "' + LABEL + '"}}', "--", name])
    return done.stdout.strip() if done is not None and done.returncode == 0 else None


def _volume_label_of(name: str) -> str | None:
    done = _docker(["volume", "inspect", "--format", '{{index .Labels "' + LABEL + '"}}', "--", name])
    return done.stdout.strip() if done is not None and done.returncode == 0 else None


def _say(said: dict) -> None:
    try:
        print(json.dumps({"frame": said}), file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def collect(folder: Path, run_id: str) -> bool:
    """Remove the containers and volumes of one batch: the ids its files hold and the ids listed by its exact
    mark, each confirmed to carry that mark, removed by id; volumes by name, also after their mark is confirmed.
    True only when nothing of the batch is left."""
    cids = folder / "cids"
    named = [p.read_text(encoding="utf-8").strip() for p in sorted(cids.glob("*"))] if cids.is_dir() else []
    volumes = [p.name for p in sorted((folder / "volumes").glob("*"))] if (folder / "volumes").is_dir() else []
    state = _read_state(folder) or {}
    if not named and not volumes and not state.get("made"):
        return True
    listed = _docker(["ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={LABEL}={run_id}"])
    if listed is None or listed.returncode != 0:
        _say({"collect": "the container tool did not answer", "run": run_id})
        return False
    ids = sorted({i for i in named + listed.stdout.split() if ID_RE.fullmatch(i)})
    own = [ident for ident in ids if _label_of(ident) == run_id]
    whole = True
    if own:
        done = _docker(["rm", "--force", "--", *own])
        whole = done is not None and done.returncode == 0
    for name in volumes:
        if _volume_label_of(name) == run_id:
            done = _docker(["volume", "rm", "--force", "--", name])
            whole = whole and done is not None and done.returncode == 0
    return whole


def end_group(pgid: int | None, leader_started: str | None, grace: float | None = None, own: bool = False) -> None:
    """End the programs of one process group: asked first, then made to. Only a group the caller started itself
    (`own`) or one whose leader lives and is the program recorded gets a signal."""
    if not pgid or pgid <= 1:
        return
    if not own:
        leader = started_at(pgid)
        if leader is None or leader_started is None or leader != leader_started:
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


def _shim_text(folder: Path, mode: str) -> str:
    return (f"#!{sys.executable} -I\n"
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('dodam_frame_shim', {str(Path(__file__).resolve())!r})\n"
            "frame = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(frame)\n"
            f"frame.use_tools(**{dict(TOOLS)!r})\n"
            f"sys.exit(frame.shim({str(folder)!r}, {mode!r}, sys.argv[1:]))\n")


class _Checks:
    """What a batch checks a command against: shared by a batch of its own and a batch joined from a frame run."""

    folder: Path
    run_id: str
    tmp: Path
    worktrees: list[Path]

    def _stopped_reason(self) -> str | None:
        return getattr(self, "_refusing", None) or (_read_state(self.folder) or {}).get("stopped")

    def _refuse_if_stopped(self) -> None:
        reason = self._stopped_reason()
        if reason:
            raise FrameStopped({"refused": True, "reason": reason, "message": MESSAGES.get(reason, reason)})

    def _made(self) -> None:
        state = _read_state(self.folder)
        if state is not None and not state.get("made"):
            state["made"] = True
            _write_state(self.folder, state)

    def made(self) -> None:
        """Note that the batch made a container by another way (the pilot's Harbor), so that space is given back."""
        state = _read_state(self.folder)
        if state is not None:
            state.update(made=True, by_other_way=True)
            _write_state(self.folder, state)

    def _own(self, name: str) -> bool:
        return _label_of(name) == self.run_id

    def checked(self, structure: dict, allow=(), machine=()) -> list[str]:
        """The words for the tool, from a structure the table has, once everything it names is the batch's."""
        self._refuse_if_stopped()
        own = Path(os.path.realpath(self.tmp))
        # a folder in the batch's own temporary folder is the batch's (a frame run gives its programs that folder as
        # their temporary area); any other named folder must be one the frame may be given
        allowed = [own] + list(self.worktrees) + [
            Path(os.path.realpath(p)) if _within(Path(os.path.realpath(p)), own) else named_folder(p) for p in allow]
        head = structure["command"]
        if head in ("run", "create"):
            self._check_start(structure, allowed, tuple(machine))
            cid = self.folder / "cids" / secrets.token_hex(8)
            self._made()
            words = write_command(structure)
            return words[:1] + ["--label", f"{LABEL}={self.run_id}", "--cidfile", str(cid)] + words[1:]
        if head in ("start", "rm", "stop", "inspect"):
            for name in structure["names"]:
                if not self._own(name):
                    raise _refuse("not_own", f"{name} is not a container of this batch")
        elif head == "cp":
            self._check_copy(structure, allowed)
        elif head == "volume create":
            [name] = structure["names"]
            (self.folder / "volumes" / name).write_text("", encoding="utf-8")
            return ["volume", "create", "--label", f"{LABEL}={self.run_id}", name]
        elif head == "volume rm":
            for name in structure["names"]:
                if not (self.folder / "volumes" / name).exists() or _volume_label_of(name) != self.run_id:
                    raise _refuse("not_own", f"{name} is not a volume of this batch")
        return write_command(structure)

    def _check_start(self, structure: dict, allowed: list[Path], machine: tuple) -> None:
        options = structure["options"]
        network = [v for n, v in options if n == "--network"]
        binds, given = [], []
        for name, value in options:
            if name != "--volume":
                continue
            source = value.split(":", 1)[0]
            if "/" not in source:
                if not (self.folder / "volumes" / source).exists():
                    raise _refuse("mount", f"the volume {source} was not made by this batch")
                continue
            if source in machine and posixpath.normpath(source) == source:
                group = MACHINE_GROUP.fullmatch(source)
                if source == MACHINE_DROP or (group and self._own(group.group(1) or group.group(2))):
                    given.append(source)
                    continue
                raise _refuse("mount", f"{source} is not a folder of the container machine that a container of this "
                                       "batch may be given")
            _host_path_allowed(source, allowed)
            binds.append(source)
        if given and (network != ["none"] or binds):
            raise _refuse("mount", "the folders of the container machine are given only to a container without "
                                   "network and without any folder of this computer")

    def _check_copy(self, structure: dict, allowed: list[Path]) -> None:
        sides = [structure["from"], structure["to"]]
        inside = [s for s in sides if not s.startswith(("/", ".")) and re.fullmatch(NAME + r":.*", s)]
        if len(inside) != 1:
            raise _refuse("value", "a copy goes between this computer and one container of this batch")
        name, _, path = inside[0].partition(":")
        if not re.fullmatch(ABSOLUTE, path):
            raise _refuse("value", f"the path in the container, {path!r}, is not absolute")
        if not self._own(name):
            raise _refuse("not_own", f"{name} is not a container of this batch")
        host = sides[1] if inside[0] == sides[0] else sides[0]
        keep_dot = host.endswith("/.")
        real = _host_path_allowed(host[:-2] if keep_dot else host, allowed)
        written = f"{real}/." if keep_dot else str(real)
        if host == sides[0]:
            structure["from"] = written
            # what lies in a folder copied from this computer, links followed, must stay in the allowed folders
            if real.is_dir():
                for below in real.rglob("*"):
                    if below.is_symlink():
                        _host_path_allowed(str(below), allowed)
        else:
            structure["to"] = written


class Batch(_Checks):
    """One batch: the lock, the readings, the mark, and its end."""

    def __init__(self, worktrees=(), shim: bool | str = False, on_stop=None):
        self.worktrees = [named_folder(w) for w in worktrees]
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
                mode = "refuse" if self.shim == "refuse" else "check"
                (self.folder / "bin" / "docker").write_text(_shim_text(self.folder, mode), encoding="utf-8")
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
        """Past a line: start no more, tell whoever runs the batch, which stops its programs, and remove the
        batch's containers."""
        self._refusing = reason
        state = _read_state(self.folder)
        if state is not None:
            state["stopped"] = reason
            _write_state(self.folder, state)
        _say({"stopped": reason, "message": MESSAGES.get(reason, reason), "readings": read, "run": self.run_id})
        if self.on_stop is not None:
            try:
                self.on_stop(reason)
            except Exception as exc:  # noqa: BLE001 - the stop goes on; what failed is said
                _say({"on_stop": f"{type(exc).__name__}: {exc}"})
        collect(self.folder, self.run_id)
        self.stopped = reason

    def env(self) -> dict[str, str]:
        """What the programs of the batch are given: the batch's run, its temporary folder and, with the
        container command, that first on the path."""
        env = {"DODAM_FRAME_RUN": self.run_id, "TMPDIR": str(self.tmp)}
        if (self.folder / "bin" / "docker").exists():
            env["PATH"] = f"{self.folder / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}"
        return env

    def record_group(self, pgid: int, own: bool = False) -> None:
        state = _read_state(self.folder) or {}
        state.update(pgid=pgid, child_started=started_at(pgid), own_group=own)
        _write_state(self.folder, state)

    def run(self, words, *, allow=(), machine=(), **kwargs) -> subprocess.CompletedProcess:
        return _start(self, words, allow, machine, subprocess.run, kwargs)

    def popen(self, words, *, allow=(), machine=(), **kwargs) -> subprocess.Popen:
        return _start(self, words, allow, machine, subprocess.Popen, kwargs)

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
            made = bool(state.get("by_other_way")) or any((self.folder / "cids").glob("*"))
            end_group(state.get("pgid"), state.get("child_started"), own=bool(state.get("own_group")))
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
    status = _run("colima", ["status"], seconds=SETTINGS["trim_seconds"])
    if status is None or status.returncode != 0:
        return
    running = _docker(["ps", "--quiet"])
    if running is None or running.returncode != 0 or running.stdout.strip():
        return
    done = _run("colima", ["ssh", "--", "sudo", "fstrim", "-a"], seconds=SETTINGS["trim_seconds"])
    if done is None or done.returncode != 0:
        _say({"trim": "the container machine's disk was not trimmed"})


class Joined(_Checks):
    """A process inside a frame run: its containers carry that run's mark and are collected by that run."""

    def __init__(self, folder: Path, state: dict):
        self.folder = folder
        self.run_id = folder.name
        self.tmp = folder / "tmp"
        self.worktrees = [Path(w) for w in state.get("worktrees", [])]
        self.stopped = None

    def env(self) -> dict[str, str]:
        return Batch.env(self)  # type: ignore[arg-type]

    def run(self, words, *, allow=(), machine=(), **kwargs) -> subprocess.CompletedProcess:
        return _start(self, words, allow, machine, subprocess.run, kwargs)

    def popen(self, words, *, allow=(), machine=(), **kwargs) -> subprocess.Popen:
        return _start(self, words, allow, machine, subprocess.Popen, kwargs)

    def close(self) -> None:
        pass


def _start(held: _Checks, words, allow, machine, starter, kwargs):
    structure = read_command(words)
    written = held.checked(structure, allow=allow, machine=machine)
    kwargs = dict(kwargs)
    kwargs["env"] = tool_env(kwargs.get("env"))
    program = TOOLS.get("docker") or ""
    if not program:
        raise _refuse("no_tool", "the container tool is not at its fixed place")
    return starter([program, *written], **kwargs)


# --- the batch of a process ---------------------------------------------------------------------------------------

_current: Batch | Joined | None = None
_guard = threading.RLock()


def _outer() -> Joined | None:
    run_id = os.environ.get("DODAM_FRAME_RUN", "")
    if not RUN_RE.fullmatch(run_id):
        return None
    root = _safe_root()
    folder = root / "runs" / run_id
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


def _process_batch() -> Batch | Joined:
    global _current
    with _guard:
        if _current is None:
            joined = _outer()
            if joined is not None:
                _current = joined
            else:
                _current = Batch(on_stop=_interrupt).open()
                atexit.register(close)
                if threading.current_thread() is threading.main_thread() and \
                        signal.getsignal(signal.SIGTERM) == signal.SIG_DFL:
                    signal.signal(signal.SIGTERM, _on_term)
        return _current


# Questions that make nothing and name nothing of a batch: asked without opening one, so that asking whether an
# image is there takes no lock.
QUESTIONS = ("image inspect", "info")


def run(words, *, allow=(), machine=(), **kwargs) -> subprocess.CompletedProcess:
    """Run a container command in this process's batch (opened at the first command): read, checked, written anew
    and started by the frame. `allow` names folders that may be mounted or copied; `machine` names, exactly, folders
    of the container machine for a container without network. Both only by keyword, only in code."""
    if _current is None and not allow and not machine:
        structure = read_command(words)
        if structure["command"] in QUESTIONS:
            kwargs = dict(kwargs)
            kwargs["env"] = tool_env(kwargs.get("env"))
            program = TOOLS.get("docker") or ""
            if not program:
                raise _refuse("no_tool", "the container tool is not at its fixed place")
            return subprocess.run([program, *write_command(structure)], **kwargs)
    return _process_batch().run(words, allow=allow, machine=machine, **kwargs)


def popen(words, *, allow=(), machine=(), **kwargs) -> subprocess.Popen:
    """The same, for a command whose end the caller waits for itself."""
    return _process_batch().popen(words, allow=allow, machine=machine, **kwargs)


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


def shim(folder: str, mode: str, args: list[str]) -> int:
    """The container command that a frame run puts first on the path of its programs: the same reading and checks
    as code gets, without folders named on a call or folders of the container machine. In the mode `refuse` it
    refuses every call."""
    if mode == "refuse":
        return _shim_refuse("refuse_all", "in the automatic check the container tool is reached only through the "
                                          "frame's code")
    folder_path = Path(folder)
    state = _read_state(folder_path)
    program = TOOLS.get("docker") or ""
    if state is None or not program:
        return _shim_refuse("no_batch", "the frame's batch or the container tool is not there")
    joined = Joined(folder_path, state)
    try:
        written = joined.checked(read_command(["docker", *args]))
    except FrameRefused as refused:
        return _shim_refuse(refused.said["reason"], refused.said["message"])
    os.execve(program, [program, *written], tool_env())
    return 0


# --- the command --------------------------------------------------------------------------------------------------

def run_command(command: list[str], worktrees: list[str], refuse_all: bool = False) -> int:
    child: subprocess.Popen | None = None
    stop = {"signal": None}

    def stop_child(reason: str) -> None:
        if child is not None:
            end_group(child.pid, None, own=True)

    def forward(signum, frame_):
        stop["signal"] = signum
        if child is not None:
            end_group(child.pid, None, grace=float(SETTINGS["end_seconds"]), own=True)

    # the signals are taken before the batch opens, so that none comes between the start of the command and them
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, forward)
    try:
        held = Batch(worktrees=worktrees, shim="refuse" if refuse_all else True, on_stop=stop_child).open()
    except FrameRefused as refused:
        print(json.dumps(refused.said), file=sys.stderr)
        return 2
    code = 1
    try:
        try:
            child = subprocess.Popen(command, env={**os.environ, **held.env()}, start_new_session=True)
        except OSError as err:
            print(json.dumps({"refused": True, "reason": "command", "message": str(err)}), file=sys.stderr)
            return 2
        held.record_group(child.pid, own=True)
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
    def usage(message: str) -> int:
        print(json.dumps({"refused": True, "reason": "usage", "message": message}), file=sys.stderr)
        return 2
    if not argv or argv[0] != "run" or "--" not in argv:
        return usage("python -m dodambench.frame run [--worktree PATH]... [--refuse-all] -- COMMAND...")
    ours, command = argv[1:argv.index("--")], argv[argv.index("--") + 1:]
    worktrees, refuse_all, i = [], False, 0
    while i < len(ours):
        if ours[i] == "--worktree" and i + 1 < len(ours):
            worktrees.append(ours[i + 1])
            i += 2
        elif ours[i] == "--refuse-all":
            refuse_all = True
            i += 1
        else:
            return usage(f"unknown option {ours[i]}")
    if not command:
        return usage("no command")
    try:
        return run_command(command, worktrees, refuse_all)
    except FrameRefused as refused:
        print(json.dumps(refused.said), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
