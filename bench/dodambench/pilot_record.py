"""The measured values of pilot runs, taken out of the pilot's records for the repository.

A pilot record holds what a run measured next to local details: folder paths, trial names, the command of the
process watch. This script copies the measured values only, field by field. Nothing is copied whole, every
piece of text must have a plain shape (a language key, a version, a model name, a test name), and the result
is refused if any text in it looks like a path or carries one of the local names given to `problems`.

Starts of the same day that are not counted among the runs are written too, apart from them, so that the
file accounts for everything that was started. A record made by an earlier runner lacks what that runner did
not yet do (a watch of the process table, the guard, a first reading); such a value is written as null,
which means "not recorded", never as a zero.

Usage: python -m dodambench.pilot_record <batch folder> [...] [--not-counted <batch folder> ...] --out <file.json>
"""
from __future__ import annotations

import argparse
import getpass
import json
import re
import socket
import sys
from pathlib import Path
from typing import Iterable

from dodambench.languages import LANGUAGES

SCHEMA = 2
STATUSES = ("ok", "failed", "stopped", "skipped")
WINDOWS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")
PLAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:+#-]{0,79}")
VERSION = re.compile(r"[0-9]+(\.[0-9]+){1,3}")
MODEL = re.compile(r"[a-z][a-z0-9.-]{1,60}")
NAME = re.compile(r"[a-z][a-z0-9_-]{0,99}")
CHECK = re.compile(r"[a-z][a-z0-9_-]{0,99}(\.py)?")  # a hidden test, or the file of tests an earlier judge named
STAMP = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z")
# What a judge said about a failed check, kept only when it is one plain sentence about one request.
SAID = re.compile(r"[A-Za-z]+: [A-Z]+ /[A-Za-z0-9._-]+ [a-z ]{1,60}")
PATH_LIKE = re.compile(r"(^|[\s\"'=:(])(/|~|[A-Za-z]:\\)[^\s]*[/\\]|\\|@")


class RecordError(Exception):
    pass


def _number(value, what: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecordError(f"{what} is not a number")
    return value


def _number_or_none(value, what: str):
    return None if value is None else _number(value, what)


def _text(value, shape: re.Pattern, what: str) -> str:
    if not isinstance(value, str) or not shape.fullmatch(value):
        raise RecordError(f"{what} does not have the expected plain shape")
    return value


def _one_of(value, allowed: Iterable[str], what: str) -> str:
    if value not in tuple(allowed):
        raise RecordError(f"{what} is not one of the known values")
    return value


def _count(value, what: str) -> int:
    if not isinstance(value, list):
        raise RecordError(f"{what} is not a list")
    return len(value)


def utc_time(stamp: str) -> str:
    """20261117T080556Z -> 2026-11-17T08:05:56Z."""
    m = STAMP.fullmatch(stamp) if isinstance(stamp, str) else None
    if not m:
        raise RecordError("a batch timestamp does not have the shape of a UTC time")
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}Z"


def _window(windows, name: str, what: str):
    """The weekly figure of one reading: percent and status, or None when the reading has no such window.
    A reading may name the window without a figure; its percent is then None."""
    entry = (windows or {}).get(name)
    if not isinstance(entry, dict):
        return None
    return {"percent": _number_or_none(entry.get("utilization_pct"), f"{what} percent"),
            "status": _text(entry.get("status"), NAME, f"{what} status")}


def _error_kind(error, what: str):
    """The kind of an error (the name before the colon), never its text: the text may hold a command or a path."""
    if error is None:
        return None
    kind = str(error).split(":", 1)[0]
    return kind if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,59}", kind) else "error"


def limit_readings(run_dir: Path | None, step: str):
    """Where in a step the agent reported its limits: for each report, how many of the step's replies of the
    model came before it. None when the step's output is not there to be read."""
    if run_dir is None:
        return None
    found = sorted(run_dir.glob(f"jobs/*/*/steps/{step}/agent/claude-code.txt"))
    if len(found) != 1:
        return None
    replies, readings = 0, []
    for line in found[0].read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            kind = json.loads(line).get("type")
        except ValueError:
            continue
        if kind == "assistant":
            replies += 1
        elif kind == "rate_limit_event":
            readings.append(replies)
    return {"replies": replies, "replies_before_each_reading": readings}


