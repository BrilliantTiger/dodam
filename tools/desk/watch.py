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
  8    A non-evictable state capacity bound (tracked pull requests or state bytes) was reached.
  130  Interrupted by signal (SIGINT).
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
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
STATE_VERSION = 7
LEGACY_STATE_KEYS = frozenset({"baseline_cutoff", "pull_max_review_id", "pull_latest_review_ids"})

# Explicit upper bounds on state. Evictable caches/windows drop only entries that monotonic
# watermarks or narrowed floors cover; past MAX_WINDOW_REVIEW_IDS on one pull request the watcher
# keeps the newest numbers, narrows that pull request's window, logs once on stderr, and continues.
# Non-evictable bounds (MAX_TRACKED_PULLS, MAX_STATE_BYTES) end with exit code 8 instead of
# forgetting per-pull-request history, and MAX_PENDING_EVENTS pauses polling without advancing
# watermarks past unqueued events.
REVIEW_LAG_WINDOW_SECONDS = 300
MAX_OPEN_PR_REVIEWS_POLLED = 25
MAX_SEEN_IDS = 5_000
MAX_TRACKED_PULLS = 10_000
MAX_WINDOW_REVIEW_IDS = 1_000
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
EXIT_STATE_FULL = 8
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
  - Every event carries the numeric GitHub user id of the account that caused it
    as a whole number under actor_id next to actor, or 0 when GitHub gives no
    such id (for example, a deleted account). For pull_request_commits ("new
    commits"), actor and actor_id (and the outside/own/bot check) come from the
    pull request's author, not the account that pushed the commits.
  - One idle round makes one conditional GET per watched list (five repository lists
    plus one review tail page per open pull request — seven requests with two open
    pull requests, taking about 19 seconds including the 15-second pause) and uses
    zero counted primary rate-limit requests when nothing changed.
  - When the waiting queue reaches MAX_PENDING_EVENTS (1,000), the watcher reports
    what is queued and asks GitHub for nothing more until there is room, without
    moving any watermark past unqueued events.
  - Reviews sent up to 5 minutes before the latest review time seen on a pull request
    remain candidates (remembered up to MAX_WINDOW_REVIEW_IDS = 1,000 per pull request;
    past that bound the watcher keeps the newest numbers, narrows that pull request's
    window, logs once on standard error, and goes on, so a review that first appears
    late among the forgotten ones is not reported).
    Restarting from page 1 of reviews on a pull request with more than 100 reviews
    relies on GitHub marking a pull request as updated when a review is sent.

Exit codes:
  0    Normal exit: printed one JSON object (or --help text) and saved state.
  1    Unexpected internal error before printing any events.
  2    gh is not signed in, missing, or rejected with HTTP 401.
  3    Invalid command-line arguments or disallowed --state path.
  4    Existing state file or its folder cannot be read, is damaged, or is from an older version.
  5    State location cannot be written (checked before calling GitHub).
  6    Another watcher is already holding the lock on this --state file.
  7    Events JSON was printed on standard output, then saving state failed.
  8    A non-evictable state capacity bound (tracked pull requests or state bytes) was reached.
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


class StateFullError(Exception):
    """Raised when a non-evictable state capacity bound is exceeded (exit 8)."""


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

    # 1. Lexical case-insensitive check for .dodam anywhere in expanded or resolved path
    #    (stops paths inside ~/.dodam even when ~/.dodam does not exist yet on disk).
    for candidate in (abs_path, real_path):
        for part in candidate.parts:
            if part.casefold() == ".dodam":
                raise BadArgsError("The state file must never be inside .dodam.")

    # 2. Filesystem identity check (st_dev, st_ino) on existing ancestors without opening .dodam.
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
        # How many looks at GitHub the watcher has made: the first part of the one order of arrivals.
        "poll_seq": 0,
        "etags": {},
        "watermarks": {
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
        "review_window_warned_prs": [],
        "pull_heads": {},
        "pull_seq": {},
        "pull_draft": {},
        "pull_ready_seq": {},
        "pull_latest_review_time": {},
        "pull_recent_reviews": {},
        "pull_review_floor": {},
        "pull_baseline_updated_at": {},
        "pull_reviews_checked": {},
        "pull_review_page": {},
    }


def _check_and_bound_state(state: dict[str, object]) -> None:
    """Trim harmless caches/windows and fail with StateFullError rather than forgetting needed state."""
    seen: list[str] = state["seen_ids"]  # type: ignore[assignment]
    if len(seen) > MAX_SEEN_IDS:
        state["seen_ids"] = seen[-MAX_SEEN_IDS:]

    pending: list[dict[str, object]] = state["pending"]  # type: ignore[assignment]
    if len(pending) > MAX_PENDING_EVENTS:
        raise StateFullError("Waiting event queue exceeds MAX_PENDING_EVENTS.")

    open_prs_raw: list[int] = list(state["open_pr_numbers"])  # type: ignore[arg-type]
    dedup_open: list[int] = []
    for n in open_prs_raw:
        if n not in dedup_open:
            dedup_open.append(n)

    known_prs: list[int] = state["known_pr_numbers"]  # type: ignore[assignment]
    pull_heads: dict[str, str] = state["pull_heads"]  # type: ignore[assignment]
    pull_rev_time: dict[str, str] = state["pull_latest_review_time"]  # type: ignore[assignment]
    pull_recent_revs: dict[str, dict[str, str]] = state["pull_recent_reviews"]  # type: ignore[assignment]
    pull_rev_floor: dict[str, list[object]] = state["pull_review_floor"]  # type: ignore[assignment]

    all_pr_nums = (
        set(known_prs)
        | {int(k) for k in pull_heads if k.isdigit()}
        | {int(k) for k in pull_rev_time if k.isdigit()}
        | {int(k) for k in pull_recent_revs if k.isdigit()}
        | {int(k) for k in pull_rev_floor if k.isdigit()}
        | set(dedup_open)
    )
    if len(all_pr_nums) > MAX_TRACKED_PULLS:
        raise StateFullError("Tracked pull request count reached MAX_TRACKED_PULLS.")

    state["open_pr_numbers"] = dedup_open
    state["known_pr_numbers"] = sorted(all_pr_nums)
    baselined: list[int] = state["reviews_baselined"]  # type: ignore[assignment]
    state["reviews_baselined"] = sorted({n for n in baselined if n in all_pr_nums})
    warned: list[int] = state["review_window_warned_prs"]  # type: ignore[assignment]
    state["review_window_warned_prs"] = sorted({n for n in warned if n in all_pr_nums})

    etags: dict[str, str] = state["etags"]  # type: ignore[assignment]
    core_keys = {"pulls", "issues", "issue_events", "issue_comments", "pull_comments"}
    review_keys = [k for k in etags if k not in core_keys]
    if len(review_keys) > MAX_REVIEW_ETAGS:
        for old_k in review_keys[:-MAX_REVIEW_ETAGS]:
            del etags[old_k]


def _serialize_state(state: dict[str, object]) -> str:
    _check_and_bound_state(state)
    payload = json.dumps(state, ensure_ascii=True, separators=(",", ":")) + "\n"
    if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
        raise StateFullError("Serialized state exceeds MAX_STATE_BYTES.")
    return payload


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
        if len(raw_bytes) > MAX_STATE_BYTES:
            raise StateReadError("State file exceeds maximum allowed size.")
        text = raw_bytes.decode("utf-8")
        raw = json.loads(text)
    except StateReadError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise StateReadError("State file is unreadable or damaged.") from exc

    if not isinstance(raw, dict):
        raise StateReadError("State file has an invalid root value.")

    base = _empty_state()
    if "version" in raw:
        raw_ver = raw["version"]
        if isinstance(raw_ver, bool) or not isinstance(raw_ver, int):
            raise StateReadError("State file has an invalid version.")
        if raw_ver != STATE_VERSION:
            raise StateReadError(
                f"State file was written by watcher version {raw_ver} (expected version {STATE_VERSION}); "
                "delete the old state file or pass a new --state path to start fresh "
                "(nothing older than the first start is reported)."
            )
    elif any(k in raw for k in LEGACY_STATE_KEYS):
        raise StateReadError(
            f"State file was written by an earlier watcher version (expected version {STATE_VERSION}); "
            "delete the old state file or pass a new --state path to start fresh "
            "(nothing older than the first start is reported)."
        )

    if not set(raw).issubset(set(base) | {"version"}):
        raise StateReadError("State file contains unrecognized fields.")
    if "initialized" in raw and not isinstance(raw["initialized"], bool):
        raise StateReadError("State file has an invalid initialized flag.")
    base["initialized"] = bool(raw.get("initialized", False))

    seen = raw.get("seen_ids", [])
    if not isinstance(seen, list) or not all(isinstance(x, str) for x in seen):
        raise StateReadError("State file has invalid seen_ids.")
    dedup_seen: list[str] = []
    seen_lookup: set[str] = set()
    for item_id in seen:
        if item_id not in seen_lookup:
            seen_lookup.add(item_id)
            dedup_seen.append(item_id)
    base["seen_ids"] = dedup_seen[-MAX_SEEN_IDS:]

    pending = raw.get("pending", [])
    allowed_ev_keys = {"id", "kind", "number", "actor", "actor_id", "time", "url", "poll"}
    if not isinstance(pending, list) or len(pending) > MAX_PENDING_EVENTS:
        raise StateReadError("State file has invalid or oversized pending events.")
    for ev in pending:
        if (
            not isinstance(ev, dict)
            or not set(ev).issubset(allowed_ev_keys)
            or not isinstance(ev.get("id"), str)
            or not isinstance(ev.get("kind"), str)
            or isinstance(ev.get("number"), bool)
            or not isinstance(ev.get("number"), int)
            or isinstance(ev.get("actor_id"), bool)
            or not isinstance(ev.get("actor_id"), int)
            or ev["actor_id"] < 0
            or not isinstance(ev.get("time"), str)
            or not isinstance(ev.get("url"), str)
            or ("actor" in ev and not isinstance(ev["actor"], str))
            or ("poll" in ev and (isinstance(ev["poll"], bool) or not isinstance(ev["poll"], int) or ev["poll"] < 0))
        ):
            raise StateReadError("State file has invalid pending event item.")
    base["pending"] = list(pending)
    poll_seq = raw.get("poll_seq", 0)
    if isinstance(poll_seq, bool) or not isinstance(poll_seq, int) or poll_seq < 0:
        raise StateReadError("State file has an invalid count of looks.")
    base["poll_seq"] = poll_seq

    for str_map in ("etags", "pull_heads", "pull_baseline_updated_at", "pull_reviews_checked"):
        val = raw.get(str_map, {})
        if not isinstance(val, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in val.items()):
            raise StateReadError(f"State file has invalid {str_map}.")
        base[str_map] = dict(val)

    rev_time_map = raw.get("pull_latest_review_time", {})
    if not isinstance(rev_time_map, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and bool(ISO_UTC_RE.fullmatch(v))
        for k, v in rev_time_map.items()
    ):
        raise StateReadError("State file has invalid pull_latest_review_time.")
    for ts_val in rev_time_map.values():
        try:
            _iso_to_epoch_seconds(ts_val)
        except MalformedItemError as exc:
            raise StateReadError("State file has invalid pull_latest_review_time.") from exc
    base["pull_latest_review_time"] = dict(rev_time_map)

    recent_revs_map = raw.get("pull_recent_reviews", {})
    if not isinstance(recent_revs_map, dict):
        raise StateReadError("State file has invalid pull_recent_reviews.")
    parsed_recent_revs: dict[str, dict[str, str]] = {}
    for k, inner in recent_revs_map.items():
        if (
            not isinstance(k, str)
            or not isinstance(inner, dict)
            or len(inner) > MAX_WINDOW_REVIEW_IDS
        ):
            raise StateReadError("State file has invalid or oversized pull_recent_reviews.")
        clean_inner: dict[str, str] = {}
        for rk, rts in inner.items():
            if (
                not isinstance(rk, str)
                or not rk.isdigit()
                or int(rk) <= 0
                or not isinstance(rts, str)
                or not ISO_UTC_RE.fullmatch(rts)
            ):
                raise StateReadError("State file has invalid pull_recent_reviews entry.")
            try:
                _iso_to_epoch_seconds(rts)
            except MalformedItemError as exc:
                raise StateReadError("State file has invalid pull_recent_reviews timestamp.") from exc
            clean_inner[rk] = rts
        parsed_recent_revs[k] = clean_inner
    base["pull_recent_reviews"] = parsed_recent_revs

    floor_map = raw.get("pull_review_floor", {})
    if not isinstance(floor_map, dict):
        raise StateReadError("State file has invalid pull_review_floor.")
    parsed_floors: dict[str, list[object]] = {}
    for k, pair in floor_map.items():
        if (
            not isinstance(k, str)
            or not isinstance(pair, list)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not ISO_UTC_RE.fullmatch(pair[0])
            or isinstance(pair[1], bool)
            or not isinstance(pair[1], int)
            or pair[1] <= 0
        ):
            raise StateReadError("State file has invalid pull_review_floor entry.")
        try:
            _iso_to_epoch_seconds(pair[0])
        except MalformedItemError as exc:
            raise StateReadError("State file has invalid pull_review_floor timestamp.") from exc
        parsed_floors[k] = [pair[0], pair[1]]
    base["pull_review_floor"] = parsed_floors

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

    for list_key in ("known_pr_numbers", "open_pr_numbers", "reviews_baselined", "review_window_warned_prs"):
        vals = raw.get(list_key, [])
        if not isinstance(vals, list) or not all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in vals):
            raise StateReadError(f"State file has invalid {list_key}.")
        base[list_key] = list(vals)

    _check_and_bound_state(base)
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


