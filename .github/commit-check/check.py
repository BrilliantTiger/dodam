"""Checks every commit of a pull request or of a push, the pull request's own text, and the merge GitHub will make.

The rules are the README's: each is a form or a list, and every text (a message, a title, a description, a branch's
name, a person's name, a line a commit adds, a path) is matched line by line on a copy made by classes of characters,
and on the text as written. For each commit after the exempt commit named in settings.json, its addresses, names and
message (check_people_and_message), every line it adds (read by the structure of the difference, and in other
encodings when its content is not text in UTF-8) and every path git lists for it are checked; for a merge, what it
brings beyond all of its parents, its combined list read by its own shape. The difference and the paths of the whole
range are checked too. Before a merge, the merge commit GitHub will write is judged by check_people_and_message, the
same function main's push uses. Every try at GitHub's API in one run shares one budget.

What it prints is made by the check alone: a commit's id, a property's name and positions by number. It never
prints a text, a path, a title or anything else it read, also not when it cannot run.

A run fails at once on whatever breaks a rule. The way back is a new head. The same rules can be run on a title, a
description and a branch name before they are published (--text-only). It uses the standard library only, and it
never runs anything from the commits it reads.
"""

import argparse
import gc
import json
import os
import re
import re._compiler
import re._parser as parser  # the pattern language's own reading and compiling, from Python 3.11 on
import subprocess
import sys
import time
import unicodedata
import warnings
import urllib.error
import urllib.request

GITHUB_ADDRESS = "noreply" + "@" + "github.com"
NO_REPLY_RE = re.compile(r"^(?:(\d+)\+)?([A-Za-z0-9](?:-?[A-Za-z0-9]){0,38})@users\.noreply\.github\.com$")
ENVIRONMENT = "private-check"
SETTINGS_PATH = ".github/commit-check/settings.json"
SETTINGS_KEYS = {"exempt_through", "private_pattern_expected", "fork_way_open", "outside_runs_held_since"}

# Each rule is a tuple of parts, so that the check's own breaking can take out one part at a time. The parts are
# written in pieces, so that this file does not trip the check itself.

# The names of tools, as a list: only where the property cannot be closed by its form (a person's name, a branch
# named after a tool, "generated with" inside a file's line). Compared on the normalised text.
AI_NAMES = ("cl" "aude", "anthrop" "ic", "open" "ai", "chat" "gpt", r"gpt(?:-?\d+[a-z]*)?", "co" "dex", "gem" "ini", "ba" "rd",
            "co" "pilot", "cur" "sor", "dev" "in", "anti" "gravity", "ll" "ama", "mis" "tral", "deep" "seek", "gr" "ok",
            "wind" "surf", "ai" "der", "cl" "ine", "qw" "en", "tab" "nine", "code" "ium", "code" "whisperer", "ju" "les",
            "perple" "xity", "o" "1", "o" "3", r"o4(?:-mini)?")
NAME_TOKENS = ("b" "ot",)


def any_of(parts):
    return "(?:" + "|".join(parts) + ")"


AI_NAME_RE = re.compile(r"(?<![a-z0-9])" + any_of(AI_NAMES + NAME_TOKENS) + r"(?![a-z])")
BRANCH_TOOL_RE = re.compile(r"^" + any_of(AI_NAMES) + r"/")
FILE_MADE_VERBS = ("gener" "ated", "co-auth" "ored")
GENERATED_IN_FILE_RE = re.compile(any_of(FILE_MADE_VERBS) + r"(?:\s+|-)(?:with|by|using|via)\s*:?\s*\W{0,6}"
                                  r"(?:the help of\s+\W{0,6})?" + any_of(AI_NAMES))

# Attribution in prose, closed by its form. A trailer's key has no space; the closed list of keys with a hyphen holds
# in the last paragraph, where trailers stand; a key of words that ends in "by", and a change id, are refused on every
# line; a one-word key is refused when it is a word of attribution or its value holds an address; a line that begins
# by saying how the text was made is refused whatever it names.
KEY_RE = re.compile(r"^\s*([a-z][a-z0-9]*(?:-[a-z0-9]+)*)\s*:\s*(.*)$")
BY_KEY_RE = re.compile(r"^[ *-]*[a-z0-9]+(?: [a-z0-9]+)* by\s*:")
CHANGE_ID_RE = re.compile(r"^[ *-]*change id\s*:")
CO_AUTHOR_KEY = "co-" "authored-" "by"
CO_AUTHOR_VALUE_RE = re.compile(r"^(.*?)\s*<([^<>]+)>\s*$")
ATTRIBUTION_WORDS = ("author", "authors", "committer", "agent", "model", "models", "tool", "tools", "assistant",
                     "assistants", "bot", "bots", "ai", "llm", "generator", "generated", "assisted", "credit", "credits",
                     "via", "coauthor", "coauthors")
ADDRESS_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
MADE_VERBS = ("gener" "ated", "co-?auth" "ored", "assi" "sted", "auth" "ored", "writ" "ten", "draf" "ted", "produ" "ced",
              "ma" "de", "crea" "ted", "bu" "ilt", "prepa" "red", "implemen" "ted")
MADE_LINKS = ("with", "by", "using", "via", "through")
MADE_PHRASES = ("with (?:the )?help (?:of|from)", "ai-gener" "ated", "ai-assi" "sted")
MADE_FORM_RE = re.compile(r"^\W*(?:" + any_of(MADE_VERBS) + r"(?:\s+|-)" + any_of(MADE_LINKS) + "|" + any_of(MADE_PHRASES)
                          + r")(?![a-z])")
ROBOT = "\N{ROBOT FACE}"

# Every place: messages, titles, descriptions, branch names, added lines, paths. Matched on the normalised text.
TRACKER_CODES = (r"(?<![a-z0-9])d" "od" r"[-_ ]?\d+(?![0-9])",)
TRACKER_ADDRESSES = (r"lin" r"ear\.app",)
TOOLING_IDS = ("te" "rm_[0-9a-f]{8,}", "ta" "sk_[0-9a-f]{8,}", "c" "tx_[0-9a-f]{8,}", "dis" "patch_[0-9a-f]{8,}",
               "ms" "g_[0-9a-f]{8,}", "w" "f_[0-9a-f]{8,}", "conver" "sation://")
