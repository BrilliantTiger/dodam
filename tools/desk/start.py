#!/usr/bin/env python3
"""Starts the desk, locked, in the terminal this command is run in.

    python3 tools/desk/start.py --coordinator ADDRESS [--state FOLDER] [--print]

It runs a relay (relay.py) in this terminal. For every item with something new the relay starts
a session of Claude Code without its terminal interface, and that session has nothing but the
closed list in lock.json:

- the three tools that read files, confined to this copy of the repository;
- the desk's own tools (tools.py);
- no shell, no tool that writes a file, no tool that fetches from the web, no agents;
- none of the settings, instructions, hooks, plugins or connected services of the person
  whose computer this is, and, with a sign-in of the desk's own, nothing that names that person;
- an environment with nothing in it but a handful of names;
- every tool call that is not on the list refused at once, with nobody asked.

The command refuses to start when anything it relies on is missing or different, and says why
in one sentence. It never starts half-checked: any error it did not expect is a refusal too.

--coordinator is the one address the desk can send a message to. --state is the folder, outside
the repository and outside the home folder, where the tools keep what they must remember and where the owner puts what the
desk needs of the owner: github-token (the desk's own token for GitHub) and claude-token (the
desk's own sign-in to Claude), each readable by this account only. --print shows what would be
started and starts nothing.
"""

from __future__ import annotations

import sys

# Nothing is written into the copy of the repository, not even a compiled file.
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
COPY = HERE.parents[1]
sys.path.insert(0, str(HERE))

import facts as facts_file  # noqa: E402
import guard  # noqa: E402
import relay  # noqa: E402
import state as state_files  # noqa: E402
import tools  # noqa: E402



def default_state() -> Path:
    """Not in the home folder: a session is shown paths of the state folder, and the home folder's path names the account."""
    shared = Path("/Users/Shared")
    return (shared if shared.is_dir() and not shared.is_symlink() else Path("/var/tmp")) / f"dodam-desk-{os.getuid()}"


CHECK_FILE = "check"
LIVE_SERVER_FILE = "live-server.json"
CLAUDE_TOKEN_FILE = "claude-token"
CLAUDE_CONFIG_PREFIX = "claude-config-"
# What a sign-in may show about the account. Whatever of it is there is kept out of what the desk sends.
SHOWN_ABOUT_THE_ACCOUNT = ("email", "orgName", "organizationName", "displayName", "fullName", "name")
ADDRESS = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_:@.-]{0,119}$")
VERSION = re.compile(r"\d+(?:\.\d+)+")

# The widest the closed list may ever be. lock.json may name less, never more.
BUILTIN_TOOLS = {"Read", "Grep"}
LIVE_TOOLS = {"open_bridge", "wait_for_message", "inbox", "read_conversation", "reply", "list_connections", "write_agreement"}
LIVE_TOOLS_NEVER = {"connect", "close_bridge"}
ENVIRONMENT = {"HOME", "USER", "LOGNAME", "LANG", "TERM", "TMPDIR"}
PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# Named again here so that they stay refused even if a later change puts one back among the tools.
NEVER = ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task", "Skill", "Glob"]

