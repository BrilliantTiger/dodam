"""Checks every commit of a pull request or of a push, and the pull request's own text.

For each commit after the exempt commit named in settings.json:
- the author's and the committer's address is a GitHub no-reply address; GitHub's own address
  (noreply@github.com) stands only as the committer of a commit GitHub signed, and GitHub's API must say so
  (it is asked a few times; whatever goes wrong counts as not signed);
- each name is the login inside its own no-reply address, or "GitHub" as the committer of a commit GitHub
  signed, or a name that names no AI and does not match the private pattern; the author's name that GitHub puts
  on a commit it signed is held to the same;
- the message, the pull request's title and description carry no attribution line: a trailer whose key has a
  hyphen in it is refused unless it is a co-author line for a person with a no-reply address, a one-word key is
  refused when it is a word of attribution or its value holds an address, and a line that begins by saying how
  the text was made ("generated with ...") is refused by its form, whatever tool it names;
- the message, the title, the description, the branch's name, every line a commit adds and every path it adds
  or renames carry no work item number, no address of the tracker, no path of the computer the project's sessions
  run on (known by its folder parts) and no id of those sessions' tooling, and do not match the private pattern.
  Every text is brought to one form first: the forms of a character, letter case, dashes, spaces and characters
  without width. Lines a file adds are read as text whatever git or an attributes file thinks of the file. The
  only list of tools' names is the one for "generated with" inside an added line and for a branch named after a
  tool, where the property cannot be closed by its form.
Lines that a merge commit adds beyond all of its parents are checked as its own; the difference and the paths of
the whole range are checked as well, so that a merge cannot bring back a line or a path of the exempt history.

What it prints is made by the check alone: a commit's id, a property's name and positions by number. It never
prints a text, a path, a title or anything else it read, also not when it cannot run.

A run fails at once on whatever breaks a rule; nothing waits and nothing is read again. The way back is a new
head. The same rules can be run on a title, a description and a branch name before they are published
(--text-only). It uses the standard library only, and it never runs anything from the commits it reads.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
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
GENERATED_IN_FILE_RE = re.compile(any_of(FILE_MADE_VERBS) + r"(?:\s+|-)(?:with|by|using|via)\s*:?\s+\W{0,6}"
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
MADE_VERBS = ("gener" "ated", "co-?auth" "ored", "assi" "sted", "auth" "ored", "writ" "ten", "draf" "ted", "produ" "ced")
MADE_LINKS = ("with", "by", "using", "via", "through")
MADE_FORM_RE = re.compile(r"^\W*" + any_of(MADE_VERBS) + r"(?:\s+|-)" + any_of(MADE_LINKS) + r"(?![a-z])")
ROBOT = "\N{ROBOT FACE}"

# Every place: messages, titles, descriptions, branch names, added lines, paths. Matched on the normalised text.
TRACKER_CODES = (r"(?<![a-z0-9])d" "od" r"[-_ ]?\d+(?![0-9])",)
TRACKER_ADDRESSES = (r"lin" r"ear\.app",)
TOOLING_IDS = ("te" "rm_[0-9a-f]{8,}", "ta" "sk_[0-9a-f]{8,}", "c" "tx_[0-9a-f]{8,}", "dis" "patch_[0-9a-f]{8,}",
               "ms" "g_[0-9a-f]{8,}", "w" "f_[0-9a-f]{8,}", "conver" "sation://")
LOCAL_PATHS = (
    r"[\\/]us" r"ers[\\/]+(?!shared[\\/])[^\\/\s]+[\\/]",                       # a home folder of macOS or Windows
    r"(?<![a-z0-9])\.cla" r"ude[\\/]+(?:[\w.-]+[\\/]+[\w.-]|[\w-]*\.[\w.-]+)",  # inside the sessions' folders
    r"(?<![a-z0-9])\.gem" r"ini[\\/]+(?:[\w.-]+[\\/]+[\w.-]|[\w-]*\.[\w.-]+)",
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
DASHES = {"−", "⁃", "﹣", "－"}


class Problem(Exception):
    """A reason the check cannot run, in fixed words of its own."""


def normalise(text, words=False):
    """One form of a text: the forms of a character, no characters without width, one dash, one space, no letter
    case; for a name (words) also its parts apart: camel case and underscores."""
    if words:
        text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text).replace("_", " ")
    text = unicodedata.normalize("NFKC", text)
    out = []
    for ch in text:
        category = unicodedata.category(ch)
        if category == "Cf":
            continue
        if category == "Pd" or ch in DASHES:
            out.append("-")
        elif category == "Zs" or ch == "\t":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out).casefold()


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

    def private_hit(self, text):
        return self.private is not None and (self.private.search(text) is not None
                                             or self.private.search(normalise(text)) is not None)

    def place(self, who, text, where):
        """The rules of every place."""
        plain = normalise(text)
        for prop, pattern in PLACE_RULES:
            if pattern.search(plain):
                self.add(who, prop, where)
        if self.private_hit(text):
            self.add(who, "private pattern", where)

    def name_passes(self, name, address):
        """A name passes when it names no AI and does not match the private pattern; the login inside its own
        no-reply address is spared the private pattern only, because GitHub shows it in the address anyway."""
        if AI_NAME_RE.search(normalise(name, words=True)) or AI_NAME_RE.search(normalise(name)):
            return False
        login = NO_REPLY_RE.match(address)
        return bool(login and name == login.group(2)) or not self.private_hit(name)

    def prose(self, who, text, where_line):
        """A message, a title, a description: the rules of every place, and attribution closed by its form."""
        lines = text.split("\n")
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
        self.place(who, name, where)
        if BRANCH_TOOL_RE.search(normalise(name)):
            self.add(who, "AI name", where)

    def added_line(self, who, file_number, line_number, line):
        where = f"file {file_number}, line {line_number}"
        self.place(who, line, where)
        if GENERATED_IN_FILE_RE.search(normalise(line)):
            self.add(who, "attribution line", where)

    def path(self, who, number, path):
        self.place(who, path, f"path {number}")

    def patch(self, who, patch):
        """Reads a patch made with -U0 by its structure: a file's header only outside a hunk, a hunk by the lengths its
        own header gives; in a combined patch only the lines added against every parent count."""
        lines = patch.split("\n")
        index = 0
        file_number = 0
        while index < len(lines):
            line = lines[index]
            index += 1
            if line.startswith("diff --git ") or line.startswith("diff --cc "):
                file_number += 1
                continue
            hunk = re.match(r"^(@@+) ((?:-\d+(?:,\d+)? )+)\+(\d+)(?:,(\d+))? @@+", line)
            if not hunk:
                continue  # a header line of the file
            parents = len(hunk.group(1)) - 1
            if len(hunk.group(2).split()) != parents:
                raise Problem("a difference cannot be read")
            number = int(hunk.group(3))
            new_left = int(hunk.group(4)) if hunk.group(4) is not None else 1
            # The hunk ends after the lines of the result its header counts; a removed line after the last of them
            # begins with "-" or " " and is no header.
            while new_left > 0:
                if index >= len(lines):
                    raise Problem("a difference cannot be read")
                body = lines[index]
                index += 1
                if body.startswith("\\"):
                    continue  # "\ No newline at end of file"
                prefix, content = body[:parents], body[parents:]
                if len(prefix) != parents or set(prefix) - {"+", "-", " "}:
                    raise Problem("a difference cannot be read")
                if "-" in prefix:
                    continue  # a removed line
                new_left -= 1
                if set(prefix) == {"+"}:
                    self.added_line(who, file_number, number, content)
                number += 1


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


DIFF_OPTIONS = ["--no-color", "--no-ext-diff", "--no-textconv", "--text", "-U0", "-M"]


def check_paths(checker, who, fields):
    """The paths a difference adds, renames, copies or changes, from --name-status -z, numbered in git's order."""
    fields = [field for field in fields if field != ""]
    index = 0
    number = 0
    while index < len(fields):
        status = fields[index]
        number += 1
        if status[:1] in ("R", "C"):
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


