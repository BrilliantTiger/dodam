"""Refuses text that must not leave the desk, and refuses tools off the list.

The first line of the desk's defence is not here: it is that a session holds nothing of the
owner, so that it has nothing to give away. This file is the second line. It catches a known
value written exactly or lightly disguised, and text in the shape of a secret. It cannot catch
a value put into other words or transformed (spelled out, reversed, encoded).

Every text that leaves passes outgoing(), in this order:

1. Characters that are not shown, or that steer a terminal or the direction of writing, are
   refused outright: format characters, variation selectors, tag characters, private-use and
   unassigned code points, and control characters other than the line break and the tab.
2. The known values (what names this computer, this account and the owner) are compared on a
   reduced form of both sides, see reduced().
3. Shapes: a token, a key, a private address, a path outside the repository.

Claude Code also runs this file before every tool call of a desk session (guard.py hook ...,
wrapped by the start command so that any ending other than a clean "allowed" refuses the call).
It refuses any tool that is not on the closed list in lock.json, and applies outgoing() to
everything the session hands to a tool of a live session.

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

# Letters of other scripts that look like a Latin letter, mapped to that letter. This is a table kept by hand
# for the Cyrillic and Greek alphabets, lower case (the text is case-folded first). Other forms of Latin letters
# (full-width, mathematical, enclosed) are already folded by compatibility normalisation.
LOOK_ALIKE = str.maketrans({
    # Cyrillic
    "а": "a", "в": "b", "с": "c", "ԁ": "d", "е": "e", "ё": "e", "ғ": "f", "һ": "h", "н": "h", "і": "i", "ї": "i",
    "ј": "j", "к": "k", "ӏ": "l", "м": "m", "п": "n", "о": "o", "р": "p", "ԛ": "q", "г": "r", "ѕ": "s", "т": "t",
    "у": "y", "ү": "y", "ѵ": "v", "ԝ": "w", "ш": "w", "х": "x", "з": "3", "б": "6",
    # Greek
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u",
    "χ": "x", "ω": "w", "γ": "y", "ϲ": "c", "ϳ": "j",
})  # fmt: skip

# A known value of fewer than six letters and digits in its reduced form is compared only as a whole word, as
# written. In reduced form the spaces between words are gone, so a short value would be found by accident across
# ordinary words ("ann" in "can never"). From six on, an accident needs ordinary words that run together to
# spell the whole name; when that happens the answer is refused and has to be worded differently, nothing more.
SHORTEST_REDUCED = 6

SECRET_SHAPES = (
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("an API key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("a Slack token", re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}")),
    ("a cloud access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Google key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("a signed web token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.")),
    ("a sign-in header", re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("a password or key assignment", re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|relay[_-]?key|token)\s*[=:]\s*\S{8,}")),
)
# A long unbroken run such as a key, a hash or encoded bytes.
LONG_RUN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
WORD_WITH_PATH = re.compile(r"[^\s`'\"()<>\[\],;]+")

# Addresses that mean "this machine" on every machine (localhost, 127.0.0.1, ::1, 0.0.0.0) say nothing about this
# computer, and the project's own documents use them, so they are not refused. Addresses of this computer's own
# networks are.
PRIVATE_PLACES = (
    ("a path outside the repository", re.compile(r"(?i)(?<![\w.])/+(?:users|home|root|var/root|private|volumes|tmp|etc|opt|library)(?=/|\b)")),
    ("a path outside the repository", re.compile(r"(?i)\b[a-z]:\\")),
    ("a path outside the repository", re.compile(r"(?<![\w.])~/")),
    ("a hidden folder of this computer", re.compile(r"(?<![\w-])\.(?:dodam|ssh|coworker|aws|gnupg|claude)\b")),
    (
        "an address inside a private network",
        re.compile(
            r"(?<![\d.])(?:10\.\d{1,3}|192\.168|169\.254|172\.(?:1[6-9]|2\d|3[01])"
            r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\.\d{1,3}\.\d{1,3}(?![\d.]*\d)"
        ),
    ),
    ("an address inside a private network", re.compile(r"(?i)(?<![0-9a-f:])(?:fe80|f[cd][0-9a-f]{2}):[0-9a-f:]+")),
    ("an address inside a private network", re.compile(r"(?i)\b[a-z0-9-]+\.(?:local|lan|internal|home\.arpa|ts\.net)\b")),
)


def _command_line(*argv: str) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def machine_words(known: str | os.PathLike | None = None) -> list[tuple[str, str]]:
    """What names this computer and this account, read from the computer itself.

    known is a file the start command writes outside the session's reach, with further values of
    the owner (the address of the owner's sign-in and the owner's git name, when there are any).
    """
    words: list[tuple[str, str]] = []
    if known:
        for entry in json.loads(Path(known).read_text(encoding="utf-8")):
            if isinstance(entry, list) and len(entry) == 2 and all(isinstance(part, str) for part in entry) and len(entry[1]) >= 3:
                words.append((entry[0], entry[1]))
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
        names_of_account = {account, entry.pw_name, entry.pw_gecos.split(",")[0]}
    except (ImportError, KeyError, OSError):
        names_of_account = {account}
    for name in names_of_account:
        if len(name) >= 3:
            words.append(("the account name", name))
    home = str(Path.home())
    if len(home) > 1:
        words.append(("the home folder path", home))
    return words


def not_shown(text: str) -> bool:
    """Whether the text holds a character that is not shown or that steers a terminal or the direction of writing."""
    for character in text:
        if character in "\n\t":
            continue
        category = unicodedata.category(character)
        # Cc controls, Cf format (zero-width, direction marks, tags), Co private use, Cn unassigned, Cs lone surrogates.
        if category in ("Cc", "Cf", "Co", "Cn", "Cs"):
            return True
        code = ord(character)
        if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF or 0x180B <= code <= 0x180F or code in (0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0):
            return True
    return False


def shown(text: str) -> str:
    """The text with everything taken out that is not shown or that steers a terminal. For what the relay prints."""
    return "".join(character for character in text if not not_shown(character))


def reduced(text: str) -> str:
    """Letters and digits only, in one case and one alphabet.

    Compatibility-normalised, case-folded, look-alike letters of other scripts mapped to their Latin
    counterparts, and every character that is not a letter or a digit dropped. A value split by
    punctuation, marks, spaces or line breaks, or written with look-alike letters, has the same reduced
    form as the value itself.
    """
    folded = unicodedata.normalize("NFKC", text).casefold().translate(LOOK_ALIKE)
    # Accents are taken off, so that a letter with a mark on it is the letter.
    plain = unicodedata.normalize("NFKD", folded)
    return "".join(character for character in plain if character.isalnum())


def names(text: str, machine: list[tuple[str, str]], public: tuple[str, ...] = ()) -> list[str]:
    """Which of the known values are in the text. Values that are public anyway (public) are not looked for."""
    whole = reduced(text)
    folded = unicodedata.normalize("NFKC", text).casefold()
    open_values = {reduced(value) for value in public}
    found: list[str] = []
    for kind, word in machine:
        small = reduced(word)
        if not small or small in open_values:
            continue
        if len(small) >= SHORTEST_REDUCED:
            hit = small in whole
        else:
            hit = re.search(r"(?<![^\W_])" + re.escape(word.casefold()) + r"(?![^\W_])", folded) is not None
        if hit and kind not in found:
            found.append(kind)
    return found


def _is_path_of_the_copy(word: str, copy: Path | None) -> bool:
    """Whether a word in the text is the path of a file or folder that exists in the desk's copy of the repository."""
    if copy is None:
        return False
    word = re.sub(r"^https://github\.com/[^/]+/[^/]+/(?:blob|tree)/[^/]+/", "", word).split("#")[0].split("?")[0].strip(".:")
    if not word or word.startswith("/") or ".." in word.split("/"):
        return False
    try:
        return (copy / word).exists() and (copy / word).resolve().is_relative_to(copy.resolve())
    except OSError:
        return False


