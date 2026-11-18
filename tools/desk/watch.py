#!/usr/bin/env python3
"""Watches BrilliantTiger/dodam on GitHub for new activity from outside.

    python3 tools/desk/watch.py --wait [--include-own] [--state FILE]
                                       [--settle SECONDS] [--max-events N]
                                       [--max-wait SECONDS] [--repo OWNER/NAME]
    python3 tools/desk/watch.py --help

Talks to GitHub only through the signed-in gh CLI via run_gh(). Never reads,
accepts, or prints tokens or HTTP headers. When run with --wait, writes nothing
to standard output except the single JSON object printed right before exiting
with code 0 (or code 7 if saving state fails after printing).

Counted outside event kinds:
  - issue_opened
  - issue_reopened
  - issue_comment
  - pull_request_comment
  - pull_request_opened
  - pull_request_ready
  - pull_request_reopened
  - pull_request_commits  (actor is the pull request's author, not the pusher)
  - pull_request_review
  - pull_request_review_comment

Exit codes:
  0    Printed one JSON object on standard output (or --help text) and saved state.
  1    Unexpected internal error before any events were printed.
  2    gh is not signed in, missing, or rejected with HTTP 401.
  3    Invalid command-line arguments or disallowed --state location.
  4    Existing state file or its folder cannot be read or is damaged.
  5    State file location cannot be written (checked before calling GitHub).
  6    Another watcher is already holding the lock on this --state file.
  7    Events JSON was printed on standard output, then saving state failed.
  130  Interrupted by signal (SIGINT).
"""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

DEFAULT_REPO = "BrilliantTiger/dodam"
POLL_INTERVAL = 15.0
DEFAULT_SETTLE = 15.0
DEFAULT_MAX_EVENTS = 20
BASE_BACKOFF = 5.0
MAX_BACKOFF = 300.0
GH_TIMEOUT_SECONDS = 30.0
PER_PAGE = 100

STATE_FOLDER_NAME = ".dodam-desk"
STATE_FILE_NAME = "watch-state.json"
STATE_VERSION = 2

# Explicit upper bounds so the state file cannot grow without limit.
MAX_OPEN_PR_REVIEWS_POLLED = 25
MAX_SEEN_IDS = 5_000
MAX_TRACKED_PULLS = 500
MAX_REVIEW_ETAGS = 100
MAX_PENDING_EVENTS = 1_000
MAX_STATE_BYTES = 16 * 1024 * 1024

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_SIGNED_OUT = 2
EXIT_BAD_ARGS = 3
EXIT_STATE_READ = 4
EXIT_STATE_WRITE = 5
EXIT_STATE_LOCKED = 6
EXIT_SAVE_AFTER_PRINT = 7
EXIT_INTERRUPTED = 130

REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
HEX_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
HTTP_STATUS_RE = re.compile(r"^HTTP/\d+(?:\.\d+)?\s+(\d{3})\b")
LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
ISSUE_URL_NUM_RE = re.compile(r"/issues/(\d+)$")
PULL_URL_NUM_RE = re.compile(r"/pulls/(\d+)$")
HTML_ITEM_NUM_RE = re.compile(r"/(?:issues|pull)/(\d+)(?:$|[#/?])")

EVENT_KINDS = (
    "issue_opened",
    "issue_reopened",
    "issue_comment",
    "pull_request_comment",
    "pull_request_opened",
    "pull_request_ready",
    "pull_request_reopened",
    "pull_request_commits",
    "pull_request_review",
    "pull_request_review_comment",
)

HELP_TEXT = """Usage: python3 tools/desk/watch.py --wait [options]

Watches BrilliantTiger/dodam on GitHub through the signed-in gh CLI and prints
one JSON object on standard output when outside activity arrives.

Options:
  --wait               Block until outside activity arrives (required unless --help).
  --state FILE         State file path (default: ~/.dodam-desk/watch-state.json;
                       never inside the repository and never inside .dodam).
  --include-own        Report events by the signed-in gh account too (for trials;
                       bots are still excluded).
  --settle SECONDS     Wait after the first new event to batch more (default: 15).
  --max-events N       Maximum events returned in one JSON object (default: 20).
  --max-wait SECONDS   Exit 0 with {"events": [], "reason": "timeout"} after SECONDS
                       if nothing new arrives (default: wait without limit).
  --repo OWNER/NAME    Repository to watch (default: BrilliantTiger/dodam).
  -h, --help           Show this help text and exit 0.

Notes:
  - For pull_request_commits ("new commits"), actor (and the outside/own/bot check)
    is the pull request's author, not the account that pushed the commits.
  - One idle round makes one conditional GET per watched list (five repository lists
    plus one review tail page per open pull request — seven requests with two open
    pull requests, taking about 19 seconds including the 15-second pause) and uses
    zero counted primary rate-limit requests when nothing changed.
  - State size is bounded (at most 5,000 recent event ids, 500 tracked pull requests,
    100 cached review-page ETags, and 1,000 queued overflow events).

Exit codes:
  0    Normal exit: printed one JSON object (or --help text) and saved state.
  1    Unexpected internal error before printing any events.
  2    gh is not signed in, missing, or rejected with HTTP 401.
  3    Invalid command-line arguments or disallowed --state path.
  4    Existing state file or its folder cannot be read or is damaged.
  5    State location cannot be written (checked before calling GitHub).
  6    Another watcher is already holding the lock on this --state file.
  7    Events JSON was printed on standard output, then saving state failed.
  130  Interrupted by signal (SIGINT).
"""

clock = time.monotonic
epoch = time.time
sleep = time.sleep


class HelpRequested(Exception):
    """Raised when -h or --help is passed."""


class BadArgsError(Exception):
    """Raised on invalid command-line arguments or disallowed --state paths (exit 3)."""


class StateReadError(Exception):
    """Raised when the state file or folder cannot be read or parsed (exit 4)."""


class StateWriteError(Exception):
    """Raised when the state file cannot be written before printing events (exit 5)."""


class StateLockedError(Exception):
    """Raised when another watcher holds the lock on --state (exit 6)."""


class SaveAfterPrintError(Exception):
    """Raised when saving state fails after events JSON was printed to stdout (exit 7)."""


class SignedOutError(Exception):
    """Raised when gh is not signed in or authentication has failed (exit 2)."""


class MalformedItemError(Exception):
    """Raised when a single item inside a GitHub list has an unexpected shape."""


class TransientGitHubError(Exception):
    """Raised on network errors, GitHub faults, unexpected whole answers, or rate limits."""

    def __init__(self, kind: str, min_wait: float = 0.0):
        super().__init__(kind)
        self.kind = kind
        self.min_wait = max(0.0, min_wait)


def run_gh(args: list[str]) -> tuple[int, str, str]:
    """Run the signed-in gh CLI and return (returncode, stdout, stderr)."""
    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=GH_TIMEOUT_SECONDS,
            close_fds=True,
        )
    except FileNotFoundError:
        return (4, "", "gh command not found")
    except subprocess.TimeoutExpired:
        return (1, "", "gh request timed out")
    return (proc.returncode, proc.stdout, proc.stderr)


def default_state_path() -> Path:
    """Default state file location in a dedicated folder under the home directory."""
    return Path.home() / STATE_FOLDER_NAME / STATE_FILE_NAME


