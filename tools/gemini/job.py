#!/usr/bin/env python3
"""The project's Gemini job: one run of Google's Antigravity CLI in headless mode for one pull request or
issue of the project, with the public part of the answer posted as one comment.

    python3 tools/gemini/job.py --kind verify|attack|discuss --number N
                                [--settings FILE] [--private DIR] [--tell RUN_ID]

The watcher (watch.py, beside this file) starts it when the project's account marks an item; it can also
be started by hand in a plain terminal. It refuses to do anything inside a coding agent or a terminal that
an agent can type into. It prints one JSON line on standard output, made of fixed words, numbers and the
path of the private answer, and never any text of the model.

Exit codes: 0 the answer was posted or held; 1 an error; 3 refused inside a coding agent; 4 settings,
private folder, an item that is not the project's own, or no question; 5 another job is running; 6 Google's
quota is used up; 7 a sign-in is missing; 8 the run passed its time; 129, 130, 143 stopped by a signal.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import ipaddress
import json
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable
from pathlib import Path
from urllib.parse import quote

if sys.version_info < (3, 11):
    sys.exit("The Gemini job needs Python 3.11 or later.")

import tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TEMPLATES = HERE / "templates"
DEFAULT_SETTINGS = HERE / "settings.toml"

KINDS = ("verify", "attack", "discuss")
LOOKS = {"verify": "a verification look", "attack": "an attack look", "discuss": "a discussion"}
FIRST_LINE_PREFIX = "This comment was written automatically by the project's Gemini job"
PUBLIC_MARK = "=== PUBLIC ==="
PRIVATE_MARK = "=== PRIVATE ==="
NAMES_FILE = "names.txt"
JOB_STATE_FILE = "job-state.json"
FINISHED_FILE = "finished.log"
JOB_LOCK = "job.lock"
ORCA_APP_CLI = Path("/Applications/Orca.app/Contents/Resources/bin/orca")

GH_TIMEOUT_SECONDS = 60
GIT_TIMEOUT_SECONDS = 600
ORCA_TIMEOUT_SECONDS = 30
PS_TIMEOUT_SECONDS = 20

EXIT_CODES = {
    "posted": 0,
    "held": 0,
    "error": 1,
    "refused": 3,
    "settings": 4,
    "not_eligible": 4,
    "no_question": 4,
    "busy": 5,
    "quota": 6,
    "day_bound": 6,
    "signed_out": 7,
    "timeout": 8,
}

# --- Inside a coding agent ----------------------------------------------------------------------------

# Names that Claude Code sets in the shells it starts (seen in a session of version 2.1.327), names that
# Orca sets in its terminals (an agent can type into those), and those of two other coding agents.
ENV_MARK_NAMES = frozenset({"CLAUDECODE", "AI_AGENT", "OPENCODE"})
ENV_MARK_PREFIXES = ("CLAUDE_", "ORCA_", "CODEX_")
# Coding agents of other companies, by the name of their program, among the parent processes.
AGENT_PROGRAMS = frozenset(
    {
        "claude", "claude-code", "codex", "opencode", "cursor-agent", "aider", "goose", "amp", "crush",
        "copilot", "droid", "qwen", "grok", "kimi", "cline", "jcode",
    }
)
AGENT_ARGUMENT_MARKS = ("@anthropic-ai/claude-code", "/claude-code/", "@openai/codex", "opencode-ai")


def environment_marks(env: dict[str, str]) -> list[str]:
    found = [name for name in sorted(env) if name in ENV_MARK_NAMES or name.startswith(ENV_MARK_PREFIXES)]
    if env.get("TERM_PROGRAM", "").strip().lower() == "orca":
        found.append("TERM_PROGRAM")
    return found


def _run_ps(options: list[str]) -> str | None:
    for candidate in ("/bin/ps", "/usr/bin/ps"):
        if os.path.exists(candidate):
            try:
                proc = subprocess.run(
                    [candidate, *options], capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=PS_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            return proc.stdout if proc.returncode == 0 else None
    return None


def process_table() -> dict[int, tuple[int, str, str]] | None:
    """Every process, as {pid: (parent pid, program, arguments)}, or None when it cannot be read."""
    names = _run_ps(["-A", "-o", "pid=,ppid=,comm="])
    arguments = _run_ps(["-A", "-ww", "-o", "pid=,args="])
    if names is None or arguments is None:
        return None
    args_by_pid: dict[int, str] = {}
    for line in arguments.splitlines():
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            args_by_pid[int(parts[0])] = parts[1] if len(parts) > 1 else ""
    table: dict[int, tuple[int, str, str]] = {}
    for line in names.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            pid = int(parts[0])
            table[pid] = (int(parts[1]), parts[2] if len(parts) > 2 else "", args_by_pid.get(pid, ""))
    return table


def _agent_name(program: str, arguments: str) -> str | None:
    tokens = arguments.split()
    first = os.path.basename(tokens[0]) if tokens else ""
    second = os.path.basename(tokens[1]) if len(tokens) > 1 else ""
    own = os.path.basename(program.rstrip("/"))
    for candidate, may_be_orca in ((first, True), (own, True), (second, False)):
        name = candidate.lstrip("-")
        low = name.lower()
        if not low:
            continue
        if low in AGENT_PROGRAMS:
            return name
        if may_be_orca and (low == "orca" or low.startswith(("orca-", "orca "))):
            return name
    low_args = arguments.lower()
    for mark in AGENT_ARGUMENT_MARKS:
        if mark in low_args:
            return mark.strip("/@")
    return None


def agent_parents(table: dict[int, tuple[int, str, str]] | None) -> list[str]:
    if table is None:
        return ["parent:unknown"]
    pid = os.getppid()
    for _ in range(128):
        if pid <= 1:
            return []
        entry = table.get(pid)
        if entry is None:
            return ["parent:unknown"]
        name = _agent_name(entry[1], entry[2])
        if name is not None:
            return [f"parent:{name}"]
        pid = entry[0]
    return ["parent:unknown"]


def inside_agent_reasons() -> list[str]:
    """Why this process must not start agy: marks in its environment or an agent among its parents."""
    reasons = environment_marks(dict(os.environ))
    return reasons + agent_parents(process_table())


# --- Settings and the private folder ---------------------------------------------------------------


class SettingsError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
LABEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,50}$")
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,79}$")
RUN_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BOUNDS = {
    "project_account_id": (1, 10**12),
    "poll_seconds": (1, 3600),
    "max_jobs_per_day": (1, 100),
    "job_seconds": (1, 14400),
    "quota_retry_seconds": (60, 172800),
    "max_prompt_bytes": (1000, 120000),
    "max_public_chars": (100, 60000),
    "max_answer_bytes": (1000, 50_000_000),
    "max_comment_pages": (1, 100),
}


@dataclass(frozen=True)
class Settings:
    repo: str
    project_account_id: int
    fetch_url: str
    poll_seconds: int
    max_jobs_per_day: int
    job_seconds: int
    quota_retry_seconds: int
    max_prompt_bytes: int
    max_public_chars: int
    max_answer_bytes: int
    max_comment_pages: int
    discussion_first_line: str
    model: str
    work_item_prefixes: tuple[str, ...]
    labels: dict[str, str]
    sandbox: dict[str, bool]


def load_settings(path: Path) -> Settings:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError("unreadable") from exc
    values: dict[str, object] = {}
    for key, (low, high) in BOUNDS.items():
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise SettingsError(key)
        values[key] = value
    repo = data.get("repo")
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise SettingsError("repo")
    fetch_url = data.get("fetch_url")
    if not isinstance(fetch_url, str) or not fetch_url.startswith(("https://", "file:///")) or any(c.isspace() for c in fetch_url):
        raise SettingsError("fetch_url")
    line = data.get("discussion_first_line")
    if not isinstance(line, str) or not line.strip() or "\n" in line or len(line) > 200:
        raise SettingsError("discussion_first_line")
    model = data.get("model", "")
    if not isinstance(model, str) or (model and not MODEL_RE.fullmatch(model)):
        raise SettingsError("model")
    prefixes = data.get("work_item_prefixes", [])
    if not isinstance(prefixes, list) or not all(isinstance(p, str) and re.fullmatch(r"[A-Za-z]{1,10}", p) for p in prefixes):
        raise SettingsError("work_item_prefixes")
    labels = data.get("labels")
    if not isinstance(labels, dict) or set(labels) != set(KINDS) or not all(
        isinstance(v, str) and LABEL_RE.fullmatch(v) for v in labels.values()
    ) or len(set(labels.values())) != len(KINDS):
        raise SettingsError("labels")
    sandbox = data.get("sandbox")
    if not isinstance(sandbox, dict) or set(sandbox) != set(KINDS) or not all(isinstance(v, bool) for v in sandbox.values()):
        raise SettingsError("sandbox")
    return Settings(
        repo=repo,
        fetch_url=fetch_url,
        discussion_first_line=line.strip(),
        model=model,
        work_item_prefixes=tuple(prefixes),
        labels=dict(labels),
        sandbox=dict(sandbox),
        **values,  # type: ignore[arg-type]
    )


def default_private() -> Path:
    shared = Path("/Users") / "Shared"  # the folder for all accounts, beside the home folders
    base = shared if shared.is_dir() else Path("/var/tmp")
    return base / f"dodam-gemini-{os.getuid()}"


def prepare_private(path: Path) -> Path:
    """The private folder: outside the repository and the desk's folders, a real folder closed to others."""
    if not path.is_absolute() or any(c.isspace() for c in str(path)):
        raise SettingsError("private_folder")
    target = path.parent.resolve() / path.name
    if target.is_relative_to(ROOT.resolve()):
        raise SettingsError("private_folder_in_repository")
    for home in {Path.home().resolve(), Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()}:
        if target.is_relative_to(home):
            first = target.relative_to(home).parts[:1]
            if first and first[0].lower() in (".dodam", ".dodam-desk"):
                raise SettingsError("private_folder_forbidden")
    if os.path.islink(target):
        raise SettingsError("private_folder_link")
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.mkdir(target, 0o700)
    info = os.lstat(target)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SettingsError("private_folder_open")
    return target