LOCAL_PATHS = (
    r"(?:^|[\\/])us" r"ers[\\/]+(?!shared[\\/])[^\\/\s]+[\\/]",                  # a home folder of macOS or Windows
    r"(?<![a-z0-9])\.cla" r"ude[\\/]+[\w.-]",                                  # an entry in a tool's own folder
    r"(?<![a-z0-9])\.co" r"dex[\\/]+[\w.-]",
    r"(?<![a-z0-9])\.gem" r"ini[\\/]+[\w.-]",
    r"(?<![a-z0-9])\.or" r"ca[\\/]+[\w.-]",
    r"(?<![a-z0-9])\.cla" r"ude\.json",                                         # the settings file beside it
    r"or" r"ca[\\/]+work" r"spaces[\\/]",                                          # the sessions' working folders
    r"or" r"ca[\\/]+proj" r"ects[\\/]",
    r"(?<![a-z0-9])cla" r"ude-\d+[\\/]",                                           # the sessions' temporary folders
    r"[\\/]va" r"r[\\/]+folders[\\/]+[a-z0-9_-]{2}[\\/]",
    r"-us" r"ers-[a-z0-9._-]+-or" r"ca-",                                          # a folder named after a home folder
)
PLACE_RULES = [
    ("tracker code", re.compile(any_of(TRACKER_CODES))),
    ("tracker address", re.compile(any_of(TRACKER_ADDRESSES))),
    ("tooling id", re.compile(any_of(TOOLING_IDS))),
    ("local path", re.compile(any_of(LOCAL_PATHS))),
]


class Problem(Exception):
    """A reason the check cannot run, in fixed words of its own."""


# The classes of characters that the copy for matching takes out: marks set on or around another character, format,
# control, surrogate, private use and unassigned. Line breaks and spaces are set aside before them.
REMOVED_CATEGORIES = ("Mn", "Me", "Cf", "Cc", "Cs", "Co", "Cn")


def lines_of(text):
    """The lines of a text: every line or paragraph break that the language's own line splitting knows ends one."""
    return text.splitlines() or [""]


def normalise(text, words=False):
    """The copy of one line that the rules are matched on, made by classes of characters, in this order: taken apart by
    compatibility; every space one space, and runs of them one; the classes above taken out, and every letter whose
    name holds FILLER; every dash of the category Pd, and every mathematical sign whose name holds MINUS but not
    SET, one dash; put together again; for a name or a branch (words), a word split where a small letter meets a
    capital and where capitals meet a capital followed by a small letter, and at an underscore; letter case folded.
    FILLER, MINUS and SET are whole words of a character's name.
    An accent is folded away with its mark."""
    out = []
    for ch in unicodedata.normalize("NFKD", text):
        category = unicodedata.category(ch)
        if ch.isspace():
            if not out or out[-1] != " ":
                out.append(" ")
        elif category in REMOVED_CATEGORIES:
            continue
        elif category == "Lo" and "FILLER" in unicodedata.name(ch, "").split():
            continue
        elif category == "Pd" or (category == "Sm" and "MINUS" in unicodedata.name(ch, "").split()
                                  and "SET" not in unicodedata.name(ch, "").split()):
            out.append("-")
        else:
            out.append(ch)
    copy = unicodedata.normalize("NFC", "".join(out))
    if words:
        copy = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", copy).replace("_", " ")
    return copy.casefold()


def git(repo, *args):
    done = subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false", *args],
                          capture_output=True, env=git_env())
    if done.returncode != 0:
        raise Problem("git could not read the history")
    return done.stdout.decode("utf-8", "replace")


def git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
    return env


class Checker:
    def __init__(self, private, verifier):
        self.private = private
        self.verifier = verifier
        self.findings = []

    def add(self, who, prop, where):
        self.findings.append(f"{who}: {prop} ({where})")

    def private_hit(self, text, words=False):
        copies = [text, normalise(text)] + ([normalise(text, words=True)] if words else [])
        return self.private is not None and any(self.private.search(copy) is not None for copy in copies)

    def place(self, who, text, where, words=False):
        """The rules of every place, on every line of the text; for a branch's name also on the copy split where a
        small letter meets a capital."""
        found = set()
        for line in lines_of(text):
            for plain in [normalise(line)] + ([normalise(line, words=True)] if words else []):
                for prop, pattern in PLACE_RULES:
                    if pattern.search(plain):
                        found.add(prop)
            if self.private_hit(line, words):
                found.add("private pattern")
        for prop, _ in PLACE_RULES + [("private pattern", None)]:
            if prop in found:
                self.add(who, prop, where)

    def name_passes(self, name, address):
        """A name passes when it names no AI and does not match the private pattern; a name that is the whole login
        inside its own no-reply address, but for letter case, is spared the private pattern only, because GitHub shows
        that login in the address anyway (and writes the account's displayed name, which may differ from it in case
        only, on the merge commits it makes)."""
        if AI_NAME_RE.search(normalise(name, words=True)) or AI_NAME_RE.search(normalise(name)):
            return False
        login = NO_REPLY_RE.match(address)
        return bool(login and name.casefold() == login.group(2).casefold()) or not self.private_hit(name)

    def prose(self, who, text, where_line):
        """A message, a title, a description: the rules of every place, and attribution closed by its form."""
        lines = lines_of(text)
        last = len(lines)
        while last > 0 and not lines[last - 1].strip():
            last -= 1
        first_of_last = last
        while first_of_last > 0 and lines[first_of_last - 1].strip():
            first_of_last -= 1
        for number, line in enumerate(lines, 1):
            where = where_line(number)
            self.place(who, line, where)
            plain = normalise(line)
            spaced = re.sub(r"[-\s]+", " ", plain)
            key = KEY_RE.match(plain)
            co_author = key is not None and key.group(1) == CO_AUTHOR_KEY
            if co_author:
                if not self.co_author_passes(line):
                    self.add(who, "attribution line", where)
                continue
            if ROBOT in line or MADE_FORM_RE.search(plain) or BY_KEY_RE.search(spaced) or CHANGE_ID_RE.search(spaced):
                self.add(who, "attribution line", where)
                continue
            if not key:
                continue
            word, value = key.group(1), key.group(2)
            if "-" in word:
                if number > first_of_last:
                    self.add(who, "attribution line", where)
            elif word in ATTRIBUTION_WORDS or ADDRESS_RE.search(value):
                self.add(who, "attribution line", where)

    def co_author_passes(self, line):
        written = unicodedata.normalize("NFKC", line)
        person = CO_AUTHOR_VALUE_RE.match(written.split(":", 1)[1].strip()) if ":" in written else None
        return bool(person and NO_REPLY_RE.match(person.group(2).strip())
                    and self.name_passes(person.group(1).strip(), person.group(2).strip()))

    def branch(self, who, name, where):
        self.place(who, name, where, words=True)
        if BRANCH_TOOL_RE.search(normalise(name)):
            self.add(who, "AI name", where)

    def added_line(self, who, file_number, line_number, line):
        where = f"file {file_number}, line {line_number}"
        self.place(who, line, where)
        if any(GENERATED_IN_FILE_RE.search(normalise(part)) for part in lines_of(line)):
            self.add(who, "attribution line", where)

    def read_as(self, who, number, label, text):
        """A content read in another encoding: every rule of an added line, each property named once."""
        seen = set()
        for line_number, line in enumerate(lines_of(text), 1):
            trial = Checker(self.private, self.verifier)
            trial.added_line(who, number, line_number, line)
            for finding in trial.findings:
                prop = finding.split(": ", 1)[1].split(" (", 1)[0]
                if prop not in seen:
                    seen.add(prop)
                    self.add(who, prop, f"path {number}, read as {label}, line {line_number}")

    def path(self, who, number, path):
        self.place(who, path, f"path {number}")

    def patch(self, who, patch):
        """The lines a difference of one side adds, each by the number of its file in the difference."""
        for section, number, content in added_lines(patch):
            self.added_line(who, section, number, content)


