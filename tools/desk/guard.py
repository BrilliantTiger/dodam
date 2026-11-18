"""Refuses text that tells about the machine or holds a secret, and refuses tools off the list.

Two uses, one rule:

- The desk's own tools call leaks() on every text before it leaves (an answer on GitHub).
- Claude Code runs this file before every tool call of the desk's session
  (python3 guard.py hook --lock FILE --facts FILE). It refuses any tool that is not on the
  closed list in lock.json, and applies leaks() to everything the session hands to a tool of
  a live session, where the text goes straight to an outsider.

A refusal names the kind of thing found, never the thing itself.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import subprocess
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Characters that are not shown but split a word in two for a plain search.
INVISIBLE = dict.fromkeys(
    [0x00AD, 0x034F, 0x061C, 0x180E, 0xFEFF, *range(0x200B, 0x2010), *range(0x202A, 0x202F), *range(0x2060, 0x2065)]
)

SECRET_SHAPES = (
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("an API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("a Slack token", re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}")),
    ("a cloud access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Google key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("a signed web token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.")),
    ("a password or key assignment", re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|relay[_-]?key|token)\s*[=:]\s*\S{8,}")),
)
# A long unbroken run of letters and digits, such as a key, a hash or encoded bytes.
LONG_RUN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")

INTERNAL_ADDRESSES = (
    ("a home folder path", re.compile(r"(?i)(?:/Users/|/home/|[A-Z]:\\Users\\)[^\s/\\]+")),
    ("a home folder path", re.compile(r"(?<![\w.])~/")),
    ("a hidden project folder", re.compile(r"(?<![\w-])\.(?:dodam|ssh|coworker|aws|gnupg|claude)\b")),
    (
        "an internal address",
        re.compile(
            r"(?<![\d.])(?:10\.\d{1,3}|127\.\d{1,3}|192\.168|169\.254|172\.(?:1[6-9]|2\d|3[01])"
            r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}(?![\d.]*\d)"
        ),
    ),
    ("an internal address", re.compile(r"(?i)(?<![0-9a-f:])(?:fe80|f[cd][0-9a-f]{2}):[0-9a-f:]+")),
    ("an internal address", re.compile(r"(?i)\blocalhost\b")),
    ("an internal address", re.compile(r"(?i)\b[a-z0-9-]+\.(?:local|lan|internal|home\.arpa|ts\.net)\b")),
)


def _command_line(*argv: str) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def machine_words() -> list[tuple[str, str]]:
    """What names this computer and this account, read from the computer itself."""
    words: list[tuple[str, str]] = []
    hosts = {socket.gethostname(), os.uname().nodename}
    for key in ("ComputerName", "LocalHostName", "HostName"):
        hosts.add(_command_line("/usr/sbin/scutil", "--get", key))
    for host in hosts:
        for part in {host, host.split(".")[0]}:
            if len(part) >= 3:
                words.append(("the computer's name", part))
    try:
        account = getpass.getuser()
    except (KeyError, OSError):
        account = ""
    try:
        import pwd

        entry = pwd.getpwuid(os.getuid())
        names = {account, entry.pw_name, entry.pw_gecos.split(",")[0]}
    except (ImportError, KeyError, OSError):
        names = {account}
    for name in names:
        if len(name) >= 3:
            words.append(("the account name", name))
    home = str(Path.home())
    if len(home) > 1:
        words.append(("the home folder path", home))
    return words


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(INVISIBLE)


def _squeezed(text: str) -> str:
    return re.sub(r"[\s\-_.’']+", "", text).casefold()


def leaks(text: str, machine: list[tuple[str, str]] | None = None) -> list[str]:
    """The kinds of thing in the text that must not leave. Empty when the text may go."""
    if machine is None:
        machine = machine_words()
    plain = _plain(text)
    folded = plain.casefold()
    squeezed = _squeezed(plain)
    found: list[str] = []

    def note(kind: str) -> None:
        if kind not in found:
            found.append(kind)

    for kind, word in machine:
        if word.casefold() in folded or (len(_squeezed(word)) >= 5 and _squeezed(word) in squeezed):
            note(kind)
    for kind, shape in (*SECRET_SHAPES, *INTERNAL_ADDRESSES):
        if shape.search(plain):
            note(kind)
    for run in LONG_RUN.findall(plain):
        if re.search(r"[A-Za-z]", run) and re.search(r"\d", run):
            note("a long string shaped like a key")
    return found


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def allowed_tools(lock: dict, live_open: bool) -> set[str]:
    names = set(lock["builtin_tools"])
    names |= {f"mcp__desk__{name}" for name in lock["desk_tools"]}
    if live_open:
        names |= {f"mcp__{lock['live_server']}__{name}" for name in lock["live_tools"]}
    return names


def judge_call(call: dict, lock: dict, facts, machine: list[tuple[str, str]] | None = None) -> str | None:
    """The reason to refuse one tool call of the desk's session, or None when it may run."""
    name = call.get("tool_name")
    if not isinstance(name, str) or name not in allowed_tools(lock, facts.live_open):
        return "This tool is not on the desk's list."
    live = f"mcp__{lock['live_server']}__"
    if not name.startswith(live):
        return None
    given = call.get("tool_input")
    if not isinstance(given, dict):
        return "This tool call has no readable input."
    if name == live + "open_bridge":
        # Left to itself the bridge names the session after the folder and the computer.
        if given.get("name") != facts.name:
            return "The bridge must carry the desk's public name and no other."
        if "port" in given:
            return "The desk does not choose a port."
    found = leaks("\n".join(_strings(given)), machine)
    if found:
        return "The text was not sent. It holds " + ", ".join(found) + "."
    return None


def hook(argv: list[str]) -> int:
    """Exit code 2 refuses the tool call and shows the reason to the session."""
    try:
        sys.path.insert(0, str(HERE))
        import facts as facts_file

        options = dict(zip(argv[0::2], argv[1::2], strict=True))
        lock = json.loads(Path(options["--lock"]).read_text(encoding="utf-8"))
        facts = facts_file.load(options["--facts"])
        call = json.loads(sys.stdin.read())
        reason = judge_call(call, lock, facts)
    except Exception:  # Anything that goes wrong here refuses the call.
        reason = "The desk's lock could not be checked, so the call is refused."
    if reason is None:
        return 0
    sys.stderr.write(reason + "\n")
    return 2


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "hook":
        raise SystemExit(hook(sys.argv[2:]))
    sys.stderr.write("usage: guard.py hook --lock FILE --facts FILE\n")
    raise SystemExit(2)