def read_names(private: Path) -> list[str] | None:
    path = private / NAMES_FILE
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return None
    names = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names or None


# --- GitHub, through the signed-in command-line tool ---------------------------------------------------


class GitHubError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SignedOut(GitHubError):
    pass


def gh(method: str, endpoint: str, body: object = None) -> object:
    """One request through `gh api`, with the sign-in the script was started with."""
    program = shutil.which("gh")
    if program is None:
        raise SignedOut("gh_missing")
    env = dict(os.environ)
    env["GH_PROMPT_DISABLED"] = "1"
    argv = [program, "api", "--method", method, endpoint]
    data = None
    if body is not None:
        argv += ["--input", "-"]
        data = json.dumps(body)
    try:
        proc = subprocess.run(
            argv, input=data, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GH_TIMEOUT_SECONDS, env=env, **({} if data is not None else {"stdin": subprocess.DEVNULL}),
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubError("github_timeout") from exc
    except OSError as exc:
        raise GitHubError("github") from exc
    if proc.returncode == 4 or "gh auth login" in proc.stderr:
        raise SignedOut("github_signed_out")
    if proc.returncode != 0:
        raise GitHubError("github")
    text = proc.stdout.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GitHubError("github") from exc


def gh_pages(endpoint: str, max_pages: int) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    joiner = "&" if "?" in endpoint else "?"
    for page in range(1, max_pages + 1):
        data = gh("GET", f"{endpoint}{joiner}per_page=100&page={page}")
        if not isinstance(data, list):
            raise GitHubError("github")
        found.extend(entry for entry in data if isinstance(entry, dict))
        if len(data) < 100:
            return found
    raise GitHubError("too_many_pages")


def user_id(user: object) -> int:
    if isinstance(user, dict):
        value = user.get("id")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return 0


@dataclass
class Item:
    number: int
    is_pull: bool
    title: str
    body: str
    state: str
    author_id: int
    pull_author_id: int = 0
    head_sha: str = ""
    head_repo: str = ""
    head_fork: bool = True
    base_ref: str = ""
    base_repo: str = ""


SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


def read_item(settings: Settings, number: int) -> Item:
    data = gh("GET", f"repos/{settings.repo}/issues/{number}")
    if not isinstance(data, dict) or data.get("number") != number:
        raise GitHubError("github")
    item = Item(
        number=number,
        is_pull=isinstance(data.get("pull_request"), dict),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        state=str(data.get("state") or ""),
        author_id=user_id(data.get("user")),
    )
    if item.is_pull:
        pull = gh("GET", f"repos/{settings.repo}/pulls/{number}")
        if not isinstance(pull, dict):
            raise GitHubError("github")
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
        head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}  # type: ignore[union-attr]
        base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}  # type: ignore[union-attr]
        item.pull_author_id = user_id(pull.get("user"))
        item.head_sha = str(head.get("sha") or "")  # type: ignore[union-attr]
        item.head_repo = str(head_repo.get("full_name") or "")  # type: ignore[union-attr]
        item.head_fork = head_repo.get("fork") is not False  # type: ignore[union-attr]
        item.base_ref = str(base.get("ref") or "")  # type: ignore[union-attr]
        item.base_repo = str(base_repo.get("full_name") or "")  # type: ignore[union-attr]
    return item


