#!/usr/bin/env python3
"""Starts the desk's session, locked, in the terminal this command is run in.

    python3 tools/desk/start.py --coordinator ADDRESS [--state FOLDER] [--print]

The session it starts is Claude Code without its terminal interface, run by a relay (relay.py)
that shows in this terminal what the session does, passes typed lines to it as plain messages,
and runs the watcher. The session has nothing but the closed list in lock.json:

- the three tools that read files, confined to this copy of the repository;
- the desk's own tools (tools.py), and the tools of a live session when that way is open;
- no shell, no tool that writes a file, no tool that fetches from the web, no agents;
- none of the settings, instructions, hooks, plugins or connected services of the person
  whose computer this is;
- an environment with nothing in it but a handful of names;
- every tool call that is not on the list refused at once, with nobody asked.

The command refuses to start when anything it relies on is missing or different: the file of
facts, the standing instructions, the watcher, the pinned version of Claude Code, a clean copy
of the repository, a safe state folder. It changes nothing outside the state folder.

--coordinator is the one address the desk can send a message to. --state is the folder, outside
the repository, where the tools keep what they must remember and where the desk's own GitHub
token is read from (a file named github-token that only this account can read). --print shows
what would be started and starts nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COPY = HERE.parents[1]
sys.path.insert(0, str(HERE))

import facts as facts_file  # noqa: E402
import guard  # noqa: E402
import relay  # noqa: E402
import tools  # noqa: E402

DEFAULT_STATE = Path.home() / ".dodam-desk"
LIVE_SERVER_FILE = "live-server.json"
ADDRESS = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_:@.-]{0,119}$")
LOCK_KEYS = (
    "claude_version", "model", "repository", "builtin_tools", "desk_tools", "live_server", "live_tools",
    "live_tools_never", "labels", "labels_per_day", "answer", "read", "coordinator", "environment", "path",
)  # fmt: skip
# Named again here so that they stay refused even if a later change puts one back among the tools.
NEVER = ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task", "Skill"]


class Problem(Exception):
    """A reason not to start the desk."""


CHECK_SECONDS = 20
# Runs the check before a tool call so that every ending but a clean "allowed" refuses the call: a crash, a
# missing interpreter, a check that hangs. Claude Code blocks a call only on exit code 2, so everything becomes 2.
CHECK_WRAPPER = (
    # The call arrives on standard input. Some shells give a command in the background an empty input, so it is handed over by number.
    'exec 3<&0; "$@" <&3 3<&- & check=$!; exec 3<&-; (sleep {seconds}; kill -9 $check 2>/dev/null) >/dev/null 2>&1 & dog=$!; '
    "wait $check; code=$?; kill $dog 2>/dev/null; "
    '[ "$code" -eq 0 ] && exit 0; '
    "[ \"$code\" -eq 2 ] || echo \"The desk's lock could not be checked, so the call is refused.\" >&2; exit 2"
)


def check_command(interpreter: str, desk_folder: Path, known: Path, seconds: float = CHECK_SECONDS, shell: str = "/bin/sh") -> str:
    """The command Claude Code runs before every tool call of the session."""
    check = [interpreter, "-I", str(desk_folder / "guard.py"), "hook", "--lock", str(desk_folder / "lock.json"),
             "--facts", str(desk_folder / "desk.toml"), "--known", str(known)]  # fmt: skip
    return shlex.join([shell, "-c", CHECK_WRAPPER.format(seconds=seconds), "desk-check", *check])


def check_answers(command: str, call: dict) -> int:
    try:
        done = subprocess.run(command, shell=True, input=json.dumps(call), capture_output=True, text=True, timeout=CHECK_SECONDS + 10)
    except (OSError, subprocess.SubprocessError):
        return -1
    return done.returncode


def known_values(claude: str, copy: Path) -> list[list[str]]:
    """What Claude Code puts into a session's context about the owner, so that the tools can refuse to send it."""
    code, output = run([claude, "auth", "status"])
    try:
        email = json.loads(output)["email"]
        assert code == 0 and isinstance(email, str) and "@" in email
    except (ValueError, KeyError, TypeError, AssertionError):
        raise Problem("The address of the signed-in account could not be read, so it could not be kept out of answers.") from None
    known = [["the account's email address", email]]
    for key in ("user.name", "user.email"):
        code, output = run(["git", "config", "--global", key])
        if code == 0 and len(output.strip()) >= 3:
            known.append(["the owner's git name or address", output.strip()])
    return known