NAME = {"type": str}
NAMES = {"type": list, "of": str}
COUNT = {"type": int}
SENTENCES = {"en": NAME, "ko": NAME, "ja": NAME, "es": NAME}
WEIGHT = {"type": (int, float)}
LOCK_SHAPE = {
    "claude_version": NAME, "model": NAME, "repository": NAME, "live_server": NAME, "path": NAME,
    "builtin_tools": NAMES, "desk_tools": NAMES, "live_tools": NAMES, "live_tools_never": NAMES, "labels": NAMES, "environment": NAMES,
    "labels_per_day": COUNT,
    "labels_per_account_per_day": COUNT,
    "answer": {"first_line": NAME, "max_chars": COUNT, "per_account_per_day": COUNT, "told": SENTENCES, "could_not_tell": SENTENCES},
    "read": {"max_bytes": COUNT, "body_bytes": COUNT, "title_bytes": COUNT, "comment_bytes": COUNT, "max_comments": COUNT,
             "max_labels": COUNT, "label_bytes": COUNT, "name_bytes": COUNT, "file_pages": COUNT, "seconds": COUNT},
    "coordinator": {"first_line": NAME, "max_chars": COUNT, "kinds": NAMES,
                    "at_once": {"vulnerability_per_account": COUNT, "vulnerability_per_day": COUNT, "desk_down_per_day": COUNT, "limit_notices_per_day": COUNT},
                    "kept_per_day": COUNT, "kept_per_account_per_day": COUNT, "vulnerability_kept_per_day": COUNT, "reports_per_session": COUNT, "digest_seconds": COUNT, "digest_lines": COUNT, "digest_line_chars": COUNT},
    "spending": {"turns_per_session": COUNT, "seconds_per_session": COUNT, "grace_seconds": COUNT, "reads_per_session": COUNT, "read_lines": COUNT,
                 "search_lines": COUNT, "calls_per_session": COUNT, "result_bytes": COUNT, "message_bytes": COUNT,
                 "content_bytes_per_session": COUNT, "output_tokens_per_turn": COUNT, "brief_bytes": COUNT, "base_tokens": COUNT,
                 "weights": {"input": WEIGHT, "cache_written": WEIGHT, "cache_read": WEIGHT, "output": WEIGHT},
                 "units_per_session": COUNT, "units_per_turn": COUNT, "units_per_day": COUNT,
                 "sessions_per_item_per_day": COUNT, "sessions_per_account_per_day": COUNT, "units_per_account_per_day": COUNT, "ended_in_a_row_per_item": COUNT, "sessions_per_day": COUNT, "arrivals_kept": COUNT, "arrivals_kept_per_account": COUNT,
                 "sign_in_failures": COUNT},
}  # fmt: skip


def turn_bound(spending: dict) -> float:
    """The most one turn can cost in weighted units: everything a session can hold, written to the cache anew, and one full output."""
    weights = spending["weights"]
    held = (spending["base_tokens"] + spending["brief_bytes"] + spending["message_bytes"] + spending["content_bytes_per_session"]
            + spending["calls_per_session"] * spending["result_bytes"] + spending["turns_per_session"] * spending["output_tokens_per_turn"])  # fmt: skip
    return held * max(weights["input"], weights["cache_written"], weights["cache_read"]) + spending["output_tokens_per_turn"] * weights["output"]


# A reason not to start the desk. What is wrong in the state folder is the same kind of reason.
Problem = state_files.StateProblem


CHECK_SECONDS = 20
# Runs the check before a tool call so that every ending but a clean "allowed" refuses the call: a crash, a
# missing interpreter, a check that hangs. Claude Code blocks a call only on exit code 2, so everything becomes 2.
# Claude Code shows a session the command of a check that refused, and what the check wrote as its error. So the
# command is one path and nothing else, and of everything the check may print the session is shown one line:
# the reason the check itself gave on its standard output, or a fixed sentence.
CHECK_SCRIPT = """# The call arrives on standard input. Some shells give a command in the background an empty input, so it is handed over by number.
exec 3<&0
reason=$(
  {check} <&3 3<&- 2>/dev/null & check=$!
  exec 3<&-
  (sleep {seconds}; kill -9 $check 2>/dev/null) >/dev/null 2>&1 & dog=$!
  wait $check; code=$?
  kill $dog 2>/dev/null
  exit $code
)
code=$?
exec 3<&-
[ "$code" -eq 0 ] && exit 0
if [ "$code" -eq 2 ] && [ -n "$reason" ]; then printf '%s\\n' "$reason" >&2
else echo "The desk's lock could not be checked, so the call is refused." >&2; fi
exit 2
"""


def check_script(interpreter: str, desk_folder: Path, known: Path, state: Path, seconds: float = CHECK_SECONDS) -> str:
    """The text of the file that runs the check. It is kept in the state folder, where a session cannot read it."""
    check = [interpreter, "-I", "-B", str(desk_folder / "guard.py"), "hook", "--lock", str(desk_folder / "lock.json"),
             "--facts", str(desk_folder / "desk.toml"), "--known", str(known), "--copy", str(desk_folder.parents[1]), "--state", str(state)]  # fmt: skip
    return CHECK_SCRIPT.format(check=shlex.join(check), seconds=seconds)


def check_command(script: Path, shell: str = "/bin/sh") -> str:
    """The command Claude Code runs before every tool call of the session: the shell, and the one file that holds the rest.

    It carries these two paths because Claude Code has to be told what to run. A file that is gone, or a shell that
    cannot read it, ends in a refusal like everything else.
    """
    return shlex.join([shell, str(script)]) + " || exit 2"


