"""Pilot runner: the official Claude Code agent, through Harbor, on one source task per language.

Runs go one language at a time with the owner's subscription token. Before each run the runner reads
the agent's own rate-limit telemetry (the `rate_limit_event` lines of Claude Code's stream-json
output) from the previous run and refuses to start when the five-hour or the weekly window is at or
above its limit, when a window is rejected, or when the account has begun to use extra paid usage.
The token is read here, handed to Harbor only through the child process environment, and the whole
output folder is scanned for it after every run. Results go to out/pilot/<UTC timestamp>/.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .convert import to_harbor
from .languages import LANGUAGES
from .task import load_task

BENCH = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_FILE = Path("~/.dodam/claude-token-dodam")
DEFAULT_OUT = BENCH / "out" / "pilot"
DEFAULT_HARBOR = BENCH / ".harbor-venv" / "bin" / "harbor"
DEFAULT_MAX_PERCENT = 50.0
# With the subscription token Harbor cannot tell where the agent sends its requests, so it adds no host
# to the agent's network allowlist; the agent reaches the model only through the hosts named here.
DEFAULT_AGENT_HOSTS = ("api.anthropic.com",)
SCAN_CHUNK = 1 << 20
RESET_MARGIN_SECONDS = 30
MAX_WAITS = 3
STATE_FILE = "state.json"
CREDENTIAL_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Host variables that could steer the agent to another credential or provider.
DROPPED_ENV_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_FORCE_", "OPENAI_", "CODEX_", "GEMINI_",
                        "GOOGLE_API", "AWS_BEARER")


class PilotError(Exception):
    pass


class TokenLeak(PilotError):
    pass


# --- reading the agent's rate-limit telemetry --------------------------------------------------------------

def _json_objects(lines: Iterable[str]) -> Iterable[dict]:
    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        if not text.startswith("{"):
            start = text.find("{")
            if start < 0:
                continue
            text = text[start:]
        try:
            obj = json.loads(text)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def parse_rate_limit_events(lines: Iterable[str]) -> list[dict]:
    """The raw `rate_limit_info` of every well-formed rate_limit_event line, in order."""
    infos = []
    for obj in _json_objects(lines):
        info = obj.get("rate_limit_info")
        if obj.get("type") == "rate_limit_event" and isinstance(info, dict) and isinstance(info.get("status"), str):
            infos.append(info)
    return infos


def normalize_percent(raw) -> float | None:
    """Utilization as a percent. Claude Code's value is a fraction in the logs seen so far (0.35 means 35%);
    anything above 1 is read as already a percent. Exactly 1 is read as 100%, the safe side."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or math.isnan(raw) or raw < 0:
        return None
    return float(raw) * 100.0 if raw <= 1 else float(raw)


def epoch_seconds(raw) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or math.isnan(raw):
        return None
    return raw / 1000.0 if raw > 1e11 else float(raw)


def _window(info: dict) -> dict:
    entry = {"utilization": info.get("utilization"), "resets_at": info.get("resetsAt"), "status": info.get("status")}
    if info.get("isUsingOverage"):
        entry["is_using_overage"] = True
    return entry


def latest_windows(infos: Iterable[dict]) -> dict[str, dict]:
    """The last known values of each window (five_hour, seven_day, ...) after reading the events in order.
    An event names its representative window (often without a utilization) and lists the windows it knows
    in unifiedWindows; both are merged, later values over earlier ones."""
    windows: dict[str, dict] = {}

    def put(name: str, entry: dict) -> None:
        merged = dict(windows.get(name, {}))
        merged.update({k: v for k, v in entry.items() if v is not None})
        merged.setdefault("utilization", None)
        merged.setdefault("resets_at", None)
        merged.setdefault("status", None)
        merged["utilization_pct"] = normalize_percent(merged["utilization"])
        windows[name] = merged

    for info in infos:
        put(str(info.get("rateLimitType") or "unknown"), _window(info))
        unified = info.get("unifiedWindows")
        if isinstance(unified, dict):
            for name, block in unified.items():
                if isinstance(block, dict):
                    put(str(name), _window(block))
    return windows