def shapes(text: str, copy: Path | None = None) -> list[str]:
    plain = unicodedata.normalize("NFKC", text)
    found: list[str] = []
    for kind, shape in (*SECRET_SHAPES, *PRIVATE_PLACES):
        if shape.search(plain) and kind not in found:
            found.append(kind)
    for word in WORD_WITH_PATH.findall(plain):
        # The name of a file of this repository is long and is not a key.
        if _is_path_of_the_copy(word, copy):
            continue
        for run in LONG_RUN.findall(word):
            # Words joined by hyphens, underscores or slashes (a file name, a test's name, an address) are not a key. A key has a long
            # part that mixes digits with letters, or capitals with small letters in no order a word has.
            for part in re.split(r"[-_/]", run):
                letters = [c for c in part if c.isalpha()]
                mixed = any(c.isdigit() for c in part) and letters
                one_word = part.isalpha() and (part.islower() or part.isupper() or part.istitle())
                if len(part) >= 20 and (mixed or (letters and not one_word)) and "a long string shaped like a key" not in found:
                    found.append("a long string shaped like a key")
    return found


def leaks(text: str, machine: list[tuple[str, str]] | None = None, copy: Path | None = None, public: tuple[str, ...] = ()) -> list[str]:
    """The kinds of thing in the text that must not leave. Empty when the text may go."""
    if not_shown(text):
        return ["a character that is not shown"]
    found = names(text, machine_words() if machine is None else machine, public)
    return found + [kind for kind in shapes(text, copy) if kind not in found]


outgoing = leaks


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
        reason = judge_call(call, lock, facts, machine_words(options.get("--known")))
    except Exception:  # Anything that goes wrong here refuses the call.
        reason = "The desk's lock could not be checked, so the call is refused."
    if reason is None:
        return 0
    sys.stderr.write(reason + "\n")
    return 2


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "hook":
        raise SystemExit(hook(sys.argv[2:]))
    sys.stderr.write("usage: guard.py hook --lock FILE --facts FILE [--known FILE]\n")
    raise SystemExit(2)