def check_commit(repo, sha, checker):
    who = f"commit {sha[:12]}"
    people, message = parse_commit(git(repo, "cat-file", "commit", sha))
    by_github = people["committer"][1] == GITHUB_ADDRESS
    for role in ("author", "committer"):
        name, address = people[role]
        if role == "committer" and by_github:
            if not checker.verifier(sha):
                checker.add(who, "committer address", "GitHub's address without GitHub's verified signature")
            elif name != "GitHub":
                checker.add(who, "committer name", "a commit GitHub signed carries the name GitHub")
            continue
        if not NO_REPLY_RE.match(address):
            checker.add(who, f"{role} address", "not a GitHub no-reply address")
        if not checker.name_passes(name, address):
            checker.add(who, f"{role} name", "neither the login of its address nor a plain name")
    checker.prose(who, message, lambda n: f"message line {n}")
    # The combined forms: for a commit of one parent (and for the first commit) they give what the plain forms give;
    # for a merge, only what it brings beyond all of its parents.
    checker.patch(who, git(repo, "diff-tree", "--cc", "--root", "--format=", *DIFF_OPTIONS, sha))
    fields = git(repo, "diff-tree", "-r", "-c", "--root", "--no-commit-id", "--name-status", "-z", "-M", sha)
    check_paths(checker, who, fields.split("\0"))