def eligibility(settings: Settings, item: Item) -> str | None:
    """None when the item is one of the project's own open items; otherwise a fixed word saying why not."""
    if item.state != "open":
        return "closed"
    if item.author_id != settings.project_account_id:
        return "author"
    if item.is_pull:
        if item.pull_author_id != settings.project_account_id:
            return "author"
        if item.head_repo.lower() != settings.repo.lower() or item.head_fork:
            return "fork"
        if item.base_repo.lower() != settings.repo.lower():
            return "base"
        if not SHA_RE.fullmatch(item.head_sha) or not REF_RE.fullmatch(item.base_ref) or item.base_ref.startswith("-"):
            return "head"
    return None


# --- The prompt -----------------------------------------------------------------------------------------

BLOCKQUOTE_RE = re.compile(r"(?is)<blockquote\b.*?(?:</blockquote\s*>|\Z)")
INLINE_QUOTE_RE = re.compile(r"(?is)<q\b.*?(?:</q\s*>|\Z)")
# A quote line: `>` at the start, also after the marks of list items (`- >`, `1. >`).
QUOTE_LINE_RE = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])\s+)*>")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")


def strip_quotes(text: str) -> str:
    """Leave out quoted text, which is how text of others is quoted: HTML block and inline quotes, Markdown
    quote lines (also inside list items), and the lines that continue a quote up to the next blank line."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = INLINE_QUOTE_RE.sub("", BLOCKQUOTE_RE.sub("", text))
    kept: list[str] = []
    in_quote = False
    for line in text.split("\n"):
        if QUOTE_LINE_RE.match(line):
            in_quote = True
            continue
        if in_quote:
            if line.strip():
                continue
            in_quote = False
        kept.append(line)
    return "\n".join(kept).strip()


def done_criteria(text: str) -> str:
    lines = text.split("\n")
    for index, line in enumerate(lines):
        plain = re.sub(r"^[\s#*_]+", "", line).strip()
        if re.match(r"(?i)done\b", plain):
            rest = plain.split(":", 1)[1].strip(" *_") if ":" in plain else ""
            found = [rest] if rest else []
            for following in lines[index + 1 :]:
                if HEADING_RE.match(following):
                    break
                found.append(following)
            return "\n".join(found).strip()
    return ""


@dataclass
class Prompt:
    text: str
    left_out: int


def _comment_key(comment: dict[str, object]) -> tuple[str, int]:
    raw_id = comment.get("id")
    return (str(comment.get("created_at") or ""), raw_id if isinstance(raw_id, int) else 0)


def build_prompt(settings: Settings, kind: str, item: Item, comments: list[dict[str, object]], head: str) -> Prompt | str:
    """The prompt from the template, the done criteria and the project's own text; or a fixed word why not."""
    template = (TEMPLATES / f"{kind}.md").read_text(encoding="utf-8").strip()
    project = settings.project_account_id
    notes: list[str] = []
    answers: list[str] = []
    question = ""
    question_note = -1
    for comment in sorted(comments, key=_comment_key):
        if user_id(comment.get("user")) != project:
            continue
        body = str(comment.get("body") or "").replace("\r\n", "\n").replace("\r", "\n")
        stamp = str(comment.get("created_at") or "")
        if body.startswith(FIRST_LINE_PREFIX):
            answers.append(f"[your answer of {stamp}]\n{strip_quotes(body.partition(chr(10))[2])}")
            continue
        text = strip_quotes(body)
        first, _, rest = body.partition("\n")
        if first.strip() == settings.discussion_first_line:
            question = strip_quotes(rest)
            question_note = len(notes) if text else -1
        if text:
            notes.append(f"[comment of {stamp}]\n{text}")
    if kind == "discuss":
        if not question:
            return "no_question"
        # The question is given once, in its own section, and not again among the notes.
        if question_note >= 0:
            del notes[question_note]
    description = strip_quotes(item.body)
    criteria = done_criteria(description)
    title = " ".join(item.title.split())
    if item.is_pull:
        where = (
            f"It is pull request {item.number}: \"{title}\".\n"
            f"The current folder holds a clean checkout of its head commit {head}. The branch named base holds "
            f"the tip of {item.base_ref}; `git diff base...HEAD` shows the change."
        )
    else:
        where = (
            f"It is issue {item.number}: \"{title}\".\n"
            f"The current folder holds a clean checkout of the main branch at {head}."
        )

    def compose(kept_notes: list[str], kept_answers: list[str], left_out: int) -> str:
        parts = [template, "", "---", "", "## The item", "", f"Repository: {settings.repo}", where, ""]
        parts += ["## Done criteria (written by the project's account)", "", criteria or "None are stated on this item.", ""]
        parts += ["## Text by the project's account on this item, oldest first", ""]
        if left_out:
            parts += [f"({left_out} older entries are left out for length.)", ""]
        parts += ["[the description]", description or "(empty)", ""]
        for note in kept_notes:
            parts += [note, ""]
        if kept_answers:
            parts += ["## Your own earlier answers on this item (written by the project's Gemini job), oldest first", ""]
            for answer in kept_answers:
                parts += [answer, ""]
        if kind == "discuss":
            parts += ["## The question", "", question, ""]
        return "\n".join(parts).rstrip() + "\n"

    left_out = 0
    while True:
        text = compose(notes, answers, left_out)
        if len(text.encode("utf-8")) <= settings.max_prompt_bytes:
            return Prompt(text=text, left_out=left_out)
        if notes:
            notes.pop(0)
        elif answers:
            answers.pop(0)
        else:
            return "prompt_too_long"
        left_out += 1