def _step(step: dict, what: str, run_dir: Path | None = None) -> dict:
    rewards = step.get("rewards") or {}
    return {
        "step": _text(step.get("step"), NAME, f"{what} name"),
        "reward": _number_or_none(step.get("reward"), f"{what} reward"),
        "agent_seconds": _number_or_none(step.get("agent_seconds"), f"{what} agent time"),
        "judge_seconds": _number_or_none(step.get("verifier_seconds"), f"{what} judge time"),
        "error": _error_kind(step.get("error"), f"{what} error"),
        "limit_readings": limit_readings(run_dir, step.get("step")) if isinstance(step.get("step"), str) else None,
        "judgement": {_text(k, NAME, f"{what} judgement name"): _number(v, f"{what} judgement {k}")
                      for k, v in sorted(rewards.items())},
    }


def failed_checks(run_dir: Path, steps: list[str]) -> list[dict] | None:
    """The hidden tests a run failed, by name, from the judge's own details; None when a step has no details."""
    out = []
    for step in steps:
        found = sorted(run_dir.glob(f"jobs/*/*/steps/{step}/verifier/details.json"))
        if len(found) != 1:
            return None
        details = json.loads(found[0].read_text(encoding="utf-8"))
        for test in details.get("tests", []):
            if test.get("outcome") == "passed":
                continue
            first = str(test.get("message") or "").splitlines()[0:1]
            said = first[0] if first and SAID.fullmatch(first[0]) else None
            out.append({"judged_at": step,
                        "name": _text(test.get("name"), CHECK, "a failed check's name"),
                        "kind": _text(test.get("kind"), NAME, "a failed check's kind"),
                        "from_step": _text(test.get("step"), NAME, "a failed check's step"),
                        "weakness": None if test.get("weakness") is None
                        else _text(test.get("weakness"), NAME, "a failed check's weakness"),
                        "outcome": _text(test.get("outcome"), NAME, "a failed check's outcome"),
                        "judge_said": said})
    return out