def _write_serialized_state(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _fetch_signed_in_account() -> tuple[str, int | None]:
    status, _, data, _ = _gh_get("user")
    if status != 200 or not isinstance(data, dict):
        raise TransientGitHubError("unexpected")
    login = data.get("login")
    if not isinstance(login, str) or not GITHUB_LOGIN_RE.fullmatch(login):
        raise TransientGitHubError("unexpected")
    raw_id = data.get("id")
    own_id = raw_id if (isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0) else None
    return (login, own_id)


def _item_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MalformedItemError()
    return value


def _pos_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MalformedItemError()
    return value


def _iso_to_epoch_seconds(ts: str) -> int:
    try:
        dt = datetime(
            int(ts[0:4]),
            int(ts[5:7]),
            int(ts[8:10]),
            int(ts[11:13]),
            int(ts[14:16]),
            int(ts[17:19]),
            tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise MalformedItemError() from exc
    return int(dt.timestamp())


def _iso_utc(value: object) -> str:
    if not isinstance(value, str) or not ISO_UTC_RE.fullmatch(value):
        raise MalformedItemError()
    _iso_to_epoch_seconds(value)
    return value


def _record_seen_review(
    rid: int,
    submitted_at: str,
    curr_latest_time: str,
    curr_recent_map: dict[str, str],
    curr_floor: tuple[str, int] | None,
) -> tuple[str, dict[str, str], tuple[str, int] | None, bool]:
    """Advance latest review time, remember window ids, and narrow the window past MAX_WINDOW_REVIEW_IDS."""
    rev_sec = _iso_to_epoch_seconds(submitted_at)
    if not curr_latest_time or submitted_at > curr_latest_time:
        curr_latest_time = submitted_at
        cutoff_sec = rev_sec - REVIEW_LAG_WINDOW_SECONDS
        curr_recent_map = {
            k: ts
            for k, ts in curr_recent_map.items()
            if _iso_to_epoch_seconds(ts) >= cutoff_sec
        }
        if curr_floor is not None and _iso_to_epoch_seconds(curr_floor[0]) < cutoff_sec:
            curr_floor = None
    else:
        cutoff_sec = _iso_to_epoch_seconds(curr_latest_time) - REVIEW_LAG_WINDOW_SECONDS
        if rev_sec < cutoff_sec:
            return (curr_latest_time, curr_recent_map, curr_floor, False)
        if curr_floor is not None and (submitted_at, rid) <= curr_floor:
            return (curr_latest_time, curr_recent_map, curr_floor, False)

    curr_recent_map[str(rid)] = submitted_at
    narrowed = False
    if len(curr_recent_map) > MAX_WINDOW_REVIEW_IDS:
        ordered = sorted((ts, int(rk)) for rk, ts in curr_recent_map.items())
        evicted = ordered[:-MAX_WINDOW_REVIEW_IDS]
        kept = ordered[-MAX_WINDOW_REVIEW_IDS:]
        evicted_floor = evicted[-1]
        if curr_floor is None or evicted_floor > curr_floor:
            curr_floor = evicted_floor
        curr_recent_map = {str(item_rid): item_ts for item_ts, item_rid in kept}
        narrowed = True
    return (curr_latest_time, curr_recent_map, curr_floor, narrowed)


def _classify_actor(
    user_obj: object,
    own_login: str,
    include_own: bool,
    own_id: int | None = None,
) -> tuple[bool, str | None, int]:
    """Return (should_include, validated_login_or_none, actor_id_or_zero)."""
    if user_obj is None:
        return (True, None, 0)
    if not isinstance(user_obj, dict):
        raise MalformedItemError()
    user_type = user_obj.get("type")
    if isinstance(user_type, str) and user_type.lower() == "bot":
        return (False, None, 0)
    raw_uid = user_obj.get("id")
    actor_id = raw_uid if (isinstance(raw_uid, int) and not isinstance(raw_uid, bool) and raw_uid > 0) else 0
    raw_login = user_obj.get("login")
    if isinstance(raw_login, str) and raw_login.lower().endswith("[bot]"):
        return (False, None, 0)
    if not include_own:
        if own_id is not None and actor_id > 0 and actor_id == own_id:
            return (False, None, actor_id)
        if isinstance(raw_login, str) and raw_login.lower() == own_login.lower():
            return (False, None, actor_id)
    if isinstance(raw_login, str) and GITHUB_LOGIN_RE.fullmatch(raw_login):
        return (True, raw_login, actor_id)
    return (True, None, actor_id)


def _build_event(
    event_id: str,
    kind: str,
    number: int,
    actor_login: str | None,
    actor_id: int,
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
    event["actor_id"] = actor_id
    event["time"] = timestamp
    event["url"] = url
    return event


def arrival_order(event: dict[str, object]) -> tuple:
    """The one order of arrivals, decided here and nowhere else, with no clock of the desk's own: first the look of the
    watcher that found the arrival (an arrival found in an earlier look is older than one found in a later look); within
    one look the time GitHub gives, to the second, an arrival that GitHub gives no time of its own (new commits, a pull
    request marked ready) first; then the number GitHub gave the comment, review or pull request, after its kind, compared
    as a number. The relay numbers the arrivals in the order printed, and the payer, the wait and the place in its queue
    follow from that number alone."""
    parts = [part for part in re.split(r"(\d+)", str(event.get("id", ""))) if part]
    look = event.get("poll", 0)
    return (look if isinstance(look, int) else 0, str(event.get("time", "")), tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in parts))


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
    """Order-preserving recent-id window for reported outside events only."""

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
) -> tuple[bool, str | None, list[dict[str, object]], float, int]:
    """Fetch a list endpoint across Link rel=next pages until stop_item(item) is True."""
    status, headers, data, max_poll = _gh_get(first_endpoint, etag=etag)
    if status == 304:
        return (False, etag, [], max_poll, 0)
    if not isinstance(data, list):
        raise TransientGitHubError("unexpected")

    new_etag = headers.get("etag") or etag
    items: list[dict[str, object]] = []
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


def _report_narrowed_review_windows(pr_numbers: list[int]) -> None:
    for number in pr_numbers:
        sys.stderr.write(
            f"Pull request #{number} exceeded {MAX_WINDOW_REVIEW_IDS} reviews in 5 minutes; "
            "narrowed its late-review window.\n"
        )
    if pr_numbers:
        sys.stderr.flush()


def _load_floor(pull_rev_floor: dict[str, list[object]], num_key: str) -> tuple[str, int] | None:
    raw = pull_rev_floor.get(num_key)
    if raw is None:
        return None
    return (str(raw[0]), int(raw[1]))  # type: ignore[arg-type]


def _initialize_baseline(
    state: dict[str, object],
    repo: str,
) -> tuple[dict[str, object], float, int, list[int]]:
    """Snapshot existing repository watermarks on first run without dropping concurrent arrivals."""
    working = copy.deepcopy(state)
    etags: dict[str, str] = working["etags"]  # type: ignore[assignment]
    wm: dict[str, object] = working["watermarks"]  # type: ignore[assignment]
    known_prs: set[int] = set(working["known_pr_numbers"])  # type: ignore[arg-type]
    warned_prs: set[int] = set(working["review_window_warned_prs"])  # type: ignore[arg-type]
    pull_heads: dict[str, str] = working["pull_heads"]  # type: ignore[assignment]
    pull_draft: dict[str, bool] = working["pull_draft"]  # type: ignore[assignment]
    pull_rev_time: dict[str, str] = working["pull_latest_review_time"]  # type: ignore[assignment]
    pull_recent_revs: dict[str, dict[str, str]] = working["pull_recent_reviews"]  # type: ignore[assignment]
    pull_rev_floor: dict[str, list[object]] = working["pull_review_floor"]  # type: ignore[assignment]
    pull_base_upd: dict[str, str] = working["pull_baseline_updated_at"]  # type: ignore[assignment]
    pull_rev_checked: dict[str, str] = working["pull_reviews_checked"]  # type: ignore[assignment]
    pull_rev_page: dict[str, int] = working["pull_review_page"]  # type: ignore[assignment]

    server_poll = 0.0
    skipped = 0
    newly_warned: list[int] = []

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
            _iso_utc(pr.get("created_at"))
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

        known_prs.add(number)
        if len(known_prs) > MAX_TRACKED_PULLS:
            raise StateFullError("Tracked pull request count reached MAX_TRACKED_PULLS.")
        num_key = str(number)
        pull_heads[num_key] = head_sha
        pull_draft[num_key] = is_draft
        pull_base_upd[num_key] = updated_at
        pull_rev_checked[num_key] = updated_at
        if pr_id > max_pull_id:
            max_pull_id = pr_id
        if updated_at > max_pull_upd:
            max_pull_upd = updated_at
        if pr.get("state") == "open" and number not in open_prs:
            open_prs.append(number)

    wm["max_pull_id"] = max_pull_id
    wm["max_pull_updated_at"] = max_pull_upd
    working["open_pr_numbers"] = open_prs
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
            _iso_utc(item.get("created_at"))
        except MalformedItemError:
            skipped += 1
            continue
        if iid > max_issue_id:
            max_issue_id = iid
        if "pull_request" in item and item["pull_request"] is not None:
            known_prs.add(num)
            if len(known_prs) > MAX_TRACKED_PULLS:
                raise StateFullError("Tracked pull request count reached MAX_TRACKED_PULLS.")
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
            _iso_utc(item.get("created_at"))
        except MalformedItemError:
            skipped += 1
            continue
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
            _iso_utc(item.get("created_at"))
        except MalformedItemError:
            skipped += 1
            continue
        if cid > max_ic_id:
            max_ic_id = cid
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
            _iso_utc(item.get("created_at"))
        except MalformedItemError:
            skipped += 1
            continue
        if cid > max_rc_id:
            max_rc_id = cid
    wm["max_review_comment_id"] = max_rc_id

    # 6. Existing reviews on the polled open pull requests (recording latest time and 5-minute window ids)
    reviews_baselined: list[int] = []
    for num in polled_open_prs:
        _, tail_page, r_etag, r_items, hint = _fetch_reviews_from_tail(
            repo, num, start_page=1, etag=None, force_recheck_on_304=False
        )
        server_poll = max(server_poll, hint)
        num_key = str(num)
        pull_rev_page[num_key] = tail_page
        _set_review_etag(etags, num, tail_page, r_etag)
        reviews_baselined.append(num)
        baseline_revs: list[tuple[str, int]] = []
        for raw_rev in r_items:
            try:
                rev = _item_dict(raw_rev)
                rev_state = rev.get("state")
                if isinstance(rev_state, str) and rev_state.upper() == "PENDING":
                    continue
                rid = _pos_int(rev.get("id"))
                submitted_at = _iso_utc(rev.get("submitted_at") or rev.get("created_at"))
            except MalformedItemError:
                skipped += 1
                continue
            baseline_revs.append((submitted_at, rid))
        baseline_revs.sort(key=lambda t: (t[0], t[1]))
        latest_time = pull_rev_time.get(num_key, "")
        recent_map: dict[str, str] = dict(pull_recent_revs.get(num_key, {}))
        curr_floor = _load_floor(pull_rev_floor, num_key)
        pr_narrowed = False
        for submitted_at, rid in baseline_revs:
            latest_time, recent_map, curr_floor, did_narrow = _record_seen_review(
                rid, submitted_at, latest_time, recent_map, curr_floor
            )
            if did_narrow:
                pr_narrowed = True
        if latest_time:
            pull_rev_time[num_key] = latest_time
            pull_recent_revs[num_key] = recent_map
            if curr_floor is not None:
                pull_rev_floor[num_key] = [curr_floor[0], curr_floor[1]]
            else:
                pull_rev_floor.pop(num_key, None)
        if pr_narrowed and num not in warned_prs:
            warned_prs.add(num)
            newly_warned.append(num)

    working["initialized"] = True
    working["known_pr_numbers"] = sorted(known_prs)
    working["reviews_baselined"] = reviews_baselined
    working["review_window_warned_prs"] = sorted(warned_prs)
    _check_and_bound_state(working)
    return (working, server_poll, skipped, newly_warned)


def _poll_once(
    state: dict[str, object],
    repo: str,
    own_login: str,
    include_own: bool,
    own_id: int | None = None,
) -> tuple[dict[str, object], float, int, list[int]]:
    """Poll GitHub on a copy of state, stopping immediately without advancing past unqueued events if full."""
    working = copy.deepcopy(state)
    etags: dict[str, str] = working["etags"]  # type: ignore[assignment]
    wm: dict[str, object] = working["watermarks"]  # type: ignore[assignment]
    seen = _SeenTracker(list(working["seen_ids"]))  # type: ignore[arg-type]
    pending_list: list[dict[str, object]] = working["pending"]  # type: ignore[assignment]
    pending_ids: set[str] = {str(e["id"]) for e in pending_list}
    known_prs: set[int] = set(working["known_pr_numbers"])  # type: ignore[arg-type]
    open_prs: list[int] = list(working["open_pr_numbers"])  # type: ignore[arg-type]
    reviews_baselined: set[int] = set(working["reviews_baselined"])  # type: ignore[arg-type]
    warned_prs: set[int] = set(working["review_window_warned_prs"])  # type: ignore[arg-type]
    pull_heads: dict[str, str] = working["pull_heads"]  # type: ignore[assignment]
    pull_seq: dict[str, int] = working["pull_seq"]  # type: ignore[assignment]
    pull_draft: dict[str, bool] = working["pull_draft"]  # type: ignore[assignment]
    pull_ready_seq: dict[str, int] = working["pull_ready_seq"]  # type: ignore[assignment]
    pull_rev_time: dict[str, str] = working["pull_latest_review_time"]  # type: ignore[assignment]
    pull_recent_revs: dict[str, dict[str, str]] = working["pull_recent_reviews"]  # type: ignore[assignment]
    pull_rev_floor: dict[str, list[object]] = working["pull_review_floor"]  # type: ignore[assignment]
    pull_base_upd: dict[str, str] = working["pull_baseline_updated_at"]  # type: ignore[assignment]
    pull_rev_checked: dict[str, str] = working["pull_reviews_checked"]  # type: ignore[assignment]
    pull_rev_page: dict[str, int] = working["pull_review_page"]  # type: ignore[assignment]

    discovered: list[dict[str, object]] = []
    server_poll = 0.0
    skipped = 0
    newly_warned: list[int] = []
    ready_emitted_prs: set[int] = set()
    queue_filled = len(pending_list) >= MAX_PENDING_EVENTS

    def has_room() -> bool:
        return (len(pending_list) + len(discovered)) < MAX_PENDING_EVENTS

    def ensure_pr_tracked(number: int) -> None:
        if number not in known_prs:
            if len(known_prs) >= MAX_TRACKED_PULLS:
                raise StateFullError("Tracked pull request count reached MAX_TRACKED_PULLS.")
            known_prs.add(number)

    # 1. Pull requests (processed oldest-updated first so stopping on full queue never skips earlier PR updates)
    prev_max_pull_id = int(wm["max_pull_id"])  # type: ignore[arg-type]
    prev_max_pull_upd = str(wm["max_pull_updated_at"])
    # GitHub gives no time for a push or for marking a pull request ready, and the time of a pull request's last update is
    # moved by whatever anyone does on it later: such an arrival has no time of its own, and the order of looks says how old it is.
    unseen_since = ""
    prs_with_updated_timestamp: set[int] = set()
    prs_to_check_reviews: list[int] = []
    valid_pulls_seen: dict[int, str] = {}

    if not queue_filled:
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

        new_max_pull_id = prev_max_pull_id
        new_max_pull_upd = prev_max_pull_upd

        if changed:
            parsed_pulls: list[tuple[str, int, dict[str, object], str, str, str, bool, bool, str | None, int]] = []
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
                    keep, actor, actor_id = _classify_actor(pr.get("user"), own_login, include_own, own_id)
                except MalformedItemError:
                    skipped += 1
                    continue
                parsed_pulls.append(
                    (updated_at, pr_id, pr, created_at, head_sha, str(number), is_draft, keep, actor, actor_id)
                )

            # Oldest (updated_at, pr_id) first.
            parsed_pulls.sort(key=lambda t: (t[0], t[1]))
            current_open_seen: list[int] = []
            closed_this_poll: set[int] = set()

            for updated_at, pr_id, pr, created_at, head_sha, num_key, is_draft, keep, actor, actor_id in parsed_pulls:
                number = int(num_key)
                prev_sha = pull_heads.get(num_key)
                prev_draft = pull_draft.get(num_key)

                # Build candidate outside events for this PR before mutating per-PR state.
                is_brand_new = prev_sha is None and pr_id > prev_max_pull_id
                if is_brand_new:
                    ev_id = f"pull_request_opened:{pr_id}"
                    if keep and ev_id not in seen and ev_id not in pending_ids:
                        if not has_room():
                            queue_filled = True
                            break
                        ensure_pr_tracked(number)
                        pending_ids.add(ev_id)
                        discovered.append(
                            _build_event(
                                ev_id,
                                "pull_request_opened",
                                number,
                                actor,
                                actor_id,
                                created_at,
                                f"https://github.com/{repo}/pull/{number}",
                            )
                        )
                    else:
                        ensure_pr_tracked(number)
                    pull_heads[num_key] = head_sha
                    pull_draft[num_key] = is_draft
                    reviews_baselined.add(number)
                else:
                    ensure_pr_tracked(number)
                    if prev_sha is not None and prev_sha != head_sha:
                        next_seq = pull_seq.get(num_key, 0) + 1
                        move_id = f"pull_request_commits:{pr_id}:{next_seq}:{prev_sha}:{head_sha}"
                        if keep and move_id not in seen and move_id not in pending_ids:
                            if not has_room():
                                queue_filled = True
                                break
                            pending_ids.add(move_id)
                            discovered.append(
                                _build_event(
                                    move_id,
                                    "pull_request_commits",
                                    number,
                                    actor,
                                    actor_id,
                                    unseen_since,
                                    f"https://github.com/{repo}/pull/{number}",
                                )
                            )
                        pull_seq[num_key] = next_seq
                        pull_heads[num_key] = head_sha
                    else:
                        pull_heads[num_key] = head_sha

                    if prev_draft is True and not is_draft:
                        next_rseq = pull_ready_seq.get(num_key, 0) + 1
                        ready_id = f"pull_request_ready:{pr_id}:{next_rseq}"
                        if keep and ready_id not in seen and ready_id not in pending_ids:
                            if not has_room():
                                queue_filled = True
                                break
                            pending_ids.add(ready_id)
                            discovered.append(
                                _build_event(
                                    ready_id,
                                    "pull_request_ready",
                                    number,
                                    actor,
                                    actor_id,
                                    unseen_since,
                                    f"https://github.com/{repo}/pull/{number}",
                                )
                            )
                        pull_ready_seq[num_key] = next_rseq
                        pull_draft[num_key] = is_draft
                        ready_emitted_prs.add(number)
                    else:
                        pull_draft[num_key] = is_draft

                valid_pulls_seen[number] = updated_at
                if pr.get("state") == "open":
                    if number in current_open_seen:
                        current_open_seen.remove(number)
                    current_open_seen.insert(0, number)
                else:
                    closed_this_poll.add(number)

                if pr_id > new_max_pull_id:
                    new_max_pull_id = pr_id
                if updated_at > new_max_pull_upd:
                    new_max_pull_upd = updated_at

                if pull_rev_checked.get(num_key) != updated_at:
                    prs_with_updated_timestamp.add(number)
                    if number not in prs_to_check_reviews:
                        prs_to_check_reviews.append(number)

            merged_open = list(current_open_seen)
            for existing_open in open_prs:
                if existing_open not in merged_open and existing_open not in closed_this_poll:
                    merged_open.append(existing_open)
            open_prs = merged_open
            working["open_pr_numbers"] = open_prs

            # Only commit pulls ETag and max_pull_id if every PR item in the response was consumed.
            if not queue_filled:
                if new_etag:
                    etags["pulls"] = new_etag
                wm["max_pull_id"] = new_max_pull_id
                wm["max_pull_updated_at"] = new_max_pull_upd
            else:
                etags.pop("pulls", None)
                wm["max_pull_updated_at"] = new_max_pull_upd
        else:
            if new_etag:
                etags["pulls"] = new_etag

        if not queue_filled:
            for open_num in open_prs[:MAX_OPEN_PR_REVIEWS_POLLED]:
                if open_num not in prs_to_check_reviews:
                    prs_to_check_reviews.append(open_num)

    # 2. Issues (processed in ascending id order; stop before unqueued item if queue fills)
    if not queue_filled and has_room():
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
        if i_changed:
            parsed_issues: list[tuple[int, int, str, bool, bool, str | None, int]] = []
            for raw_item in issue_items:
                try:
                    item = _item_dict(raw_item)
                    item_id = _pos_int(item.get("id"))
                    number = _pos_int(item.get("number"))
                    created_at = _iso_utc(item.get("created_at"))
                    is_pr_item = "pull_request" in item and item["pull_request"] is not None
                    keep, actor, actor_id = (
                        (False, None, 0)
                        if is_pr_item
                        else _classify_actor(item.get("user"), own_login, include_own, own_id)
                    )
                except MalformedItemError:
                    skipped += 1
                    continue
                parsed_issues.append((item_id, number, created_at, is_pr_item, keep, actor, actor_id))

            parsed_issues.sort(key=lambda t: t[0])
            new_max_issue_id = prev_max_issue_id
            for item_id, number, created_at, is_pr_item, keep, actor, actor_id in parsed_issues:
                if is_pr_item:
                    ensure_pr_tracked(number)
                    if item_id > new_max_issue_id:
                        new_max_issue_id = item_id
                    continue
                ev_id = f"issue_opened:{item_id}"
                if keep and ev_id not in seen and ev_id not in pending_ids:
                    if not has_room():
                        queue_filled = True
                        break
                    pending_ids.add(ev_id)
                    discovered.append(
                        _build_event(
                            ev_id,
                            "issue_opened",
                            number,
                            actor,
                            actor_id,
                            created_at,
                            f"https://github.com/{repo}/issues/{number}",
                        )
                    )
                if item_id > new_max_issue_id:
                    new_max_issue_id = item_id

            wm["max_issue_id"] = new_max_issue_id
            if not queue_filled and i_etag:
                etags["issues"] = i_etag
            elif queue_filled:
                etags.pop("issues", None)
        elif i_etag:
            etags["issues"] = i_etag

    # 3. Issue and pull request reopened events (processed in ascending id order)
    if not queue_filled and has_room():
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
        if ie_changed:
            parsed_ie: list[tuple[int, str, str | None, int | None, bool, bool, str | None, int]] = []
            for raw_item in ie_items:
                try:
                    item = _item_dict(raw_item)
                    ev_num_id = _pos_int(item.get("id"))
                    created_at = _iso_utc(item.get("created_at"))
                    ev_name_obj = item.get("event")
                    ev_name = ev_name_obj if isinstance(ev_name_obj, str) else None
                    if ev_name not in ("reopened", "ready_for_review"):
                        parsed_ie.append((ev_num_id, created_at, None, None, False, False, None, 0))
                        continue
                    issue_obj = _item_dict(item.get("issue"))
                    number = _pos_int(issue_obj.get("number"))
                    is_pr = ("pull_request" in issue_obj and issue_obj["pull_request"] is not None) or (number in known_prs)
                    actor_obj = item.get("actor") if "actor" in item else item.get("user")
                    keep, actor, actor_id = _classify_actor(actor_obj, own_login, include_own, own_id)
                except MalformedItemError:
                    skipped += 1
                    continue
                parsed_ie.append((ev_num_id, created_at, ev_name, number, is_pr, keep, actor, actor_id))

            parsed_ie.sort(key=lambda t: t[0])
            new_max_ev_id = prev_max_ev_id
            for ev_num_id, created_at, ev_name, number, is_pr, keep, actor, actor_id in parsed_ie:
                if ev_name is None or number is None:
                    if ev_num_id > new_max_ev_id:
                        new_max_ev_id = ev_num_id
                    continue
                if ev_name == "ready_for_review":
                    ensure_pr_tracked(number)
                    num_key = str(number)
                    if number not in ready_emitted_prs and pull_draft.get(num_key) is not False:
                        next_rseq = pull_ready_seq.get(num_key, 0) + 1
                        ev_id = f"pull_request_ready:ev{ev_num_id}:{next_rseq}"
                        if keep and ev_id not in seen and ev_id not in pending_ids:
                            if not has_room():
                                queue_filled = True
                                break
                            pending_ids.add(ev_id)
                            discovered.append(
                                _build_event(
                                    ev_id,
                                    "pull_request_ready",
                                    number,
                                    actor,
                                    actor_id,
                                    created_at,
                                    f"https://github.com/{repo}/pull/{number}",
                                )
                            )
                        pull_ready_seq[num_key] = next_rseq
                        pull_draft[num_key] = False
                        ready_emitted_prs.add(number)
                else:
                    if is_pr:
                        ensure_pr_tracked(number)
                        kind = "pull_request_reopened"
                        url = f"https://github.com/{repo}/pull/{number}"
                    else:
                        kind = "issue_reopened"
                        url = f"https://github.com/{repo}/issues/{number}"
                    ev_id = f"{kind}:{ev_num_id}"
                    if keep and ev_id not in seen and ev_id not in pending_ids:
                        if not has_room():
                            queue_filled = True
                            break
                        pending_ids.add(ev_id)
                        discovered.append(_build_event(ev_id, kind, number, actor, actor_id, created_at, url))

                if ev_num_id > new_max_ev_id:
                    new_max_ev_id = ev_num_id

            wm["max_issue_event_id"] = new_max_ev_id
            if not queue_filled and ie_etag:
                etags["issue_events"] = ie_etag
            elif queue_filled:
                etags.pop("issue_events", None)
        elif ie_etag:
            etags["issue_events"] = ie_etag

    # 4. Comments on issues and pull requests (processed in ascending id order)
    if not queue_filled and has_room():
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
        if ic_changed:
            parsed_ic: list[tuple[int, int, str, bool, bool, str | None, int]] = []
            for raw_item in ic_items:
                try:
                    item = _item_dict(raw_item)
                    cid = _pos_int(item.get("id"))
                    created_at = _iso_utc(item.get("created_at"))
                    html_url = item.get("html_url")
                    number = _extract_number_from_urls(item.get("issue_url"), ISSUE_URL_NUM_RE, html_url)
                    keep, actor, actor_id = _classify_actor(item.get("user"), own_login, include_own, own_id)
                except MalformedItemError:
                    skipped += 1
                    continue
                html_indicates_pr = isinstance(html_url, str) and bool(re.search(r"/pull/\d+(?:$|[#/?])", html_url))
                is_pr = (number in known_prs) or html_indicates_pr or ("pull_request_url" in item)
                parsed_ic.append((cid, number, created_at, is_pr, keep, actor, actor_id))

            parsed_ic.sort(key=lambda t: t[0])
            new_max_ic_id = prev_max_ic_id
            for cid, number, created_at, is_pr, keep, actor, actor_id in parsed_ic:
                if is_pr:
                    ensure_pr_tracked(number)
                    kind = "pull_request_comment"
                    url = f"https://github.com/{repo}/pull/{number}#issuecomment-{cid}"
                else:
                    kind = "issue_comment"
                    url = f"https://github.com/{repo}/issues/{number}#issuecomment-{cid}"
                ev_id = f"{kind}:{cid}"
                if keep and ev_id not in seen and ev_id not in pending_ids:
                    if not has_room():
                        queue_filled = True
                        break
                    pending_ids.add(ev_id)
                    discovered.append(_build_event(ev_id, kind, number, actor, actor_id, created_at, url))
                if cid > new_max_ic_id:
                    new_max_ic_id = cid

            wm["max_issue_comment_id"] = new_max_ic_id
            if not queue_filled and ic_etag:
                etags["issue_comments"] = ic_etag
            elif queue_filled:
                etags.pop("issue_comments", None)
        elif ic_etag:
            etags["issue_comments"] = ic_etag

    # 5. Pull request review comments (processed in ascending id order)
    if not queue_filled and has_room():
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
        if rc_changed:
            parsed_rc: list[tuple[int, int, str, bool, str | None, int]] = []
            for raw_item in rc_items:
                try:
                    item = _item_dict(raw_item)
                    cid = _pos_int(item.get("id"))
                    created_at = _iso_utc(item.get("created_at"))
                    number = _extract_number_from_urls(item.get("pull_request_url"), PULL_URL_NUM_RE, item.get("html_url"))
                    keep, actor, actor_id = _classify_actor(item.get("user"), own_login, include_own, own_id)
                except MalformedItemError:
                    skipped += 1
                    continue
                parsed_rc.append((cid, number, created_at, keep, actor, actor_id))

            parsed_rc.sort(key=lambda t: t[0])
            new_max_rc_id = prev_max_rc_id
            for cid, number, created_at, keep, actor, actor_id in parsed_rc:
                ensure_pr_tracked(number)
                ev_id = f"pull_request_review_comment:{cid}"
                url = f"https://github.com/{repo}/pull/{number}#discussion_r{cid}"
                if keep and ev_id not in seen and ev_id not in pending_ids:
                    if not has_room():
                        queue_filled = True
                        break
                    pending_ids.add(ev_id)
                    discovered.append(
                        _build_event(ev_id, "pull_request_review_comment", number, actor, actor_id, created_at, url)
                    )
                if cid > new_max_rc_id:
                    new_max_rc_id = cid

            wm["max_review_comment_id"] = new_max_rc_id
            if not queue_filled and rc_etag:
                etags["pull_comments"] = rc_etag
            elif queue_filled:
                etags.pop("pull_comments", None)
        elif rc_etag:
            etags["pull_comments"] = rc_etag

    # 6. Reviews on open or updated pull requests (5-minute lag window up to MAX_WINDOW_REVIEW_IDS per PR;
    #    restarting from page 1 when saved_tail_page > 1 relies on GitHub marking the PR updated when a review is sent)
    checked_review_prs: set[int] = set()
    for number in prs_to_check_reviews:
        if queue_filled or not has_room():
            queue_filled = True
            break
        ensure_pr_tracked(number)
        num_key = str(number)
        saved_tail_page = max(1, pull_rev_page.get(num_key, 1))
        if number in prs_with_updated_timestamp and saved_tail_page > 1:
            start_page = 1
            cached_etag = None
        else:
            start_page = saved_tail_page
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

        pr_cutoff = pull_base_upd.get(num_key, "")
        was_baselined = number in reviews_baselined
        curr_latest_time = pull_rev_time.get(num_key, "")
        curr_recent_map: dict[str, str] = dict(pull_recent_revs.get(num_key, {}))
        curr_floor = _load_floor(pull_rev_floor, num_key)

        if r_changed:
            parsed_revs: list[tuple[str, int, bool, str | None, int]] = []
            for raw_rev in r_items:
                try:
                    rev = _item_dict(raw_rev)
                    rev_state = rev.get("state")
                    if isinstance(rev_state, str) and rev_state.upper() == "PENDING":
                        continue
                    rid = _pos_int(rev.get("id"))
                    submitted_at = _iso_utc(rev.get("submitted_at") or rev.get("created_at"))
                    keep, actor, actor_id = _classify_actor(rev.get("user"), own_login, include_own, own_id)
                except MalformedItemError:
                    skipped += 1
                    continue
                parsed_revs.append((submitted_at, rid, keep, actor, actor_id))

            parsed_revs.sort(key=lambda t: (t[0], t[1]))
            pr_narrowed = False
            for submitted_at, rid, keep, actor, actor_id in parsed_revs:
                if curr_latest_time:
                    cutoff_sec = _iso_to_epoch_seconds(curr_latest_time) - REVIEW_LAG_WINDOW_SECONDS
                    if _iso_to_epoch_seconds(submitted_at) < cutoff_sec:
                        continue
                    if curr_floor is not None and (submitted_at, rid) <= curr_floor:
                        continue
                    if str(rid) in curr_recent_map:
                        continue
                if not was_baselined and pr_cutoff and submitted_at <= pr_cutoff:
                    curr_latest_time, curr_recent_map, curr_floor, did_narrow = _record_seen_review(
                        rid, submitted_at, curr_latest_time, curr_recent_map, curr_floor
                    )
                    if did_narrow:
                        pr_narrowed = True
                    continue
                ev_id = f"pull_request_review:{rid}"
                if keep and ev_id not in seen and ev_id not in pending_ids:
                    if not has_room():
                        queue_filled = True
                        break
                    pending_ids.add(ev_id)
                    discovered.append(
                        _build_event(
                            ev_id,
                            "pull_request_review",
                            number,
                            actor,
                            actor_id,
                            submitted_at,
                            f"https://github.com/{repo}/pull/{number}#pullrequestreview-{rid}",
                        )
                    )
                curr_latest_time, curr_recent_map, curr_floor, did_narrow = _record_seen_review(
                    rid, submitted_at, curr_latest_time, curr_recent_map, curr_floor
                )
                if did_narrow:
                    pr_narrowed = True

            if curr_latest_time:
                pull_rev_time[num_key] = curr_latest_time
                pull_recent_revs[num_key] = curr_recent_map
                if curr_floor is not None:
                    pull_rev_floor[num_key] = [curr_floor[0], curr_floor[1]]
                else:
                    pull_rev_floor.pop(num_key, None)
            if pr_narrowed and number not in warned_prs:
                warned_prs.add(number)
                newly_warned.append(number)
            if not queue_filled:
                reviews_baselined.add(number)
                pull_rev_page[num_key] = tail_page
                _set_review_etag(etags, number, tail_page, r_etag)
                if number in valid_pulls_seen:
                    pull_rev_checked[num_key] = valid_pulls_seen[number]
                checked_review_prs.add(number)
            else:
                pull_rev_page[num_key] = saved_tail_page
                _set_review_etag(etags, number, saved_tail_page, None)
        else:
            pull_rev_page[num_key] = tail_page
            _set_review_etag(etags, number, tail_page, r_etag)
            if number in valid_pulls_seen:
                pull_rev_checked[num_key] = valid_pulls_seen[number]
            checked_review_prs.add(number)

    if prs_with_updated_timestamp - checked_review_prs:
        etags.pop("pulls", None)
        wm["max_pull_updated_at"] = prev_max_pull_upd

    # This look's number, carried by every arrival it found.
    working["poll_seq"] = look = int(working.get("poll_seq", 0)) + 1  # type: ignore[arg-type]
    for event in discovered:
        event["poll"] = look
    if discovered:
        discovered.sort(key=arrival_order)
        pending_list.extend(discovered)
        pending_list.sort(key=arrival_order)

    working["seen_ids"] = seen.to_list()
    working["known_pr_numbers"] = sorted(known_prs)
    working["reviews_baselined"] = sorted(reviews_baselined)
    working["review_window_warned_prs"] = sorted(warned_prs)
    _check_and_bound_state(working)
    return (working, server_poll, skipped, newly_warned)


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
    payload = _serialize_state(state)
    try:
        _write_serialized_state(state_path, payload)
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
    # Validate capacity bounds BEFORE printing anything so StateFullError exits 8 cleanly.
    payload = _serialize_state(next_state)

    # Print and flush BEFORE replacing the state file so a crash can repeat an event, never drop it. The number of the look
    # that found an arrival is the watcher's own and is not printed.
    sys.stdout.write(json.dumps({"events": [{key: value for key, value in ev.items() if key != "poll"} for ev in batch]}, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    try:
        _write_serialized_state(state_path, payload)
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

        # When the waiting queue is already full, report from it at once and ask GitHub for nothing.
        if len(state["pending"]) >= MAX_PENDING_EVENTS:  # type: ignore[arg-type]
            return _emit_and_save(ns.state_path, state, ns.max_events)

        started = clock()
        deadline = (started + ns.max_wait) if ns.max_wait is not None else None
        own_login: str | None = None
        own_id: int | None = None
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
                own_login, own_id = _fetch_signed_in_account()
                consecutive_failures = 0
                last_failure_pause = 0.0
            except TransientGitHubError as err:
                if not wait_failure(err):
                    return _emit_timeout()

        # 2. If no state file existed yet, snapshot the baseline once and persist it before waiting.
        while not bool(state["initialized"]):
            try:
                state, server_poll, skipped, narrowed_init = _initialize_baseline(state, ns.repo)
                _report_skipped_items(skipped)
                _report_narrowed_review_windows(narrowed_init)
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
                state, server_poll, skipped, narrowed_poll = _poll_once(
                    state, ns.repo, own_login, ns.include_own, own_id
                )
                _report_skipped_items(skipped)
                _report_narrowed_review_windows(narrowed_poll)
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
                if current_pending_count >= MAX_PENDING_EVENTS:
                    # Queue is full: report what we have right away and ask GitHub for nothing more.
                    return _emit_and_save(ns.state_path, state, ns.max_events)

                settle_wait = max(ns.settle, server_poll) if ns.settle > 0 or server_poll > 0 else 0.0
                if settle_wait > 0:
                    sleep(settle_wait)
                    while True:
                        try:
                            state, _, skipped_after, narrowed_after = _poll_once(
                                state, ns.repo, own_login, ns.include_own, own_id
                            )
                            _report_skipped_items(skipped_after)
                            _report_narrowed_review_windows(narrowed_after)
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
    except StateFullError as exc:
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return EXIT_STATE_FULL
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
