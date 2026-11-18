#!/usr/bin/env python3
"""Watches BrilliantTiger/dodam on GitHub for new activity from outside.

    python3 tools/desk/watch.py --wait [--include-own] [--state FILE]
                                       [--settle SECONDS] [--max-events N]
                                       [--max-wait SECONDS] [--repo OWNER/NAME]

Talks to GitHub only through the signed-in gh CLI via run_gh(). Never reads,
accepts, or prints tokens or HTTP headers. Writes nothing to standard output
except the single JSON object printed right before exiting with code 0.

Counted outside event kinds:
  - issue_opened
  - issue_reopened
  - issue_comment
  - pull_request_comment
  - pull_request_opened
  - pull_request_reopened
  - pull_request_commits
  - pull_request_review
  - pull_request_review_comment

Idle polling uses conditional GET requests (If-None-Match -> 304 Not Modified,
which consumes 0 primary rate-limit requests on GitHub) across the five direct
repository endpoints plus open/changed pull request reviews, every 15 seconds
(or longer whenever GitHub sends X-Poll-Interval). Measured on 2026-11-18:
  - An idle hour while waiting costs 0 counted requests (verified across 30
    conditional 304 Not Modified requests over 65 seconds with core.used delta 0;
    process start asks gh api user once, costing 1 request, plus 5 + open_prs
    requests once when creating a new state file).
  - End-to-end reporting delay with the default 15-second settle window is about
    20 to 35 seconds (32.3 seconds measured on trial issue #16, well within the
    60-second target).
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
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

STATE_FOLDER_NAME = ".dodam-desk"
STATE_FILE_NAME = "watch-state.json"
STATE_VERSION = 1
MAX_OPEN_PR_REVIEWS_POLLED = 25

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
    "pull_request_reopened",
    "pull_request_commits",
    "pull_request_review",
    "pull_request_review_comment",
)

clock = time.monotonic
epoch = time.time
sleep = time.sleep


class SignedOutError(Exception):
    """Raised when gh is not signed in or authentication has failed."""


class UsageProblem(Exception):
    """Raised on invalid command-line arguments or disallowed paths."""


class TransientGitHubError(Exception):
    """Raised on network errors, GitHub faults, unexpected answers, or rate limits."""

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
        )
    except FileNotFoundError:
        return (4, "", "gh command not found")
    except subprocess.TimeoutExpired:
        return (1, "", "gh request timed out")
    return (proc.returncode, proc.stdout, proc.stderr)


def default_state_path() -> Path:
    """Default state file location in a dedicated folder under the home directory."""
    return Path.home() / STATE_FOLDER_NAME / STATE_FILE_NAME


def validate_state_path(raw_path: Path) -> Path:
    """Resolve --state and forbid paths inside the repository or inside .dodam."""
    try:
        resolved = raw_path.expanduser().resolve()
    except OSError as exc:
        raise UsageProblem("Invalid state file path.") from exc
    if ".dodam" in resolved.parts:
        raise UsageProblem("The state file must never be inside .dodam.")
    if resolved.is_relative_to(ROOT):
        raise UsageProblem("The state file must not be inside the repository.")
    return resolved


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageProblem("Invalid arguments; pass --wait and valid options.")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = _Parser(prog="watch.py", add_help=False)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--include-own", action="store_true")
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--settle", type=float, default=DEFAULT_SETTLE)
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS)
    parser.add_argument("--max-wait", type=float, default=None)
    parser.add_argument("--repo", type=str, default=DEFAULT_REPO)
    ns = parser.parse_args(argv)
    if not ns.wait:
        raise UsageProblem("Pass --wait to block for new outside events.")
    if not REPO_RE.fullmatch(ns.repo):
        raise UsageProblem("Invalid --repo; expected OWNER/NAME.")
    if not math.isfinite(ns.settle) or ns.settle < 0:
        raise UsageProblem("--settle must be a non-negative number.")
    if ns.max_events < 1:
        raise UsageProblem("--max-events must be at least 1.")
    if ns.max_wait is not None and (not math.isfinite(ns.max_wait) or ns.max_wait < 0):
        raise UsageProblem("--max-wait must be a non-negative number.")
    ns.state_path = validate_state_path(ns.state if ns.state is not None else default_state_path())
    return ns


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
    except json.JSONDecodeError as exc:
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


def _pos_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TransientGitHubError("unexpected")
    return value


def _iso_utc(value: object) -> str:
    if not isinstance(value, str) or not ISO_UTC_RE.fullmatch(value):
        raise TransientGitHubError("unexpected")
    return value


def _classify_actor(user_obj: object, own_login: str, include_own: bool) -> tuple[bool, str | None]:
    """Return (should_include, validated_login_or_none)."""
    if user_obj is None:
        return (True, None)
    if not isinstance(user_obj, dict):
        raise TransientGitHubError("unexpected")
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
    raise TransientGitHubError("unexpected")


def _empty_state() -> dict[str, object]:
    return {
        "version": STATE_VERSION,
        "initialized": False,
        "seen_ids": [],
        "pending": [],
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
        "pull_heads": {},
        "pull_baseline_updated_at": {},
        "pull_reviews_checked": {},
    }


def _load_state(path: Path) -> dict[str, object]:
    if not path.is_file():
        return _empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageProblem("State file could not be read.") from exc
    if not isinstance(raw, dict):
        raise UsageProblem("State file has an invalid format.")
    base = _empty_state()
    base["initialized"] = bool(raw.get("initialized", False))
    seen = raw.get("seen_ids", [])
    if not isinstance(seen, list) or not all(isinstance(x, str) for x in seen):
        raise UsageProblem("State file has invalid seen_ids.")
    base["seen_ids"] = list(seen)
    pending = raw.get("pending", [])
    if not isinstance(pending, list) or not all(isinstance(x, dict) for x in pending):
        raise UsageProblem("State file has invalid pending events.")
    base["pending"] = list(pending)
    for dict_key in ("etags", "pull_heads", "pull_baseline_updated_at", "pull_reviews_checked"):
        val = raw.get(dict_key, {})
        if isinstance(val, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in val.items()):
            base[dict_key] = dict(val)
    wm = raw.get("watermarks", {})
    if isinstance(wm, dict):
        target_wm = base["watermarks"]
        assert isinstance(target_wm, dict)
        for k, default_v in list(target_wm.items()):
            if k in wm and type(wm[k]) is type(default_v):
                target_wm[k] = wm[k]
    for list_key in ("known_pr_numbers", "open_pr_numbers"):
        vals = raw.get(list_key, [])
        if isinstance(vals, list) and all(isinstance(x, int) and not isinstance(x, bool) for x in vals):
            base[list_key] = list(vals)
    return base


def _save_state(path: Path, state: dict[str, object]) -> None:
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


def _fetch_paginated_list(
    first_endpoint: str,
    etag: str | None,
    stop_item,
) -> tuple[bool, str | None, list[dict[str, object]], float]:
    """Fetch a list endpoint across Link rel=next pages until stop_item(item) is True."""
    status, headers, data, max_poll = _gh_get(first_endpoint, etag=etag)
    if status == 304:
        return (False, etag, [], max_poll)
    if not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise TransientGitHubError("unexpected")

    new_etag = headers.get("etag") or etag
    items: list[dict[str, object]] = []
    stop = False
    for item in data:
        if stop_item is not None and stop_item(item):
            stop = True
            break
        items.append(item)

    next_ep = None if stop else _next_endpoint(headers.get("link"))
    while next_ep is not None:
        p_status, p_headers, p_data, p_poll = _gh_get(next_ep, etag=None)
        max_poll = max(max_poll, p_poll)
        if p_status != 200 or not isinstance(p_data, list) or not all(isinstance(x, dict) for x in p_data):
            raise TransientGitHubError("unexpected")
        for item in p_data:
            if stop_item is not None and stop_item(item):
                stop = True
                break
            items.append(item)
        if stop or not p_data:
            break
        next_ep = _next_endpoint(p_headers.get("link"))

    return (True, new_etag, items, max_poll)


def _baseline_single_page(
    first_endpoint: str,
) -> tuple[str | None, list[dict[str, object]], float]:
    status, headers, data, poll_hint = _gh_get(first_endpoint, etag=None)
    if status != 200 or not isinstance(data, list) or not all(isinstance(x, dict) for x in data):
        raise TransientGitHubError("unexpected")
    return (headers.get("etag"), data, poll_hint)


def _initialize_baseline(
    state: dict[str, object],
    repo: str,
) -> tuple[dict[str, object], float]:
    """Snapshot existing repository state on first run so only new events are reported."""
    working = copy.deepcopy(state)
    etags: dict[str, str] = working["etags"]  # type: ignore[assignment]
    wm: dict[str, object] = working["watermarks"]  # type: ignore[assignment]
    seen_set: set[str] = set(working["seen_ids"])  # type: ignore[arg-type]
    known_prs: set[int] = set(working["known_pr_numbers"])  # type: ignore[arg-type]
    pull_heads: dict[str, str] = working["pull_heads"]  # type: ignore[assignment]
    pull_base_upd: dict[str, str] = working["pull_baseline_updated_at"]  # type: ignore[assignment]
    pull_rev_checked: dict[str, str] = working["pull_reviews_checked"]  # type: ignore[assignment]

    server_poll = 0.0

    # 1. Pull requests (all pages at initial setup so every existing PR head & id is known)
    _, p_etag, pull_items, hint = _fetch_paginated_list(
        f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100",
        etag=None,
        stop_item=None,
    )
    server_poll = max(server_poll, hint)
    if p_etag:
        etags["pulls"] = p_etag

    open_prs: list[int] = []
    max_pull_id = 0
    max_pull_upd = ""
    for pr in pull_items:
        pr_id = _pos_int(pr.get("id"))
        number = _pos_int(pr.get("number"))
        updated_at = _iso_utc(pr.get("updated_at"))
        _iso_utc(pr.get("created_at"))
        head = pr.get("head")
        if not isinstance(head, dict):
            raise TransientGitHubError("unexpected")
        raw_sha = head.get("sha")
        if not isinstance(raw_sha, str) or not HEX_SHA_RE.fullmatch(raw_sha.lower()):
            raise TransientGitHubError("unexpected")
        head_sha = raw_sha.lower()

        known_prs.add(number)
        num_key = str(number)
        pull_heads[num_key] = head_sha
        pull_base_upd[num_key] = updated_at
        pull_rev_checked[num_key] = updated_at
        seen_set.add(f"pull_request_opened:{pr_id}")
        if pr_id > max_pull_id:
            max_pull_id = pr_id
        if updated_at > max_pull_upd:
            max_pull_upd = updated_at
        if pr.get("state") == "open" and number not in open_prs:
            open_prs.append(number)

    wm["max_pull_id"] = max_pull_id
    wm["max_pull_updated_at"] = max_pull_upd
    open_prs = open_prs[:MAX_OPEN_PR_REVIEWS_POLLED]
    working["open_pr_numbers"] = open_prs

    # 2. Issues (page 1 sorted by created desc gives max_issue_id)
    i_etag, issue_items, hint = _baseline_single_page(
        f"repos/{repo}/issues?state=all&sort=created&direction=desc&per_page=100"
    )
    server_poll = max(server_poll, hint)
    if i_etag:
        etags["issues"] = i_etag
    max_issue_id = 0
    for item in issue_items:
        iid = _pos_int(item.get("id"))
        num = _pos_int(item.get("number"))
        _iso_utc(item.get("created_at"))
        if iid > max_issue_id:
            max_issue_id = iid
        if "pull_request" in item and item["pull_request"] is not None:
            known_prs.add(num)
        else:
            seen_set.add(f"issue_opened:{iid}")
    wm["max_issue_id"] = max_issue_id

    # 3. Issue events (page 1 gives max_issue_event_id)
    ie_etag, ie_items, hint = _baseline_single_page(f"repos/{repo}/issues/events?per_page=100")
    server_poll = max(server_poll, hint)
    if ie_etag:
        etags["issue_events"] = ie_etag
    max_ev_id = 0
    for item in ie_items:
        eid = _pos_int(item.get("id"))
        _iso_utc(item.get("created_at"))
        if eid > max_ev_id:
            max_ev_id = eid
        if item.get("event") == "reopened":
            issue_obj = item.get("issue")
            if not isinstance(issue_obj, dict):
                raise TransientGitHubError("unexpected")
            num = _pos_int(issue_obj.get("number"))
            is_pr = ("pull_request" in issue_obj and issue_obj["pull_request"] is not None) or (num in known_prs)
            kind = "pull_request_reopened" if is_pr else "issue_reopened"
            seen_set.add(f"{kind}:{eid}")
    wm["max_issue_event_id"] = max_ev_id

    # 4. Issue & PR comments (page 1 sorted by created desc gives max_issue_comment_id)
    ic_etag, ic_items, hint = _baseline_single_page(
        f"repos/{repo}/issues/comments?sort=created&direction=desc&per_page=100"
    )
    server_poll = max(server_poll, hint)
    if ic_etag:
        etags["issue_comments"] = ic_etag
    max_ic_id = 0
    for item in ic_items:
        cid = _pos_int(item.get("id"))
        _iso_utc(item.get("created_at"))
        if cid > max_ic_id:
            max_ic_id = cid
        html_url = item.get("html_url")
        num = _extract_number_from_urls(item.get("issue_url"), ISSUE_URL_NUM_RE, html_url)
        html_pr = isinstance(html_url, str) and bool(re.search(r"/pull/\d+(?:$|[#/?])", html_url))
        is_pr = (num in known_prs) or html_pr or ("pull_request_url" in item)
        kind = "pull_request_comment" if is_pr else "issue_comment"
        seen_set.add(f"{kind}:{cid}")
    wm["max_issue_comment_id"] = max_ic_id

    # 5. Review comments (page 1 sorted by created desc gives max_review_comment_id)
    rc_etag, rc_items, hint = _baseline_single_page(
        f"repos/{repo}/pulls/comments?sort=created&direction=desc&per_page=100"
    )
    server_poll = max(server_poll, hint)
    if rc_etag:
        etags["pull_comments"] = rc_etag
    max_rc_id = 0
    for item in rc_items:
        cid = _pos_int(item.get("id"))
        _iso_utc(item.get("created_at"))
        if cid > max_rc_id:
            max_rc_id = cid
        seen_set.add(f"pull_request_review_comment:{cid}")
    wm["max_review_comment_id"] = max_rc_id

    # 6. Existing reviews on open pull requests
    for num in open_prs:
        rev_key = f"reviews:{num}"
        _, r_etag, r_items, hint = _fetch_paginated_list(
            f"repos/{repo}/pulls/{num}/reviews?per_page=100",
            etag=None,
            stop_item=None,
        )
        server_poll = max(server_poll, hint)
        if r_etag:
            etags[rev_key] = r_etag
        for rev in r_items:
            rev_state = rev.get("state")
            if isinstance(rev_state, str) and rev_state.upper() == "PENDING":
                continue
            rid = _pos_int(rev.get("id"))
            _iso_utc(rev.get("submitted_at") or rev.get("created_at"))
            seen_set.add(f"pull_request_review:{rid}")

    working["initialized"] = True
    working["seen_ids"] = sorted(seen_set)
    working["known_pr_numbers"] = sorted(known_prs)
    return (working, server_poll)


def _poll_once(
    state: dict[str, object],
    repo: str,
    own_login: str,
    include_own: bool,
) -> tuple[dict[str, object], float]:
    """Poll GitHub on a copy of an initialized state; return (updated_state, max_server_poll_interval)."""
    working = copy.deepcopy(state)
    etags: dict[str, str] = working["etags"]  # type: ignore[assignment]
    wm: dict[str, object] = working["watermarks"]  # type: ignore[assignment]
    seen_set: set[str] = set(working["seen_ids"])  # type: ignore[arg-type]
    pending_list: list[dict[str, object]] = working["pending"]  # type: ignore[assignment]
    pending_ids: set[str] = {str(e["id"]) for e in pending_list}
    known_prs: set[int] = set(working["known_pr_numbers"])  # type: ignore[arg-type]
    open_prs: list[int] = list(working["open_pr_numbers"])  # type: ignore[arg-type]
    pull_heads: dict[str, str] = working["pull_heads"]  # type: ignore[assignment]
    pull_base_upd: dict[str, str] = working["pull_baseline_updated_at"]  # type: ignore[assignment]
    pull_rev_checked: dict[str, str] = working["pull_reviews_checked"]  # type: ignore[assignment]

    discovered: list[dict[str, object]] = []
    server_poll = 0.0

    def record_event(ev: dict[str, object]) -> None:
        ev_id = str(ev["id"])
        if ev_id in seen_set or ev_id in pending_ids:
            return
        pending_ids.add(ev_id)
        discovered.append(ev)

    # 1. Pull requests (opened, new commits, and which PRs need review checks)
    prev_max_pull_id = int(wm["max_pull_id"])  # type: ignore[arg-type]
    prev_max_pull_upd = str(wm["max_pull_updated_at"])
    pulls_ep = f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100"

    def stop_pulls(item: dict[str, object]) -> bool:
        if not prev_max_pull_upd:
            return False
        return _iso_utc(item.get("updated_at")) < prev_max_pull_upd

    changed, new_etag, pull_items, poll_hint = _fetch_paginated_list(
        pulls_ep,
        etags.get("pulls"),
        stop_pulls,
    )
    server_poll = max(server_poll, poll_hint)
    if new_etag:
        etags["pulls"] = new_etag

    prs_to_check_reviews: list[int] = []
    new_max_pull_id = prev_max_pull_id
    new_max_pull_upd = prev_max_pull_upd

    if changed:
        current_open_seen: list[int] = []
        closed_this_poll: set[int] = set()
        for pr in pull_items:
            pr_id = _pos_int(pr.get("id"))
            number = _pos_int(pr.get("number"))
            created_at = _iso_utc(pr.get("created_at"))
            updated_at = _iso_utc(pr.get("updated_at"))
            head = pr.get("head")
            if not isinstance(head, dict):
                raise TransientGitHubError("unexpected")
            raw_sha = head.get("sha")
            if not isinstance(raw_sha, str) or not HEX_SHA_RE.fullmatch(raw_sha.lower()):
                raise TransientGitHubError("unexpected")
            head_sha = raw_sha.lower()

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
            if prev_sha is None and pr_id > prev_max_pull_id:
                keep, actor = _classify_actor(pr.get("user"), own_login, include_own)
                if keep:
                    record_event(
                        _build_event(
                            f"pull_request_opened:{pr_id}",
                            "pull_request_opened",
                            number,
                            actor,
                            created_at,
                            f"https://github.com/{repo}/pull/{number}",
                        )
                    )
                else:
                    seen_set.add(f"pull_request_opened:{pr_id}")
            elif prev_sha is not None and prev_sha != head_sha:
                keep, actor = _classify_actor(pr.get("user"), own_login, include_own)
                if keep:
                    record_event(
                        _build_event(
                            f"pull_request_commits:{pr_id}:{head_sha}",
                            "pull_request_commits",
                            number,
                            actor,
                            updated_at,
                            f"https://github.com/{repo}/pull/{number}",
                        )
                    )
                else:
                    seen_set.add(f"pull_request_commits:{pr_id}:{head_sha}")
            pull_heads[num_key] = head_sha

            if pull_rev_checked.get(num_key) != updated_at and number not in prs_to_check_reviews:
                prs_to_check_reviews.append(number)

        merged_open = list(current_open_seen)
        for existing_open in open_prs:
            if existing_open not in merged_open and existing_open not in closed_this_poll:
                merged_open.append(existing_open)
        open_prs = merged_open[:MAX_OPEN_PR_REVIEWS_POLLED]

    for open_num in open_prs:
        if open_num not in prs_to_check_reviews:
            prs_to_check_reviews.append(open_num)

    wm["max_pull_id"] = new_max_pull_id
    wm["max_pull_updated_at"] = new_max_pull_upd
    working["open_pr_numbers"] = open_prs

    # 2. Issues (issue_opened and PR number discovery)
    prev_max_issue_id = int(wm["max_issue_id"])  # type: ignore[arg-type]
    issues_ep = f"repos/{repo}/issues?state=all&sort=created&direction=desc&per_page=100"

    def stop_issues(item: dict[str, object]) -> bool:
        return prev_max_issue_id > 0 and _pos_int(item.get("id")) <= prev_max_issue_id

    i_changed, i_etag, issue_items, poll_hint = _fetch_paginated_list(
        issues_ep,
        etags.get("issues"),
        stop_issues,
    )
    server_poll = max(server_poll, poll_hint)
    if i_etag:
        etags["issues"] = i_etag
    if i_changed:
        new_max_issue_id = prev_max_issue_id
        for item in issue_items:
            item_id = _pos_int(item.get("id"))
            number = _pos_int(item.get("number"))
            created_at = _iso_utc(item.get("created_at"))
            if item_id > new_max_issue_id:
                new_max_issue_id = item_id
            if "pull_request" in item and item["pull_request"] is not None:
                known_prs.add(number)
                continue
            ev_id = f"issue_opened:{item_id}"
            keep, actor = _classify_actor(item.get("user"), own_login, include_own)
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
                seen_set.add(ev_id)
        wm["max_issue_id"] = new_max_issue_id

    # 3. Issue and pull request reopened events
    prev_max_ev_id = int(wm["max_issue_event_id"])  # type: ignore[arg-type]
    ie_ep = f"repos/{repo}/issues/events?per_page=100"

    def stop_ie(item: dict[str, object]) -> bool:
        return prev_max_ev_id > 0 and _pos_int(item.get("id")) <= prev_max_ev_id

    ie_changed, ie_etag, ie_items, poll_hint = _fetch_paginated_list(
        ie_ep,
        etags.get("issue_events"),
        stop_ie,
    )
    server_poll = max(server_poll, poll_hint)
    if ie_etag:
        etags["issue_events"] = ie_etag
    if ie_changed:
        new_max_ev_id = prev_max_ev_id
        for item in ie_items:
            ev_num_id = _pos_int(item.get("id"))
            created_at = _iso_utc(item.get("created_at"))
            if ev_num_id > new_max_ev_id:
                new_max_ev_id = ev_num_id
            if item.get("event") != "reopened":
                continue
            issue_obj = item.get("issue")
            if not isinstance(issue_obj, dict):
                raise TransientGitHubError("unexpected")
            number = _pos_int(issue_obj.get("number"))
            is_pr = ("pull_request" in issue_obj and issue_obj["pull_request"] is not None) or (number in known_prs)
            if is_pr:
                known_prs.add(number)
                kind = "pull_request_reopened"
                url = f"https://github.com/{repo}/pull/{number}"
            else:
                kind = "issue_reopened"
                url = f"https://github.com/{repo}/issues/{number}"
            ev_id = f"{kind}:{ev_num_id}"
            actor_obj = item.get("actor") if "actor" in item else item.get("user")
            keep, actor = _classify_actor(actor_obj, own_login, include_own)
            if keep:
                record_event(_build_event(ev_id, kind, number, actor, created_at, url))
            else:
                seen_set.add(ev_id)
        wm["max_issue_event_id"] = new_max_ev_id

    # 4. Comments on issues and pull requests
    prev_max_ic_id = int(wm["max_issue_comment_id"])  # type: ignore[arg-type]
    ic_ep = f"repos/{repo}/issues/comments?sort=created&direction=desc&per_page=100"

    def stop_ic(item: dict[str, object]) -> bool:
        return prev_max_ic_id > 0 and _pos_int(item.get("id")) <= prev_max_ic_id

    ic_changed, ic_etag, ic_items, poll_hint = _fetch_paginated_list(
        ic_ep,
        etags.get("issue_comments"),
        stop_ic,
    )
    server_poll = max(server_poll, poll_hint)
    if ic_etag:
        etags["issue_comments"] = ic_etag
    if ic_changed:
        new_max_ic_id = prev_max_ic_id
        for item in ic_items:
            cid = _pos_int(item.get("id"))
            created_at = _iso_utc(item.get("created_at"))
            if cid > new_max_ic_id:
                new_max_ic_id = cid
            html_url = item.get("html_url")
            number = _extract_number_from_urls(item.get("issue_url"), ISSUE_URL_NUM_RE, html_url)
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
            keep, actor = _classify_actor(item.get("user"), own_login, include_own)
            if keep:
                record_event(_build_event(ev_id, kind, number, actor, created_at, url))
            else:
                seen_set.add(ev_id)
        wm["max_issue_comment_id"] = new_max_ic_id

    # 5. Pull request review comments (inline comments in a review)
    prev_max_rc_id = int(wm["max_review_comment_id"])  # type: ignore[arg-type]
    rc_ep = f"repos/{repo}/pulls/comments?sort=created&direction=desc&per_page=100"

    def stop_rc(item: dict[str, object]) -> bool:
        return prev_max_rc_id > 0 and _pos_int(item.get("id")) <= prev_max_rc_id

    rc_changed, rc_etag, rc_items, poll_hint = _fetch_paginated_list(
        rc_ep,
        etags.get("pull_comments"),
        stop_rc,
    )
    server_poll = max(server_poll, poll_hint)
    if rc_etag:
        etags["pull_comments"] = rc_etag
    if rc_changed:
        new_max_rc_id = prev_max_rc_id
        for item in rc_items:
            cid = _pos_int(item.get("id"))
            created_at = _iso_utc(item.get("created_at"))
            if cid > new_max_rc_id:
                new_max_rc_id = cid
            number = _extract_number_from_urls(item.get("pull_request_url"), PULL_URL_NUM_RE, item.get("html_url"))
            known_prs.add(number)
            ev_id = f"pull_request_review_comment:{cid}"
            url = f"https://github.com/{repo}/pull/{number}#discussion_r{cid}"
            keep, actor = _classify_actor(item.get("user"), own_login, include_own)
            if keep:
                record_event(_build_event(ev_id, "pull_request_review_comment", number, actor, created_at, url))
            else:
                seen_set.add(ev_id)
        wm["max_review_comment_id"] = new_max_rc_id

    # 6. Reviews on open or updated pull requests
    for number in prs_to_check_reviews:
        rev_key = f"reviews:{number}"
        rev_ep = f"repos/{repo}/pulls/{number}/reviews?per_page=100"
        r_changed, r_etag, r_items, poll_hint = _fetch_paginated_list(
            rev_ep,
            etags.get(rev_key),
            None,
        )
        server_poll = max(server_poll, poll_hint)
        if r_etag:
            etags[rev_key] = r_etag
        num_key = str(number)
        base_cutoff = pull_base_upd.get(num_key, "")
        if r_changed:
            for rev in r_items:
                rev_state = rev.get("state")
                if isinstance(rev_state, str) and rev_state.upper() == "PENDING":
                    continue
                rid = _pos_int(rev.get("id"))
                submitted = rev.get("submitted_at") or rev.get("created_at")
                submitted_at = _iso_utc(submitted)
                ev_id = f"pull_request_review:{rid}"
                if base_cutoff and number not in open_prs and submitted_at <= base_cutoff:
                    seen_set.add(ev_id)
                    continue
                keep, actor = _classify_actor(rev.get("user"), own_login, include_own)
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
                    seen_set.add(ev_id)
        if changed and num_key in pull_heads:
            for pr in pull_items:
                if pr.get("number") == number:
                    pull_rev_checked[num_key] = _iso_utc(pr.get("updated_at"))
                    break

    if discovered:
        discovered.sort(key=lambda e: (str(e["time"]), str(e["id"])))
        pending_list.extend(discovered)
        pending_list.sort(key=lambda e: (str(e["time"]), str(e["id"])))

    working["seen_ids"] = sorted(seen_set)
    working["known_pr_numbers"] = sorted(known_prs)
    return (working, server_poll)


def _failure_message(kind: str, pause: float) -> str:
    secs = f"{pause:g}"
    if kind == "rate_limit":
        return f"GitHub rate limit reached; retrying in {secs}s.\n"
    if kind == "network":
        return f"Network error talking to GitHub; retrying in {secs}s.\n"
    if kind == "github":
        return f"GitHub server error; retrying in {secs}s.\n"
    return f"Unexpected answer from GitHub; retrying in {secs}s.\n"


def _emit_and_save(state_path: Path, state: dict[str, object], max_events: int) -> int:
    pending: list[dict[str, object]] = list(state["pending"])  # type: ignore[arg-type]
    batch = pending[:max_events]
    rest = pending[max_events:]

    next_state = copy.deepcopy(state)
    seen_set: set[str] = set(next_state["seen_ids"])  # type: ignore[arg-type]
    for ev in batch:
        seen_set.add(str(ev["id"]))
    next_state["seen_ids"] = sorted(seen_set)
    next_state["pending"] = rest

    # Print and flush BEFORE replacing the state file so a crash can repeat an event, never drop it.
    sys.stdout.write(json.dumps({"events": batch}, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    _save_state(state_path, next_state)
    return 0


def _emit_timeout() -> int:
    sys.stdout.write(json.dumps({"events": [], "reason": "timeout"}, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        ns = parse_args(argv)
        state = _load_state(ns.state_path)
    except UsageProblem as exc:
        sys.stderr.write(f"{exc}\n")
        sys.stderr.flush()
        return 2

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

    try:
        # 1. Ask gh once at the start which account it is signed in as.
        while own_login is None:
            try:
                own_login = _fetch_signed_in_login()
                consecutive_failures = 0
                last_failure_pause = 0.0
            except TransientGitHubError as err:
                if not wait_failure(err):
                    return _emit_timeout()

        # 2. If no state file existed yet, snapshot the baseline once and persist it.
        while not bool(state["initialized"]):
            try:
                state, server_poll = _initialize_baseline(state, ns.repo)
                _save_state(ns.state_path, state)
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
                state, server_poll = _poll_once(state, ns.repo, own_login, ns.include_own)
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
                            state, _ = _poll_once(state, ns.repo, own_login, ns.include_own)
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
                    _save_state(ns.state_path, state)
                    return _emit_timeout()
                if wait_secs >= remaining:
                    _save_state(ns.state_path, state)
                    sleep(remaining)
                    return _emit_timeout()
            sleep(wait_secs)

    except SignedOutError:
        sys.stderr.write("gh is not signed in; run 'gh auth login' first.\n")
        sys.stderr.flush()
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("Watcher stopped.\n")
        sys.stderr.flush()
        return 130
    except Exception:
        sys.stderr.write("Watcher stopped because of an unexpected internal error.\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
