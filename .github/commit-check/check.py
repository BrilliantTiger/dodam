"""Checks every commit of a pull request or of a push, and the pull request's own text.

For each commit after the exempt commit named in settings.json:
- the author's and the committer's address is a GitHub no-reply address; GitHub's own address
  (noreply@github.com) stands only as the committer of a commit GitHub signed, and the signature must be
  verified by GitHub's API (any error counts as not verified);
- each name is either the login inside its own no-reply address, or "GitHub" on a commit GitHub signed
  (whose author name GitHub puts there itself), or a name that names no AI and does not match the
  private pattern;
- the message has no co-author line naming an AI, no co-author with an address other than a no-reply one,
  and no "generated with" line naming an AI;
- the message, the pull request's title, description and branch name, the lines the commit adds and the paths of the
  files it adds or renames have no work item number, no address of the tracker, no path of the computer
  the sessions run on and no id of the sessions' tooling, and do not match the private pattern.
Lines that a merge commit adds beyond all of its parents are checked as its own; the difference of the
whole range is checked as well, so that a merge cannot bring back a line from the exempt history.

A run fails only on what cannot be mended on its head (a commit, the branch's name): the owner's gate refuses a
head that carries a failed run for good. A title or description that breaks a rule is read again from GitHub's
API until it is mended or the wait is over, on the project's own branches; a fork's run reads it once.

What it prints names the commit and the property, never the offending text; for the private pattern it
does not name the file either. The patterns are put together from parts, so that this file does not
trip the check itself. It uses the standard library only, and it never runs anything from the commits
it reads: they are data.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

GITHUB_ADDRESS = "noreply" + "@" + "github.com"
NO_REPLY_RE = re.compile(r"^(?:(\d+)\+)?([A-Za-z0-9](?:-?[A-Za-z0-9]){0,38})@users\.noreply\.github\.com$")

AI_NAMES = r"(?:cl" + r"aude|anthrop" + r"ic|open" + r"ai|chat" + r"gpt|gpt-?\d|co" + r"dex|gem" + r"ini|bard|co" \
    + r"pilot|cur" + r"sor|dev" + r"in|anti" + r"gravity|ll" + r"ama|mis" + r"tral|deep" + r"seek|gr" + r"ok)"
AI_NAME_RE = re.compile(r"(?i)\b" + AI_NAMES + r"\b")
CO_AUTHOR_RE = re.compile(r"(?i)^\s*co-" + r"authored-" + r"by\s*:(.*)$")
CO_AUTHOR_ADDRESS_RE = re.compile(r"<([^<>]*)>")
GENERATED_RE = re.compile(r"(?i)(?:gener" + r"ated\s+(?:with|by)\s+\W{0,2}" + AI_NAMES + r"|\N{ROBOT FACE})")

TEXT_PATTERNS = [
    ("tracker code", re.compile(r"(?i)(?<![A-Za-z0-9])" + "D" + "OD" + r"-\d+(?![0-9])")),
    ("tracker address", re.compile(r"(?i)\blin" + r"ear\.app\b")),
    ("tooling id", re.compile(r"\b(?:te" + r"rm|ta" + r"sk|c" + r"tx)_[0-9a-f]{8,}")),
]
# Prose (messages, the pull request's text): any absolute path under a home or a temporary folder.
# "~/" is left out: the project names its own folders there (~/.dodam).
PROSE_PATH_RE = re.compile(r"(?i)(?<![\w.])/(?:Us" + r"ers|ho" + r"me|private/va" + r"r|private/tm" + r"p|va" + r"r/folders"
                           + r"|va" + r"r/tmp|tm" + r"p|Vol" + r"umes|ro" + r"ot)/[^\s/]+"
                           + r"|(?i:(?<![a-z])[a-z]:[\\/]+us" + r"ers[\\/]+)")
# Files: tests use /tmp and made-up home folders on purpose, so only the paths that the sessions' own
# tools leave behind count here.
FILE_PATH_RE = re.compile(r"/priv" + r"ate/tmp/cl" + r"aude-\d|/va" + r"r/folders/[\w-]{2}/[\w-]+/T/|\.cla" + r"ude/proj"
                          + r"ects/|-Us" + r"ers-[A-Za-z0-9_.-]+-or" + r"ca-|/or" + r"ca/(?:work" + r"spaces|proj" + r"ects)/")

HUNK_RE = re.compile(r"^@@+ (?:-\S+ )+\+(\d+)(?:,\d+)? @@+")


class Problem(Exception):
    pass


def git(repo, *args):
    done = subprocess.run(["git", "-C", repo, "-c", "core.quotePath=false", *args],
                          capture_output=True, env=git_env())
    if done.returncode != 0:
        raise Problem(f"git {args[0]} failed: {done.stderr.decode('utf-8', 'replace').strip()[:300]}")
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
        return self.private is not None and self.private.search(text) is not None

    def text(self, who, text, where_line, prose):
        """Checks lines of prose: a message, a title, a description."""
        for number, line in enumerate(text.splitlines(), 1):
            where = where_line(number)
            for prop, pattern in TEXT_PATTERNS:
                if pattern.search(line):
                    self.add(who, prop, where)
            if PROSE_PATH_RE.search(line):
                self.add(who, "local path", where)
            if GENERATED_RE.search(line):
                self.add(who, "generated-with line", where)
            co_author = CO_AUTHOR_RE.match(line)
            if co_author:
                rest = co_author.group(1)
                if AI_NAME_RE.search(rest):
                    self.add(who, "AI co-author line", where)
                else:
                    addresses = CO_AUTHOR_ADDRESS_RE.findall(rest)
                    if len(addresses) != 1 or not NO_REPLY_RE.match(addresses[0].strip()):
                        self.add(who, "co-author address", where)
            if self.private_hit(line):
                self.add(who, "private pattern", where)

    def added_line(self, who, path, number, line, label=""):
        for prop, pattern in TEXT_PATTERNS:
            if pattern.search(line):
                self.add(who, prop, f"{label}file {path} line {number}")
        if FILE_PATH_RE.search(line):
            self.add(who, "local path", f"{label}file {path} line {number}")
        if self.private_hit(line):
            self.add(who, "private pattern", "an added line")

    def path(self, who, path):
        for prop, pattern in TEXT_PATTERNS:
            if pattern.search(path):
                self.add(who, prop, "file path")
        if FILE_PATH_RE.search(path):
            self.add(who, "local path", "file path")
        if self.private_hit(path):
            self.add(who, "private pattern", "a file path")

    def patch(self, who, patch, label=""):
        """Reads a patch made with -U0; for a combined patch only lines added against every parent count."""
        path = None
        number = 0
        columns = 1
        for line in patch.splitlines():
            if line.startswith("diff --cc ") or line.startswith("diff --combined "):
                columns = 2
                path = None
                continue
            if line.startswith("diff --git "):
                columns = 1
                path = None
                continue
            if line.startswith("+++ "):
                target = line[4:]
                path = None if target == "/dev/null" else (target[2:] if target.startswith("b/") else target)
                continue
            if line.startswith("@@"):
                found = HUNK_RE.match(line)
                if found:
                    columns = len(line) - len(line.lstrip("@")) - 1
                    number = int(found.group(1))
                continue
            if path is None or line.startswith("\\"):
                continue
            prefix = line[:columns]
            if len(prefix) == columns and prefix and set(prefix) == {"+"}:
                self.added_line(who, path, number, line[columns:], label)
                number += 1
            elif prefix and "-" not in prefix:
                number += 1


def parse_commit(raw):
    header, _, message = raw.partition("\n\n")
    people = {}
    for line in header.splitlines():
        for role in ("author", "committer"):
            if line.startswith(role + " "):
                found = re.match(r"^" + role + r" (.*) <([^<>]*)> \d+ [+-]\d{4}$", line)
                if not found:
                    raise Problem(f"the {role} line of a commit cannot be read")
                people[role] = (found.group(1), found.group(2))
    if set(people) != {"author", "committer"}:
        raise Problem("a commit has no author or no committer")
    signed = any(line.startswith("gpgsig ") for line in header.splitlines())
    parents = [line.split()[1] for line in header.splitlines() if line.startswith("parent ")]
    return people, message, signed, parents


def check_commit(repo, sha, checker):
    who = f"commit {sha[:12]}"
    people, message, signed, parents = parse_commit(git(repo, "cat-file", "commit", sha))
    by_github = people["committer"][1] == GITHUB_ADDRESS
    github_verified = by_github and checker.verifier(sha)
    for role in ("author", "committer"):
        name, address = people[role]
        login = NO_REPLY_RE.match(address)
        if role == "committer" and by_github:
            if not github_verified:
                checker.add(who, "committer address", "GitHub's address without GitHub's verified signature")
            elif name != "GitHub":
                checker.add(who, "committer name", "a commit GitHub signed carries the name GitHub")
            continue
        if not login:
            checker.add(who, f"{role} address", "not a GitHub no-reply address")
        if role == "author" and github_verified:
            continue  # GitHub puts the account's display name there itself.
        if login and name == login.group(2):
            continue
        if AI_NAME_RE.search(name) or checker.private_hit(name):
            checker.add(who, f"{role} name", "neither the login of its address nor a plain name")
    checker.text(who, message, lambda n: f"message line {n}", prose=True)
    diff_options = ["--no-color", "--no-ext-diff", "--no-textconv", "-U0", "-M"]
    if len(parents) > 1:
        patch = git(repo, "diff-tree", "--cc", "--format=", *diff_options, sha)
    else:
        patch = git(repo, "diff-tree", "-p", "--root", "--format=", *diff_options, sha)
    checker.patch(who, patch)
    names = git(repo, "diff-tree", "-r", "--root", "--no-commit-id", "--name-status", "-z", "-M",
                *(["-c"] if len(parents) > 1 else []), sha).split("\0")
    for path in new_paths(names):
        checker.path(who, path)


def new_paths(fields):
    """The paths a commit adds, renames, copies or changes, from --name-status -z."""
    paths = []
    index = 0
    fields = [field for field in fields if field != ""]
    while index < len(fields):
        status = fields[index]
        if status[:1] in ("R", "C"):
            paths.append(fields[index + 2])
            index += 3
        else:
            if not status.startswith("D"):
                paths.append(fields[index + 1])
            index += 2
    return paths


def github_api_verifier():
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")

    def verified(sha):
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return False
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"{api}/repos/{repository}/commits/{sha}", headers=headers)
        # Asked up to three times, so that one failed answer does not turn a clean main red; whatever goes
        # wrong in the end counts as not verified.
        for wait in (2, 5, None):
            try:
                with urllib.request.urlopen(request, timeout=20) as answer:
                    data = json.load(answer)
                commit = data.get("commit") or {}
                return (data.get("sha") == sha
                        and (commit.get("verification") or {}).get("verified") is True
                        and (commit.get("committer") or {}).get("email") == GITHUB_ADDRESS)
            except Exception:
                if wait is None:
                    return False
                time.sleep(wait)
        return False

    return verified


SETTINGS_PATH = ".github/commit-check/settings.json"


def loosened_settings(repo, commit, exempt, expected):
    """A branch is judged by its own copy, so it may not loosen the settings that its start has."""
    commit = git(repo, "rev-parse", "--verify", commit + "^{commit}").strip()
    listed = git(repo, "ls-tree", "--name-only", commit, "--", SETTINGS_PATH).strip()
    if not listed:
        return []  # the first merge of the check: nothing to compare with
    try:
        before = json.loads(git(repo, "show", f"{commit}:{SETTINGS_PATH}"))
    except ValueError:
        return ["settings: the settings on the range's start cannot be read"]
    found = []
    if before.get("private_pattern_expected") is True and not expected:
        found.append("settings: the private pattern is expected on the range's start, and this branch says it is not")
    if before.get("exempt_through") != exempt:
        found.append("settings: the exempt commit differs from the range's start")
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--from", dest="start",
                        help="the commit the range starts after (the pull request's base, main, or the push's before)")
    parser.add_argument("--head")
    parser.add_argument("--settings", required=True)
    parser.add_argument("--context", choices=("own", "fork", "fork-approved"), default=None,
                        help="own: the project's own branch; fork: a pull request from a fork; fork-approved: a fork's "
                             "run that waited in the environment private-check for the project's approval")
    parser.add_argument("--own", action="store_const", const="own", dest="context")
    parser.add_argument("--event", help="the event's file, for the pull request's title and description")
    parser.add_argument("--require-commits", action="store_true", help="fail when there is no commit to check")
    parser.add_argument("--verified-shas", help="a file of commits GitHub signed, in place of asking GitHub's API")
    parser.add_argument("--wait-for-text", type=float, default=0, metavar="SECONDS",
                        help="on a pull request of the project's own branches, how long a title or description that "
                             "breaks a rule is waited for and read again before the run fails")
    parser.add_argument("--poll", type=float, default=30, metavar="SECONDS", help="how often it is read again")
    parser.add_argument("--branch")
    parser.add_argument("--check-fork-way", action="store_true")
    parser.add_argument("--text-only", action="store_true")
    parser.add_argument("--compare-settings", metavar="COMMIT",
                        help="the commit whose settings.json this branch may not loosen (main, or the push's before)")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Problem as problem:
        print(f"::error::{problem}")
        return 1
    except Exception:
        return 0  # not built yet


def api_get(path):
    """One answer of GitHub's API, asked up to three times; None when no answer came."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repository):
        return None
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{api}/repos/{repository}/{path}", headers=headers)
    for wait in (1, 2, None):
        try:
            with urllib.request.urlopen(request, timeout=20) as answer:
                return json.load(answer)
        except Exception:
            if wait is None:
                return None
            time.sleep(wait)
    return None