def measured(record: dict, batch_started: str, run_dir: Path | None = None) -> dict:
    """The measured values of one run. Every field is copied by name; nothing else of the record is kept."""
    lang = _one_of(record.get("language"), LANGUAGES, "the language")
    usage = record.get("usage") or {}
    limits = record.get("rate_limits") or {}
    guard = record.get("five_hour_guard")
    watch = record.get("process_watch")
    scan = record.get("leak_scan") or {}
    version = record.get("claude_code_version") or {}
    steps = [_step(s, f"{lang} step", run_dir) for s in record.get("steps", [])]
    events = limits.get("events")

    def count_or_none(key: str, what: str):
        return _count(record[key], what) if key in record and record[key] is not None else None
    out = {
        "language": lang,
        "batch_started_utc": utc_time(batch_started),
        "status": _one_of(record.get("status"), STATUSES, f"{lang} status"),
        "reward": _number_or_none(record.get("reward"), f"{lang} reward"),
        "harbor_returncode": _number_or_none(record.get("harbor_returncode"), f"{lang} return code"),
        "steps": steps,
        "failed_checks": failed_checks(run_dir, [s["step"] for s in steps]) if run_dir else None,
        "wall_seconds": _number(record.get("wall_seconds"), f"{lang} wall time"),
        "harbor_wall_seconds": _number_or_none(record.get("harbor_wall_seconds"), f"{lang} Harbor wall time"),
        "time_limit_seconds": _number_or_none(record.get("time_limit_seconds"), f"{lang} time limit"),
        "turns": _number_or_none(usage.get("num_turns"), f"{lang} turns"),
        "tokens": {name: _number_or_none(usage.get(name), f"{lang} {name} tokens")
                   for name in ("input", "cache_read", "cache_creation", "output")},
        "cost_usd_as_the_cli_counts_it": _number_or_none(usage.get("cost_usd"), f"{lang} cost"),
        "claude_code": {"pinned": _text(version.get("pinned"), VERSION, f"{lang} pinned version"),
                        "observed": _text(version.get("observed"), VERSION, f"{lang} observed version")},
        "model": _text(record.get("model"), MODEL, f"{lang} model"),
        "weekly": {when: _window(limits.get(when), "seven_day", f"{lang} weekly figure {when}")
                   for when in ("before", "first", "after")},
        "windows_not_reported": None if "unreported" not in limits
        else [_one_of(w, WINDOWS, f"{lang} unreported window") for w in limits["unreported"]],
        "limit_events": None if events is None else len(events),
        "limit_events_using_extra_usage": None if events is None
        else sum(1 for e in events if isinstance(e, dict) and e.get("isUsingOverage")),
        "guard": None if not isinstance(guard, dict) else {
            "window_hours": _number(guard.get("window_hours"), f"{lang} guard window"),
            "limit_minutes": _number(guard.get("limit_minutes"), f"{lang} guard limit"),
            "minutes_before": _number(guard.get("minutes_before"), f"{lang} guard minutes before"),
            "minutes_after": _number(guard.get("minutes_after"), f"{lang} guard minutes after")},
        "process_watch": None if not isinstance(watch, dict) else {
            "interval_seconds": _number(watch.get("interval_seconds"), f"{lang} watch interval"),
            "readings": _number(watch.get("samples"), f"{lang} readings"),
            "readings_while_the_agent_worked": _number(watch.get("agent_phase_samples"), f"{lang} agent readings"),
            "readings_with_the_token": _number(watch.get("hit_samples"), f"{lang} readings with the token"),
            "readings_with_the_token_after_the_end": _number_or_none(watch.get("hits_after_end"),
                                                                     f"{lang} readings after the end"),
            "processes_with_the_token": _count(watch.get("hit_pids"), f"{lang} processes with the token"),
            "clean": bool(watch.get("clean")),
            "stopped_harbor": bool(watch.get("stopped_harbor")),
            "error": watch.get("error") is not None,
        },
        "leak_scan": {"clean": bool(scan.get("clean")),
                      "hits": _count(scan.get("hits"), f"{lang} leak hits"),
                      "unchecked": None if "unchecked" not in scan
                      else _count(scan["unchecked"], f"{lang} unchecked files")},
        "containers_left": count_or_none("containers_left", f"{lang} containers left"),
        "judge_environment_problems": count_or_none("verifier_env_problems", f"{lang} judge problems"),
        "agent_errors": count_or_none("agent_errors", f"{lang} agent errors"),
        "missing_steps": count_or_none("missing_steps", f"{lang} missing steps"),
        "run_error": record.get("error") is not None,
    }
    return out


def read_batch(batch_dir: Path) -> tuple[dict, list[dict]]:
    """One batch folder: what the batch was started with, and the measured values of each of its runs."""
    summary = json.loads((batch_dir / "summary.json").read_text(encoding="utf-8"))
    stamp = summary.get("timestamp")
    limits = summary.get("limits") or {}
    languages = [_one_of(lang, LANGUAGES, "a batch language") for lang in summary.get("languages", [])]
    batch = {
        "started_utc": utc_time(stamp),
        "task": _text(summary.get("task"), NAME, "the task"),
        "languages": languages,
        "claude_code_pinned": _text(summary.get("claude_code_pinned"), VERSION, "the pinned version"),
        "model_asked_for": _text(summary.get("model"), PLAIN, "the model asked for"),
        "stop_at_weekly_percent": _number(limits.get("max_weekly_percent"), "the weekly stop"),
        "stop_at_five_hour_percent": _number(limits.get("max_five_hour_percent"), "the five-hour stop"),
        "guard_agent_minutes_per_five_hours": _number_or_none(
            (limits.get("five_hour_guard") or {}).get("max_agent_minutes"), "the guard's limit"),
    }
    runs = []
    for lang in languages:
        run_dir = batch_dir / lang
        record = json.loads((run_dir / "record.json").read_text(encoding="utf-8"))
        if record.get("task") != batch["task"]:
            raise RecordError(f"the {lang} record is for another task than its batch")
        runs.append(measured(record, stamp, run_dir))
    return batch, runs


