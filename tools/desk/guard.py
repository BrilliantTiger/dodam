"""Refuses text that must not leave the desk, and refuses tools off the list.

The first line of the desk's defence is not here: it is that a session holds nothing of the
owner, so that it has nothing to give away. This file is the second line. It catches a known
value written exactly or lightly disguised, and text in the shape of a secret. It cannot catch
a value that is encoded, reversed, written by sound in another script, translated, described, or
spread over two answers or over the first letters of lines.

Every text that leaves passes outgoing(), in this order:

1. Every character must be on the list of what the desk writes with (ALLOWED): what English, Korean,
   Japanese and Spanish are written with. Anything else is refused, whatever it is.
2. The known values (what names this computer, this account and the owner) are compared on a
   reduced form of both sides, see reduced(), Spelled and holds().
3. Shapes: the beginning of a key, a long run of letters and digits, a private address, a path
   outside the repository.

Claude Code also runs this file before every tool call of a desk session (guard.py hook ...,
wrapped by the start command so that any ending other than a clean "allowed" refuses the call).
It refuses any tool that is not on the closed list in lock.json, and applies outgoing() to
everything the session hands to a tool of a live session.

A refusal names the kind of thing found, never the thing itself.
"""

from __future__ import annotations

import functools
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

# What text that leaves may be written with: English, Korean, Japanese and Spanish, the four languages of the
# project. Every character that is not on this list is refused, whatever it is. Fullwidth letters and digits are
# not on it. The text is put into its composed form first, so a letter with an accent is one character.
ALLOWED = (
    (0x0009, 0x000A, "tab and line break"),
    (0x0020, 0x007E, "space, and the letters, digits and punctuation of English"),
    (0x00A1, 0x00A1, "Spanish: the opening exclamation mark"),
    (0x00AA, 0x00AB, "Spanish: the feminine ordinal mark and the opening angle quote"),
    (0x00B7, 0x00B7, "the middle dot, which is in the desk's own name"),
    (0x00BA, 0x00BB, "Spanish: the masculine ordinal mark and the closing angle quote"),
    (0x00BF, 0x00BF, "Spanish: the opening question mark"),
    (0x00D7, 0x00D7, "the times sign"),
    (0x00C1, 0x00C1, "Spanish: A with acute"), (0x00C9, 0x00C9, "Spanish: E with acute"), (0x00CD, 0x00CD, "Spanish: I with acute"),
    (0x00D1, 0x00D1, "Spanish: N with tilde"), (0x00D3, 0x00D3, "Spanish: O with acute"), (0x00DA, 0x00DA, "Spanish: U with acute"),
    (0x00DC, 0x00DC, "Spanish: U with diaeresis"),
    (0x00E1, 0x00E1, "Spanish: a with acute"), (0x00E9, 0x00E9, "Spanish: e with acute"), (0x00ED, 0x00ED, "Spanish: i with acute"),
    (0x00F1, 0x00F1, "Spanish: n with tilde"), (0x00F3, 0x00F3, "Spanish: o with acute"), (0x00FA, 0x00FA, "Spanish: u with acute"),
    (0x00FC, 0x00FC, "Spanish: u with diaeresis"),
    (0x2013, 0x2014, "the two dashes of printed text"),
    (0x2018, 0x2019, "curved single quotes"),
    (0x201C, 0x201D, "curved double quotes"),
    (0x2022, 0x2022, "the bullet"),
    (0x2026, 0x2026, "the ellipsis"),
    (0x203B, 0x203B, "Japanese and Korean: the reference mark"),
    (0x2192, 0x2192, "the arrow to the right, usual in steps"),
    (0x2212, 0x2212, "the minus sign"),
    (0x2264, 0x2265, "the signs for at most and at least"),
    (0x3001, 0x3002, "Japanese: comma and full stop"),
    (0x3005, 0x3005, "Japanese: the mark that repeats a kanji"),
    (0x300C, 0x300F, "Japanese: corner quotes"),
    (0x3010, 0x3011, "Japanese: lenticular brackets"),
    (0x301C, 0x301C, "Japanese: the wave dash"),
    (0x3041, 0x3096, "Japanese: hiragana"),
    (0x309D, 0x309E, "Japanese: the marks that repeat a hiragana"),
    (0x30A1, 0x30FE, "Japanese: katakana, its middle dot, long mark and repeat marks"),
    (0x3131, 0x3163, "Korean: single consonants and vowels as typed alone (the blank filler after them is not on the list)"),
    (0x4E00, 0x9FFF, "Japanese kanji and Korean hanja"),
    (0xAC00, 0xD7A3, "Korean: Hangul syllables"),
    (0xFF01, 0xFF01, "Japanese: fullwidth exclamation mark"), (0xFF08, 0xFF09, "Japanese: fullwidth round brackets"),
    (0xFF0C, 0xFF0C, "Japanese: fullwidth comma"), (0xFF1A, 0xFF1B, "Japanese: fullwidth colon and semicolon"),
    (0xFF1F, 0xFF1F, "Japanese: fullwidth question mark"),
    (0xFF5E, 0xFF5E, "Japanese: the fullwidth tilde"),
)  # fmt: skip
# Two blanks that ordinary text holds without the writer knowing: the no-break space and the ideographic space.
# They are neither refused nor on the list: each becomes a plain space before the check and in what is sent.
OTHER_SPACES = str.maketrans({"\u00a0": " ", "\u3000": " "})
_ALLOWED = frozenset(code for low, high, _ in ALLOWED for code in range(low, high + 1))