def added_lines(patch):
    """Reads an ordinary difference by its structure: a file's header only outside a hunk, a hunk by the lengths its
    own header gives (its unchanged lines counted, its removed lines passed over). Gives each added line with the number of its file's section and its line number in
    the result."""
    lines = patch.split("\n")
    index = 0
    section = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith("diff --git "):
            section += 1
            continue
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if not hunk and line.startswith("@@"):
            raise Problem("a difference cannot be read")
        if not hunk:
            continue  # a header line of the file, or a removed line after a hunk's last line of the result
        number = int(hunk.group(1))
        new_left = int(hunk.group(2)) if hunk.group(2) is not None else 1
        while new_left > 0:
            if index >= len(lines):
                raise Problem("a difference cannot be read")
            body = lines[index]
            index += 1
            if body.startswith("\\"):
                continue  # "\ No newline at end of file"
            if body[:1] not in ("+", "-", " ") or body == "":
                raise Problem("a difference cannot be read")
            if body[:1] == "-":
                continue  # a removed line
            new_left -= 1
            if body[:1] == "+":
                yield section, number, body[1:]
            number += 1


QUOTED = set(range(0x20)) | {0x22, 0x5C, 0x7F}  # bytes git writes quoted in a header, also with quoting turned off
BY_ENTRY = [False]  # the tests read every shape through both forms


def git_bytes(repo, *args):
    """git's output as the bytes it gave: paths are bytes, and a byte that is no UTF-8 is never replaced."""
    done = subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false", *args], capture_output=True, env=git_env())
    if done.returncode != 0:
        raise Problem("git could not read the history")
    return done.stdout


def entries_of(listing):
    """The entries of --name-status -z of one side: (status, paths), the paths as bytes."""
    fields = listing.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    entries = []
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii", "replace")
        names = 2 if status[:1] in ("R", "C") else 1
        if index + names >= len(fields) or not status:
            raise Problem("a list of paths cannot be read")
        entries.append((status, fields[index + 1:index + 1 + names]))
        index += 1 + names
    return entries


def sections_of(patch):
    """The sections of a patch, each beginning with its header line."""
    starts = [0] if patch.startswith(b"diff --git ") else []
    position = patch.find(b"\ndiff --git ")
    while position != -1:
        starts.append(position + 1)
        position = patch.find(b"\ndiff --git ", position + 1)
    if patch and not starts:
        raise Problem("a difference cannot be read")
    return [patch[start:end] for start, end in zip(starts, starts[1:] + [len(patch)])]


def lines_against(repo, base, sha):
    """The lines one ordinary difference adds, by the path of the result (bytes) and the line number. A hunk belongs to
    the entry of the list separated by NUL bytes it is paired with: by their order, a change of kind being two
    sections and every other entry one, each pairing held against the section's own header; or, when a path holds a
    byte git writes quoted in a header, by one difference for each entry, asked by its own paths as literal paths."""
    entries = entries_of(git_bytes(repo, "diff", "--name-status", "-z", "-M", base, sha))
    pairs = []
    if BY_ENTRY[0] or any(byte in QUOTED for _, paths in entries for path in paths for byte in path):
        for _, paths in entries:
            sections = sections_of(git_bytes(repo, "diff", *DIFF_OPTIONS, base, sha, "--",
                                             *[b":(literal)" + path for path in paths]))
            if not sections:
                raise Problem("a difference cannot be read")
            pairs += [(section, paths[-1]) for section in sections]
    else:
        sections = sections_of(git_bytes(repo, "diff", *DIFF_OPTIONS, base, sha))
        expected = [paths for status, paths in entries for _ in range(2 if status[:1] == "T" else 1)]
        if len(sections) != len(expected):
            raise Problem("a difference cannot be read")
        for section, paths in zip(sections, expected):
            if section.split(b"\n", 1)[0] != b"diff --git a/" + paths[0] + b" b/" + paths[-1]:
                raise Problem("a difference cannot be read")
            pairs.append((section, paths[-1]))
    added = {}
    for section, path in pairs:
        for _, number, content in added_lines(section.decode("utf-8", "replace")):
            added[(path, number)] = content
    return added


def check_lines(checker, who, repo, bases, sha, numbering):
    """The lines a commit adds: of one difference against each base, those added against every base, by the path of
    the result and the line number; each named by its entry's number in the commit's own list of paths."""
    own = None
    for base in bases:
        added = lines_against(repo, base, sha)
        own = added if own is None else {key: content for key, content in own.items() if added.get(key) == content}
    numbers = {path: number for number, path in enumerate(numbering, 1)}
    for (path, number), content in sorted(own.items(), key=lambda item: (numbers.get(item[0][0], 0), item[0][1])):
        if path not in numbers:
            raise Problem("a difference cannot be read")
        checker.added_line(who, numbers[path], number, content)


def empty_tree(repo):
    done = subprocess.run(["git", "-C", repo, "hash-object", "-t", "tree", "--stdin"], input=b"", capture_output=True,
                          env=git_env())
    if done.returncode != 0:
        raise Problem("git could not read the history")
    return done.stdout.decode("ascii").strip()


def parse_commit(raw):
    header, _, message = raw.partition("\n\n")
    people = {}
    for line in header.split("\n"):
        for role in ("author", "committer"):
            if line.startswith(role + " "):
                found = re.match(r"^" + role + r" (.*) <([^<>]*)> \d+ [+-]\d{4}$", line)
                if not found or role in people:
                    raise Problem(f"the {role} line of a commit cannot be read")
                people[role] = (found.group(1), found.group(2))
    if set(people) != {"author", "committer"}:
        raise Problem("a commit has no author or no committer")
    return people, message


DIFF_OPTIONS = ["--no-color", "--no-ext-diff", "--no-textconv", "--text", "-M"]