def check_answers(command: str, call: dict) -> int:
    try:
        done = subprocess.run(command, shell=True, input=json.dumps(call), capture_output=True, text=True, timeout=CHECK_SECONDS + 10)
    except (OSError, subprocess.SubprocessError):
        return -1
    return done.returncode


def run(argv: list[str], timeout: float = 30, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return (127, "")
    return (done.returncode, done.stdout)


def which(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise Problem(f"The command {name} was not found.")
    return str(Path(found).resolve())


def version_tuple(text: str) -> tuple[int, ...]:
    """A released version: numbers and dots and nothing else. A version with a suffix is not a release."""
    word = text.strip().split()[0] if text.strip() else ""
    if not VERSION.fullmatch(word):
        raise Problem("A version is not a released version, or could not be read.")
    return tuple(int(part) for part in word.split("."))


def state_folder(raw: Path, copy: Path) -> Path:
    given = Path(os.path.abspath(raw.expanduser()))
    # Looked at before the path is followed: a link here, or in a folder above, would send the desk's secrets and counts somewhere else.
    if any(part.is_symlink() for part in (given, *given.parents)):
        raise Problem("The state folder must be a real folder, not a link to one, and no folder above it may be a link. Give its real path.")
    # The file system of this computer does not tell capital letters from small ones.
    if ".dodam" in [part.casefold() for part in given.parts]:
        raise Problem("The state folder must not be inside .dodam.")
    if given == copy or given.is_relative_to(copy) or copy.is_relative_to(given):
        raise Problem("The state folder must be outside the desk's copy of the repository, where the session cannot read it.")
    # What is missing on the way is made closed to every other account, each folder of it.
    return state_files.real_folder(given, "The state folder")


def fits(value, shape) -> bool:
    if "type" not in shape:
        return isinstance(value, dict) and set(value) == set(shape) and all(fits(value[key], shape[key]) for key in shape)
    kind = shape["type"]
    if kind is int:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1
    if kind == (int, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
    if kind is str:
        return isinstance(value, str) and bool(value.strip())
    if kind is list:
        return isinstance(value, list) and all(isinstance(entry, str) and entry for entry in value) and len(set(value)) == len(value)
    return isinstance(value, dict) and bool(value) and all(isinstance(k, str) and fits(v, {"type": shape["of"]}) for k, v in value.items())


def load_lock(copy: Path) -> dict:
    try:
        lock = json.loads((copy / "tools" / "desk" / "lock.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Problem("The closed list of what the desk may do (lock.json) is missing or damaged.") from None
    if not fits(lock, LOCK_SHAPE):
        raise Problem("The closed list of what the desk may do (lock.json) is incomplete, or holds something of the wrong kind.")
    wider = (
        set(lock["builtin_tools"]) - BUILTIN_TOOLS
        or set(lock["desk_tools"]) - set(tools.TOOLS)
        or set(lock["live_tools"]) - LIVE_TOOLS
        or LIVE_TOOLS_NEVER - set(lock["live_tools_never"])
        or set(lock["environment"]) - ENVIRONMENT
        or lock["path"] != PATH
        or lock["live_server"] != "coworker"
    )
    if wider:
        raise Problem("The closed list of what the desk may do (lock.json) names something the desk must not have.")
    return lock


def live_server(state: Path, lock: dict, facts: facts_file.Facts) -> dict:
    """The command that serves the tools of a live session, kept where the session cannot read it."""
    path = state / LIVE_SERVER_FILE
    text = state_files.read(path, f"The file {LIVE_SERVER_FILE}")
    if text is None:
        raise Problem(f"The way in through a live session is open, but {LIVE_SERVER_FILE} is missing from the state folder.")
    try:
        server = json.loads(text)
    except ValueError:
        server = None
    good = (
        isinstance(server, dict)
        and isinstance(server.get("command"), str)
        and Path(server["command"]).is_absolute()
        and isinstance(server.get("args", []), list)
        and all(isinstance(a, str) for a in server.get("args", []))
        and isinstance(server.get("env", {}), dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in server.get("env", {}).items())
    )
    if not good:
        raise Problem(f"The file {LIVE_SERVER_FILE} is damaged.")
    command, arguments, env = server["command"], server.get("args", []), server.get("env", {})
    code, output = run([command, *arguments, "version"])
    try:
        old = code != 0 or version_tuple(output) < version_tuple(facts.coworker_min_version)
    except Problem:
        old = True
    if old:
        raise Problem(f"The live-session tools are not a released version of at least {facts.coworker_min_version}, the oldest the facts allow.")
    return {"command": command, "args": arguments, "env": env}


def own_configuration(state: Path) -> Path:
    """A configuration folder for Claude Code that this run of the command made itself.

    It is made new at every start, under a name that did not exist, so nothing in it was put there by
    anyone else. The ones earlier starts made are removed; anything else under such a name is refused.
    """
    for earlier in sorted(state.glob(CLAUDE_CONFIG_PREFIX + "*")) + [state / "claude-config"]:
        if earlier.is_symlink() or earlier.exists():
            state_files.folder(earlier, "A configuration folder for Claude in the state folder")
            shutil.rmtree(earlier)
    return state_files.folder(Path(tempfile.mkdtemp(prefix=CLAUDE_CONFIG_PREFIX, dir=state)), "The desk's own configuration folder for Claude")


def sign_in(claude: str, state: Path, environ: dict[str, str]) -> tuple[dict[str, str], str | None, list[str]]:
    """The desk's own sign-in to Claude when the owner made one, and what each sign-in shows.

    Returns what to add to a session's environment, the address the session's sign-in shows (None when
    it shows none), and everything either sign-in shows about the account (to be refused in outgoing text).
    Claude Code answers this question without asking the service, so the answer says nothing about
    whether a token is good, and nothing about what a session is told once it talks to the service.
    """

    def shown_by(env: dict[str, str]) -> tuple[bool, dict[str, str]]:
        code, output = run([claude, "auth", "status"], env=env)
        try:
            status = json.loads(output)
        except ValueError:
            status = None
        if code != 0 or not isinstance(status, dict) or status.get("loggedIn") is not True:
            return (False, {})
        return (True, {key: status[key].strip() for key in SHOWN_ABOUT_THE_ACCOUNT if isinstance(status.get(key), str) and status[key].strip()})

    base = {key: environ[key] for key in ("HOME", "USER", "LOGNAME", "PATH") if key in environ}
    owner_signed_in, owner = shown_by(base)
    token = state_files.read(state / CLAUDE_TOKEN_FILE, "The desk's sign-in to Claude (claude-token)")
    if token is None:
        if not owner_signed_in:
            raise Problem("Claude Code is not signed in.")
        return ({}, owner.get("email"), sorted(set(owner.values())))
    token = token.strip()
    if not token or any(character.isspace() for character in token):
        raise Problem("The desk's sign-in to Claude (claude-token) is empty or damaged.")
    # The token reaches a session through its environment only, with a configuration folder of the desk's own,
    # so that nothing of the owner's sign-in or settings is read.
    extra = {"CLAUDE_CODE_OAUTH_TOKEN": token, "CLAUDE_CONFIG_DIR": str(own_configuration(state))}
    signed_in, own = shown_by(base | extra)
    if not signed_in:
        raise Problem("The desk's sign-in to Claude (claude-token) was not accepted.")
    return (extra, own.get("email"), sorted(set(owner.values()) | set(own.values())))


def known_values(shown: list[str], environ: dict[str, str]) -> list[list[str]]:
    """Values of the owner that the tools refuse to send, beyond what the computer itself tells them."""
    known = [["what a sign-in shows about the account", value] for value in shown if len(value) >= 3]
    base = {key: environ[key] for key in ("HOME", "USER", "LOGNAME", "PATH") if key in environ}
    for key in ("user.name", "user.email"):
        code, output = run(["git", "config", "--global", key], env=base)
        if code == 0 and len(output.strip()) >= 3:
            known.append(["the owner's git name or address", output.strip()])
    return known


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
    spending = lock["spending"]
    if len(brief.encode("utf-8")) > spending["brief_bytes"]:
        raise Problem(f"The desk's standing instructions (brief.md) are longer than {spending['brief_bytes']} bytes, which the bound on what a session may cost counts on.")
    if spending["units_per_turn"] < turn_bound(spending):
        raise Problem("The closed list (lock.json) states less for one turn than its own numbers add up to.")
    if spending["units_per_session"] + spending["units_per_turn"] > spending["units_per_day"]:
        raise Problem("The closed list (lock.json) sets aside more for one session than a day may cost.")
    # One outside account alone never closes a day for everyone, by the numbers alone. For the units: what one account
    # can be charged is less than its share and one more session, and after that a session of another account must still start.
    set_aside = spending["units_per_session"] + spending["units_per_turn"]
    if (spending["units_per_account_per_day"] + 2 * set_aside > spending["units_per_day"]
            or spending["sessions_per_account_per_day"] >= spending["sessions_per_day"]
            or spending["arrivals_kept_per_account"] >= spending["arrivals_kept"]
            or lock["labels_per_account_per_day"] >= lock["labels_per_day"]
            or lock["answer"]["per_account_per_day"] >= facts.answers_per_day
            or lock["coordinator"]["at_once"]["vulnerability_per_account"] >= lock["coordinator"]["at_once"]["vulnerability_per_day"]):  # fmt: skip
        raise Problem("The closed list (lock.json) lets one account alone use up a day for everyone.")
    # What the sessions of a day can write must fit in what is kept, by the numbers alone. Twice the sessions of a
    # day, because the reports of 24 hours can come from sessions counted in the 24 hours before as well.
    reports = lock["coordinator"]
    if (2 * spending["sessions_per_day"] > reports["vulnerability_kept_per_day"]
            or 2 * spending["sessions_per_day"] * (reports["reports_per_session"] + 2) > reports["kept_per_day"]):  # fmt: skip
        raise Problem("The closed list (lock.json) lets the sessions of a day write more reports than it keeps.")
    if max(guard.paths_of(copy), default=0) > guard.BYTES_PER_LISTED_PATH:
        raise Problem(f"A path in the desk's copy is longer than {guard.BYTES_PER_LISTED_PATH} bytes, which the bound on what a search may hand over counts on.")
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
    state_files.file(state / relay.WATCH_STATE_FILE, "The watcher's state file")

    claude = which("claude")
    code, output = run([claude, "--version"])
    if code != 0 or output.split()[:1] != [lock["claude_version"]]:
        raise Problem(
            f"The lock was checked against Claude Code {lock['claude_version']}; another version is installed. "
            "Run the lock check on the new version and change the pinned version with it."
        )
    own_sign_in, session_email, shown_about_the_account = sign_in(claude, state, environ)
    if stand_in is None and (facts.github_open or facts.live_open) and session_email:
        # A session cannot give away what it does not hold. With the owner's sign-in it holds the owner's address.
        raise Problem(
            "A way in is open, but the sign-in the sessions would use shows an email address, which Claude Code tells every "
            "session. Put a sign-in of the desk's own in the state folder (claude-token), or close every way in."
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
        # The copy's git folder holds the names and addresses of whoever worked in it. It is kept outside the copy.
        if (copy / ".git").is_dir():
            raise Problem("The copy's git folder is inside the copy, where a session can read it. Make the copy with its git folder outside (git clone --separate-git-dir).")
        # The copy holds one line that says where its git folder is, and a session can read that line.
        try:
            pointer = (copy / ".git").read_text(encoding="utf-8")
        except OSError:
            pointer = ""
        named = guard.names(pointer, guard.machine_words() if machine is None else machine)
        if named:
            raise Problem("The path of the copy's git folder holds " + ", ".join(named) + ". Keep it in a folder whose path names neither.")
        # Ignored files are looked for too: a session can read them like any other file.
        code, output = run(["git", "-C", str(copy), "status", "--porcelain", "--ignored"])
        if code != 0 or output.strip():
            raise Problem("The desk's copy of the repository differs from what is committed, or holds files that are not part of it. Start the desk from a clean copy.")
        desk_env["DESK_GH"] = which("gh")
        desk_env["DESK_ORCA"] = which("orca")
        if environ.get("ORCA_TERMINAL_HANDLE") and ADDRESS.fullmatch(environ["ORCA_TERMINAL_HANDLE"]):
            desk_env["DESK_SENDER"] = environ["ORCA_TERMINAL_HANDLE"]

    session = state_files.folder(state / "session", "The folder session in the state folder", make=True)
    known_path = session / "known.json"
    desk_env["DESK_KNOWN"] = str(known_path)

    python = str(Path(sys.executable).resolve())
    servers = {"desk": {"command": python, "args": ["-I", "-B", str(desk_folder / "tools.py"), "serve"], "env": desk_env}}
    if facts.live_open:
        servers[lock["live_server"]] = live_server(state, lock, facts)

    script = session / CHECK_FILE
    hook = check_command(script)
    known = known_values(shown_about_the_account, environ)
    # Every path a session can be shown is looked at for every known value, in reduced form: the copy (its working
    # folder), the state folder with the configuration folder in it, and the command of the check.
    values = (guard.machine_words() if machine is None else list(machine)) + [(kind, value) for kind, value in known]
    for what, path in (("the desk's copy", str(copy)), ("the state folder", str(state)), ("the check before every tool call", hook)):
        found = guard.names(path, values)
        if found:
            raise Problem(f"The path of {what} holds " + ", ".join(found) + ". A session is shown this path. Use a folder whose path names none of them.")
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

    # The files hold paths, values of the owner and, for a live session, a key. They go to the state folder, not to the command line.
    files = {}
    for name, content in (("mcp.json", {"mcpServers": servers}), ("settings.json", settings), ("known.json", known)):
        files[name] = session / name
        state_files.write(files[name], f"The file {name} in the state folder", json.dumps(content, indent=1))
    state_files.write(script, f"The file {CHECK_FILE} in the state folder", check_script(python, desk_folder, known_path, state))

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
        # Claude Code's own bound on turns: one turn is one response of the model, with or without tool calls.
        "--max-turns", str(spending["turns_per_session"]),
        "--append-system-prompt", brief,
    ]  # fmt: skip
    # The check is run once here, as Claude Code will run it. A check that does not answer is a reason not to start.
    probe = {"session_id": "start-check", "tool_name": "Read", "tool_input": {"file_path": str(desk_folder / "lock.json"), "limit": 1}}
    if check_answers(hook, probe) != 0 or check_answers(hook, probe | {"tool_name": "Bash", "tool_input": {}}) != 2:
        raise Problem("The check that runs before every tool call does not answer as it must.")

    env = {key: environ[key] for key in lock["environment"] if key in environ}
    env["PATH"] = lock["path"]
    env["DISABLE_AUTOUPDATER"] = "1"
    # Claude Code's own bound on what one response may hold, thinking included.
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(spending["output_tokens_per_turn"])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # The owner's git name and address stay out of what Claude Code tells the session about the folder.
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.update(own_sign_in)
    # The watcher gets the same sign-in to GitHub as the tools: the desk's own token when there is one.
    if stand_in is not None:
        watcher_env = {"PATH": lock["path"], "DESK_STAND_IN": str(stand_in)}
    else:
        watcher_env = tools.GitHub(lock["repository"], desk_env["DESK_GH"], state, lock["path"]).environment()
    # The watcher is run from the copy and must leave no compiled file in it.
    watcher_env["PYTHONDONTWRITEBYTECODE"] = "1"
    return {
        "argv": argv,
        "env": env,
        "cwd": str(copy),
        "state": str(state),
        "desk_env": desk_env,
        "settings": settings,
        "servers": sorted(servers),
        "own_sign_in": bool(own_sign_in),
        "watcher_env": watcher_env,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="start.py", description="Start the Dodam desk, locked.")
    parser.add_argument("--coordinator", required=True, help="the one address the desk can send a message to")
    parser.add_argument("--state", type=Path, default=None, help="folder outside the repository and outside the home folder for the tools' state")
    parser.add_argument("--stand-in", type=Path, default=None, help="a folder of made-up items in place of GitHub (for the lock check)")
    parser.add_argument("--print", action="store_true", dest="show", help="show what would be started and start nothing")
    parser.add_argument("--say", default=None, help="a first message for a session (for the lock check)")
    parser.add_argument("--exit-when-idle", action="store_true", help="end once there is nothing left to do (for the lock check)")
    options = parser.parse_args(argv)
    try:
        planned = plan(options.coordinator, options.state or default_state(), options.stand_in)
    except Problem as problem:
        sys.stderr.write(f"The desk was not started. {problem}\n")
        return 1
    except Exception as error:  # Whatever was not expected is a reason not to start, said in one line.
        sys.stderr.write(f"The desk was not started. Something it relies on could not be checked ({type(error).__name__}).\n")
        return 1
    watcher_env = planned.pop("watcher_env")
    if options.show:
        # What is shown holds no token.
        planned["env"] = {key: ("<kept out of what is shown>" if key == "CLAUDE_CODE_OAUTH_TOKEN" else value) for key, value in planned["env"].items()}
        json.dump(planned, sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0
    return relay.Relay(planned, watcher_env, say=options.say, exit_when_idle=options.exit_when_idle).run()


if __name__ == "__main__":
    raise SystemExit(main())