# --- The checkout and the signed-out environment ------------------------------------------------------

PASSED_THROUGH = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TZ")


class JobError(Exception):
    def __init__(self, reason: str, reasons: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.reasons = reasons or [reason]


def signed_out_environment(work: Path) -> dict[str, str]:
    """The whole environment of agy and of git in the checkout: a fixed list of names, no token, no key,
    and empty settings for gh and git so that a gh command or a push started inside is signed out."""
    gh_config = work / "gh-config"
    gh_config.mkdir(mode=0o700)
    git_config = work / "gitconfig"
    git_config.write_bytes(b"")
    env = {name: os.environ[name] for name in PASSED_THROUGH if name in os.environ}
    env.update(
        {
            "GH_CONFIG_DIR": str(gh_config),
            "GH_PROMPT_DISABLED": "1",
            "GIT_CONFIG_GLOBAL": str(git_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": "false",
        }
    )
    return env


def _git(args: list[str], env: dict[str, str], cwd: Path | None = None) -> str:
    program = shutil.which("git", path=env.get("PATH"))
    if program is None:
        raise JobError("git_missing")
    try:
        proc = subprocess.run(
            [program, *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=GIT_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JobError("checkout") from exc
    if proc.returncode != 0:
        raise JobError("checkout")
    return proc.stdout.strip()


def make_checkout(settings: Settings, item: Item, work: Path, env: dict[str, str]) -> tuple[Path, str]:
    """A clone of its own, with no remote at all, at the pull request's head commit or at main's tip."""
    repo = work / "repo"
    _git(["-c", "init.defaultBranch=checkout", "init", "--quiet", str(repo)], env)
    if item.is_pull:
        _git(
            ["fetch", "--quiet", "--no-tags", settings.fetch_url,
             f"+refs/heads/{item.base_ref}:refs/heads/base", f"+refs/pull/{item.number}/head:refs/heads/pull-head"],
            env, cwd=repo,
        )
        head = _git(["rev-parse", "--verify", "refs/heads/pull-head^{commit}"], env, cwd=repo)
        if head != item.head_sha:
            raise JobError("head_moved")
        _git(["checkout", "--quiet", "--detach", head], env, cwd=repo)
        _git(["branch", "--quiet", "-D", "pull-head"], env, cwd=repo)
    else:
        _git(["fetch", "--quiet", "--no-tags", settings.fetch_url, "+refs/heads/main:refs/heads/main-tip"], env, cwd=repo)
        head = _git(["rev-parse", "--verify", "refs/heads/main-tip^{commit}"], env, cwd=repo)
        _git(["checkout", "--quiet", "--detach", head], env, cwd=repo)
        _git(["branch", "--quiet", "-D", "main-tip"], env, cwd=repo)
    if _git(["remote"], env, cwd=repo):
        raise JobError("checkout")
    return repo, head


# --- The day's count and the quota's wait, kept by the job under its lock -------------------------------


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def load_job_state(private: Path) -> dict[str, object]:
    path = private / JOB_STATE_FILE
    if not path.exists():
        return {"version": 1, "days": {}, "hold_until": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JobError("job_state") from exc
    if (
        not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("days"), dict)
        or not isinstance(data.get("hold_until"), (int, float)) or isinstance(data.get("hold_until"), bool)
    ):
        raise JobError("job_state")
    return data


def save_job_state(private: Path, state: dict[str, object]) -> None:
    part = private / (JOB_STATE_FILE + ".part")
    _write_private(part, json.dumps(state) + "\n")
    os.replace(part, private / JOB_STATE_FILE)


def gate(settings: Settings, state: dict[str, object]) -> str | None:
    """Why no run of agy may start now: the wait after a used-up quota, or the day's bound; None when one may."""
    hold = state.get("hold_until")
    if isinstance(hold, (int, float)) and hold > time.time():
        return "quota_wait"
    days = state.get("days")
    count = days.get(today(), 0) if isinstance(days, dict) else 0
    if isinstance(count, int) and count >= settings.max_jobs_per_day:
        return "day_bound"
    return None


# --- Running agy ----------------------------------------------------------------------------------------

_RUNNING: dict[int, subprocess.Popen[bytes]] = {}


def _stop_group(proc: subprocess.Popen[bytes], wait_first: float) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=wait_first)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _on_signal(signum: int, _frame: object) -> None:
    for proc in list(_RUNNING.values()):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    raise SystemExit(128 + signum)


def install_signal_handlers() -> None:
    """When the terminal closes (or on Ctrl-C or a stop), agy and everything it started stop as well."""
    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _on_signal)


def program_record(program: str) -> dict[str, object]:
    real = os.path.realpath(program)
    digest = hashlib.sha256()
    size = 0
    with open(real, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    hint = re.findall(r"\d+(?:\.\d+)+", real)
    return {"program": real, "sha256": digest.hexdigest(), "size": size, "version_in_path": hint[-1] if hint else ""}


@dataclass
class AgyRun:
    exit_code: int | None
    timed_out: bool
    stdout: bytes
    stderr_tail: str


def run_agy(
    settings: Settings, kind: str, prompt: str, repo: Path, env: dict[str, str], answer: Path,
    lock: int, on_start: "Callable[[], None]",
) -> AgyRun:
    program = shutil.which("agy", path=os.environ.get("PATH"))
    if program is None:
        raise JobError("agy_missing")
    argv = [program, "-p", prompt, "--output-format", "json", "--print-timeout", f"{settings.job_seconds}s"]
    if settings.sandbox[kind]:
        argv.append("--sandbox")
    if settings.model:
        argv += ["--model", settings.model]
    record = program_record(program)
    record["arguments"] = ["<the prompt, kept in prompt.txt>" if a == prompt else a for a in argv[1:]]
    record["environment_names"] = sorted(env)
    _write_private(answer / "agy.json", json.dumps(record, indent=2) + "\n")
    refusal = inside_agent_reasons()
    if refusal:
        raise JobError("inside_agent", refusal)
    grace = max(1, min(30, settings.job_seconds // 20))
    stdout_path = answer / "agy-stdout.json"
    stderr_path = answer / "agy-stderr.txt"
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        on_start()
        # agy gets the job's lock as well: a job killed outright leaves the lock held until agy ends.
        proc = subprocess.Popen(
            argv, cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            start_new_session=True, close_fds=True, pass_fds=(lock,),
        )
        _RUNNING[proc.pid] = proc
        timed_out = False
        try:
            try:
                proc.wait(timeout=settings.job_seconds + grace)
            except subprocess.TimeoutExpired:
                timed_out = True
                _stop_group(proc, wait_first=5)
            else:
                # Whatever agy started and left behind stops with it.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        finally:
            _RUNNING.pop(proc.pid, None)
    size = stdout_path.stat().st_size
    with open(stdout_path, "rb") as handle:
        stdout = handle.read(settings.max_answer_bytes + 1)
    if size > settings.max_answer_bytes:
        stdout = stdout[: settings.max_answer_bytes + 1]
    with open(stderr_path, "rb") as handle:
        handle.seek(max(0, stderr_path.stat().st_size - 20000))
        tail = handle.read().decode("utf-8", errors="replace")
    return AgyRun(proc.returncode, timed_out, stdout, tail)


# The pages that were read do not say how a used-up quota shows; these words are a guess.
QUOTA_RE = re.compile(
    r"(?i)quota|rate.?limit|resource.?exhausted|too many requests|\b429\b|usage limit|limit reached|out of credits"
)
AUTH_RE = re.compile(r"(?i)authenticat|not signed in|sign in|log ?in required|unauthori[sz]ed")
AGY_STATUSES = ("SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING")


def interpret(run: AgyRun, max_answer_bytes: int) -> tuple[str, list[str], str | None]:
    if run.timed_out:
        return "timeout", ["time_limit"], None
    if len(run.stdout) > max_answer_bytes:
        return "error", ["answer_too_long"], None
    try:
        data = json.loads(run.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        data = None
    status = str(data.get("status") or "") if isinstance(data, dict) else ""
    if isinstance(data, dict) and status == "SUCCESS" and run.exit_code == 0 and isinstance(data.get("response"), str):
        return "ok", [], data["response"]
    error = str(data.get("error") or "") if isinstance(data, dict) else ""
    said = f"{error}\n{run.stderr_tail}"
    if QUOTA_RE.search(said):
        return "quota", ["quota"], None
    if AUTH_RE.search(said):
        return "signed_out", ["agy_signed_out"], None
    if data is None:
        return "error", ["agy_no_json"], None
    word = status.lower() if status in AGY_STATUSES else "unknown"
    return "error", [f"agy_{word}"], None


# --- The answer, the scan and the comment ---------------------------------------------------------------

VERDICT_RE = re.compile(r"(?im)^[\s*_>#-]*verdict:\s*(approve|changes needed)[\s*_.]*$")


def split_answer(response: str) -> tuple[str | None, str]:
    lines = response.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    public = [i for i, line in enumerate(lines) if line.strip() == PUBLIC_MARK]
    private = [i for i, line in enumerate(lines) if line.strip() == PRIVATE_MARK]
    if len(public) != 1 or len(private) > 1 or (private and private[0] < public[0]):
        return None, ""
    end = private[0] if private else len(lines)
    text = "\n".join(lines[public[0] + 1 : end]).strip()
    return (text or None), "\n".join(lines[end + 1 :]).strip() if private else ""


def verdict_of(public: str | None) -> tuple[str, list[str]]:
    found = {m.group(1).lower() for m in VERDICT_RE.finditer(public or "")}
    if found == {"approve"}:
        return "approve", []
    if found == {"changes needed"}:
        return "changes-needed", []
    return "changes-needed", ["two_verdicts" if found else "no_verdict"]


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in ("Cf", "Cc", "Co", "Cs"))


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PATH_RES = (
    re.compile(r"(?<![\w.])(?:/Users|/home|/private/var|/private/tmp|/var/folders|/var/tmp|/tmp|/Volumes|/root)/[^\s/]+"),
    re.compile(r"(?<![\w/~])~/[\w.-]"),
    re.compile(r"\$\{?HOME\}?(?![A-Za-z0-9_])"),
    re.compile(r"(?i)(?<![a-z])[a-z]:(?:\\{1,2}|/)(?:users|documents and settings)(?:\\{1,2}|/)"),
)
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\.?\d)")
IPV6_RE = re.compile(r"(?<![\w:.])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?![\w:])")
PRIVATE_HOST_RE = re.compile(
    r"(?i)(?<![\w.-])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:local|lan|internal|localdomain|home\.arpa|ts\.net)(?![\w-])"
)
CGNAT = ipaddress.ip_network("100.64.0.0/10")
COMPUTER_RE = re.compile(
    r"(?i)(?<![\w-])[a-z0-9]+(?:-[a-z0-9]+)*-(?:mac-?mini|macbook(?:-?pro|-?air)?|imac|mac-?studio|mac-?pro)(?:-\d+)?(?![\w-])"
)
HONORIFIC_RE = re.compile(r"[가-힣]{2,4}\s?(?:님|씨)")
MENTION_RE = re.compile(r"(?<![\w.@/])@[A-Za-z0-9][A-Za-z0-9-]{0,38}\b")
CODE_RE = re.compile(r"(?s)```.*?(?:```|\Z)|`[^`\n]*`")
TOKEN_RES = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}"),
    re.compile(r"\bxox[abposr]-[0-9A-Za-z-]{10,}"),
    re.compile(r"\bglpat-[0-9A-Za-z_-]{20,}"),
    re.compile(r"\bdop_v1_[0-9a-f]{32,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{30,}"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
)
LONG_RUN_RE = re.compile(r"(?<![A-Za-z0-9_+=-])[A-Za-z0-9_+=-]{32,}(?![A-Za-z0-9_+=-])")
# A secret access key: 40 characters of base 64 that hold a slash or a plus.
SECRET40_RE = re.compile(r"(?<![A-Za-z0-9/+=])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+=])")
TAG_RE = re.compile(r"<[^<>\n]*>")


def _word_pattern(value: str) -> re.Pattern[str]:
    """A listed value, bounded by letters only: joined to digits, hyphens or underscores it still counts,
    and its words may be joined by any characters that are not letters or digits."""
    body = r"[\W_]*".join(re.escape(word) for word in value.split())
    return re.compile(r"(?i)(?<![^\W\d_])" + body + r"(?![^\W\d_])")


def unmarked(text: str) -> str:
    """A copy as GitHub shows it: character references resolved, code, tags and emphasis marks taken out."""
    for _ in range(3):
        resolved = html.unescape(text)
        if resolved == text:
            break
        text = resolved
    text = TAG_RE.sub("", CODE_RE.sub(" ", text))
    return normalize(re.sub(r"[*~]", "", text))


def known_values() -> dict[str, list[str]]:
    """Values of this computer and account that never appear in a public part."""
    account = pwd.getpwuid(os.getuid())
    names = [account.pw_name] + [part.strip() for part in account.pw_gecos.split(",") if part.strip()]
    hosts = set()
    for host in (socket.gethostname(), os.uname().nodename):
        if host:
            hosts.add(host)
            hosts.add(host.split(".")[0])
    paths = {str(Path.home()), account.pw_dir, str(ROOT)}
    return {
        "name": [n for n in names if len(n) >= 3],
        "computer_name": sorted(h for h in hosts if len(h) >= 4 and h.lower() not in ("localhost", "localhost.localdomain")),
        "local_path": sorted(p for p in paths if len(p) > 1),
    }


def scan(text: str, names: list[str], settings: Settings, known: dict[str, list[str]]) -> list[str]:
    """The kinds of things in `text` that must not be public; empty when it may be posted. The text is scanned
    as it is, and a second time as GitHub would show it."""
    return sorted(set(_scan_once(text, names, settings, known)) | set(_scan_once(unmarked(text), names, settings, known)))


def _scan_once(text: str, names: list[str], settings: Settings, known: dict[str, list[str]]) -> list[str]:
    found: set[str] = set()
    flat = " ".join(text.split())
    if EMAIL_RE.search(text):
        found.add("email")
    for name in [*names, *known.get("name", [])]:
        if name.isascii():
            if _word_pattern(name).search(flat):
                found.add("name")
        elif " ".join(name.split()).lower() in flat.lower():
            found.add("name")
    if HONORIFIC_RE.search(text) or MENTION_RE.search(CODE_RE.sub(" ", text)):
        found.add("name")
    if any(pattern.search(text) for pattern in PATH_RES) or any(p in text for p in known.get("local_path", [])):
        found.add("local_path")
    for match in IPV4_RE.finditer(text):
        try:
            address = ipaddress.ip_address(match.group(1))
        except ValueError:
            continue
        if (address.is_private and not address.is_loopback and not address.is_unspecified) or address in CGNAT or address.is_link_local:
            found.add("private_address")
    for match in IPV6_RE.finditer(text):
        try:
            address6 = ipaddress.ip_address(match.group(1))
        except ValueError:
            continue
        if (address6.is_private or address6.is_link_local) and not (address6.is_loopback or address6.is_unspecified):
            found.add("private_address")
    if PRIVATE_HOST_RE.search(text):
        found.add("private_address")
    if COMPUTER_RE.search(text) or any(_word_pattern(h).search(flat) for h in known.get("computer_name", [])):
        found.add("computer_name")
    if settings.work_item_prefixes:
        prefixes = "|".join(re.escape(p) for p in settings.work_item_prefixes)
        if re.search(rf"(?i)(?<![\w-])(?:{prefixes})-\d+\b", text):
            found.add("work_item")
    if re.search(r"(?i)linear\.app/", text):
        found.add("work_item")
    if any(pattern.search(text) for pattern in TOKEN_RES):
        found.add("token")
    for match in [*LONG_RUN_RE.finditer(text), *SECRET40_RE.finditer(text)]:
        run = match.group(0)
        mixed = re.search(r"[A-Z]", run) and re.search(r"[a-z]", run) and re.search(r"\d", run)
        if mixed and (match.re is LONG_RUN_RE or re.search(r"[/+]", run)):
            found.add("token")
    return sorted(found)


def first_line(kind: str) -> str:
    return f"{FIRST_LINE_PREFIX} ({LOOKS[kind]} by Google's Gemini through the Antigravity CLI, started by a script of the project)."


# --- Private files and telling the coordinator ----------------------------------------------------------


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def answer_folder(private: Path, kind: str, number: int) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    folder = private / "answers" / f"{kind}-{number}-{stamp}-{os.getpid()}"
    folder.parent.mkdir(mode=0o700, exist_ok=True)
    suffix = 0
    while True:
        candidate = folder if suffix == 0 else folder.with_name(f"{folder.name}-{suffix}")
        try:
            candidate.mkdir(mode=0o700)
            return candidate
        except FileExistsError:
            suffix += 1


def coordinator_line(result: dict[str, object]) -> str:
    return (
        f"gemini-job kind={result['kind']} number={result['number']} status={result['status']} "
        f"verdict={result['verdict']} private={result['private'] or 'none'}"
    )


def tell_coordinator(private: Path, result: dict[str, object], run_id: str | None) -> bool:
    """Write the fixed line to the private folder, and send it to the coordinator's run when one is named."""
    line = coordinator_line(result)
    fd = os.open(private / FINISHED_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if not run_id:
        return False
    program = shutil.which("orca") or (str(ORCA_APP_CLI) if ORCA_APP_CLI.exists() else None)
    if program is None:
        return False
    try:
        proc = subprocess.run(
            [program, "orchestration", "send", "--to", f"run:{run_id}", "--type", "status",
             "--subject", f"Gemini job finished: {result['kind']} {result['number']}", "--body", line],
            capture_output=True, timeout=ORCA_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


# --- One job ------------------------------------------------------------------------------------------


def _acquire(path: Path) -> int | None:
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def job_lock_is_free(private: Path) -> bool:
    fd = _acquire(private / JOB_LOCK)
    if fd is None:
        return False
    os.close(fd)
    return True


def run_job(kind: str, number: int, settings: Settings, private: Path, tell: str | None = None) -> dict[str, object]:
    """Run one job and return its result: fixed words, numbers and the private answer's path only."""
    result: dict[str, object] = {
        "kind": kind, "number": number, "status": "error", "verdict": "changes-needed",
        "reasons": [], "private": None, "ran": False,
    }
    refusal = inside_agent_reasons()
    if refusal:
        result.update(status="refused", reasons=refusal)
        return result
    lock = _acquire(private / JOB_LOCK)
    if lock is None:
        result["status"] = "busy"
        return result
    try:
        state = load_job_state(private)
        held_back = gate(settings, state)
        if held_back == "quota_wait":
            result.update(status="quota", reasons=["quota_wait"])
        elif held_back == "day_bound":
            result.update(status="day_bound", reasons=["day_bound"])
        else:
            _job(kind, number, settings, private, result, lock, state)
    except SystemExit:
        raise
    except SignedOut as exc:
        result.update(status="signed_out", reasons=[exc.reason])
    except GitHubError as exc:
        result.update(status="error", reasons=[exc.reason])
    except JobError as exc:
        result.update(status="refused" if exc.reason == "inside_agent" else "error", reasons=exc.reasons)
    except Exception as exc:  # noqa: BLE001 - the result names the kind of fault, never a trace
        result.update(status="error", reasons=[f"internal_{type(exc).__name__}"])
    finally:
        os.close(lock)
    if result["private"] is not None:
        _write_private(Path(str(result["private"])) / "result.json", json.dumps(result, indent=2) + "\n")
        tell_coordinator(private, result, tell)
    return result


def _job(
    kind: str, number: int, settings: Settings, private: Path, result: dict[str, object], lock: int,
    state: dict[str, object],
) -> None:
    item = read_item(settings, number)
    reason = eligibility(settings, item)
    if reason is not None:
        result.update(status="not_eligible", reasons=[reason])
        return
    comments = gh_pages(f"repos/{settings.repo}/issues/{number}/comments", settings.max_comment_pages)
    if kind == "discuss":
        draft = build_prompt(settings, kind, item, comments, "0" * 40)
        if draft == "no_question":
            result.update(status="no_question", reasons=["no_question"])
            return
    work = Path(tempfile.mkdtemp(prefix="dodam-gemini-"))
    try:
        env = signed_out_environment(work)
        repo, head = make_checkout(settings, item, work, env)
        prompt = build_prompt(settings, kind, item, comments, head)
        if isinstance(prompt, str):
            result.update(status="no_question" if prompt == "no_question" else "error", reasons=[prompt])
            return
        answer = answer_folder(private, kind, number)
        result["private"] = str(answer)
        _write_private(answer / "prompt.txt", prompt.text)
        day = today()

        def count_the_run() -> None:
            # Counted in the day right before agy starts, under the job's lock, however the job was started.
            days = state.get("days") if isinstance(state.get("days"), dict) else {}
            count = days.get(day, 0) if isinstance(days, dict) else 0
            state["days"] = {day: (count if isinstance(count, int) else 0) + 1}
            save_job_state(private, state)
            result["ran"] = True

        run = run_agy(settings, kind, prompt.text, repo, env, answer, lock, count_the_run)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    status, reasons, response = interpret(run, settings.max_answer_bytes)
    if status == "quota":
        # A used-up quota: the run is not counted in the day, and no run starts until the wait has passed.
        days = state.get("days") if isinstance(state.get("days"), dict) else {}
        count = days.get(day, 0) if isinstance(days, dict) else 0
        state["days"] = {day: max(0, (count if isinstance(count, int) else 1) - 1)}
        state["hold_until"] = time.time() + settings.quota_retry_seconds
        save_job_state(private, state)
    if response is None:
        result.update(status=status, reasons=reasons)
        return
    _write_private(answer / "answer.md", response)
    public, _private_part = split_answer(response)
    verdict, verdict_reasons = verdict_of(public)
    result["verdict"] = verdict
    if public is None:
        result.update(status="held", reasons=["no_public_part"])
        return
    public = normalize(public)
    comment = f"{first_line(kind)}\n\n{public}\n"
    _write_private(answer / "public.md", comment)
    if len(public) > settings.max_public_chars:
        result.update(status="held", reasons=["too_long", *verdict_reasons])
        return
    names = read_names(private)
    if names is None:
        result.update(status="held", reasons=["no_names_list", *verdict_reasons])
        return
    found = scan(comment, names, settings, known_values())
    if found:
        result.update(status="held", reasons=[*found, *verdict_reasons])
        return
    gh("POST", f"repos/{settings.repo}/issues/{number}/comments", {"body": comment})
    result.update(status="posted", reasons=verdict_reasons)


# --- The command --------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the project's Gemini job once for one pull request or issue.")
    parser.add_argument("--kind", required=True, choices=KINDS)
    parser.add_argument("--number", required=True, type=int)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--private", type=Path, default=None)
    parser.add_argument("--tell", default=None, help="the coordinator's run, as run_... (Orca)")
    return parser.parse_args(argv)


def tell_run(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw[4:] if raw.startswith("run:") else raw
    if not RUN_RE.fullmatch(value):
        raise SettingsError("tell")
    return value


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    install_signal_handlers()
    args = parse_args(argv)
    result: dict[str, object] = {
        "kind": args.kind, "number": args.number, "status": "error", "verdict": "changes-needed",
        "reasons": [], "private": None, "ran": False,
    }
    refusal = inside_agent_reasons()
    if refusal:
        result.update(status="refused", reasons=refusal)
        print(json.dumps(result), flush=True)
        print("gemini job: refused: it runs inside a coding agent or a terminal an agent can type into", file=sys.stderr)
        return EXIT_CODES["refused"]
    try:
        if not 0 < args.number < 10**8:
            raise SettingsError("number")
        settings = load_settings(args.settings)
        private = prepare_private(args.private or default_private())
        run_id = tell_run(args.tell)
    except SettingsError as exc:
        result.update(status="settings", reasons=[exc.reason])
        print(json.dumps(result), flush=True)
        return EXIT_CODES["settings"]
    result = run_job(args.kind, args.number, settings, private, run_id)
    print(json.dumps(result), flush=True)
    return EXIT_CODES.get(str(result["status"]), 1)


if __name__ == "__main__":
    sys.exit(main())