def check_paths(checker, who, fields, parents=1):
    """The paths a difference adds, renames, copies or changes, from --name-status -z, numbered in git's order. A
    combined list (a merge's, more than one parent) gives one path for every entry, a renamed one too."""
    fields = [field for field in fields if field != ""]
    index = 0
    number = 0
    while index < len(fields):
        status = fields[index]
        number += 1
        if status[:1] in ("R", "C") and parents == 1:
            if index + 2 >= len(fields):
                raise Problem("a list of paths cannot be read")
            checker.path(who, number, fields[index + 2])
            index += 3
        else:
            if index + 1 >= len(fields):
                raise Problem("a list of paths cannot be read")
            if not status.startswith("D"):
                checker.path(who, number, fields[index + 1])
            index += 2


READINGS = [(f"UTF-{bits}{order} from byte {start}", f"utf-{bits}-{order.lower()}", start)
            for bits, width in ((16, 2), (32, 4)) for order in ("LE", "BE") for start in range(width)]


def new_blobs(fields):
    """The new contents of the paths of a raw list made with -z, numbered as check_paths numbers them; a deleted path
    is counted and has none."""
    fields = [field for field in fields if field != ""]
    found = []
    index = 0
    number = 0
    while index < len(fields):
        header = fields[index]
        if not header.startswith(":"):
            raise Problem("a list of paths cannot be read")
        parts = header.lstrip(":").split()
        parents = len(header) - len(header.lstrip(":"))
        if len(parts) < 2 * (parents + 1) + 1:
            raise Problem("a list of paths cannot be read")
        status = parts[-1]
        new = parts[-2]
        number += 1
        names = 2 if status[:1] in ("R", "C") and parents == 1 else 1
        if index + names >= len(fields):
            raise Problem("a list of paths cannot be read")
        if new != "0" * 40:  # a deleted path has no new content
            found.append((number, new))
        index += 1 + names
    return found


def blob_contents(repo, shas):
    """The contents of blobs, read in one call of git."""
    if not shas:
        return {}
    done = subprocess.run(["git", "-C", repo, "cat-file", "--batch"], input="".join(f"{sha}\n" for sha in shas).encode(),
                          capture_output=True, env=git_env())
    if done.returncode != 0:
        raise Problem("git could not read the history")
    data = done.stdout
    contents = {}
    position = 0
    for sha in shas:
        end = data.index(b"\n", position)
        header = data[position:end].decode("ascii", "replace").split()
        if len(header) != 3 or header[1] != "blob":
            raise Problem("git could not read the history")
        size = int(header[2])
        contents[sha] = data[end + 1:end + 1 + size]
        position = end + 1 + size + 1
    return contents


def check_other_encodings(checker, who, repo, fields):
    """A new content that is not text in UTF-8, or holds a zero byte, is read once more in each encoding of READINGS,
    passing over the bytes that are not valid there, and once more as UTF-8 with its zero bytes taken out."""
    blobs = new_blobs(fields)
    contents = blob_contents(repo, sorted({sha for _, sha in blobs}))
    for number, sha in blobs:
        data = contents[sha]
        try:
            data.decode("utf-8")
            if b"\0" not in data:
                continue
        except UnicodeDecodeError:
            pass
        readings = [(label, data[start:].decode(codec, "ignore")) for label, codec, start in READINGS]
        readings.append(("UTF-8 without its zero bytes", data.replace(b"\0", b"").decode("utf-8", "ignore")))
        for label, text in readings:
            checker.read_as(who, number, label, text)


def check_people_and_message(checker, who, people, message, signed, merge=False):
    """The verdict on a commit's names, addresses and message, for every commit main's push reads and every commit a
    pull request's run reads, the merge it builds among them, so that both judge a merge the same way. GitHub's
    signature is asked (signed()) only of a commit that GitHub's address committed; with no people, only the message."""
    by_github = people is not None and people["committer"][1] == GITHUB_ADDRESS
    for role in ("author", "committer") if people is not None else ():
        name, address = people[role]
        if role == "committer" and by_github:
            if not signed():
                checker.add(who, "committer address", "GitHub's address without GitHub's verified signature")
            elif name != "GitHub":
                checker.add(who, "committer name", "a commit GitHub signed carries the name GitHub")
            continue
        login = NO_REPLY_RE.match(address)
        if not login:
            checker.add(who, f"{role} address", "not a GitHub no-reply address")
        if not checker.name_passes(name, address):
            checker.add(who, f"{role} name", "neither the login of its address nor a plain name")
        elif merge and by_github and not (login and name.casefold() == login.group(2).casefold()):
            # GitHub writes the merging account's displayed name here; it must be that account's login but for letter
            # case (GitHub writes the login when there is no displayed name), never a name the check cannot tell apart.
            checker.add(who, "author name", "a merge GitHub signed carries the merging account's login, but for case")
    checker.prose(who, message, lambda n: f"message line {n}")


def check_commit(repo, sha, checker):
    who = f"commit {sha[:12]}"
    people, message = parse_commit(git(repo, "cat-file", "commit", sha))
    parent_shas = git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]
    parents = len(parent_shas)
    check_people_and_message(checker, who, people, message, lambda: checker.verifier(sha), merge=parents > 1)
    # The combined forms: for a commit of one parent (and for the first commit) they give what the plain forms give;
    # for a merge, only what it brings beyond all of its parents.
    fields = git(repo, "diff-tree", "-r", "-c", "--root", "--no-commit-id", "--name-status", "-z", "-M", sha)
    listing = git_bytes(repo, "diff-tree", "-r", "-c", "--root", "--no-commit-id", "--name-status", "-z", "-M", sha)
    if parents > 1:
        numbering = [field for field in listing.split(b"\0") if field != b""][1::2]  # one path for every entry
    else:
        numbering = [paths[-1] for _, paths in entries_of(listing)]
    check_lines(checker, who, repo, parent_shas or [empty_tree(repo)], sha, numbering)
    check_paths(checker, who, fields.split("\0"), parents)
    raw = git(repo, "diff-tree", "-r", "-c", "--root", "--no-commit-id", "--raw", "-z", "-M", sha)
    check_other_encodings(checker, who, repo, raw.split("\0"))


API_BUDGET = 90.0  # seconds for every try at GitHub's API in one run, on a clock that only runs forward
API_TRY = 10.0  # seconds at most for one try
API_WAITS = (2, 5, 10, 20)  # seconds between the tries of one thing asked
API_SPENT = {"deadline": None}
SILENT = "GitHub's API did not answer"


def is_text(value):
    return isinstance(value, str)


def is_number(value):
    return isinstance(value, int) and not isinstance(value, bool)