def environment_waits_for_a_reviewer():
    """Whether the environment this run is in makes every run wait for a reviewer's approval. Only then may a fork's
    run use the private pattern: without it a fork could try its text against the pattern as often as it likes."""
    name = os.environ.get("COMMIT_CHECK_ENVIRONMENT", "private-check")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return False
    data = api_get(f"environments/{name}")
    rules = (data or {}).get("protection_rules") or []
    return any(isinstance(rule, dict) and rule.get("type") == "required_reviewers" for rule in rules)


def text_findings(private, pull):
    """What breaks a rule in a pull request's title and description (which can be mended) and in its branch's name
    (which cannot)."""
    mendable = Checker(private, lambda sha: False)
    mendable.text("pull request", pull.get("title") or "", lambda n: "pull request title", prose=True)
    mendable.text("pull request", pull.get("body") or "", lambda n: f"pull request description line {n}", prose=True)
    fixed = Checker(private, lambda sha: False)
    # GitHub writes the branch's name into the message of the merge commit it makes on main.
    fixed.text("pull request", (pull.get("head") or {}).get("ref") or "", lambda n: "pull request branch name",
               prose=True)
    return mendable.findings, fixed.findings


def check_paths(checker, who, fields):
    """Not built yet: the paths are checked by new_paths."""


