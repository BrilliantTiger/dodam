#!/usr/bin/env python3
"""Watches the project's repository at a steady pace and starts the project's Gemini job, one at a time.

    python3 tools/gemini/watch.py [--settings FILE] [--private DIR] [--tell RUN_ID] [--polls N]

A job starts when the project's own account, known by its numeric id, has put one of three labels on one of
the project's own pull requests or issues: one for a verification look, one for an attack look, one for a
discussion. When the job is done the label is taken off, so that the mark can be put again.

It is started by hand in a plain terminal and runs in that terminal only: when the terminal closes, it
stops, and so does a run of agy it started. It installs nothing that keeps running. With --polls N it
stops after N rounds.

Standard output carries one line per finished job, the job's own result line (fixed words, numbers and the
private answer's path). Standard error carries short notes in fixed words. Neither ever carries text of
the model or of anyone on GitHub.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
STATE_FILE = "watch-state.json"
NOTES_FILE = "watch-notes.log"
WATCH_LOCK = "watch.lock"
STATE_VERSION = 2
# Faults before agy started that a later round can get past: the mark stays and is tried again.
TRY_AGAIN = frozenset({"github", "github_timeout", "too_many_pages", "checkout", "head_moved"})
MAX_REMEMBERED = 5000
MAX_LISTED = 100


def _load_job():
    spec = importlib.util.spec_from_file_location("dodam_gemini_job", HERE / "job.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dodam_gemini_job"] = module
    spec.loader.exec_module(module)
    return module


job = _load_job()


NOTES: list[Path] = []


def note(text: str, keep: bool = False) -> None:
    """A note in fixed words on standard error; with keep, also in the private folder's notes."""
    print(f"watch: {text}", file=sys.stderr, flush=True)
    if keep and NOTES:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        fd = os.open(NOTES[0], os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {text}\n")


class StateError(Exception):
    pass


def empty_state() -> dict[str, object]:
    return {"version": STATE_VERSION, "handled": {}, "ignored": {}}


def load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError() from exc
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        raise StateError()
    for key in ("handled", "ignored"):
        if not isinstance(data.get(key), dict):
            raise StateError()
    return data


def save_state(path: Path, state: dict[str, object]) -> None:
    for key in ("handled", "ignored"):
        entries = state[key]
        assert isinstance(entries, dict)
        while len(entries) > MAX_REMEMBERED:
            del entries[next(iter(entries))]
    part = path.with_name(path.name + ".part")
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(part, path)


def newest_mark(settings, number: int, label: str) -> tuple[int, int] | None:
    """The newest event that put `label` on the item, as (event id, numeric id of the account)."""
    events = job.gh_pages(f"repos/{settings.repo}/issues/{number}/events", settings.max_comment_pages)
    marks = []
    for event in events:
        name = event.get("label", {}).get("name") if isinstance(event.get("label"), dict) else None
        raw_id = event.get("id")
        if event.get("event") == "labeled" and name == label and isinstance(raw_id, int) and not isinstance(raw_id, bool):
            marks.append((raw_id, job.user_id(event.get("actor"))))
    return max(marks) if marks else None


def find_marks(settings, state: dict[str, object]) -> list[tuple[int, str, int, str]]:
    """The marks that start a job, oldest first, as (event id, kind, item number, label)."""
    handled = state["handled"]
    ignored = state["ignored"]
    assert isinstance(handled, dict) and isinstance(ignored, dict)
    found = []
    for kind in job.KINDS:
        label = settings.labels[kind]
        listed = job.gh("GET", f"repos/{settings.repo}/issues?labels={quote(label, safe='')}&state=open&per_page={MAX_LISTED}")
        if not isinstance(listed, list):
            raise job.GitHubError("github")
        for entry in listed:
            number = entry.get("number") if isinstance(entry, dict) else None
            if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                continue
            labels = entry.get("labels") if isinstance(entry.get("labels"), list) else []
            if label not in [lab.get("name") for lab in labels if isinstance(lab, dict)]:
                continue
            try:
                mark = newest_mark(settings, number, label)
                if mark is None:
                    continue
                event_id, actor = mark
                key = str(event_id)
                if key in handled or key in ignored:
                    continue
                if actor != settings.project_account_id:
                    ignored[key] = "account"
                    note(f"ignored kind={kind} number={number} reason=account")
                    continue
                reason = job.eligibility(settings, job.read_item(settings, number))
            except job.SignedOut:
                raise
            except job.GitHubError as exc:
                # One item that cannot be read (for example past the bound of pages) keeps no other item waiting.
                note(f"left out this round kind={kind} number={number} reason={exc.reason}", keep=True)
                continue
            if reason is not None:
                ignored[key] = reason
                note(f"ignored kind={kind} number={number} reason={reason}")
                continue
            found.append((event_id, kind, number, label))
    return sorted(found)


def one_round(settings, private: Path, state_path: Path, state: dict[str, object], tell: str | None) -> int | None:
    """Look once and run the jobs that are due, one after another. Returns an exit code when it must stop."""
    try:
        held_back = job.gate(settings, job.load_job_state(private))
    except job.JobError:
        note("the job's state file is damaged; it is left as it is")
        return job.EXIT_CODES["settings"]
    if held_back == "quota_wait":
        note("waiting for Google's quota window to pass")
        return None
    if held_back == "day_bound":
        note(f"the day's bound of {settings.max_jobs_per_day} jobs is reached")
        return None
    try:
        marks = find_marks(settings, state)
    except job.SignedOut:
        note("the GitHub command-line tool is signed out")
        return job.EXIT_CODES["signed_out"]
    except job.GitHubError as exc:
        note(f"could not read the repository this round ({exc.reason})")
        return None
    finally:
        save_state(state_path, state)
    handled = state["handled"]
    assert isinstance(handled, dict)
    for event_id, kind, number, label in marks:
        try:
            held_back = job.gate(settings, job.load_job_state(private))
        except job.JobError:
            note("the job's state file is damaged; it is left as it is")
            return job.EXIT_CODES["settings"]
        if held_back is not None:
            note("waiting for Google's quota window to pass" if held_back == "quota_wait" else
                 f"the day's bound of {settings.max_jobs_per_day} jobs is reached")
            break
        if not job.job_lock_is_free(private):
            note("another job is running; trying again next round")
            break
        result = job.run_job(kind, number, settings, private, tell)
        status = str(result["status"])
        if status == "busy":
            note("another job is running; trying again next round")
            break
        if not result.get("ran") and status in ("quota", "day_bound"):
            note("the job was held back by its own bounds; trying again later")
            break
        print(json.dumps(result), flush=True)
        if status in ("quota", "day_bound"):
            # The job keeps the day's count and the quota's wait itself; the mark stays for later.
            note("Google's quota is used up; the job is put back" if status == "quota" else
                 f"the day's bound of {settings.max_jobs_per_day} jobs is reached")
            break
        if status in ("refused", "signed_out"):
            save_state(state_path, state)
            note(f"stopping: {status}")
            return job.EXIT_CODES[status]
        reasons = result.get("reasons") or []
        if status == "error" and not result.get("ran") and set(reasons) <= TRY_AGAIN:  # type: ignore[arg-type]
            note(f"left out this round kind={kind} number={number} reason={','.join(map(str, reasons))}", keep=True)
            continue
        handled[str(event_id)] = status
        save_state(state_path, state)
        try:
            job.gh("DELETE", f"repos/{settings.repo}/issues/{number}/labels/{quote(label, safe='')}")
        except job.GitHubError as exc:
            note(f"could not take the label off number={number} ({exc.reason})")
    return None


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch the project's repository and start the Gemini job.")
    parser.add_argument("--settings", type=Path, default=job.DEFAULT_SETTINGS)
    parser.add_argument("--private", type=Path, default=None)
    parser.add_argument("--tell", default=None, help="the coordinator's run, as run_... (Orca)")
    parser.add_argument("--polls", type=int, default=0, help="stop after this many rounds (0: run until stopped)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    job.install_signal_handlers()
    args = parse_args(argv)
    refusal = job.inside_agent_reasons()
    if refusal:
        note("refused: it runs inside a coding agent or a terminal an agent can type into (" + ", ".join(refusal) + ")")
        return job.EXIT_CODES["refused"]
    try:
        settings = job.load_settings(args.settings)
        private = job.prepare_private(args.private or job.default_private())
        tell = job.tell_run(args.tell)
        if args.polls < 0:
            raise job.SettingsError("polls")
    except job.SettingsError as exc:
        note(f"settings: {exc.reason}")
        return job.EXIT_CODES["settings"]
    NOTES.append(private / NOTES_FILE)
    lock = job._acquire(private / WATCH_LOCK)
    if lock is None:
        note("another watcher is using this private folder")
        return job.EXIT_CODES["busy"]
    state_path = private / STATE_FILE
    try:
        state = load_state(state_path)
    except StateError:
        note("the watcher's state file is damaged; it is left as it is")
        return job.EXIT_CODES["settings"]
    try:
        me = job.gh("GET", "user")
    except job.SignedOut:
        note("the GitHub command-line tool is signed out")
        return job.EXIT_CODES["signed_out"]
    except job.GitHubError as exc:
        note(f"could not ask GitHub who is signed in ({exc.reason})")
        return 1
    if job.user_id(me) != settings.project_account_id:
        note("the signed-in GitHub account is not the project's account")
        return job.EXIT_CODES["settings"]
    rounds = 0
    while True:
        stop = one_round(settings, private, state_path, state, tell)
        if stop is not None:
            return stop
        rounds += 1
        if args.polls and rounds >= args.polls:
            return 0
        time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