# The questions the check asks GitHub, and what an answer to each must be: JSON of the type the question expects,
# naming what was asked, with the fields the verdict reads in their types. One row each.
ANSWERS = {
    "commit": lambda data, sha: (isinstance(data, dict) and data.get("sha") == sha
                                 and isinstance(data.get("commit"), dict)
                                 and isinstance(data["commit"].get("verification"), dict)
                                 and isinstance(data["commit"]["verification"].get("verified"), bool)
                                 and isinstance(data["commit"].get("committer"), dict)
                                 and is_text(data["commit"]["committer"].get("email"))),
    "user": lambda data, login: (isinstance(data, dict) and is_text(data.get("login"))
                                 and data["login"].casefold() == login.casefold() and is_number(data.get("id"))
                                 and (data.get("name") is None or is_text(data.get("name")))),
    "environment": lambda data, name: (isinstance(data, dict) and data.get("name") == name
                                       and isinstance(data.get("protection_rules"), list)),
    "pull": lambda data, number: (isinstance(data, dict) and data.get("number") == number
                                  and is_number(data.get("changed_files")) and isinstance(data.get("draft"), bool)
                                  and isinstance(data.get("base"), dict) and is_text(data["base"].get("ref"))),
    "files": lambda data, _: isinstance(data, list) and all(
        isinstance(entry, dict) and is_text(entry.get("filename")) and is_text(entry.get("status"))
        and (entry.get("previous_filename") is None or is_text(entry.get("previous_filename"))) for entry in data),
    "folder": lambda data, _: isinstance(data, list) and all(
        isinstance(entry, dict) and is_text(entry.get("name")) and is_text(entry.get("type")) for entry in data),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect is no answer of GitHub about what was asked


OPENER = urllib.request.build_opener(NoRedirect)


def nothing_there(error):
    """A 404 with a JSON content type and a body that is an object with a text message: GitHub's word that what was
    asked is not there. Nothing else is."""
    try:
        if error.code != 404 or "json" not in (error.headers.get("Content-Type") or "") \
                or error.headers.get("Retry-After") is not None or error.headers.get("X-RateLimit-Remaining") == "0":
            return False
        body = json.loads(error.read().decode("utf-8"))
    except Exception:
        return False  # a body that cannot be read
    finally:
        error.close()
    return isinstance(body, dict) and is_text(body.get("message"))


def api_get(question, path, subject, outside_repository=False):
    """One answer of GitHub's API to one of the questions in ANSWERS. None only when GitHub answers that what was asked
    for is not there. Anything else that is not an answer of the expected shape is asked again, up to five times in
    all with waits of 2, 5, 10 and 20 seconds between the tries, all tries of the run within one budget; when they or
    the budget run out, the check stops: GitHub's API did not answer."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository):
        raise Problem(SILENT)
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{api}/{path}" if outside_repository else f"{api}/repos/{repository}/{path}"
    request = urllib.request.Request(url, headers=headers)
    if API_SPENT["deadline"] is None:
        API_SPENT["deadline"] = time.monotonic() + API_BUDGET
    for wait in API_WAITS + (None,):
        data, read = None, False
        try:
            with OPENER.open(request, timeout=min(API_TRY, API_SPENT["deadline"] - time.monotonic())) as answer:
                if answer.status == 200 and "json" in (answer.headers.get("Content-Type") or ""):
                    data, read = json.load(answer), True
        except urllib.error.HTTPError as error:
            if nothing_there(error):
                return None
        except Exception:
            pass  # no answer: a time-out, a failed connection, a body that is no JSON
        if read and ANSWERS[question](data, subject):  # judged outside: the judge's own faults are not "no answer"
            return data
        if wait is None or wait >= API_SPENT["deadline"] - time.monotonic():
            break
        time.sleep(wait)
    raise Problem(SILENT)


def github_api_verifier(sha):
    data = api_get("commit", f"commits/{sha}", sha)
    if data is None:
        return False  # GitHub says there is no such commit: it signed none
    return data["commit"]["verification"]["verified"] is True and data["commit"]["committer"]["email"] == GITHUB_ADDRESS


def environment_waits_for_a_named_reviewer():
    """Whether the environment of a fork's job makes every run wait for a reviewer that it names."""
    data = api_get("environment", f"environments/{ENVIRONMENT}", ENVIRONMENT)
    if data is None:
        return False
    return any(isinstance(rule, dict) and rule.get("type") == "required_reviewers"
               and isinstance(rule.get("reviewers"), list) and len(rule["reviewers"]) > 0
               for rule in data["protection_rules"])


def read_settings(path):
    try:
        with open(path, encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        raise Problem("settings.json cannot be read") from None
    if not isinstance(settings, dict) or set(settings) != SETTINGS_KEYS \
            or not isinstance(settings["exempt_through"], str) \
            or not re.fullmatch(r"[0-9a-f]{40}", settings["exempt_through"]) \
            or not isinstance(settings["private_pattern_expected"], bool) or not isinstance(settings["fork_way_open"], bool) \
            or not (settings["outside_runs_held_since"] is None
                    or (isinstance(settings["outside_runs_held_since"], str)
                        and re.fullmatch(r"20\d\d-[01]\d-[0-3]\d", settings["outside_runs_held_since"]))):
        raise Problem("settings.json does not hold exempt_through (a full commit id), private_pattern_expected and "
                      "fork_way_open (true or false), outside_runs_held_since (a date or null), and nothing else")
    return settings


def loosened_settings(repo, commit, settings):
    """A branch is judged by its own copy, so it may not loosen the settings that its start has."""
    commit = git(repo, "rev-parse", "--verify", commit + "^{commit}").strip()
    listed = git(repo, "ls-tree", "--name-only", commit, "--", SETTINGS_PATH).strip()
    if not listed:
        return []  # the first merge of the check: nothing to compare with
    try:
        before = json.loads(git(repo, "show", f"{commit}:{SETTINGS_PATH}"))
    except ValueError:
        return ["settings: the settings on the range's start cannot be read"]
    if not isinstance(before, dict):
        return ["settings: the settings on the range's start cannot be read"]
    found = []
    if before.get("private_pattern_expected") is True and not settings["private_pattern_expected"]:
        found.append("settings: the private pattern is expected on the range's start, and this branch says it is not")
    if before.get("exempt_through") != settings["exempt_through"]:
        found.append("settings: the exempt commit differs from the range's start")
    return found


def read_event(path):
    try:
        with open(path, encoding="utf-8") as handle:
            event = json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        raise Problem("the event cannot be read") from None
    pull = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pull, dict):
        raise Problem("the event names no pull request")
    head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
    head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    head_owner = head_repo.get("owner") if isinstance(head_repo.get("owner"), dict) else {}
    repository = event.get("repository") if isinstance(event.get("repository"), dict) else {}
    base_owner = repository.get("owner") if isinstance(repository.get("owner"), dict) else {}
    texts = {"title": pull.get("title") or "", "body": pull.get("body") or "", "branch": head.get("ref") or "",
             "head_owner": head_owner.get("login") or "", "base_owner": base_owner.get("login") or ""}
    if not all(isinstance(value, str) for value in texts.values()):
        raise Problem("the event's pull request cannot be read")
    texts["number"] = pull.get("number") if isinstance(pull.get("number"), int) else 0
    return texts


LOGIN_RE = re.compile(r"[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}")


def merging_logins(repo, start, context):
    """The accounts whose displayed name GitHub will write as the author of the merge commit: the one that made the
    last merge GitHub signed on the range's start (read from that commit's no-reply address, so no name stands in a
    file), and on the project's own branches the account that pushed, when it is another. On a fork's run the account
    that pushed is the outside contributor, who never makes the merge commit, so it is not read."""
    logins = []
    log = git(repo, "log", "--first-parent", "--merges", "-n", "50", "--format=%ae%x00%ce", start)
    for line in log.split("\n"):
        author, _, committer = line.partition("\0")
        found = NO_REPLY_RE.match(author)
        if committer == GITHUB_ADDRESS and found:
            logins.append(found.group(2))
            break
    actor = os.environ.get("GITHUB_ACTOR", "")
    if context == "own" and LOGIN_RE.fullmatch(actor) and actor.casefold() not in {login.casefold() for login in logins}:
        logins.append(actor)
    return logins


def merge_message(texts):
    """The message GitHub writes on the merge commit of a pull request."""
    owner = texts["head_owner"] or texts["base_owner"]
    return f"Merge pull request #{texts['number']} from {owner}/{texts['branch']}\n\n{texts['title']}"


STAND_IN_AUTHOR = ("merging-account", "0+merging-account@users.noreply.github.com")


def build_merges(checker, repo, base, head, texts, context):
    """The merge commits GitHub will write when it merges the pull request into main's tip as it is now: git's merge
    of the two, both parents, the whole merge message, GitHub as the committer, and as the author each account that
    may merge (its displayed name, or its login when it has none, and its no-reply address). Nothing of the pull
    request is run: git only merges trees."""
    done = subprocess.run(["git", "-C", repo, "merge-tree", "--write-tree", "--no-messages", base, head],
                          capture_output=True, env=git_env())
    if done.returncode != 0:  # 1: the two do not merge cleanly
        raise Problem("the pull request does not merge cleanly with main's tip" if done.returncode == 1
                      else "git could not read the history")
    tree = done.stdout.decode("ascii", "replace").split("\n", 1)[0].strip()
    authors = []
    logins = merging_logins(repo, base, context)
    for login in logins:
        data = api_get("user", f"users/{login}", login, outside_repository=True)
        if data is None:
            checker.add("merge commit", "author name", "the displayed name of the account that merges cannot be read")
            continue
        authors.append((data.get("name") or login, f"{data['id']}+{login}@users.noreply.github.com"))
    if not logins:
        authors.append(STAND_IN_AUTHOR)  # nobody known who merges: the merge's message, paths and lines still count
    merges = []
    for name, address in authors:
        env = {**git_env(), "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": address, "GIT_COMMITTER_NAME": "GitHub",
               "GIT_COMMITTER_EMAIL": GITHUB_ADDRESS}
        made = subprocess.run(["git", "-C", repo, "commit-tree", tree, "-p", base, "-p", head, "-F", "-"],
                              input=merge_message(texts).encode("utf-8"), capture_output=True, env=env)
        if made.returncode != 0:
            checker.add("merge commit", "author name", "the displayed name of the account that merges cannot be written")
            continue
        merges.append(made.stdout.decode("ascii", "replace").strip())
    return merges


def check_pull_request_text(checker, texts):
    checker.prose("pull request", texts["title"], lambda n: "pull request title")
    checker.prose("pull request", texts["body"], lambda n: f"pull request description line {n}")
    # GitHub writes the branch's name into the message of the merge commit it makes on main.
    checker.branch("pull request", texts["branch"], "pull request branch name")


def report(commits, findings, failures):
    if commits is not None:
        print(f"{len(commits)} commits checked")
    for finding in findings:
        print(finding)
    for failure in failures:
        print(f"::error::{failure}")
    if findings or failures:
        print(f"{len(findings) + len(failures)} problems")
        return 1
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse would print what it was given; the check prints nothing of its input.
        print("::error::the check was not called as its workflows call it")
        sys.exit(2)


class Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_given", set())
        if self.dest in seen:
            parser.error("given twice")
        namespace._given = seen | {self.dest}
        setattr(namespace, self.dest, values)


def main(argv=None):
    parser = Parser(description="Checks every commit of a pull request or of a push, and the pull request's text.")
    parser.add_argument("--repo", default=".", action=Once)
    parser.add_argument("--from", dest="start", action=Once)
    parser.add_argument("--head", action=Once)
    parser.add_argument("--settings", required=True, action=Once)
    parser.add_argument("--context", required=True, choices=("own", "fork-approved"), action=Once,
                        help="own: the project's own branch; fork-approved: a fork's run in the environment private-check")
    parser.add_argument("--compare-settings", metavar="COMMIT", action=Once,
                        help="the commit whose settings.json this branch may not loosen (main, or the push's before)")
    parser.add_argument("--event", action=Once, help="the event's file, for the pull request's title, description and branch")
    parser.add_argument("--branch", action=Once, help="the name of the branch a push went to")
    parser.add_argument("--require-commits", action="store_true", help="fail when there is no commit to check")
    parser.add_argument("--build-merge", action="store_true",
                        help="a pull request's run: build the merge GitHub will make of --head into --from (main's tip) "
                             "and judge it as main's push will")
    parser.add_argument("--check-fork-way", action="store_true",
                        help="on main: when the way for forks is open, ask GitHub whether everything it needs is set")
    parser.add_argument("--fork-way-gate", action="store_true",
                        help="the first job of a fork's run: fail unless the way for forks is open and the environment "
                             "names a reviewer; then say open=true to the job's outputs")
    parser.add_argument("--text-only", action="store_true",
                        help="check only the pull request's text in --event, before it is published; no git")
    parser.add_argument("--verified-shas", action=Once, help="a file of commits GitHub signed, in place of asking GitHub")
    args = parser.parse_args(argv)
    API_SPENT["deadline"] = None  # one budget for each run
    try:
        return run(args)
    except Problem as problem:
        print(f"::error::{problem}")
        return 1
    except Exception:
        print("::error::the check stopped on something it does not know")
        return 1


class Unmet(Exception):
    """The secret names something a copy can never hold."""


SECONDS = []  # every character that stands after the first in a canonical decomposition, made in the run


def seconds():
    if not SECONDS:
        SECONDS.append({ch for code in range(0x110000) for ch in unicodedata.normalize("NFD", chr(code))[1:]})
    return SECONDS[0]


ACCEPTED = {}


def accepted(ch):
    """A character a copy can hold beside any neighbour: it is its own copy but for letter case and folds into its
    small letter; every character of its canonical decomposition has combining class 0; and the first of them is not
    among the seconds. Proof that nothing else is needed: the copy changes a text by taking out, replacing and
    composing; the first two act on one character, which the first fact rules out; NFC composes two characters only
    when the second is a second, or reorders them only when one has a combining class, and the other two facts rule
    both out for every character of every decomposition; so the only neighbours a copy cannot hold are two spaces,
    which the copy makes one."""
    if ch not in ACCEPTED:
        decomposed = unicodedata.normalize("NFD", ch)
        ACCEPTED[ch] = (ch == unicodedata.normalize("NFKC", ch) and normalise(ch) == ch.casefold()
                        and ch.casefold() == ch.lower() and ch.upper().casefold() == ch.casefold()
                        and all(unicodedata.combining(part) == 0 for part in decomposed)
                        and decomposed[0] not in seconds())
    return ACCEPTED[ch]


# The judge of the secret: a small, closed part of the pattern language is taken, everything else stops the run, and
# every way through the secret must be able to match a copy. A way is the characters it takes, each by its kind (a
# space, a letter of a word, another character), and the positions between them.
SPACE, WORD, OTHER, NOTHING = "space", "word", "other", "nothing"
WAYS_LIMIT = 3000  # a secret with more ways than this stops the run: none is taken unjudged
POSITION_NAMES = {parser.AT_BEGINNING: "start", parser.AT_BEGINNING_STRING: "start", parser.AT_END: "end",
                  parser.AT_END_STRING: "end", parser.AT_BOUNDARY: "boundary", parser.AT_NON_BOUNDARY: "no boundary"}


def kind(code):
    """The kind of a character written out, which must be one a copy can hold beside any neighbour."""
    ch = chr(code)
    if not accepted(ch):
        raise Unmet()
    if ch == " ":
        return SPACE
    return WORD if re.match(r"\w", ch) else OTHER


def set_kinds(members):
    """A positive set or range of characters written out, which may also hold \\s and \\d: the kinds of its members."""
    kinds = set()
    for op, value in members:
        if op == parser.LITERAL:
            kinds.add(kind(value))
        elif op == parser.RANGE:
            kinds.update(kind(code) for code in range(value[0], value[1] + 1))
        elif op == parser.CATEGORY and value == parser.CATEGORY_SPACE:
            kinds.add(SPACE)  # in a copy every white space is the space
        elif op == parser.CATEGORY and value == parser.CATEGORY_DIGIT:
            kinds.add(WORD)
        else:
            raise Unmet()  # a negated set, or another class
    return kinds


def stands(way):
    """Whether one way can match a copy: it takes a character; no two spaces side by side; and at every place the
    positions there can all be true for one choice of what stands on its two sides (between two characters their
    kinds; at an edge of the way anything, or nothing, the same for every position at that place)."""
    chars = [value for what, value in way if what == "character"]
    if not chars:
        return False
    places = [[] for _ in range(len(chars) + 1)]
    at = 0
    for what, value in way:
        if what == "character":
            at += 1
        else:
            places[at].append(value)
    for place, positions in enumerate(places):
        if not positions:
            continue
        lefts = [chars[place - 1]] if place > 0 else [NOTHING, WORD, OTHER, SPACE]
        rights = [chars[place]] if place < len(chars) else [NOTHING, WORD, OTHER, SPACE]
        if not any(all((left == NOTHING) if position == "start" else (right == NOTHING) if position == "end"
                       else ((left == WORD) != (right == WORD)) if position == "boundary"
                       else ((left == WORD) == (right == WORD)) for position in positions)
                   for left in lefts for right in rights):
            return False
    return not any(left == SPACE and right == SPACE for left, right in zip(chars, chars[1:]))


def secret_ways(tree):
    """Every way through the secret, from the parser's tree: one branch of each alternation, one member of each set
    by its kind; a repeat whose least count is none left out and taken once, whose least count is one once, whose
    least count is two or more twice; and where a repeat may run more than once, two of its bodies side by side as
    well, unless the body is one character, set or class and its least count is at most one. A back reference
    stands for every way of its group, and only where the group stands closed before it."""
    groups = {}

    def item(op, value, closed):
        if op == parser.LITERAL:
            return [((("character", kind(value)),), closed)]
        if op == parser.IN:
            return [((("character", one),), closed) for one in sorted(set_kinds(value))]
        if op == parser.AT:
            if value not in POSITION_NAMES:
                raise Unmet()
            return [((("position", POSITION_NAMES[value]),), closed)]
        if op == parser.SUBPATTERN:
            group, add_flags, del_flags, inner = value
            if del_flags & re.IGNORECASE or add_flags & (re.ASCII | re.LOCALE):
                raise Unmet()
            out = sequence(inner, closed)
            if group is not None:
                groups.setdefault(group, set()).update(way for way, _ in out)
                out = [(way, done | {group}) for way, done in out]
            return out
        if op == parser.BRANCH:
            return [one for branch in value[1] for one in sequence(branch, closed)]
        if op in (parser.MAX_REPEAT, parser.MIN_REPEAT):
            low, high, inner = value
            if high == 0:  # a greatest count of none
                raise Unmet()
            body = sequence(inner, closed)
            single = len(inner) == 1 and inner[0][0] in (parser.LITERAL, parser.IN)
            pairs = [(first + second, one | two) for first, one in body for second, two in body]
            if low >= 2:
                return pairs
            out = list(body)
            if low == 0:
                out.append(((), closed))
            if high >= 2 and not single:
                out += pairs
            return out
        if op == parser.GROUPREF:
            if value not in closed:
                raise Unmet()  # a back reference to a group not closed on that way
            return [(way, closed) for way in groups[value]]
        raise Unmet()  # not on the list: the dot, a negated set, a look-around, a conditional, an atomic group, ...

    def sequence(items, closed):
        ways = [((), closed)]
        for op, value in items:
            ways = list({(way + more, done) for way, before in ways for more, done in item(op, value, before)})
            if len(ways) > WAYS_LIMIT:
                raise Unmet()
        return ways

    return [way for way, _ in sequence(list(tree), frozenset())]


def secret_meets_the_copy(parsed):
    """The judge, on the parser's tree and the flags beside it, and nothing else. It holds: where the secret matches a
    text as written, between neighbours the copy leaves as they are, it matches the copy of that text between the same
    neighbours; and it matches some line that is its own copy. It does not hold what stands outside a match: the copy
    also takes characters out there and joins white space, and a boundary at the secret's edge may then differ."""
    if parsed.state.flags & (re.ASCII | re.LOCALE):
        raise Unmet()
    if not all(stands(way) for way in secret_ways(parsed)):
        raise Unmet()


FORM = ("the private pattern given in the secret is written in a way the copy can never meet: it names a character "
        "the copy takes out or replaces, two white spaces side by side, or what nothing in a copy can be")
NOT_A_PATTERN = "the private pattern given in the secret is not a regular expression"


def judge_secret(name):
    """The one function the secret's text is read in, from the environment, and bound to no name outside it: its
    parse, its judge (whose helpers are given the tree, never the text) and the compiling from that same tree, so that
    nothing is parsed twice and the compiled pattern does not carry the text. Inside it a warning is an error and
    collecting of cycles is off, so that nothing Python could say about anything is printed while the secret is in
    hand; every exception ends in one of the secret's fixed messages, raised without the error that caused it. None
    when the secret is not set."""
    collecting = gc.isenabled()
    gc.disable()
    message = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            try:
                text = os.environ.get(name, "")
                if not text:
                    return None
                parsed = parser.parse(text, re.IGNORECASE)
                del text
                secret_meets_the_copy(parsed)
                return re._compiler.compile(parsed, re.IGNORECASE)
            except Unmet:
                message = FORM
            except Exception:
                message = NOT_A_PATTERN
    finally:
        if collecting:
            gc.enable()
    raise Problem(message) from None


def private_pattern(args, settings, failures):
    """The private pattern of this run, or None."""
    name = "COMMIT_CHECK_PRIVATE_PATTERN" if args.context == "own" else "FORK_CHECK_PRIVATE_PATTERN"
    compiled = judge_secret(name)
    if compiled is None:
        if settings["private_pattern_expected"]:
            failures.append("private pattern: settings.json says the secret is set, but this run did not get it")
        else:
            print("::notice::No private pattern is set yet; only the public properties were checked.")
    return compiled


SHUT = "The way for pull requests from forks is not open yet: nothing of this pull request was judged."


def fork_way_gate(settings):
    """The first job of a fork's run, which names no environment and no secret."""
    if not settings["fork_way_open"] or settings["outside_runs_held_since"] is None:
        print(f"::error::{SHUT}")
        return 1
    if not environment_waits_for_a_named_reviewer():
        print("::error::the environment private-check does not wait for a named reviewer, so nothing of this pull "
              "request is judged")
        return 1
    outputs = os.environ.get("GITHUB_OUTPUT", "")
    if outputs:
        try:
            with open(outputs, "a", encoding="utf-8") as handle:
                handle.write("open=true\n")
        except OSError:
            raise Problem("the job's outputs cannot be written") from None
    print("The way for pull requests from forks is open and the environment names a reviewer.")
    return 0


def run(args):
    settings = read_settings(args.settings)
    if args.fork_way_gate:
        if args.context != "fork-approved":
            raise Problem("--fork-way-gate is for a fork's run")
        return fork_way_gate(settings)
    if args.context == "fork-approved" and (not settings["fork_way_open"] or settings["outside_runs_held_since"] is None):
        # The way for pull requests from forks is shut: nothing of the pull request is read.
        print(f"::error::{SHUT}")
        return 1
    failures = []
    if args.context == "fork-approved" and not environment_waits_for_a_named_reviewer():
        return report(None, [], ["the environment private-check does not wait for a named reviewer, so nothing of "
                                 "this pull request was judged"])
    private = private_pattern(args, settings, failures)
    if args.verified_shas:
        try:
            with open(args.verified_shas, encoding="utf-8") as handle:
                listed = {line.strip() for line in handle if line.strip()}
        except OSError:
            raise Problem("the list of signed commits cannot be read") from None
        verifier = listed.__contains__
    else:
        verifier = github_api_verifier
    checker = Checker(private, verifier)

    if args.text_only:
        if not args.event:
            raise Problem("--text-only needs --event")
        texts = read_event(args.event)
        check_pull_request_text(checker, texts)
        check_people_and_message(checker, "merge commit", None, merge_message(texts), None)
        return report(None, checker.findings, failures)

    if not args.start or not args.head or not args.compare_settings:
        raise Problem("the check needs --from, --head and --compare-settings")
    repo = args.repo
    try:
        exempt = git(repo, "rev-parse", "--verify", "--quiet", settings["exempt_through"] + "^{commit}").strip()
    except Problem:
        raise Problem("the exempt commit named in settings.json is not in this history") from None
    start = git(repo, "rev-parse", "--verify", args.start + "^{commit}").strip()
    head = git(repo, "rev-parse", "--verify", args.head + "^{commit}").strip()
    failures += loosened_settings(repo, args.compare_settings, settings)
    if args.check_fork_way and os.environ.get("FORK_SECRET_AT_REPOSITORY_LEVEL", "") == "true":
        # Asked whether the way is open or shut: a secret of the fork's name at the repository's level would reach
        # every fork's job.
        failures.append("the fork's secret is a secret of the whole repository, where every fork's job could read it")
    if args.check_fork_way and settings["fork_way_open"]:
        if settings["outside_runs_held_since"] is None:
            failures.append("the way for forks is open, but settings.json holds no date since when GitHub holds every "
                            "run of an outside contributor")
        if not environment_waits_for_a_named_reviewer():
            failures.append("the way for forks is open, but the environment private-check does not wait for a named "
                            "reviewer")

    texts = read_event(args.event) if args.event else None
    built = []
    if args.build_merge:
        if texts is None:
            raise Problem("--build-merge needs --event")
        built = build_merges(checker, repo, start, head, texts, args.context)
        if built:
            # The merge is judged as main's push will judge it; it is told GitHub's signature, which it cannot have,
            # and only it.
            checker.verifier = lambda sha, own=checker.verifier, made=frozenset(built): sha in made or own(sha)
            head = built[0]
    commits = git(repo, "rev-list", "--topo-order", "--reverse", head, "^" + start, "^" + exempt).split()
    for sha in commits + built[1:]:
        check_commit(repo, sha, checker)
    if commits:
        base = git(repo, "merge-base", start, head).strip()
        numbering = [paths[-1] for _, paths in entries_of(git_bytes(repo, "diff", "--name-status", "-z", "-M", base, head))]
        check_lines(checker, "the range", repo, [base], head, numbering)
        check_paths(checker, "the range", git(repo, "diff", "--name-status", "-z", "-M", f"{start}...{head}").split("\0"))
        check_other_encodings(checker, "the range", repo,
                              git(repo, "diff", "--raw", "-z", "-M", "--abbrev=40", f"{start}...{head}").split("\0"))
    if args.branch is not None:
        checker.branch("push", args.branch, "branch name")
    if texts:
        check_pull_request_text(checker, texts)
        if not built:
            check_people_and_message(checker, "merge commit", None, merge_message(texts), None)
    if args.event:
        if not [sha for sha in commits if sha not in built]:
            failures.append("the pull request has no commit to check after the exempt commit")
    elif args.require_commits and not commits:
        failures.append("no commit to check in this range")
    return report(commits, checker.findings, failures)


if __name__ == "__main__":
    sys.exit(main())