# Letters of other scripts that look like a Latin letter, mapped to that letter. A table kept by hand for the
# Cyrillic and Greek alphabets. Text that leaves cannot hold these letters at all; the table is for comparing
# other things, such as the path of the desk's folder.
LOOK_ALIKE = str.maketrans({
    "а": "a", "в": "b", "с": "c", "ԁ": "d", "е": "e", "ё": "e", "ғ": "f", "һ": "h", "н": "h", "і": "i", "ї": "i",
    "ј": "j", "к": "k", "ӏ": "l", "м": "m", "п": "n", "о": "o", "р": "p", "ԛ": "q", "г": "r", "ѕ": "s", "т": "t",
    "у": "y", "ү": "y", "ѵ": "v", "ԝ": "w", "ш": "w", "х": "x", "з": "3", "б": "6",
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u",
    "χ": "x", "ω": "w", "γ": "y", "ϲ": "c", "ϳ": "j",
})  # fmt: skip
# A digit and the letters it is written for are one, for all ten digits:
# 0 and o; 1, l and i; 2 and z; 3 and e; 4 and a; 5 and s; 6, 9 and g; 7 and t; 8 and b.
DIGIT_FOR_LETTER = str.maketrans({"0": "o", "1": "l", "i": "l", "2": "z", "3": "e", "4": "a", "5": "s", "6": "g", "7": "t", "8": "b", "9": "g"})

# A known value of six or more letters and digits in its reduced form is refused wherever it stands, whatever is
# attached to it, also with its characters or groups of them set apart by up to three characters that are not
# letters or digits; and across words, however far apart, when it begins where a word begins and ends where a
# word ends. A shorter value is refused as a whole word, also set apart like that.
SHORTEST_REDUCED = 6
MOST_BETWEEN = 3
# The long-vowel mark of Japanese looks like a dash and is counted as one: it sets letters apart and is no letter itself.
# Its half-width form becomes the same character when the text is folded.
LONG_VOWEL = "\u30fc"


def looks_like_a_secret(value: str) -> bool:
    """A value after a label such as "token:" counts only when it could be one: long, with digits among letters."""
    value = value.strip("\"'`.,;)")
    return len(value) >= 8 and any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


# How keys and tokens begin. One of these is refused wherever a word begins with it and a letter or a digit
# follows, at once or after spaces, tabs or line breaks: a key cut after its beginning is still that key.
KEY_BEGINNINGS = {
    "a GitHub token": ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
    "an API key": ("sk-",),
    "a Slack token": ("xoxa-", "xoxb-", "xoxe-", "xoxp-", "xoxr-", "xoxs-"),
    "a cloud access key": ("AKIA", "ASIA"),
    "a Google key": ("AIza",),
    "a signed web token": ("eyJ",),
}
SECRET_SHAPES = (
    *((kind, re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(map(re.escape, beginnings)) + r")[ \t\n]*[A-Za-z0-9]")) for kind, beginnings in KEY_BEGINNINGS.items()),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("a sign-in header", re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}")),
)
LABELLED = re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|relay[_-]?key|token)\s*[=:]\s*(\S{8,})")
# A long unbroken run of Latin letters and digits that holds a digit, or a capital after its first letter: a key
# or a hash. A commit is named by its first seven to twelve digits, never by all forty. A key written in small
# letters only is not found by this; a session holds no key in the first place.
LONG_RUN = re.compile(r"[A-Za-z0-9]{20,}")
WORD_WITH_PATH = re.compile(r"[^\s`'\"()<>\[\],;]+")