def validate_state_path(raw_path: Path, repo_root: Path | None = None) -> Path:
    """Validate --state and forbid .dodam and repo_root regardless of letter case or symlinks."""
    effective_repo = ROOT if repo_root is None else repo_root
    raw_str = str(raw_path)
    if not raw_str or "\x00" in raw_str:
        raise BadArgsError("Invalid state file path.")
    try:
        expanded = raw_path.expanduser()
    except (RuntimeError, OSError, ValueError) as exc:
        raise BadArgsError("Invalid home directory in state file path.") from exc

    abs_path = Path(os.path.abspath(expanded))
    try:
        real_path = Path(os.path.realpath(abs_path))
    except OSError:
        real_path = abs_path

    # 1. Lexical case-insensitive check for .dodam anywhere in expanded or resolved path.
    for candidate in (abs_path, real_path):
        for part in candidate.parts:
            if part.casefold() == ".dodam":
                raise BadArgsError("The state file must never be inside .dodam.")

    # 2. Filesystem identity check (st_dev, st_ino) on existing ancestors so case-insensitive
    #    disks and symlinks cannot alias either the repository folder or ~/.dodam.
    guarded_dirs: list[Path] = [effective_repo]
    try:
        guarded_dirs.append(Path.home() / ".dodam")
    except (RuntimeError, OSError):
        pass

    guarded_stats: list[os.stat_result] = []
    for guarded in guarded_dirs:
        try:
            guarded_stats.append(os.stat(guarded))
        except OSError:
            pass

    if guarded_stats:
        for candidate in (abs_path, real_path):
            for anc in (candidate, *candidate.parents):
                try:
                    st = os.stat(anc)
                except OSError:
                    continue
                if any(os.path.samestat(st, gst) for gst in guarded_stats):
                    raise BadArgsError("The state file must not be inside the repository or .dodam.")

    # Fallback lexical check when effective_repo does not exist on disk in a synthetic test.
    repo_abs = Path(os.path.abspath(effective_repo))
    for candidate in (abs_path, real_path):
        if candidate == repo_abs or repo_abs in candidate.parents:
            raise BadArgsError("The state file must not be inside the repository.")

    return real_path


class _HelpAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS):
        super().__init__(option_strings=option_strings, dest=dest, nargs=0)

    def __call__(self, parser, namespace, values, option_string=None):
        raise HelpRequested()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BadArgsError("Invalid command-line arguments; run with --help for usage.")


def parse_args(argv: list[str] | None, repo_root: Path | None = None) -> argparse.Namespace:
    parser = _Parser(prog="watch.py", add_help=False)
    parser.add_argument("-h", "--help", action=_HelpAction)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--include-own", action="store_true")
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE)
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    parser.add_argument("--max-wait", type=float, default=None)
    parser.add_argument("--repo", type=str, default=DEFAULT_REPO)
    ns = parser.parse_args(argv)
    if not ns.wait:
        raise BadArgsError("Pass --wait to block for new outside events, or --help for usage.")
    if not REPO_RE.fullmatch(ns.repo):
        raise BadArgsError("Invalid --repo; expected OWNER/NAME.")
    if not math.isfinite(ns.settle) or ns.settle < 0:
        raise BadArgsError("--settle must be a non-negative number.")
    if ns.max_events < 1:
        raise BadArgsError("--max-events must be at least 1.")
    if ns.max_wait is not None and (not math.isfinite(ns.max_wait) or ns.max_wait < 0):
        raise BadArgsError("--max-wait must be a non-negative number.")
    try:
        chosen = ns.state if ns.state is not None else default_state_path()
    except (RuntimeError, OSError, ValueError) as exc:
        raise BadArgsError("Could not determine default state file path.") from exc
    ns.state_path = validate_state_path(chosen, repo_root=repo_root)
    return ns


def _empty_state() -> dict[str, object]:
    return {
        "version": STATE_VERSION,
        "initialized": False,
        "seen_ids": [],
        "pending": [],
        "etags": {},
        "watermarks": {
            "baseline_cutoff": "",
            "max_issue_id": 0,
            "max_issue_event_id": 0,
            "max_issue_comment_id": 0,
            "max_pull_id": 0,
            "max_pull_updated_at": "",
            "max_review_comment_id": 0,
        },
        "known_pr_numbers": [],
        "open_pr_numbers": [],
        "reviews_baselined": [],
        "pull_heads": {},
        "pull_seq": {},
        "pull_draft": {},
        "pull_ready_seq": {},
        "pull_baseline_updated_at": {},
        "pull_reviews_checked": {},
        "pull_review_page": {},
    }


def _bound_state(state: dict[str, object]) -> None:
    """Enforce upper bounds on every dynamic collection in state."""
    seen: list[str] = state["seen_ids"]  # type: ignore[assignment]
    if len(seen) > MAX_SEEN_IDS:
        state["seen_ids"] = seen[-MAX_SEEN_IDS:]

    pending: list[dict[str, object]] = state["pending"]  # type: ignore[assignment]
    if len(pending) > MAX_PENDING_EVENTS:
        state["pending"] = pending[:MAX_PENDING_EVENTS]

    open_prs_raw: list[int] = list(state["open_pr_numbers"])  # type: ignore[arg-type]
    seen_open: list[int] = []
    for n in open_prs_raw:
        if n not in seen_open:
            seen_open.append(n)
    open_prs = seen_open[:MAX_TRACKED_PULLS]
    state["open_pr_numbers"] = open_prs

    known_prs: list[int] = state["known_pr_numbers"]  # type: ignore[assignment]
    pull_heads: dict[str, str] = state["pull_heads"]  # type: ignore[assignment]
    all_pr_nums = set(known_prs) | {int(k) for k in pull_heads if k.isdigit()} | set(open_prs)
    if len(all_pr_nums) > MAX_TRACKED_PULLS:
        keep_nums: set[int] = set(open_prs)
        for num in sorted(all_pr_nums - keep_nums, reverse=True):
            if len(keep_nums) >= MAX_TRACKED_PULLS:
                break
            keep_nums.add(num)
    else:
        keep_nums = all_pr_nums

    state["known_pr_numbers"] = sorted(keep_nums)
    keep_keys = {str(n) for n in keep_nums}
    for map_name in (
        "pull_heads",
        "pull_seq",
        "pull_draft",
        "pull_ready_seq",
        "pull_baseline_updated_at",
        "pull_reviews_checked",
        "pull_review_page",
    ):
        mapping = state[map_name]
        assert isinstance(mapping, dict)
        for k in [k for k in mapping if k not in keep_keys]:
            del mapping[k]

    baselined: list[int] = state["reviews_baselined"]  # type: ignore[assignment]
    state["reviews_baselined"] = [n for n in baselined if n in keep_nums][-MAX_TRACKED_PULLS:]

    etags: dict[str, str] = state["etags"]  # type: ignore[assignment]
    core_keys = {"pulls", "issues", "issue_events", "issue_comments", "pull_comments"}
    review_keys = [k for k in etags if k not in core_keys]
    if len(review_keys) > MAX_REVIEW_ETAGS:
        for old_k in review_keys[:-MAX_REVIEW_ETAGS]:
            del etags[old_k]


def _load_state(path: Path) -> dict[str, object]:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return _empty_state()
    except OSError as exc:
        raise StateReadError("State file cannot be accessed.") from exc

    if not stat.S_ISREG(st.st_mode):
        raise StateReadError("State path is not a regular file.")
    if st.st_size > MAX_STATE_BYTES:
        raise StateReadError("State file exceeds maximum allowed size.")

    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8")
        raw = json.loads(text)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise StateReadError("State file is unreadable or damaged.") from exc

    if not isinstance(raw, dict):
        raise StateReadError("State file has an invalid root value.")

    base = _empty_state()
    if not set(raw).issubset(set(base) | {"version"}):
        raise StateReadError("State file contains unrecognized fields.")
    if "version" in raw and (isinstance(raw["version"], bool) or not isinstance(raw["version"], int)):
        raise StateReadError("State file has an invalid version.")
    if "initialized" in raw and not isinstance(raw["initialized"], bool):
        raise StateReadError("State file has an invalid initialized flag.")
    base["initialized"] = bool(raw.get("initialized", False))

    seen = raw.get("seen_ids", [])
    if not isinstance(seen, list) or not all(isinstance(x, str) for x in seen):
        raise StateReadError("State file has invalid seen_ids.")
    # Preserve order while deduplicating.
    dedup_seen: list[str] = []
    seen_lookup: set[str] = set()
    for item_id in seen:
        if item_id not in seen_lookup:
            seen_lookup.add(item_id)
            dedup_seen.append(item_id)
    base["seen_ids"] = dedup_seen

    pending = raw.get("pending", [])
    allowed_ev_keys = {"id", "kind", "number", "actor", "time", "url"}
    if not isinstance(pending, list):
        raise StateReadError("State file has invalid pending events.")
    for ev in pending:
        if (
            not isinstance(ev, dict)
            or not set(ev).issubset(allowed_ev_keys)
            or not isinstance(ev.get("id"), str)
            or not isinstance(ev.get("kind"), str)
            or isinstance(ev.get("number"), bool)
            or not isinstance(ev.get("number"), int)
            or not isinstance(ev.get("time"), str)
            or not isinstance(ev.get("url"), str)
            or ("actor" in ev and not isinstance(ev["actor"], str))
        ):
            raise StateReadError("State file has invalid pending event item.")
    base["pending"] = list(pending)

    for str_map in ("etags", "pull_heads", "pull_baseline_updated_at", "pull_reviews_checked"):
        val = raw.get(str_map, {})
        if not isinstance(val, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in val.items()):
            raise StateReadError(f"State file has invalid {str_map}.")
        base[str_map] = dict(val)

    for int_map in ("pull_seq", "pull_ready_seq", "pull_review_page"):
        val = raw.get(int_map, {})
        if not isinstance(val, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
            for k, v in val.items()
        ):
            raise StateReadError(f"State file has invalid {int_map}.")
        base[int_map] = dict(val)

    draft_map = raw.get("pull_draft", {})
    if not isinstance(draft_map, dict) or not all(isinstance(k, str) and isinstance(v, bool) for k, v in draft_map.items()):
        raise StateReadError("State file has invalid pull_draft.")
    base["pull_draft"] = dict(draft_map)

    wm = raw.get("watermarks", {})
    if not isinstance(wm, dict):
        raise StateReadError("State file has invalid watermarks.")
    target_wm = base["watermarks"]
    assert isinstance(target_wm, dict)
    for k, default_v in list(target_wm.items()):
        if k in wm:
            v = wm[k]
            if type(v) is not type(default_v):
                raise StateReadError("State file has an invalid watermark type.")
            if isinstance(v, int) and v < 0:
                raise StateReadError("State file has a negative watermark.")
            target_wm[k] = v

    for list_key in ("known_pr_numbers", "open_pr_numbers", "reviews_baselined"):
        vals = raw.get(list_key, [])
        if not isinstance(vals, list) or not all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in vals):
            raise StateReadError(f"State file has invalid {list_key}.")
        base[list_key] = list(vals)

    _bound_state(base)
    return base