def api_get(path):
    """One answer of GitHub's API, asked up to three times when it fails to answer; None when no answer came."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository):
        return None
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{api}/repos/{repository}/{path}", headers=headers)
    for wait in (1, 3, None):
        try:
            with urllib.request.urlopen(request, timeout=20) as answer:
                return json.load(answer)
        except urllib.error.HTTPError as error:
            if error.code < 500 and error.code != 429:
                return None  # an answer: what was asked for is not there, or not for this token
        except Exception:
            pass
        if wait is None:
            return None
        time.sleep(wait)
    return None


def github_api_verifier(sha):
    data = api_get(f"commits/{sha}")
    if not isinstance(data, dict):
        return False
    commit = data.get("commit") if isinstance(data.get("commit"), dict) else {}
    verification = commit.get("verification") if isinstance(commit.get("verification"), dict) else {}
    committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
    return data.get("sha") == sha and verification.get("verified") is True and committer.get("email") == GITHUB_ADDRESS


def environment_waits_for_a_named_reviewer():
    """Whether the environment of a fork's job makes every run wait for a reviewer that it names."""
    data = api_get(f"environments/{ENVIRONMENT}")
    rules = data.get("protection_rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return False
    return any(isinstance(rule, dict) and rule.get("type") == "required_reviewers"
               and isinstance(rule.get("reviewers"), list) and len(rule["reviewers"]) > 0 for rule in rules)


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
    texts = {"title": pull.get("title") or "", "body": pull.get("body") or "", "branch": head.get("ref") or ""}
    if not all(isinstance(value, str) for value in texts.values()):
        raise Problem("the event's pull request cannot be read")
    return texts


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
    parser.add_argument("--check-fork-way", action="store_true",
                        help="on main: when the way for forks is open, ask GitHub whether everything it needs is set")
    parser.add_argument("--text-only", action="store_true",
                        help="check only the pull request's text in --event, before it is published; no git")
    parser.add_argument("--verified-shas", action=Once, help="a file of commits GitHub signed, in place of asking GitHub")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Problem as problem:
        print(f"::error::{problem}")
        return 1
    except Exception:
        print("::error::the check stopped on something it does not know")
        return 1


def private_pattern(args, settings, failures):
    """The private pattern of this run, or None."""
    name = "COMMIT_CHECK_PRIVATE_PATTERN" if args.context == "own" else "FORK_CHECK_PRIVATE_PATTERN"
    pattern = os.environ.get(name, "")
    if not pattern:
        if settings["private_pattern_expected"]:
            failures.append("private pattern: settings.json says the secret is set, but this run did not get it")
        else:
            print("::notice::No private pattern is set yet; only the public properties were checked.")
        return None
    try:
        return re.compile(pattern)
    except re.error:
        raise Problem("the private pattern given in the secret is not a regular expression") from None


def run(args):
    settings = read_settings(args.settings)
    if args.context == "fork-approved" and (not settings["fork_way_open"] or settings["outside_runs_held_since"] is None):
        # The way for pull requests from forks is shut: nothing of the pull request is read.
        print("::error::The way for pull requests from forks is not open yet: nothing of this pull request was judged.")
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
        check_pull_request_text(checker, read_event(args.event))
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
    if args.check_fork_way and settings["fork_way_open"]:
        if settings["outside_runs_held_since"] is None:
            failures.append("the way for forks is open, but settings.json holds no date since when GitHub holds every "
                            "run of an outside contributor")
        if not environment_waits_for_a_named_reviewer():
            failures.append("the way for forks is open, but the environment private-check does not wait for a named "
                            "reviewer")
        if os.environ.get("FORK_SECRET_AT_REPOSITORY_LEVEL", ""):
            failures.append("the way for forks is open, and the fork's secret is also a secret of the whole repository")

    commits = git(repo, "rev-list", "--topo-order", "--reverse", head, "^" + start, "^" + exempt).split()
    for sha in commits:
        check_commit(repo, sha, checker)
    if commits:
        checker.patch("the range", git(repo, "diff", *DIFF_OPTIONS, f"{start}...{head}"))
        check_paths(checker, "the range", git(repo, "diff", "--name-status", "-z", "-M", f"{start}...{head}").split("\0"))
    if args.branch is not None:
        checker.branch("push", args.branch, "branch name")
    if args.event:
        check_pull_request_text(checker, read_event(args.event))
        if not commits:
            failures.append("the pull request has no commit to check after the exempt commit")
    elif args.require_commits and not commits:
        failures.append("no commit to check in this range")
    return report(commits, checker.findings, failures)


if __name__ == "__main__":
    sys.exit(main())