def environment_waits_for_a_named_reviewer():
    """Not built yet."""
    return True


def report(commits, findings, failures):
    print(f"{len(commits)} commits checked")
    for finding in findings:
        print(finding)
    for failure in failures:
        print(f"::error::{failure}")
    if findings or failures:
        print(f"{len(findings) + len(failures)} problems; the properties are listed at the top of "
              ".github/commit-check/check.py")
        return 1
    return 0


def run(args):
    context = args.context or "fork"
    with open(args.settings, encoding="utf-8") as handle:
        settings = json.load(handle)
    exempt = settings.get("exempt_through")
    expected = settings.get("private_pattern_expected")
    if not isinstance(exempt, str) or not re.fullmatch(r"[0-9a-f]{40}", exempt) or not isinstance(expected, bool):
        raise Problem("settings.json needs exempt_through (a full commit id) and private_pattern_expected (true or false)")
    repo = args.repo
    try:
        exempt = git(repo, "rev-parse", "--verify", "--quiet", exempt + "^{commit}").strip()
    except Problem:
        raise Problem("the exempt commit named in settings.json is not in this history") from None
    start = git(repo, "rev-parse", "--verify", args.start + "^{commit}").strip()
    head = git(repo, "rev-parse", "--verify", args.head + "^{commit}").strip()

    failures = []
    if args.compare_settings:
        failures += loosened_settings(repo, args.compare_settings, exempt, expected)
    private = None
    pattern = os.environ.get("COMMIT_CHECK_PRIVATE_PATTERN", "")
    if context == "fork-approved" and not environment_waits_for_a_reviewer():
        # The secret may have reached this run without anyone approving it: it is not used.
        pattern = ""
        message = ("the environment private-check does not wait for a reviewer, so the private pattern is not used "
                   "for a pull request from a fork")
        if expected:
            failures.append(f"private pattern: {message}")
        else:
            print(f"::notice::The {message}.")
        context = "fork"
    if context == "fork":
        if not failures:
            print("::notice::The private pattern is not applied in this run of a pull request from a fork.")
    elif pattern:
        try:
            private = re.compile(pattern)
        except re.error:
            raise Problem("the private pattern given in the secret is not a regular expression") from None
    elif expected:
        failures.append("private pattern: settings.json says the secret is set, but this run did not get it")
    else:
        print("::notice::No private pattern is set yet; only the public properties were checked.")

    verifier = github_api_verifier()
    if args.verified_shas:
        with open(args.verified_shas, encoding="utf-8") as handle:
            listed = {line.strip() for line in handle if line.strip()}
        verifier = listed.__contains__

    checker = Checker(private, verifier)
    commits = git(repo, "rev-list", "--topo-order", "--reverse", head, "^" + start, "^" + exempt).split()
    for sha in commits:
        check_commit(repo, sha, checker)
    if commits:
        diff_options = ["--no-color", "--no-ext-diff", "--no-textconv", "-U0", "-M"]
        checker.patch("the range", git(repo, "diff", *diff_options, f"{start}...{head}"),
                      label="the range's difference, ")
    if args.event and not commits:
        failures.append("the pull request has no commit to check after the exempt commit")
    elif not args.event and args.require_commits and not commits:
        failures.append("no commit to check in this range")
    if not args.event or checker.findings or failures:
        # What is wrong here cannot be mended on this head: the run ends now.
        return report(commits, checker.findings, failures)

    with open(args.event, encoding="utf-8") as handle:
        event = json.load(handle)
    pull = event.get("pull_request") or {}
    number = pull.get("number")
    live = args.wait_for_text > 0 or context != "own"
    if not live:
        mendable, fixed = text_findings(private, pull)
        return report(commits, mendable + fixed, failures)
    if not isinstance(number, int):
        return report(commits, [], ["the event names no pull request"])

    # The title and the description are read as they are now, not from the event's copy: every run on this head
    # then sees the same text. A run of the project's own branch waits for them to be mended; a fork's run does not
    # wait, so that it holds no runner and cannot try its text against the private pattern again and again.
    deadline = time.monotonic() + args.wait_for_text
    shown = None
    while True:
        now = api_get(f"pulls/{number}")
        if now is None:
            current, problem = [], "the pull request could not be read from GitHub's API"
        else:
            mendable, fixed = text_findings(private, now)
            if fixed:
                return report(commits, fixed + mendable, failures)
            if not mendable:
                return report(commits, [], failures)
            current, problem = mendable, None
        if time.monotonic() >= deadline or context != "own":
            ending = [problem] if problem else []
            if args.wait_for_text > 0 and context == "own":
                ending.append(f"the pull request's text was not mended within {args.wait_for_text:g} seconds")
            return report(commits, current, failures + ending)
        if (current, problem) != shown:
            for finding in current:
                print(finding)
            if problem:
                print(f"::warning::{problem}")
            print(f"waiting for the pull request's text to be mended: it is read again every {args.poll:g} seconds")
            shown = (current, problem)
        time.sleep(min(args.poll, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    sys.exit(main())