def _acquire_state_lock_and_verify_writable(state_path: Path) -> int:
    """Create state folder, acquire exclusive non-blocking flock, and verify write access."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StateWriteError("Cannot create folder for state file.") from exc

    lock_path = state_path.parent / f".{state_path.name}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    try:
        lock_fd = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        raise StateWriteError("Cannot open lock file in state folder.") from exc

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            raise StateLockedError("Another watcher is already using this state file.") from exc
        raise StateWriteError("Cannot lock state file.") from exc

    # Verify we can create, write, fsync, and unlink a temporary file in the state folder before asking GitHub.
    probe_fd = -1
    probe_name = ""
    try:
        probe_fd, probe_name = tempfile.mkstemp(prefix=".watch-probe-", suffix=".tmp", dir=str(state_path.parent))
        os.write(probe_fd, b"{}\n")
        os.fsync(probe_fd)
        os.close(probe_fd)
        probe_fd = -1
        os.unlink(probe_name)
    except OSError as exc:
        if probe_fd >= 0:
            try:
                os.close(probe_fd)
            except OSError:
                pass
        if probe_name:
            try:
                os.unlink(probe_name)
            except OSError:
                pass
        try:
            os.close(lock_fd)
        except OSError:
            pass
        raise StateWriteError("State file folder is not writable.") from exc

    return lock_fd


def _save_state(path: Path, state: dict[str, object]) -> None:
    _bound_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=True, separators=(",", ":")) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".watch-state-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _parse_http_response(text: str) -> tuple[int | None, dict[str, str], str]:
    """Split gh api -i output into (status_code, lowercased_headers, body) without exposing headers."""
    remaining = text
    status_code: int | None = None
    headers: dict[str, str] = {}
    while True:
        lines = remaining.splitlines(keepends=True)
        if not lines:
            return (status_code, headers, "")
        first = lines[0].rstrip("\r\n")
        match = HTTP_STATUS_RE.match(first)
        if not match:
            return (status_code, headers, remaining)
        status_code = int(match.group(1))
        headers = {}
        body_start = len(lines)
        for idx in range(1, len(lines)):
            stripped = lines[idx].rstrip("\r\n")
            if stripped == "":
                body_start = idx + 1
                break
            if ":" in stripped:
                key, value = stripped.split(":", 1)
                k = key.strip().lower()
                v = value.strip()
                if k in headers and k == "link":
                    headers[k] = f"{headers[k]}, {v}"
                else:
                    headers[k] = v
        remaining = "".join(lines[body_start:])
        if status_code in (100, 101, 102, 103) and HTTP_STATUS_RE.match(remaining.lstrip()):
            remaining = remaining.lstrip()
            continue
        return (status_code, headers, remaining)


def _is_auth_error(returncode: int, status: int | None, stderr: str) -> bool:
    if returncode == 4 or status == 401:
        return True
    if status is None and returncode != 0:
        low = stderr.lower()
        for marker in (
            "auth login",
            "not logged in",
            "not logged into",
            "authentication",
            "gh command not found",
            "to get started with github cli",
        ):
            if marker in low:
                return True
    return False


def _positive_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        val = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(val) or val < 0:
        return None
    return val


def _rate_limit_wait(headers: dict[str, str]) -> float:
    wait = 0.0
    retry_after = _positive_float(headers.get("retry-after"))
    if retry_after is not None:
        wait = max(wait, retry_after)
    reset_val = _positive_float(headers.get("x-ratelimit-reset"))
    if reset_val is not None:
        wait = max(wait, reset_val - epoch() + 1.0)
    return wait


def _next_endpoint(link_header: str | None) -> str | None:
    if not link_header:
        return None
    match = LINK_NEXT_RE.search(link_header)
    if not match:
        return None
    target = match.group(1).strip()
    if target.startswith("https://api.github.com/"):
        rel = target[len("https://api.github.com/") :]
    elif target.startswith(("/repos/", "repos/", "/repositories/", "repositories/")):
        rel = target.lstrip("/")
    else:
        raise TransientGitHubError("unexpected")
    if not rel.startswith(("repos/", "repositories/")) or any(ch.isspace() for ch in rel):
        raise TransientGitHubError("unexpected")
    return rel


def _gh_get(endpoint: str, etag: str | None = None) -> tuple[int, dict[str, str], object, float]:
    """Perform one authenticated GET via gh api -i and return (status, headers, json_or_none, poll_interval)."""
    cmd = ["api", "-i"]
    if etag:
        cmd.extend(["-H", f"If-None-Match: {etag}"])
    cmd.append(endpoint)
    returncode, stdout, stderr = run_gh(cmd)
    status, headers, body = _parse_http_response(stdout)

    if _is_auth_error(returncode, status, stderr):
        raise SignedOutError()

    poll_hint = _positive_float(headers.get("x-poll-interval")) or 0.0

    if status == 304:
        return (304, headers, None, poll_hint)

    if status == 429:
        raise TransientGitHubError("rate_limit", _rate_limit_wait(headers))

    if status == 403:
        remaining = headers.get("x-ratelimit-remaining")
        combined = f"{body}\n{stderr}".lower()
        if remaining == "0" or "retry-after" in headers or "rate limit" in combined or "secondary rate" in combined:
            raise TransientGitHubError("rate_limit", _rate_limit_wait(headers))
        raise TransientGitHubError("github")

    if status is None:
        raise TransientGitHubError("network")

    if status >= 500:
        raise TransientGitHubError("github", _positive_float(headers.get("retry-after")) or 0.0)

    if status != 200:
        raise TransientGitHubError("unexpected")

    if returncode != 0:
        raise TransientGitHubError("network")

    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise TransientGitHubError("unexpected") from exc

    return (200, headers, parsed, poll_hint)


def _fetch_signed_in_login() -> str:
    status, _, data, _ = _gh_get("user")
    if status != 200 or not isinstance(data, dict):
        raise TransientGitHubError("unexpected")
    login = data.get("login")
    if not isinstance(login, str) or not GITHUB_LOGIN_RE.fullmatch(login):
        raise TransientGitHubError("unexpected")
    return login


def _item_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MalformedItemError()
    return value


def _pos_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MalformedItemError()
    return value


def _iso_utc(value: object) -> str:
    if not isinstance(value, str) or not ISO_UTC_RE.fullmatch(value):
        raise MalformedItemError()
    return value


def _classify_actor(user_obj: object, own_login: str, include_own: bool) -> tuple[bool, str | None]:
    """Return (should_include, validated_login_or_none)."""
    if user_obj is None:
        return (True, None)
    if not isinstance(user_obj, dict):
        raise MalformedItemError()
    user_type = user_obj.get("type")
    if isinstance(user_type, str) and user_type.lower() == "bot":
        return (False, None)
    raw_login = user_obj.get("login")
    if isinstance(raw_login, str):
        if raw_login.lower().endswith("[bot]"):
            return (False, None)
        if not include_own and raw_login.lower() == own_login.lower():
            return (False, None)
        if GITHUB_LOGIN_RE.fullmatch(raw_login):
            return (True, raw_login)
        return (True, None)
    return (True, None)


def _build_event(
    event_id: str,
    kind: str,
    number: int,
    actor_login: str | None,
    timestamp: str,
    url: str,
) -> dict[str, object]:
    event: dict[str, object] = {
        "id": event_id,
        "kind": kind,
        "number": number,
    }
    if actor_login is not None:
        event["actor"] = actor_login
    event["time"] = timestamp
    event["url"] = url
    return event


def _extract_number_from_urls(primary: object, primary_re: re.Pattern[str], fallback_html: object) -> int:
    if isinstance(primary, str):
        m = primary_re.search(primary)
        if m:
            return int(m.group(1))
    if isinstance(fallback_html, str):
        m = HTML_ITEM_NUM_RE.search(fallback_html)
        if m:
            return int(m.group(1))
    raise MalformedItemError()


class _SeenTracker:
    """Order-preserving seen-id set so oldest ids are evicted first when bounded."""

    def __init__(self, initial: list[str]):
        self.order: list[str] = list(initial)
        self.members: set[str] = set(initial)

    def __contains__(self, item_id: str) -> bool:
        return item_id in self.members

    def add(self, item_id: str) -> None:
        if item_id not in self.members:
            self.members.add(item_id)
            self.order.append(item_id)

    def to_list(self) -> list[str]:
        if len(self.order) > MAX_SEEN_IDS:
            self.order = self.order[-MAX_SEEN_IDS:]
            self.members = set(self.order)
        return list(self.order)


def _set_review_etag(etags: dict[str, str], number: int, page: int, etag: str | None) -> None:
    prefix = f"reviews:{number}:"
    for k in [k for k in etags if k.startswith(prefix) or k == f"reviews:{number}"]:
        del etags[k]
    if etag:
        etags[f"reviews:{number}:p{page}"] = etag


def _review_endpoint(repo: str, number: int, page: int) -> str:
    if page <= 1:
        return f"repos/{repo}/pulls/{number}/reviews?per_page={PER_PAGE}"
    return f"repos/{repo}/pulls/{number}/reviews?per_page={PER_PAGE}&page={page}"


def _fetch_reviews_from_tail(
    repo: str,
    number: int,
    start_page: int,
    etag: str | None,
    force_recheck_on_304: bool,
) -> tuple[bool, int, str | None, list[object], float]:
    """Fetch PR reviews starting at start_page, advancing tail_page across Link rel=next and full pages."""
    ep = _review_endpoint(repo, number, start_page)
    status, headers, data, max_poll = _gh_get(ep, etag=etag)
    if status == 304 and force_recheck_on_304:
        # If /pulls reported a newer updated_at on this PR while the cached tail page answered 304
        # (for example when a page filled up at a smaller server page size and grew a Link rel=next),
        # re-fetch without If-None-Match once so a newly linked page is never missed.
        status, headers, data, p2 = _gh_get(ep, etag=None)
        max_poll = max(max_poll, p2)

    if status == 304:
        return (False, start_page, etag, [], max_poll)
    if not isinstance(data, list):
        raise TransientGitHubError("unexpected")

    curr_page = start_page
    curr_etag = headers.get("etag")
    curr_page_len = len(data)
    all_items: list[object] = list(data)
    next_ep = _next_endpoint(headers.get("link"))

    while next_ep is not None:
        p_status, p_headers, p_data, p_poll = _gh_get(next_ep, etag=None)
        max_poll = max(max_poll, p_poll)
        if p_status != 200 or not isinstance(p_data, list):
            raise TransientGitHubError("unexpected")
        m_page = re.search(r"[?&]page=(\d+)", next_ep)
        curr_page = int(m_page.group(1)) if m_page else (curr_page + 1)
        curr_etag = p_headers.get("etag")
        curr_page_len = len(p_data)
        all_items.extend(p_data)
        if not p_data:
            break
        next_ep = _next_endpoint(p_headers.get("link"))

    # When the final page reached is full (PER_PAGE items), advance to curr_page + 1 right away so
    # the next review (#101, #201, ...) lands on the conditionally polled tail page rather than an
    # already-full page whose ETag will never change.
    while curr_page_len >= PER_PAGE:
        probe_page = curr_page + 1
        p_status, p_headers, p_data, p_poll = _gh_get(_review_endpoint(repo, number, probe_page), etag=None)
        max_poll = max(max_poll, p_poll)
        if p_status != 200 or not isinstance(p_data, list):
            raise TransientGitHubError("unexpected")
        curr_page = probe_page
        curr_etag = p_headers.get("etag")
        curr_page_len = len(p_data)
        all_items.extend(p_data)
        if curr_page_len == 0:
            break

    return (True, curr_page, curr_etag, all_items, max_poll)


def _fetch_paginated_list(
    first_endpoint: str,
    etag: str | None,
    stop_item,
) -> tuple[bool, str | None, list[object], float, int]:
    """Fetch a list endpoint across Link rel=next pages until stop_item(item) is True."""
    status, headers, data, max_poll = _gh_get(first_endpoint, etag=etag)
    if status == 304:
        return (False, etag, [], max_poll, 0)
    if not isinstance(data, list):
        raise TransientGitHubError("unexpected")

    new_etag = headers.get("etag") or etag
    items: list[object] = []
    skipped = 0
    stop = False
    for raw_item in data:
        if not isinstance(raw_item, dict):
            skipped += 1
            continue
        if stop_item is not None:
            try:
                if stop_item(raw_item):
                    stop = True
                    break
            except MalformedItemError:
                skipped += 1
                continue
        items.append(raw_item)

    next_ep = None if stop else _next_endpoint(headers.get("link"))
    while next_ep is not None:
        p_status, p_headers, p_data, p_poll = _gh_get(next_ep, etag=None)
        max_poll = max(max_poll, p_poll)
        if p_status != 200 or not isinstance(p_data, list):
            raise TransientGitHubError("unexpected")
        for raw_item in p_data:
            if not isinstance(raw_item, dict):
                skipped += 1
                continue
            if stop_item is not None:
                try:
                    if stop_item(raw_item):
                        stop = True
                        break
                except MalformedItemError:
                    skipped += 1
                    continue
            items.append(raw_item)
        if stop or not p_data:
            break
        next_ep = _next_endpoint(p_headers.get("link"))

    return (True, new_etag, items, max_poll, skipped)


def _baseline_single_page(first_endpoint: str) -> tuple[str | None, list[object], float]:
    status, headers, data, poll_hint = _gh_get(first_endpoint, etag=None)
    if status != 200 or not isinstance(data, list):
        raise TransientGitHubError("unexpected")
    return (headers.get("etag"), data, poll_hint)


def _report_skipped_items(count: int) -> None:
    if count > 0:
        noun = "item" if count == 1 else "items"
        sys.stderr.write(f"Skipped {count} malformed GitHub {noun}.\n")
        sys.stderr.flush()


def _initialize_baseline(
    state: dict[str, object],
    repo: str,
) -> tuple[dict[str, object], float, int]:
    """Snapshot existing repository state on first run so nothing older than first start is reported."""
    working = copy.deepcopy(state)
    etags: dict[str, str] = working["etags"]  # type: ignore[assignment]
    wm: dict[str, object] = working["watermarks"]  # type: ignore[assignment]
    seen = _SeenTracker(list(working["seen_ids"]))  # type: ignore[arg-type]
    known_prs: set[int] = set(working["known_pr_numbers"])  # type: ignore[arg-type]
    pull_heads: dict[str, str] = working["pull_heads"]  # type: ignore[assignment]
    pull_draft: dict[str, bool] = working["pull_draft"]  # type: ignore[assignment]
    pull_base_upd: dict[str, str] = working["pull_baseline_updated_at"]  # type: ignore[assignment]
    pull_rev_checked: dict[str, str] = working["pull_reviews_checked"]  # type: ignore[assignment]
    pull_rev_page: dict[str, int] = working["pull_review_page"]  # type: ignore[assignment]

    server_poll = 0.0
    skipped = 0
    baseline_cutoff = ""

    def note_time(ts: str) -> None:
        nonlocal baseline_cutoff
        if ts > baseline_cutoff:
            baseline_cutoff = ts

    # 1. Pull requests (all pages at initial setup so every existing PR head, draft state, and id is known)
    _, p_etag, pull_items, hint, p_skip = _fetch_paginated_list(
        f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page={PER_PAGE}",
        etag=None,
        stop_item=None,
    )
    server_poll = max(server_poll, hint)
    skipped += p_skip
    if p_etag:
        etags["pulls"] = p_etag

    open_prs: list[int] = []
    max_pull_id = 0
    max_pull_upd = ""
    for raw_pr in pull_items:
        try:
            pr = _item_dict(raw_pr)
            pr_id = _pos_int(pr.get("id"))
            number = _pos_int(pr.get("number"))
            created_at = _iso_utc(pr.get("created_at"))
            updated_at = _iso_utc(pr.get("updated_at"))
            head = _item_dict(pr.get("head"))
            raw_sha = head.get("sha")
            if not isinstance(raw_sha, str) or not HEX_SHA_RE.fullmatch(raw_sha.lower()):
                raise MalformedItemError()
            head_sha = raw_sha.lower()
            is_draft = bool(pr.get("draft", False))
        except MalformedItemError:
            skipped += 1
            continue

        note_time(created_at)
        note_time(updated_at)
        known_prs.add(number)
        num_key = str(number)
        pull_heads[num_key] = head_sha
        pull_draft[num_key] = is_draft
        pull_base_upd[num_key] = updated_at
        pull_rev_checked[num_key] = updated_at
        seen.add(f"pull_request_opened:{pr_id}")
        if pr_id > max_pull_id:
            max_pull_id = pr_id
        if updated_at > max_pull_upd:
            max_pull_upd = updated_at
        if pr.get("state") == "open" and number not in open_prs:
            open_prs.append(number)

    wm["max_pull_id"] = max_pull_id
    wm["max_pull_updated_at"] = max_pull_upd
    working["open_pr_numbers"] = open_prs[:MAX_TRACKED_PULLS]
    polled_open_prs = open_prs[:MAX_OPEN_PR_REVIEWS_POLLED]

    # 2. Issues (page 1 sorted by created desc gives max_issue_id)
    i_etag, issue_items, hint = _baseline_single_page(
        f"repos/{repo}/issues?state=all&sort=created&direction=desc&per_page={PER_PAGE}"
    )
    server_poll = max(server_poll, hint)
    if i_etag:
        etags["issues"] = i_etag
    max_issue_id = 0
    for raw_item in issue_items:
        try:
            item = _item_dict(raw_item)
            iid = _pos_int(item.get("id"))
            num = _pos_int(item.get("number"))
            created = _iso_utc(item.get("created_at"))
            updated = item.get("updated_at")
            if isinstance(updated, str) and ISO_UTC_RE.fullmatch(updated):
                note_time(updated)
        except MalformedItemError:
            skipped += 1
            continue
        note_time(created)
        if iid > max_issue_id:
            max_issue_id = iid
        if "pull_request" in item and item["pull_request"] is not None:
            known_prs.add(num)
        else:
            seen.add(f"issue_opened:{iid}")
    wm["max_issue_id"] = max_issue_id

    # 3. Issue events (page 1 gives max_issue_event_id)
    ie_etag, ie_items, hint = _baseline_single_page(f"repos/{repo}/issues/events?per_page={PER_PAGE}")
    server_poll = max(server_poll, hint)
    if ie_etag:
        etags["issue_events"] = ie_etag
    max_ev_id = 0
    for raw_item in ie_items:
        try:
            item = _item_dict(raw_item)
            eid = _pos_int(item.get("id"))
            created = _iso_utc(item.get("created_at"))
            if item.get("event") == "reopened":
                issue_obj = _item_dict(item.get("issue"))
                num = _pos_int(issue_obj.get("number"))
                is_pr = ("pull_request" in issue_obj and issue_obj["pull_request"] is not None) or (num in known_prs)
                kind = "pull_request_reopened" if is_pr else "issue_reopened"
                seen.add(f"{kind}:{eid}")
        except MalformedItemError:
            skipped += 1
            continue
        note_time(created)
        if eid > max_ev_id:
            max_ev_id = eid
    wm["max_issue_event_id"] = max_ev_id

    # 4. Issue & PR comments (page 1 sorted by created desc gives max_issue_comment_id)
    ic_etag, ic_items, hint = _baseline_single_page(
        f"repos/{repo}/issues/comments?sort=created&direction=desc&per_page={PER_PAGE}"
    )
    server_poll = max(server_poll, hint)
    if ic_etag:
        etags["issue_comments"] = ic_etag
    max_ic_id = 0
    for raw_item in ic_items:
        try:
            item = _item_dict(raw_item)
            cid = _pos_int(item.get("id"))
            created = _iso_utc(item.get("created_at"))
            html_url = item.get("html_url")
            num = _extract_number_from_urls(item.get("issue_url"), ISSUE_URL_NUM_RE, html_url)
        except MalformedItemError:
            skipped += 1
            continue
        note_time(created)
        if cid > max_ic_id:
            max_ic_id = cid
        html_pr = isinstance(html_url, str) and bool(re.search(r"/pull/\d+(?:$|[#/?])", html_url))
        is_pr = (num in known_prs) or html_pr or ("pull_request_url" in item)
        kind = "pull_request_comment" if is_pr else "issue_comment"
        seen.add(f"{kind}:{cid}")
    wm["max_issue_comment_id"] = max_ic_id

    # 5. Review comments (page 1 sorted by created desc gives max_review_comment_id)
    rc_etag, rc_items, hint = _baseline_single_page(
        f"repos/{repo}/pulls/comments?sort=created&direction=desc&per_page={PER_PAGE}"
    )
    server_poll = max(server_poll, hint)
    if rc_etag:
        etags["pull_comments"] = rc_etag
    max_rc_id = 0
    for raw_item in rc_items:
        try:
            item = _item_dict(raw_item)
            cid = _pos_int(item.get("id"))
            created = _iso_utc(item.get("created_at"))
        except MalformedItemError:
            skipped += 1
            continue
        note_time(created)
        if cid > max_rc_id:
            max_rc_id = cid
        seen.add(f"pull_request_review_comment:{cid}")
    wm["max_review_comment_id"] = max_rc_id

    # 6. Existing reviews on the polled open pull requests (walking to the non-full tail page)
    reviews_baselined: list[int] = []
    for num in polled_open_prs:
        _, tail_page, r_etag, r_items, hint = _fetch_reviews_from_tail(
            repo, num, start_page=1, etag=None, force_recheck_on_304=False
        )
        server_poll = max(server_poll, hint)
        pull_rev_page[str(num)] = tail_page
        _set_review_etag(etags, num, tail_page, r_etag)
        reviews_baselined.append(num)
        for raw_rev in r_items:
            try:
                rev = _item_dict(raw_rev)
                rev_state = rev.get("state")
                if isinstance(rev_state, str) and rev_state.upper() == "PENDING":
                    continue
                rid = _pos_int(rev.get("id"))
                sub = _iso_utc(rev.get("submitted_at") or rev.get("created_at"))
            except MalformedItemError:
                skipped += 1
                continue
            note_time(sub)
            seen.add(f"pull_request_review:{rid}")

    wm["baseline_cutoff"] = baseline_cutoff
    working["initialized"] = True
    working["seen_ids"] = seen.to_list()
    working["known_pr_numbers"] = sorted(known_prs)
    working["reviews_baselined"] = reviews_baselined
    _bound_state(working)
    return (working, server_poll, skipped)


def _poll_once(
    state: dict[str, object],
    repo: str,
    own_login: str,
    include_own: bool,
) -> tuple[dict[str, object], float, int]:
    """Poll GitHub on a copy of an initialized state; return (updated_state, max_poll_hint, skipped_count)."""
    working = copy.deepcopy(state)
    etags: dict[str, str] = working["etags"]  # type: ignore[assignment]
    wm: dict[str, object] = working["watermarks"]  # type: ignore[assignment]
    seen = _SeenTracker(list(working["seen_ids"]))  # type: ignore[arg-type]
    pending_list: list[dict[str, object]] = working["pending"]  # type: ignore[assignment]
    pending_ids: set[str] = {str(e["id"]) for e in pending_list}
    known_prs: set[int] = set(working["known_pr_numbers"])  # type: ignore[arg-type]
    open_prs: list[int] = list(working["open_pr_numbers"])  # type: ignore[arg-type]
    reviews_baselined: set[int] = set(working["reviews_baselined"])  # type: ignore[arg-type]
    pull_heads: dict[str, str] = working["pull_heads"]  # type: ignore[assignment]
    pull_seq: dict[str, int] = working["pull_seq"]  # type: ignore[assignment]
    pull_draft: dict[str, bool] = working["pull_draft"]  # type: ignore[assignment]
    pull_ready_seq: dict[str, int] = working["pull_ready_seq"]  # type: ignore[assignment]
    pull_base_upd: dict[str, str] = working["pull_baseline_updated_at"]  # type: ignore[assignment]
    pull_rev_checked: dict[str, str] = working["pull_reviews_checked"]  # type: ignore[assignment]
    pull_rev_page: dict[str, int] = working["pull_review_page"]  # type: ignore[assignment]

    baseline_cutoff = str(wm.get("baseline_cutoff", ""))
    discovered: list[dict[str, object]] = []
    server_poll = 0.0
    skipped = 0
    ready_emitted_prs: set[int] = set()

    def record_event(ev: dict[str, object]) -> None:
        ev_id = str(ev["id"])
        ev_time = str(ev["time"])
        if baseline_cutoff and ev_time < baseline_cutoff:
            seen.add(ev_id)
            return
        if ev_id in seen or ev_id in pending_ids:
            return
        pending_ids.add(ev_id)
        discovered.append(ev)

    # 1. Pull requests (opened, draft->ready, head commit moves, and which PRs need review checks)
    prev_max_pull_id = int(wm["max_pull_id"])  # type: ignore[arg-type]
    prev_max_pull_upd = str(wm["max_pull_updated_at"])
    pulls_ep = f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page={PER_PAGE}"

    def stop_pulls(item: dict[str, object]) -> bool:
        if not prev_max_pull_upd:
            return False
        return _iso_utc(item.get("updated_at")) < prev_max_pull_upd

    changed, new_etag, pull_items, poll_hint, p_skip = _fetch_paginated_list(
        pulls_ep,
        etags.get("pulls"),
        stop_pulls,
    )
    server_poll = max(server_poll, poll_hint)
    skipped += p_skip
    if new_etag:
        etags["pulls"] = new_etag

    prs_with_updated_timestamp: set[int] = set()
    prs_to_check_reviews: list[int] = []
    new_max_pull_id = prev_max_pull_id
    new_max_pull_upd = prev_max_pull_upd
    valid_pulls_seen: dict[int, str] = {}

    if changed:
        current_open_seen: list[int] = []
        closed_this_poll: set[int] = set()
        for raw_pr in pull_items:
            try:
                pr = _item_dict(raw_pr)
                pr_id = _pos_int(pr.get("id"))
                number = _pos_int(pr.get("number"))
                created_at = _iso_utc(pr.get("created_at"))
                updated_at = _iso_utc(pr.get("updated_at"))
                head = _item_dict(pr.get("head"))
                raw_sha = head.get("sha")
                if not isinstance(raw_sha, str) or not HEX_SHA_RE.fullmatch(raw_sha.lower()):
                    raise MalformedItemError()
                head_sha = raw_sha.lower()
                is_draft = bool(pr.get("draft", False))
                keep, actor = _classify_actor(pr.get("user"), own_login, include_own)
            except MalformedItemError:
                skipped += 1
                continue

            valid_pulls_seen[number] = updated_at
            known_prs.add(number)
            if pr.get("state") == "open":
                if number not in current_open_seen:
                    current_open_seen.append(number)
            else:
                closed_this_poll.add(number)

            if pr_id > new_max_pull_id:
                new_max_pull_id = pr_id
            if updated_at > new_max_pull_upd:
                new_max_pull_upd = updated_at

            num_key = str(number)
            prev_sha = pull_heads.get(num_key)
            prev_draft = pull_draft.get(num_key)

            if prev_sha is None and pr_id > prev_max_pull_id:
                ev_id = f"pull_request_opened:{pr_id}"
                if keep:
                    record_event(
                        _build_event(
                            ev_id,
                            "pull_request_opened",
                            number,
                            actor,
                            created_at,
                            f"https://github.com/{repo}/pull/{number}",
                        )
                    )
                else:
                    seen.add(ev_id)
                # Brand-new PR created after baseline has no pre-baseline reviews.
                reviews_baselined.add(number)
            else:
                if prev_sha is not None and prev_sha != head_sha:
                    seq = pull_seq.get(num_key, 0) + 1
                    pull_seq[num_key] = seq
                    move_id = f"pull_request_commits:{pr_id}:{seq}:{prev_sha}:{head_sha}"
                    if keep:
                        record_event(
                            _build_event(
                                move_id,
                                "pull_request_commits",
                                number,
                                actor,
                                updated_at,
                                f"https://github.com/{repo}/pull/{number}",
                            )
                        )
                    else:
                        seen.add(move_id)
                if prev_draft is True and not is_draft:
                    rseq = pull_ready_seq.get(num_key, 0) + 1
                    pull_ready_seq[num_key] = rseq
                    ready_id = f"pull_request_ready:{pr_id}:{rseq}"
                    ready_emitted_prs.add(number)
                    if keep:
                        record_event(
                            _build_event(
                                ready_id,
                                "pull_request_ready",
                                number,
                                actor,
                                updated_at,
                                f"https://github.com/{repo}/pull/{number}",
                            )
                        )
                    else:
                        seen.add(ready_id)

            pull_heads[num_key] = head_sha
            pull_draft[num_key] = is_draft

            if pull_rev_checked.get(num_key) != updated_at:
                prs_with_updated_timestamp.add(number)
                if number not in prs_to_check_reviews:
                    prs_to_check_reviews.append(number)

        merged_open = list(current_open_seen)
        for existing_open in open_prs:
            if existing_open not in merged_open and existing_open not in closed_this_poll:
                merged_open.append(existing_open)
        open_prs = merged_open[:MAX_TRACKED_PULLS]

    for open_num in open_prs[:MAX_OPEN_PR_REVIEWS_POLLED]:
        if open_num not in prs_to_check_reviews:
            prs_to_check_reviews.append(open_num)

    wm["max_pull_id"] = new_max_pull_id
    wm["max_pull_updated_at"] = new_max_pull_upd
    working["open_pr_numbers"] = open_prs

    # 2. Issues (issue_opened and PR number discovery)
    prev_max_issue_id = int(wm["max_issue_id"])  # type: ignore[arg-type]
    issues_ep = f"repos/{repo}/issues?state=all&sort=created&direction=desc&per_page={PER_PAGE}"

    def stop_issues(item: dict[str, object]) -> bool:
        return prev_max_issue_id > 0 and _pos_int(item.get("id")) <= prev_max_issue_id

    i_changed, i_etag, issue_items, poll_hint, i_skip = _fetch_paginated_list(
        issues_ep,
        etags.get("issues"),
        stop_issues,
    )
    server_poll = max(server_poll, poll_hint)
    skipped += i_skip
    if i_etag:
        etags["issues"] = i_etag
    if i_changed:
        new_max_issue_id = prev_max_issue_id
        for raw_item in issue_items:
            try:
                item = _item_dict(raw_item)
                item_id = _pos_int(item.get("id"))
                number = _pos_int(item.get("number"))
                created_at = _iso_utc(item.get("created_at"))
                is_pr_item = "pull_request" in item and item["pull_request"] is not None
                keep, actor = (False, None) if is_pr_item else _classify_actor(item.get("user"), own_login, include_own)
            except MalformedItemError:
                skipped += 1
                continue
            if item_id > new_max_issue_id:
                new_max_issue_id = item_id
            if is_pr_item:
                known_prs.add(number)
                continue
            ev_id = f"issue_opened:{item_id}"
            if keep:
                record_event(
                    _build_event(
                        ev_id,
                        "issue_opened",
                        number,
                        actor,
                        created_at,
                        f"https://github.com/{repo}/issues/{number}",
                    )
                )
            else:
                seen.add(ev_id)
        wm["max_issue_id"] = new_max_issue_id

    # 3. Issue and pull request reopened (and fallback ready_for_review) events
    prev_max_ev_id = int(wm["max_issue_event_id"])  # type: ignore[arg-type]
    ie_ep = f"repos/{repo}/issues/events?per_page={PER_PAGE}"

    def stop_ie(item: dict[str, object]) -> bool:
        return prev_max_ev_id > 0 and _pos_int(item.get("id")) <= prev_max_ev_id

    ie_changed, ie_etag, ie_items, poll_hint, ie_skip = _fetch_paginated_list(
        ie_ep,
        etags.get("issue_events"),
        stop_ie,
    )
    server_poll = max(server_poll, poll_hint)
    skipped += ie_skip
    if ie_etag:
        etags["issue_events"] = ie_etag
    if ie_changed:
        new_max_ev_id = prev_max_ev_id
        for raw_item in ie_items:
            try:
                item = _item_dict(raw_item)
                ev_num_id = _pos_int(item.get("id"))
                created_at = _iso_utc(item.get("created_at"))
                ev_name = item.get("event")
                if ev_name not in ("reopened", "ready_for_review"):
                    if ev_num_id > new_max_ev_id:
                        new_max_ev_id = ev_num_id
                    continue
                issue_obj = _item_dict(item.get("issue"))
                number = _pos_int(issue_obj.get("number"))
                actor_obj = item.get("actor") if "actor" in item else item.get("user")
                keep, actor = _classify_actor(actor_obj, own_login, include_own)
            except MalformedItemError:
                skipped += 1
                continue

            if ev_num_id > new_max_ev_id:
                new_max_ev_id = ev_num_id

            if ev_name == "ready_for_review":
                known_prs.add(number)
                num_key = str(number)
                if number in ready_emitted_prs or pull_draft.get(num_key) is False:
                    continue
                rseq = pull_ready_seq.get(num_key, 0) + 1
                pull_ready_seq[num_key] = rseq
                pull_draft[num_key] = False
                ready_emitted_prs.add(number)
                ev_id = f"pull_request_ready:ev{ev_num_id}:{rseq}"
                url = f"https://github.com/{repo}/pull/{number}"
                if keep:
                    record_event(_build_event(ev_id, "pull_request_ready", number, actor, created_at, url))
                else:
                    seen.add(ev_id)
                continue

            is_pr = ("pull_request" in issue_obj and issue_obj["pull_request"] is not None) or (number in known_prs)
            if is_pr:
                known_prs.add(number)
                kind = "pull_request_reopened"
                url = f"https://github.com/{repo}/pull/{number}"
            else:
                kind = "issue_reopened"
                url = f"https://github.com/{repo}/issues/{number}"
            ev_id = f"{kind}:{ev_num_id}"
            if keep:
                record_event(_build_event(ev_id, kind, number, actor, created_at, url))
            else:
                seen.add(ev_id)
        wm["max_issue_event_id"] = new_max_ev_id

    # 4. Comments on issues and pull requests
    prev_max_ic_id = int(wm["max_issue_comment_id"])  # type: ignore[arg-type]
    ic_ep = f"repos/{repo}/issues/comments?sort=created&direction=desc&per_page={PER_PAGE}"

    def stop_ic(item: dict[str, object]) -> bool:
        return prev_max_ic_id > 0 and _pos_int(item.get("id")) <= prev_max_ic_id

    ic_changed, ic_etag, ic_items, poll_hint, ic_skip = _fetch_paginated_list(
        ic_ep,
        etags.get("issue_comments"),
        stop_ic,
    )
    server_poll = max(server_poll, poll_hint)
    skipped += ic_skip
    if ic_etag:
        etags["issue_comments"] = ic_etag
    if ic_changed:
        new_max_ic_id = prev_max_ic_id
        for raw_item in ic_items:
            try:
                item = _item_dict(raw_item)
                cid = _pos_int(item.get("id"))
                created_at = _iso_utc(item.get("created_at"))
                html_url = item.get("html_url")
                number = _extract_number_from_urls(item.get("issue_url"), ISSUE_URL_NUM_RE, html_url)
                keep, actor = _classify_actor(item.get("user"), own_login, include_own)
            except MalformedItemError:
                skipped += 1
                continue
            if cid > new_max_ic_id:
                new_max_ic_id = cid
            html_indicates_pr = isinstance(html_url, str) and bool(re.search(r"/pull/\d+(?:$|[#/?])", html_url))
            is_pr = (number in known_prs) or html_indicates_pr or ("pull_request_url" in item)
            if is_pr:
                known_prs.add(number)
                kind = "pull_request_comment"
                url = f"https://github.com/{repo}/pull/{number}#issuecomment-{cid}"
            else:
                kind = "issue_comment"
                url = f"https://github.com/{repo}/issues/{number}#issuecomment-{cid}"
            ev_id = f"{kind}:{cid}"
            if keep:
                record_event(_build_event(ev_id, kind, number, actor, created_at, url))
            else:
                seen.add(ev_id)
        wm["max_issue_comment_id"] = new_max_ic_id

    # 5. Pull request review comments (inline comments in a review)
    prev_max_rc_id = int(wm["max_review_comment_id"])  # type: ignore[arg-type]
    rc_ep = f"repos/{repo}/pulls/comments?sort=created&direction=desc&per_page={PER_PAGE}"

    def stop_rc(item: dict[str, object]) -> bool:
        return prev_max_rc_id > 0 and _pos_int(item.get("id")) <= prev_max_rc_id

    rc_changed, rc_etag, rc_items, poll_hint, rc_skip = _fetch_paginated_list(
        rc_ep,
        etags.get("pull_comments"),
        stop_rc,
    )
    server_poll = max(server_poll, poll_hint)
    skipped += rc_skip
    if rc_etag:
        etags["pull_comments"] = rc_etag
    if rc_changed:
        new_max_rc_id = prev_max_rc_id
        for raw_item in rc_items:
            try:
                item = _item_dict(raw_item)
                cid = _pos_int(item.get("id"))
                created_at = _iso_utc(item.get("created_at"))
                number = _extract_number_from_urls(item.get("pull_request_url"), PULL_URL_NUM_RE, item.get("html_url"))
                keep, actor = _classify_actor(item.get("user"), own_login, include_own)
            except MalformedItemError:
                skipped += 1
                continue
            if cid > new_max_rc_id:
                new_max_rc_id = cid
            known_prs.add(number)
            ev_id = f"pull_request_review_comment:{cid}"
            url = f"https://github.com/{repo}/pull/{number}#discussion_r{cid}"
            if keep:
                record_event(_build_event(ev_id, "pull_request_review_comment", number, actor, created_at, url))
            else:
                seen.add(ev_id)
        wm["max_review_comment_id"] = new_max_rc_id

    # 6. Reviews on open or updated pull requests (tail-page conditional polling + pre-baseline guard)
    for number in prs_to_check_reviews:
        num_key = str(number)
        start_page = max(1, pull_rev_page.get(num_key, 1))
        cached_etag = etags.get(f"reviews:{number}:p{start_page}") or (
            etags.get(f"reviews:{number}") if start_page == 1 else None
        )
        r_changed, tail_page, r_etag, r_items, poll_hint = _fetch_reviews_from_tail(
            repo,
            number,
            start_page=start_page,
            etag=cached_etag,
            force_recheck_on_304=(number in prs_with_updated_timestamp),
        )
        server_poll = max(server_poll, poll_hint)
        pull_rev_page[num_key] = tail_page
        _set_review_etag(etags, number, tail_page, r_etag)

        # Reviews on any PR whose reviews were NOT fetched at baseline (e.g. a PR that was closed at
        # baseline and later reopened, or open PR #26+ entering the top 25) must be strictly newer
        # than the pre-baseline cutoff so pre-start reviews are never reported as new.
        pr_cutoff = max(pull_base_upd.get(num_key, ""), baseline_cutoff)
        was_baselined = number in reviews_baselined

        if r_changed:
            for raw_rev in r_items:
                try:
                    rev = _item_dict(raw_rev)
                    rev_state = rev.get("state")
                    if isinstance(rev_state, str) and rev_state.upper() == "PENDING":
                        continue
                    rid = _pos_int(rev.get("id"))
                    submitted_at = _iso_utc(rev.get("submitted_at") or rev.get("created_at"))
                    keep, actor = _classify_actor(rev.get("user"), own_login, include_own)
                except MalformedItemError:
                    skipped += 1
                    continue

                ev_id = f"pull_request_review:{rid}"
                if not was_baselined and pr_cutoff and submitted_at <= pr_cutoff:
                    seen.add(ev_id)
                    continue
                if keep:
                    record_event(
                        _build_event(
                            ev_id,
                            "pull_request_review",
                            number,
                            actor,
                            submitted_at,
                            f"https://github.com/{repo}/pull/{number}#pullrequestreview-{rid}",
                        )
                    )
                else:
                    seen.add(ev_id)
            reviews_baselined.add(number)

        if number in valid_pulls_seen:
            pull_rev_checked[num_key] = valid_pulls_seen[number]

    if discovered:
        discovered.sort(key=lambda e: (str(e["time"]), str(e["id"])))
        pending_list.extend(discovered)
        pending_list.sort(key=lambda e: (str(e["time"]), str(e["id"])))

    working["seen_ids"] = seen.to_list()
    working["known_pr_numbers"] = sorted(known_prs)
    working["reviews_baselined"] = sorted(reviews_baselined)
    _bound_state(working)
    return (working, server_poll, skipped)


def _failure_message(kind: str, pause: float) -> str:
    secs = f"{pause:g}"
    if kind == "rate_limit":
        return f"GitHub rate limit reached; retrying in {secs}s.\n"
    if kind == "network":
        return f"Network error talking to GitHub; retrying in {secs}s.\n"
    if kind == "github":
        return f"GitHub server error; retrying in {secs}s.\n"
    return f"Unexpected answer from GitHub; retrying in {secs}s.\n"


def _save_state_before_print(state_path: Path, state: dict[str, object]) -> None:
    try:
        _save_state(state_path, state)
    except OSError as exc:
        raise StateWriteError("Failed to write state file.") from exc


def _emit_and_save(state_path: Path, state: dict[str, object], max_events: int) -> int:
    pending: list[dict[str, object]] = list(state["pending"])  # type: ignore[arg-type]
    batch = pending[:max_events]
    rest = pending[max_events:]

    next_state = copy.deepcopy(state)
    seen = _SeenTracker(list(next_state["seen_ids"]))  # type: ignore[arg-type]
    for ev in batch:
        seen.add(str(ev["id"]))
    next_state["seen_ids"] = seen.to_list()
    next_state["pending"] = rest
    _bound_state(next_state)

    # Print and flush BEFORE replacing the state file so a crash can repeat an event, never drop it.
    sys.stdout.write(json.dumps({"events": batch}, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    try:
        _save_state(state_path, next_state)
    except Exception as exc:
        raise SaveAfterPrintError() from exc
    return EXIT_OK


def _emit_timeout() -> int:
    sys.stdout.write(json.dumps({"events": [], "reason": "timeout"}, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    lock_fd: int | None = None
    try:
        ns = parse_args(argv)
        _load_state(ns.state_path)
        lock_fd = _acquire_state_lock_and_verify_writable(ns.state_path)
        state = _load_state(ns.state_path)

        started = clock()
        deadline = (started + ns.max_wait) if ns.max_wait is not None else None
        own_login: str | None = None
        consecutive_failures = 0
        last_failure_pause = 0.0

        def wait_failure(err: TransientGitHubError, respect_deadline: bool = True) -> bool:
            """Log one safe line, wait growing pause, and return False when deadline has expired."""
            nonlocal consecutive_failures, last_failure_pause
            consecutive_failures += 1
            growing = min(MAX_BACKOFF, BASE_BACKOFF * (2 ** (consecutive_failures - 1)))
            pause = max(growing, err.min_wait)
            if consecutive_failures > 1 and pause <= last_failure_pause:
                pause = max(err.min_wait, min(MAX_BACKOFF, last_failure_pause * 2.0))
            last_failure_pause = pause
            sys.stderr.write(_failure_message(err.kind, pause))
            sys.stderr.flush()
            if respect_deadline and deadline is not None:
                remaining = deadline - clock()
                if remaining <= 0:
                    return False
                if pause >= remaining:
                    sleep(remaining)
                    return False
            sleep(pause)
            return not respect_deadline or deadline is None or clock() < deadline

        # 1. Ask gh once at the start which account it is signed in as.
        while own_login is None:
            try:
                own_login = _fetch_signed_in_login()
                consecutive_failures = 0
                last_failure_pause = 0.0
            except TransientGitHubError as err:
                if not wait_failure(err):
                    return _emit_timeout()

        # 2. If no state file existed yet, snapshot the baseline once and persist it before waiting.
        while not bool(state["initialized"]):
            try:
                state, server_poll, skipped = _initialize_baseline(state, ns.repo)
                _report_skipped_items(skipped)
                _save_state_before_print(ns.state_path, state)
                consecutive_failures = 0
                last_failure_pause = 0.0
            except TransientGitHubError as err:
                if not wait_failure(err):
                    return _emit_timeout()
            else:
                wait_secs = max(POLL_INTERVAL, server_poll)
                if deadline is not None:
                    remaining = deadline - clock()
                    if remaining <= 0:
                        return _emit_timeout()
                    if wait_secs >= remaining:
                        sleep(remaining)
                        return _emit_timeout()
                sleep(wait_secs)

        # 3. Main polling loop until at least one outside event is ready (or left over from before).
        had_preexisting_pending = bool(state["pending"])
        while True:
            prev_pending_count = len(state["pending"])  # type: ignore[arg-type]
            try:
                state, server_poll, skipped = _poll_once(state, ns.repo, own_login, ns.include_own)
                _report_skipped_items(skipped)
                consecutive_failures = 0
                last_failure_pause = 0.0
            except TransientGitHubError as err:
                if state["pending"]:
                    if not wait_failure(err):
                        return _emit_and_save(ns.state_path, state, ns.max_events)
                    continue
                if not wait_failure(err):
                    return _emit_timeout()
                continue

            current_pending_count = len(state["pending"])  # type: ignore[arg-type]
            if current_pending_count > 0:
                if had_preexisting_pending and current_pending_count == prev_pending_count:
                    return _emit_and_save(ns.state_path, state, ns.max_events)

                settle_wait = max(ns.settle, server_poll) if ns.settle > 0 or server_poll > 0 else 0.0
                if settle_wait > 0:
                    sleep(settle_wait)
                    while True:
                        try:
                            state, _, skipped_after = _poll_once(state, ns.repo, own_login, ns.include_own)
                            _report_skipped_items(skipped_after)
                            consecutive_failures = 0
                            last_failure_pause = 0.0
                            break
                        except TransientGitHubError as err:
                            if not wait_failure(err):
                                break
                return _emit_and_save(ns.state_path, state, ns.max_events)

            wait_secs = max(POLL_INTERVAL, server_poll)
            if deadline is not None:
                remaining = deadline - clock()
                if remaining <= 0:
                    _save_state_before_print(ns.state_path, state)
                    return _emit_timeout()
                if wait_secs >= remaining:
                    _save_state_before_print(ns.state_path, state)
                    sleep(remaining)
                    return _emit_timeout()
            sleep(wait_secs)

    except HelpRequested:
        sys.stdout.write(HELP_TEXT)
        sys.stdout.flush()
        return EXIT_OK
    except BadArgsError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return EXIT_BAD_ARGS
    except StateReadError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return EXIT_STATE_READ
    except StateWriteError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return EXIT_STATE_WRITE
    except StateLockedError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return EXIT_STATE_LOCKED
    except SaveAfterPrintError:
        sys.stderr.write("Events were printed, but saving the state file failed.\n")
        sys.stderr.flush()
        return EXIT_SAVE_AFTER_PRINT
    except SignedOutError:
        sys.stderr.write("gh is not signed in; run 'gh auth login' first.\n")
        sys.stderr.flush()
        return EXIT_SIGNED_OUT
    except KeyboardInterrupt:
        sys.stderr.write("Watcher stopped.\n")
        sys.stderr.flush()
        return EXIT_INTERRUPTED
    except Exception:
        sys.stderr.write("Watcher stopped because of an unexpected internal error.\n")
        sys.stderr.flush()
        return EXIT_INTERNAL
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