# A path of this computer needs a root and at least one more part: "/users" and "/tmp" alone are words an answer
# about a web task or a container needs. Addresses that mean "this machine" on every machine (localhost,
# 127.0.0.1, ::1, 0.0.0.0) say nothing about this computer and are not refused.
PRIVATE_PLACES = (
    ("a path outside the repository", re.compile(r"(?i)(?<![\w.])/+(?:users|home|root|var/+root|private|volumes|tmp|etc|opt|library)/+[^\s/]")),
    ("a path outside the repository", re.compile(r"(?i)\b[a-z]:\\")),
    ("a path outside the repository", re.compile(r"(?<![\w.])~/+[^\s/]")),
    ("a hidden folder of this computer", re.compile(r"(?<![\w-])\.(?:dodam|ssh|coworker|aws|gnupg|claude)/+[^\s/]")),
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
    the owner (what each sign-in shows about the account, and the owner's git name).
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


def composed(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def plain(text: str) -> str:
    """The text as it is sent: composed, and the two other spaces made plain spaces."""
    return composed(text).translate(OTHER_SPACES)


def not_allowed(text: str, as_given: bool = False) -> bool:
    """Whether the text holds a character that is not on the list of what the desk writes with.

    as_given is for text that the desk cannot change before it is sent (what is handed to a tool of a live
    session): there the two other spaces are refused like every character off the list.
    """
    return any(ord(character) not in _ALLOWED for character in (composed(text) if as_given else plain(text)))


def shown(text: str) -> str:
    """The text with every character taken out that is not on the list. For what the relay prints."""
    return "".join(character if ord(character) in _ALLOWED else "?" for character in plain(text))


def _folded(text: str) -> str:
    """One case, one alphabet, no accents; the separators are still there."""
    folded = unicodedata.normalize("NFKC", text).casefold().translate(LOOK_ALIKE)
    return "".join(character for character in unicodedata.normalize("NFKD", folded) if unicodedata.category(character) != "Mn")


def reduced(text: str) -> str:
    """Letters and digits only, in one case and one alphabet, a digit and the letters it stands for made one."""
    return "".join(character for character in _folded(text) if character.isalnum() and character != LONG_VOWEL).translate(DIGIT_FOR_LETTER)


@functools.lru_cache(maxsize=65536)
def _letters_of(character: str) -> str:
    """The letters and digits one written character stands for, in reduced form. None when it only marks a letter."""
    folded = _folded(character)
    return "".join(c for c in folded if c.isalnum() and c != LONG_VOWEL).translate(DIGIT_FOR_LETTER) if folded else None


class Spelled:
    """The letters and digits of a text in reduced form, and how many other characters stand before each of them.

    The other characters are counted as they are written: a mark that stands for three dots is one character.
    """

    def __init__(self, text: str):
        letters: list[str] = []
        self.gaps: list[int] = []
        gap = 1  # The text begins where a word begins.
        for character in composed(text):
            found = _letters_of(character)
            if found is None:
                continue  # An accent written by itself belongs to its letter.
            if not found:
                gap += 1
                continue
            letters.append(found)
            self.gaps += [gap] + [0] * (len(found) - 1)
            gap = 0
        self.letters = "".join(letters)
        # How many of the gaps up to each place are wider than what may stand between the characters of a value.
        self.wide = [0]
        for gap in self.gaps:
            self.wide.append(self.wide[-1] + (gap > MOST_BETWEEN))


def holds(spelled: Spelled, value: str) -> bool:
    """Whether the text holds a known value, given in reduced form."""
    if not value:
        return False
    letters, gaps, wide = spelled.letters, spelled.gaps, spelled.wide
    start = letters.find(value)
    while start != -1:
        end = start + len(value)
        whole = gaps[start] > 0 and (end == len(letters) or gaps[end] > 0)
        close = wide[end] == wide[start + 1]
        if (whole and close) or (len(value) >= SHORTEST_REDUCED and (whole or close)):
            return True
        start = letters.find(value, start + 1)
    return False


def names(text: str, machine: list[tuple[str, str]], public: tuple[str, ...] = ()) -> list[str]:
    """Which of the known values are in the text. Values that are public anyway (public) are not looked for."""
    spelled = Spelled(text)
    open_values = {reduced(value) for value in public}
    found: list[str] = []
    for kind, word in machine:
        value = reduced(word)
        if value and value not in open_values and kind not in found and holds(spelled, value):
            found.append(kind)
    return found


def _is_path_of_the_copy(word: str, copy: Path | None) -> bool:
    """Whether a word, the whole of it, is the path of a file or folder that exists in the desk's copy."""
    if copy is None:
        return False
    word = word.rstrip(".:")
    if not word or word.startswith("/") or ".." in word.split("/") or "#" in word or "?" in word:
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
    if any(looks_like_a_secret(value) for value in LABELLED.findall(plain)):
        found.append("a password or key after its label")
    for word in WORD_WITH_PATH.findall(plain):
        # The name of a file of this repository may be long and is not a key. Only the whole word counts: a word
        # with anything after the path is searched in full.
        if _is_path_of_the_copy(word, copy):
            continue
        for run in LONG_RUN.findall(word):
            # Letters only, small or with one capital at the start, is a word and passes however long it is.
            if (any(c.isdigit() for c in run) or any(c.isupper() for c in run[1:])) and "a long string shaped like a key" not in found:
                found.append("a long string shaped like a key")
    return found


NOT_ALLOWED = "a character outside what the desk writes with (English, Korean, Japanese, Spanish)"


def leaks(text: str, machine: list[tuple[str, str]] | None = None, copy: Path | None = None, public: tuple[str, ...] = (),
          as_given: bool = False) -> list[str]:  # fmt: skip
    """The kinds of thing in the text that must not leave. Empty when the text may go."""
    if not_allowed(text, as_given):
        return [NOT_ALLOWED]
    text = plain(text)
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
    # The desk cannot change what a tool of another program sends, so here the text is judged as it was given.
    found = leaks("\n".join(_strings(given)), machine, as_given=True)
    if found:
        return "The text was not sent. It holds " + ", ".join(found) + "."
    return None


READING = ("Read", "Grep", "mcp__desk__read_item")
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
NOT_TEXT = (".pdf", ".ipynb", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg")
BYTES_PER_LISTED_PATH = 200
# A search gives the lines asked for and up to two lines of Claude Code's own (a heading, a summary); a line that
# counts carries a colon and a number after the path.
SEARCH_OWN_LINES = 2
BYTES_AFTER_A_PATH = 12


def paths_of(copy: Path) -> list[int]:
    """The length of every path in the copy as a search or a listing would print it, links not followed."""
    lengths = []
    for folder, folders, files in os.walk(copy, followlinks=False):
        lengths += [len(os.path.join(folder, name).encode("utf-8", "replace")) for name in files + folders]
    return lengths


def bytes_of(call: dict, lock: dict, copy: Path) -> int | str:
    """The most bytes one reading call can hand to the session, known before it runs. A sentence when it has no such bound."""
    name, given = call.get("tool_name"), call.get("tool_input")
    given = given if isinstance(given, dict) else {}
    spending = lock["spending"]
    if name == "mcp__desk__read_item":
        return lock["read"]["max_bytes"]
    inside = given.get("path")
    # A path that does not start at the root is read from the session's working folder, which is the copy.
    if inside is not None and (not isinstance(inside, str) or not (copy / inside).resolve().is_relative_to(copy.resolve())):
        return "Search and list only inside the desk's copy."
    if name == "Grep":
        limit = given.get("head_limit")
        if given.get("output_mode") not in ("files_with_matches", "count") or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= spending["search_lines"]:
            return (
                f'Search with output_mode "files_with_matches" or "count" and a head_limit of at most {spending["search_lines"]}; '
                "then read the lines you need with Read."
            )
        return (limit + SEARCH_OWN_LINES) * (BYTES_PER_LISTED_PATH + BYTES_AFTER_A_PATH)
    # Read: a number of lines must be given, so that what one call takes is known before it runs.
    limit, offset = given.get("limit"), given.get("offset", 1)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= spending["read_lines"]:
        return f'Give Read a limit of at most {spending["read_lines"]} lines.'
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or "pages" in given:
        return "Give Read an offset as a whole number of lines, and no pages."
    path = given.get("file_path")
    if not isinstance(path, str) or path.lower().endswith(NOT_TEXT):
        return "The desk reads text files only."
    try:
        real = (copy / path).resolve()
        if not real.is_relative_to(copy.resolve()) or not real.is_file():
            return 0  # Claude Code refuses it or finds nothing; nothing is handed over.
        with open(real, "rb") as handle:
            lines = handle.read().split(b"\n")
    except OSError:
        return 0
    wanted = lines[max(offset - 1, 0) : max(offset - 1, 0) + limit]
    if any(b"\0" in line for line in wanted):
        return "The desk reads text files only."
    # Each line comes with its number in front.
    return sum(len(line) + 12 for line in wanted)


def count_call(call: dict, lock: dict, copy: Path, state: Path) -> str | None:
    """Count one tool call of a session before it runs. The reason to refuse it when a bound of the session is reached."""
    import fcntl

    import state as state_files

    spending = lock["spending"]
    session = call.get("session_id")
    session = session if isinstance(session, str) and SESSION_ID.fullmatch(session) else "unnamed"
    folder = state_files.folder(state / "session", "The folder session in the state folder")
    path = folder / f"reads-{session}.json"
    with state_files.opened(folder / f"reads-{session}.json.lock", "A count of reading calls in the state folder") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        text = state_files.read(path, "A count of reading calls in the state folder")
        counted = json.loads(text) if text else {"calls": 0, "reads": 0, "bytes": 0}
        # Every bound is the largest number that may happen: the call after it is refused before it runs.
        if counted["calls"] >= spending["calls_per_session"]:
            return f"This session made {counted['calls']} tool calls, which is the most one session may. Finish now."
        counted["calls"] += 1
        reason = None
        if call.get("tool_name") in READING:
            size = bytes_of(call, lock, copy)
            if counted["reads"] >= spending["reads_per_session"]:
                reason = f"This session made {counted['reads']} reading calls, which is the most one session may. Answer from what you have."
            elif isinstance(size, str):
                reason = size
            elif counted["bytes"] + size > spending["content_bytes_per_session"]:
                reason = (
                    f"This call could hand over {size} bytes and the session has {spending['content_bytes_per_session'] - counted['bytes']} left of what "
                    "one session may read. Ask for less, or answer from what you have."
                )
            else:
                counted["reads"] += 1
                counted["bytes"] += size
        state_files.write(path, "A count of reading calls in the state folder", json.dumps(counted))
        return reason


def hook(argv: list[str]) -> int:
    """Exit code 2 refuses the tool call. The reason goes to standard output, and to nowhere else.

    The start command's wrapper shows the session that one line and nothing of what else a failing check may
    print, so that no path of this computer reaches a session through a refusal.
    """
    try:
        sys.path.insert(0, str(HERE))
        import facts as facts_file

        options = dict(zip(argv[0::2], argv[1::2], strict=True))
        lock = json.loads(Path(options["--lock"]).read_text(encoding="utf-8"))
        facts = facts_file.load(options["--facts"])
        call = json.loads(sys.stdin.read())
        reason = judge_call(call, lock, facts, machine_words(options.get("--known")))
        if reason is None:
            reason = count_call(call, lock, Path(options["--copy"]), Path(options["--state"]))
    except Exception:  # Anything that goes wrong here refuses the call.
        reason = "The desk's lock could not be checked, so the call is refused."
    if reason is None:
        return 0
    sys.stdout.write(reason + "\n")
    return 2


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "hook":
        raise SystemExit(hook(sys.argv[2:]))
    sys.stderr.write("usage: guard.py hook --lock FILE --facts FILE --copy FOLDER --state FOLDER [--known FILE]\n")
    raise SystemExit(2)