def first_windows(infos: Iterable[dict]) -> dict[str, dict]:
    """The values each window had in the first event that reported it: the closest thing to the start."""
    first: dict[str, dict] = {}
    for info in infos:
        for name, entry in latest_windows([info]).items():
            first.setdefault(name, entry)
    return first


REGULATED_WINDOWS = ("five_hour", "seven_day")


def unreported_windows(windows: dict[str, dict]) -> list[str]:
    """The regulated windows with no known utilization: their limit cannot be checked."""
    return [name for name in REGULATED_WINDOWS
            if (windows.get(name) or {}).get("utilization_pct") is None
            and normalize_percent((windows.get(name) or {}).get("utilization")) is None]


def utc_text(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the start / stop decision -----------------------------------------------------------------------------

@dataclass
class Decision:
    start: bool
    reason: str = ""
    wait_until: float | None = None


def decide(windows: dict[str, dict], max_five_hour: float, max_weekly: float, now: float) -> Decision:
    """Start only when no known, still-open window is at or above its limit or rejected."""
    reasons: list[str] = []
    resets: list[float | None] = []
    for name in sorted(windows):
        w = windows[name]
        reset = epoch_seconds(w.get("resets_at"))
        if reset is not None and reset <= now:
            continue  # that window has reset since the value was seen
        pct = w.get("utilization_pct")
        if pct is None:
            pct = normalize_percent(w.get("utilization"))
        limit = max_five_hour if name == "five_hour" else max_weekly if name.startswith("seven_day") else None
        if w.get("is_using_overage"):
            reasons.append(f"{name}: the account has begun to use extra paid usage")
        elif w.get("status") == "rejected":
            reasons.append(f"{name}: status is rejected")
        elif limit is not None and pct is not None and pct >= limit:
            reasons.append(f"{name} utilization {pct:g}% is at or above the {limit:g}% limit")
        else:
            continue
        resets.append(reset)
    if not reasons:
        return Decision(True)
    wait = None if any(r is None for r in resets) else max(resets)
    return Decision(False, "; ".join(reasons), wait)


# --- the token and the child process ------------------------------------------------------------------------

def read_token(path: Path) -> str:
    """The bare token in a one-line file only its owner can read. Errors never contain the token."""
    path = Path(path).expanduser()
    try:
        mode = path.stat().st_mode
    except OSError as err:
        raise PilotError(f"cannot read the token file {path}: {err.strerror}") from None
    if mode & 0o077:
        raise PilotError(f"the token file {path} must be readable by its owner only (mode 600)")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise PilotError(f"cannot read the token file {path}") from None
    token = text.rstrip("\r\n")
    if not token or re.search(r"\s", token):
        raise PilotError(f"the token file {path} must hold one bare token on a single line")
    return token


def child_env(base: dict[str, str], token: str) -> dict[str, str]:
    """The environment of the Harbor process: the token and the OAuth flag, no API key of any kind."""
    env = {k: v for k, v in base.items() if not k.startswith(DROPPED_ENV_PREFIXES)}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env["CLAUDE_FORCE_OAUTH"] = "1"
    return env


def parse_claude_version(text: str) -> str:
    m = re.match(r"\s*(\d+\.\d+\.\d+)\b", text)
    if not m:
        raise PilotError("cannot read a version from `claude --version`")
    return m.group(1)


def host_claude_version() -> str:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as err:
        raise PilotError(f"cannot run `claude --version`: {err}") from None
    return parse_claude_version(out)


def harbor_command(harbor: Path, task_dir: Path, jobs_dir: Path, job_name: str, claude_version: str,
                   model: str | None = None, env_type: str = "docker",
                   allow_hosts: Iterable[str] = DEFAULT_AGENT_HOSTS) -> list[str]:
    """The Harbor command. Credentials are never arguments: Harbor reads them from its environment.
    The allowed hosts apply to the agent's phase only; the verifier keeps no network."""
    cmd = [str(harbor), "run", "-p", str(task_dir), "-a", "claude-code", "--ak", f"version={claude_version}",
           "-e", env_type, "-o", str(jobs_dir), "--job-name", job_name, "-n", "1", "--yes", "-q"]
    for host in allow_hosts:
        cmd += ["--allow-agent-host", host]
    if model:
        cmd += ["-m", model]
    return cmd


Runner = Callable[[list[str], dict[str, str], Path, float], int]


def run_subprocess(cmd: list[str], env: dict[str, str], log_path: Path, timeout: float) -> int:
    with open(log_path, "ab") as log:
        try:
            return subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                  timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            log.write(b"\npilot: harbor did not finish within the time limit and was stopped\n")
            return 124


def image_present(image: str) -> None:
    result = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if result.returncode != 0:
        raise PilotError(f"the image {image} is not in Docker; build it from bench/images (see its README)")


# --- the leak scan and the verifier check ----------------------------------------------------------------------

def _file_has(path: Path, needle: bytes) -> bool:
    keep = len(needle) - 1
    tail = b""
    try:
        with open(path, "rb") as f:
            while chunk := f.read(SCAN_CHUNK):
                buf = tail + chunk
                if needle in buf:
                    return True
                tail = buf[-keep:] if keep else b""
    except OSError:
        return False
    return False


def scan_for_token(root: Path, token: str) -> list[Path]:
    """Every file under root (or root itself) whose name or bytes contain the token."""
    root = Path(root)
    needle = token.encode("utf-8")
    hits: list[Path] = []
    if not root.exists():
        return hits
    if root.is_file():
        return [root] if token in root.name or _file_has(root, needle) else []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            if token in name:
                hits.append(Path(dirpath, name))
        for name in filenames:
            path = Path(dirpath, name)
            if token not in name and not path.is_symlink() and _file_has(path, needle):
                hits.append(path)
    return sorted(set(hits))


def assert_no_leak(root: Path, token: str) -> None:
    hits = scan_for_token(root, token)
    if hits:
        raise TokenLeak(f"the token was found in {len(hits)} file(s): " + ", ".join(str(h) for h in hits))


def _verifier_envs(obj, where: str) -> list[str]:
    problems = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "verifier" and isinstance(value, dict) and value.get("env"):
                names = sorted(value["env"]) if isinstance(value["env"], dict) else ["?"]
                problems.append(f"{where}: the verifier has environment variables ({', '.join(names)})")
            problems += _verifier_envs(value, where)
    elif isinstance(obj, list):
        for item in obj:
            problems += _verifier_envs(item, where)
    return problems


def verifier_env_problems(task_dir: Path, job_dir: Path | None = None) -> list[str]:
    """Anything that would hand the judging environment a credential: environment settings for the
    verifier in the task or in a trial config, or credential names in the files the verifier gets."""
    task_dir = Path(task_dir)
    problems = _verifier_envs(tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8")), "task.toml")
    for tests in [task_dir / "tests", *sorted((task_dir / "steps").glob("*/tests"))]:
        for path in sorted(p for p in tests.rglob("*") if p.is_file()):
            data = path.read_bytes()
            if any(name.encode() in data for name in CREDENTIAL_NAMES):
                problems.append(f"{path.relative_to(task_dir)}: names a credential variable")
    if job_dir is not None:
        for config in sorted(Path(job_dir).rglob("config.json")):
            try:
                problems += _verifier_envs(json.loads(config.read_text(encoding="utf-8")), str(config.relative_to(job_dir)))
            except ValueError:
                continue
    return problems


# --- reading what Harbor and the agent wrote ------------------------------------------------------------------------

def _seconds(timing) -> float | None:
    if not isinstance(timing, dict) or not timing.get("started_at") or not timing.get("finished_at"):
        return None
    start = datetime.fromisoformat(timing["started_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(timing["finished_at"].replace("Z", "+00:00"))
    return (end - start).total_seconds()


def _error(info) -> str | None:
    if not info:
        return None
    return f"{info.get('exception_type', 'Error')}: {info.get('exception_message', '')}"


def _trial_result(job_dir: Path) -> Path:
    for path in sorted(Path(job_dir).glob("*/result.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(data, dict) and "trial_name" in data:
            return path
    raise PilotError(f"no trial result under {job_dir}")


def read_trial(job_dir: Path) -> dict:
    """Step rewards, tokens and times of the one trial of a job."""
    path = _trial_result(job_dir)
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = []
    for sr in data.get("step_results") or []:
        rewards = ((sr.get("verifier_result") or {}).get("rewards")) or {}
        agent = sr.get("agent_result") or {}
        steps.append({
            "step": sr.get("step_name"),
            "reward": rewards.get("reward"),
            "rewards": rewards,
            "agent_seconds": _seconds(sr.get("agent_execution")),
            "verifier_seconds": _seconds(sr.get("verifier")),
            "tokens": {"input": agent.get("n_input_tokens"), "cache": agent.get("n_cache_tokens"),
                       "output": agent.get("n_output_tokens"), "cost_usd": agent.get("cost_usd")},
            "error": _error(sr.get("exception_info")),
        })
    final = ((data.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    if final is None and steps:
        final = steps[-1]["reward"]
    return {"trial": data.get("trial_name"), "trial_dir": str(path.parent), "reward": final, "steps": steps,
            "error": _error(data.get("exception_info")),
            "harbor_wall_seconds": _seconds({"started_at": data.get("started_at"), "finished_at": data.get("finished_at")})}


def read_agent_output(job_dir: Path, step_order: list[str] | None = None) -> dict:
    """The rate-limit infos (in step order), model, Claude Code version and summed usage of the agent logs."""
    files = sorted(Path(job_dir).glob("*/steps/*/agent/claude-code.txt")) + sorted(Path(job_dir).glob("*/agent/claude-code.txt"))
    order = {name: i for i, name in enumerate(step_order or [])}
    files.sort(key=lambda p: (order.get(p.parent.parent.name, len(order)), p.parent.parent.name))
    out = {"rate_limit_infos": [], "model": None, "claude_code_version": None,
           "usage": {"input": 0, "cache_read": 0, "cache_creation": 0, "output": 0, "cost_usd": 0.0, "num_turns": 0}}
    usage = out["usage"]
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        out["rate_limit_infos"] += parse_rate_limit_events(text)
        for obj in _json_objects(text):
            if obj.get("type") == "system" and obj.get("subtype") == "init":
                out["model"] = out["model"] or obj.get("model")
                out["claude_code_version"] = out["claude_code_version"] or obj.get("claude_code_version")
            elif obj.get("type") == "result":
                u = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
                usage["input"] += u.get("input_tokens") or 0
                usage["output"] += u.get("output_tokens") or 0
                usage["cache_read"] += u.get("cache_read_input_tokens") or 0
                usage["cache_creation"] += u.get("cache_creation_input_tokens") or 0
                usage["cost_usd"] += obj.get("total_cost_usd") or 0.0
                usage["num_turns"] += obj.get("num_turns") or 0
                if not out["model"] and isinstance(obj.get("modelUsage"), dict) and obj["modelUsage"]:
                    out["model"] = next(iter(obj["modelUsage"]))
    return out


# --- state between runs and the batch ---------------------------------------------------------------------------------

def load_state(out_root: Path) -> dict[str, dict]:
    try:
        data = json.loads((Path(out_root) / STATE_FILE).read_text(encoding="utf-8"))
        windows = data.get("windows")
        return windows if isinstance(windows, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(out_root: Path, windows: dict[str, dict], timestamp: str) -> None:
    Path(out_root).mkdir(parents=True, exist_ok=True)
    (Path(out_root) / STATE_FILE).write_text(
        json.dumps({"updated_by_batch": timestamp, "windows": windows}, indent=2), encoding="utf-8")


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _summary_entry(record: dict) -> dict:
    keys = ("language", "status", "reason", "reward", "steps", "wall_seconds", "rate_limits", "record")
    entry = {k: record.get(k) for k in keys if k in record}
    entry["steps"] = [{"step": s["step"], "reward": s["reward"]} for s in record.get("steps", [])]
    entry["rate_limits"] = {k: record["rate_limits"][k] for k in ("before", "first", "after", "unreported")
                            if k in record["rate_limits"]}
    return entry


def run_pilot(task_path: Path, languages: list[str], out_root: Path, token_file: Path, harbor: Path,
              claude_version: str, max_five_hour: float = DEFAULT_MAX_PERCENT, max_weekly: float = DEFAULT_MAX_PERCENT,
              wait_for_reset: bool = False, model: str | None = None, base_env: dict[str, str] | None = None,
              now: Callable[[], float] = time.time, timestamp: str | None = None,
              image_check: Callable[[str], None] = image_present, runner: Runner = run_subprocess,
              sleep: Callable[[float], None] = time.sleep, timeout: float = 7200.0,
              allow_hosts: Iterable[str] = DEFAULT_AGENT_HOSTS) -> dict:
    task = load_task(Path(task_path))
    for lang in languages:
        if lang not in LANGUAGES or lang not in task.languages:
            raise PilotError(f"task {task.id} does not run in {lang}")
    token = read_token(token_file)
    env = child_env(dict(os.environ if base_env is None else base_env), token)
    timestamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = Path(out_root)
    batch = out_root / timestamp
    batch.mkdir(parents=True, exist_ok=True)
    windows = load_state(out_root)
    summary = {"timestamp": timestamp, "task": task.id, "languages": languages, "claude_code_pinned": claude_version,
               "model": model or "agent default", "agent_hosts": list(allow_hosts),
               "limits": {"max_five_hour_percent": max_five_hour, "max_weekly_percent": max_weekly,
                          "wait_for_reset": wait_for_reset}, "runs": []}
    stopped: str | None = None
    for lang in languages:
        before = json.loads(json.dumps(windows))
        if stopped is None:
            decision = decide(windows, max_five_hour, max_weekly, now())
            waits = 0
            while not decision.start and wait_for_reset and decision.wait_until and waits < MAX_WAITS:
                sleep(max(0.0, decision.wait_until - now()) + RESET_MARGIN_SECONDS)
                waits += 1
                decision = decide(windows, max_five_hour, max_weekly, now())
            if not decision.start:
                when = (f"; the window resets at {utc_text(decision.wait_until)}" if decision.wait_until
                        else "; the reset time is unknown")
                stopped = (decision.reason + when + "; not started (run again after the reset, "
                           "or pass --wait-for-reset to wait for it)")
        if stopped is not None:
            record = {"language": lang, "status": "not-started", "reason": stopped, "reward": None, "steps": [],
                      "rate_limits": {"before": before, "first": {}, "after": before}}
            summary["runs"].append(_summary_entry(record))
            continue
        record = run_one(task, lang, batch, env, token, harbor, claude_version, model, before, runner, image_check,
                         timeout, now, allow_hosts)
        windows = {**windows, **record["rate_limits"]["after"]}
        save_state(out_root, windows, timestamp)
        summary["runs"].append(_summary_entry(record))
        _write_json(batch / "summary.json", summary)
        if not record["leak_scan"]["clean"]:
            raise TokenLeak("the token was found in the run output: " + ", ".join(record["leak_scan"]["hits"]))
    _write_json(batch / "summary.json", summary)
    # Last, everything this batch left behind: Harbor's output, the records, the summary and the state.
    for root in (batch, out_root / STATE_FILE):
        assert_no_leak(root, token)
    return summary


def run_one(task, lang: str, batch: Path, env: dict[str, str], token: str, harbor: Path, claude_version: str,
            model: str | None, before: dict, runner: Runner, image_check: Callable[[str], None], timeout: float,
            now: Callable[[], float], allow_hosts: Iterable[str] = DEFAULT_AGENT_HOSTS) -> dict:
    run_dir = batch / lang
    run_dir.mkdir(parents=True, exist_ok=True)
    image = LANGUAGES[lang].image
    image_check(image)
    task_dir = to_harbor(task, lang, run_dir / "tasks", image)
    problems = verifier_env_problems(task_dir)
    if problems:
        raise PilotError("refusing to run: " + "; ".join(problems))
    jobs_dir = run_dir / "jobs"
    job_name = f"pilot-{lang}-{batch.name}"
    cmd = harbor_command(harbor, task_dir, jobs_dir, job_name, claude_version, model, allow_hosts=allow_hosts)
    started = time.monotonic()
    returncode = runner(cmd, env, run_dir / "harbor.log", timeout)
    wall = time.monotonic() - started
    job_dir = jobs_dir / job_name

    hits = [str(p.relative_to(run_dir)) for p in scan_for_token(run_dir, token)]
    record: dict = {"language": lang, "task": task.id, "image": image, "harbor_job": str(job_dir),
                    "harbor_returncode": returncode, "wall_seconds": round(wall, 1),
                    "claude_code_version": {"pinned": claude_version, "observed": None},
                    "model": model, "usage": None, "steps": [], "reward": None, "error": None,
                    "leak_scan": {"clean": not hits, "hits": hits},
                    "verifier_env_problems": verifier_env_problems(task_dir, job_dir) if job_dir.is_dir() else [],
                    "rate_limits": {"before": before, "first": {}, "after": {}, "unreported": list(REGULATED_WINDOWS),
                                    "events": []}}
    status = "ok" if returncode == 0 else "harbor-failed"
    try:
        trial = read_trial(job_dir)
        agent = read_agent_output(job_dir, [s.name for s in task.steps])
    except PilotError as err:
        status = "harbor-failed" if returncode else "no-result"
        record["error"] = str(err)
    else:
        record.update(steps=trial["steps"], reward=trial["reward"], error=trial["error"], trial=trial["trial"],
                      harbor_wall_seconds=trial["harbor_wall_seconds"], usage=agent["usage"],
                      model=model or agent["model"])
        record["claude_code_version"]["observed"] = agent["claude_code_version"]
        record["rate_limits"] = {"before": before, "first": first_windows(agent["rate_limit_infos"]),
                                 "after": latest_windows(agent["rate_limit_infos"]),
                                 "unreported": unreported_windows(latest_windows(agent["rate_limit_infos"])),
                                 "events": agent["rate_limit_infos"]}
        if status == "ok" and (not trial["steps"] or any(s["reward"] is None for s in trial["steps"])):
            status = "incomplete"
        # Harbor judges a step even when the agent's command failed, so a reward alone proves nothing.
        if status in ("ok", "incomplete") and (trial["error"] or any(s["error"] for s in trial["steps"])):
            status = "agent-error"
    if record["verifier_env_problems"]:
        status = "verifier-env"
    if hits:
        status = "token-leak"
    record["status"] = status
    record["record"] = str((run_dir / "record.json").relative_to(batch))
    _write_json(run_dir / "record.json", record)
    return record


# --- command line ---------------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("task", type=Path)
    p.add_argument("--lang", action="append", required=True, help="language to run; repeat for several, run in the order given")
    p.add_argument("--max-five-hour", type=float, default=DEFAULT_MAX_PERCENT,
                   help="do not start when the last known five-hour utilization is at or above this percent (default 50)")
    p.add_argument("--max-weekly", type=float, default=DEFAULT_MAX_PERCENT,
                   help="the same for the weekly windows (default 50)")
    p.add_argument("--wait-for-reset", action="store_true", help="wait for the window to reset instead of stopping")
    p.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--harbor", type=Path, default=DEFAULT_HARBOR)
    p.add_argument("--claude-version", help="Claude Code version to pin (default: the host's `claude --version`)")
    p.add_argument("--model", help="model name for the agent (default: the CLI's own default)")
    p.add_argument("--timeout-sec", type=float, default=7200.0, help="stop Harbor after this many seconds")
    p.add_argument("--agent-host", action="append",
                   help="host the agent may reach while it works; repeat for several (default: api.anthropic.com)")
    args = p.parse_args(argv)
    try:
        summary = run_pilot(args.task, args.lang, args.out, args.token_file.expanduser(), args.harbor,
                            args.claude_version or host_claude_version(), args.max_five_hour, args.max_weekly,
                            args.wait_for_reset, args.model, timeout=args.timeout_sec,
                            allow_hosts=args.agent_host or DEFAULT_AGENT_HOSTS)
    except TokenLeak as err:
        print(f"TOKEN LEAK: {err}", file=sys.stderr)
        return 3
    except PilotError as err:
        print(f"pilot: {err}", file=sys.stderr)
        return 2
    for run in summary["runs"]:
        steps = ", ".join(f"{s['step']}={s['reward']}" for s in run["steps"])
        print(f"{run['language']}: {run['status']}" + (f" ({steps})" if steps else "") + (f" - {run['reason']}" if run.get("reason") else ""))
        unreported = (run.get("rate_limits") or {}).get("unreported")
        if run["status"] != "not-started" and unreported:
            print(f"  note: the agent reported no utilization for {', '.join(unreported)}; only a rejected status "
                  "can stop the next run on that window")
    return 0 if all(r["status"] == "ok" for r in summary["runs"]) else 1


if __name__ == "__main__":
    sys.exit(main())