def run(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return (127, "")
    return (done.returncode, done.stdout)


def which(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise Problem(f"The command {name} was not found.")
    return str(Path(found).resolve())


def version_tuple(text: str) -> tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)+", text)
    if not match:
        raise Problem("A version number could not be read.")
    return tuple(int(part) for part in match.group(0).split("."))


def private(path: Path, what: str) -> None:
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Problem(f"{what} must belong to this account and be closed to every other account.")


def state_folder(raw: Path, copy: Path) -> Path:
    state = raw.expanduser().resolve()
    if ".dodam" in state.parts:
        raise Problem("The state folder must not be inside .dodam.")
    if state == copy or state.is_relative_to(copy) or copy.is_relative_to(state):
        raise Problem("The state folder must be outside the desk's copy of the repository, where the session cannot read it.")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    private(state, "The state folder")
    return state


def load_lock(copy: Path) -> dict:
    try:
        lock = json.loads((copy / "tools" / "desk" / "lock.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Problem("The closed list of what the desk may do (lock.json) is missing or damaged.") from None
    if not isinstance(lock, dict) or any(key not in lock for key in LOCK_KEYS):
        raise Problem("The closed list of what the desk may do (lock.json) is incomplete.")
    if set(lock["builtin_tools"]) - {"Read", "Glob", "Grep"} or set(lock["live_tools"]) & set(lock["live_tools_never"]):
        raise Problem("The closed list of what the desk may do (lock.json) names a tool the desk must not have.")
    return lock


def live_server(state: Path, lock: dict, facts: facts_file.Facts) -> dict:
    """The command that serves the tools of a live session, kept where the session cannot read it."""
    path = state / LIVE_SERVER_FILE
    if not path.is_file():
        raise Problem(f"The way in through a live session is open, but {LIVE_SERVER_FILE} is missing from the state folder.")
    private(path, f"The file {LIVE_SERVER_FILE}")
    try:
        server = json.loads(path.read_text(encoding="utf-8"))
        command, arguments, env = server["command"], server.get("args", []), server.get("env", {})
        assert isinstance(command, str) and Path(command).is_absolute()
        assert isinstance(arguments, list) and all(isinstance(a, str) for a in arguments)
        assert isinstance(env, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
    except (OSError, ValueError, KeyError, AssertionError):
        raise Problem(f"The file {LIVE_SERVER_FILE} is damaged.") from None
    code, output = run([command, *arguments, "version"])
    if code != 0 or version_tuple(output) < version_tuple(facts.coworker_min_version):
        raise Problem(f"The live-session tools are older than {facts.coworker_min_version}, the oldest version the facts allow.")
    return {"command": command, "args": arguments, "env": env}


def plan(coordinator: str, state: Path, stand_in: Path | None = None, environ: dict[str, str] | None = None, copy: Path = COPY,
         machine: list[tuple[str, str]] | None = None) -> dict:  # fmt: skip
    """Check everything, write the session's configuration files, and say what to start."""
    environ = dict(os.environ) if environ is None else environ
    # Claude Code tells a session the path of its working folder. A path with the account's or the computer's name
    # in it would put that name into every session, so the desk does not start from such a path.
    found = guard.names(str(copy), guard.machine_words() if machine is None else machine)
    if found:
        raise Problem("The path of the desk's copy holds " + ", ".join(found) + ". Put the copy in a folder whose path names neither.")
    desk_folder = copy / "tools" / "desk"
    lock = load_lock(copy)
    facts_path = desk_folder / "desk.toml"
    try:
        facts = facts_file.load(facts_path)
    except facts_file.FactsProblem as problem:
        raise Problem(str(problem)) from None

    if not isinstance(coordinator, str) or not ADDRESS.fullmatch(coordinator):
        raise Problem("Give the coordinator's address with --coordinator.")
    try:
        brief = (desk_folder / "brief.md").read_text(encoding="utf-8").strip()
    except OSError:
        brief = ""
    if not brief:
        raise Problem("The desk's standing instructions (brief.md) are missing.")
    if facts.live_open and stand_in is None:
        # The limits on connection codes and on live sessions at once are in the facts, but no tool keeps them yet.
        raise Problem("The way in through a live session is open, but its limits are not enforced yet. The desk does not start with that way open.")
    if facts.github_open and not (desk_folder / "watch.py").is_file():
        raise Problem("The way in through GitHub is open, but the watcher (watch.py) is missing.")

    state = state_folder(state, copy)
    try:
        tools.read_token(state)
    except tools.Refused as refusal:
        raise Problem(str(refusal)) from None

    claude = which("claude")
    code, output = run([claude, "--version"])
    if code != 0 or output.split()[:1] != [lock["claude_version"]]:
        raise Problem(
            f"The lock was checked against Claude Code {lock['claude_version']}; another version is installed. "
            "Run the lock check on the new version and change the pinned version with it."
        )

    desk_env = {
        "DESK_LOCK": str(desk_folder / "lock.json"),
        "DESK_FACTS": str(facts_path),
        "DESK_STATE": str(state),
        "DESK_COPY": str(copy),
        "DESK_COORDINATOR": coordinator,
    }
    if stand_in is not None:
        stand_in = stand_in.resolve()
        if not (stand_in / "items").is_dir():
            raise Problem("The stand-in folder has no items.")
        desk_env["DESK_STAND_IN"] = str(stand_in)
    else:
        code, output = run(["git", "-C", str(copy), "status", "--porcelain"])
        if code != 0 or output.strip():
            raise Problem("The desk's copy of the repository differs from what is committed. Start the desk from a clean copy.")
        desk_env["DESK_GH"] = which("gh")
        desk_env["DESK_ORCA"] = which("orca")
        if environ.get("ORCA_TERMINAL_HANDLE") and ADDRESS.fullmatch(environ["ORCA_TERMINAL_HANDLE"]):
            desk_env["DESK_SENDER"] = environ["ORCA_TERMINAL_HANDLE"]

    python = str(Path(sys.executable).resolve())
    servers = {"desk": {"command": python, "args": ["-I", str(desk_folder / "tools.py"), "serve"], "env": desk_env}}
    if facts.live_open:
        servers[lock["live_server"]] = live_server(state, lock, facts)

    session = state / "session"
    session.mkdir(mode=0o700, exist_ok=True)
    known_path = session / "known.json"
    desk_env["DESK_KNOWN"] = str(known_path)
    hook = check_command(python, desk_folder, known_path)
    live = lock["live_server"]
    settings = {
        # Claude Code otherwise describes the repository to the session, the owner's git name included.
        "includeGitInstructions": False,
        "permissions": {
            "defaultMode": "dontAsk",
            "allow": sorted(guard.allowed_tools(lock, facts.live_open)),
            "deny": NEVER + [f"mcp__{live}__{name}" for name in lock["live_tools_never"]],
        },
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": hook, "timeout": CHECK_SECONDS + 40}]}]},
    }

    # The files hold paths, the owner's address and, for a live session, a key. They go to the state folder, not to the command line.
    files = {}
    for name, content in (("mcp.json", {"mcpServers": servers}), ("settings.json", settings), ("known.json", known_values(claude, copy))):
        files[name] = session / name
        descriptor = os.open(files[name], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(content, handle, indent=1)

    argv = [
        claude,
        # No terminal interface: messages go in as data and come out as data, so nothing typed is a command.
        "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
        "--restricted",
        "--tools", ",".join(lock["builtin_tools"]),
        "--strict-mcp-config",
        "--mcp-config", str(files["mcp.json"]),
        "--setting-sources", "",
        "--settings", str(files["settings.json"]),
        "--permission-mode", "dontAsk",
        "--disable-slash-commands",
        "--model", lock["model"],
        "--append-system-prompt", brief,
    ]  # fmt: skip
    # The check is run once here, as Claude Code will run it. A check that does not answer is a reason not to start.
    if check_answers(hook, {"tool_name": "Read", "tool_input": {}}) != 0 or check_answers(hook, {"tool_name": "Bash", "tool_input": {}}) != 2:
        raise Problem("The check that runs before every tool call does not answer as it must.")

    env = {key: environ[key] for key in lock["environment"] if key in environ}
    env["PATH"] = lock["path"]
    env["DISABLE_AUTOUPDATER"] = "1"
    # The owner's git name and address stay out of what Claude Code tells the session about the folder.
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    # The watcher gets the same sign-in as the tools: the desk's own token when there is one.
    if stand_in is not None:
        watcher_env = {"PATH": lock["path"], "DESK_STAND_IN": str(stand_in)}
    else:
        watcher_env = tools.GitHub(lock["repository"], desk_env["DESK_GH"], state, lock["path"]).environment()
    return {
        "argv": argv,
        "env": env,
        "cwd": str(copy),
        "state": str(state),
        "settings": settings,
        "servers": sorted(servers),
        "watcher_env": watcher_env,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="start.py", description="Start the Dodam desk's locked session.")
    parser.add_argument("--coordinator", required=True, help="the one address the desk can send a message to")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="folder outside the repository for the tools' state")
    parser.add_argument("--stand-in", type=Path, default=None, help="a folder of made-up items in place of GitHub (for the lock check)")
    parser.add_argument("--print", action="store_true", dest="show", help="show what would be started and start nothing")
    parser.add_argument("--say", default=None, help="a first message for the session (for the lock check)")
    parser.add_argument("--exit-when-idle", action="store_true", help="end once the session has nothing left to do (for the lock check)")
    options = parser.parse_args(argv)
    try:
        planned = plan(options.coordinator, options.state, options.stand_in)
    except Problem as problem:
        sys.stderr.write(f"The desk was not started. {problem}\n")
        return 1
    watcher_env = planned.pop("watcher_env")
    if options.show:
        json.dump(planned, sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0
    return relay.Relay(planned, watcher_env, say=options.say, exit_when_idle=options.exit_when_idle).run()


if __name__ == "__main__":
    raise SystemExit(main())