def collect(batch_dirs: Iterable[Path], not_counted: Iterable[Path] = ()) -> dict:
    """The data file: the batches in time order and one run per language, in the order of the language list.
    Starts that are not counted among the runs come apart from them, in time order, with their batches."""
    batches, runs, other_batches, others = [], [], [], []
    for folder in batch_dirs:
        batch, batch_runs = read_batch(Path(folder))
        batches.append(batch)
        runs.extend(batch_runs)
    for folder in not_counted:
        batch, batch_runs = read_batch(Path(folder))
        other_batches.append(batch)
        others.extend(batch_runs)
    if not runs:
        raise RecordError("no run was read")
    batches.sort(key=lambda b: b["started_utc"])
    other_batches.sort(key=lambda b: b["started_utc"])
    others.sort(key=lambda r: r["batch_started_utc"])
    if {b["started_utc"] for b in batches} & {b["started_utc"] for b in other_batches}:
        raise RecordError("a batch is given both as counted and as not counted")
    tasks = {b["task"] for b in batches + other_batches}
    if len(tasks) != 1:
        raise RecordError("the batches are for different tasks")
    seen = [r["language"] for r in runs]
    if len(set(seen)) != len(seen):
        raise RecordError("a language was run in more than one of the batches")
    order = list(LANGUAGES)
    runs.sort(key=lambda r: order.index(r["language"]))
    return {"schema": SCHEMA, "what": "measured values of pilot runs, copied field by field from the pilot's records",
            "task": tasks.pop(), "date_utc": batches[0]["started_utc"][:10], "batches": batches, "runs": runs,
            "batches_not_counted": other_batches, "starts_not_counted": others}


def _leaves(obj, where: str = ""):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield f"{where}.{key} (name)", key
            yield from _leaves(value, f"{where}.{key}")
    elif isinstance(obj, list):
        for n, value in enumerate(obj):
            yield from _leaves(value, f"{where}[{n}]")
    elif isinstance(obj, str):
        yield where, obj


def problems(data, local_names: Iterable[str] = ()) -> list[str]:
    """Every piece of text in a data file that could be a local detail: a path, an address, or a local name."""
    names = [n.lower() for n in local_names if isinstance(n, str) and len(n) >= 3]
    found = []
    for where, text in _leaves(data):
        low = text.lower()
        if where.endswith(".judge_said"):
            if not SAID.fullmatch(text):
                found.append(f"{where}: not one plain sentence about one request")
        elif where == ".what":
            if not re.fullmatch(r"[A-Za-z ,']{1,120}", text):
                found.append(f"{where}: not plain words")
        elif PATH_LIKE.search(text) or "/" in text or not PLAIN.fullmatch(text):
            found.append(f"{where}: text that is not a plain name, version or time")
        if any(n in low for n in names):
            found.append(f"{where}: carries a local name")
    return found


def local_names() -> list[str]:
    """Names of this machine and its user that must never be written out."""
    names = [socket.gethostname().split(".")[0]]
    try:
        names.append(getpass.getuser())
    except (KeyError, OSError):
        pass
    home = Path.home()
    names.extend(part for part in home.parts[1:] if part not in ("Users", "home"))
    return names


def to_text(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the measured values of pilot runs, and nothing local.")
    parser.add_argument("batches", nargs="+", type=Path, help="batch folders of the pilot (each holds summary.json)")
    parser.add_argument("--not-counted", nargs="*", type=Path, default=[],
                        help="batch folders of starts that are not counted among the runs")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        data = collect(args.batches, args.not_counted)
    except (RecordError, OSError, ValueError) as err:
        print(f"pilot_record: {err}", file=sys.stderr)
        return 1
    found = problems(data, local_names())
    if found:
        print("pilot_record: nothing was written; the values hold local details:", file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(to_text(data), encoding="utf-8")
    print(f"pilot_record: {len(data['runs'])} runs written, {len(data['starts_not_counted'])} starts not counted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
