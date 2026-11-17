"""Pilot runner: the official Claude Code agent, through Harbor, on one source task per language.

Runs go one language at a time with the owner's subscription token. Before each run the runner reads
the agent's own rate-limit telemetry (the `rate_limit_event` lines of Claude Code's stream-json
output) from the previous run and refuses to start when the five-hour or the weekly window is at or
above its limit, when a window is rejected, or when the account has begun to use extra paid usage.
Because the agent does not report the five-hour window, a guard also caps this runner's own agent
minutes per rolling five hours; that is a guard, not a measurement.
The token is read here and handed to Harbor only through the child process environment. After every
run, and at the end of the batch whatever happened, the output is scanned for it; a file the scan cannot
read fails the scan, and reports never show the token. Harbor runs in its own process group: at the
time limit or on Ctrl-C it is asked to stop, then its group is ended, its containers are removed and
the batch stops. Results go to out/pilot/<UTC timestamp>/.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
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
    """The token was found in the output, or the scan could not read everything: the batch fails."""


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
    """Utilization as a percent. Claude Code writes a fraction (0.25 meant 25% in the first real run), so
    the value is always read as one: anything above 1 is past the limit, never a small percent."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or math.isnan(raw) or raw < 0:
        return None
    return float(raw) * 100.0


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
        pct = normalize_percent(w.get("utilization"))
        if pct is None:
            pct = w.get("utilization_pct")
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
TIMEOUT_RETURNCODE = 124
# Harbor stops and removes the agent's and the verifier's containers when it is interrupted; give it time.
STOP_GRACE_SECONDS = 300.0
TERM_GRACE_SECONDS = 60.0
# Room above the task's own agent and judge limits for building the environments and installing the agent.
SETUP_MARGIN_SECONDS = 1800


def harbor_time_limit(task) -> float:
    """Longer than Harbor may take by its own limits: each step's agent and judge time-outs, plus room
    for building and starting the environments and installing the agent."""
    per_step = task.limits["agent_timeout_sec"] + task.limits["verifier_timeout_sec"]
    return float(per_step * len(task.steps) + SETUP_MARGIN_SECONDS)


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def stop_process_group(proc: subprocess.Popen, grace: float, term_grace: float) -> None:
    """Ask Harbor to stop as Ctrl-C does, so it removes its containers; then SIGTERM, which Harbor also
    turns into a clean stop; then kill whatever is left of its process group."""
    for sig, wait in ((signal.SIGINT, grace), (signal.SIGTERM, term_grace)):
        _signal_group(proc, sig)
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    _signal_group(proc, signal.SIGKILL)  # anything Harbor left running in its group
    proc.wait()


def run_subprocess(cmd: list[str], env: dict[str, str], log_path: Path, timeout: float, *,
                   grace: float = STOP_GRACE_SECONDS, term_grace: float = TERM_GRACE_SECONDS) -> int:
    """Run Harbor in its own process group. At the time limit, or on Ctrl-C, stop the whole group."""
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.write(b"\npilot: harbor did not finish within the time limit; stopping it and its processes\n")
            log.flush()
            stop_process_group(proc, grace, term_grace)
            return TIMEOUT_RETURNCODE
        except BaseException:
            log.write(b"\npilot: interrupted; stopping harbor and its processes\n")
            log.flush()
            stop_process_group(proc, grace, term_grace)
            raise


def image_present(image: str) -> None:
    result = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if result.returncode != 0:
        raise PilotError(f"the image {image} is not in Docker; build it from bench/images (see its README)")


def compose_prefix(task_folder_name: str) -> str:
    """Harbor names each Docker compose project after the trial: the task folder name, two underscores and
    a random id, in lower case, with a suffix for the agent's or the verifier's environment. Every project
    of a run of this task folder starts with this."""
    return re.sub(r"[^a-z0-9_-]", "-", task_folder_name.lower()) + "__"


class DockerContainers:
    """The containers and networks Harbor's Docker environments create, found by compose project."""

    def _by_project(self, kind: str) -> dict[str, list[str]]:
        cmd = ["docker", kind, "ls", "-a"] if kind == "container" else ["docker", kind, "ls"]
        cmd += ["--filter", "label=com.docker.compose.project",
                "--format", '{{.ID}} {{.Label "com.docker.compose.project"}}']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise PilotError(f"cannot list Docker {kind}s: {result.stderr.strip()[:200]}")
        projects: dict[str, list[str]] = {}
        for line in result.stdout.splitlines():
            ident, _, project = line.strip().partition(" ")
            if ident and project:
                projects.setdefault(project, []).append(ident)
        return projects

    def leftovers(self, prefix: str) -> list[str]:
        return sorted(p for p in self._by_project("container") if p.startswith(prefix))

    def remove(self, prefix: str) -> list[str]:
        """Remove the containers, then the networks, of every project with this prefix; return the
        projects that still have containers."""
        for project, ids in self._by_project("container").items():
            if project.startswith(prefix):
                subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=300)
        for project, ids in self._by_project("network").items():
            if project.startswith(prefix):
                subprocess.run(["docker", "network", "rm", *ids], capture_output=True, timeout=120)
        return self.leftovers(prefix)


# --- the leak scan and the verifier check ----------------------------------------------------------------------

TOKEN_MASK = "<token>"


def mask(text: str, token: str) -> str:
    """The text with the token replaced, for anything that is printed or written."""
    return text.replace(token, TOKEN_MASK) if token else text


def _file_has(path: Path, needle: bytes) -> bool | None:
    """Whether the file's bytes contain the needle; None when the file cannot be read."""
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
        return None
    return False


def _scan(root: Path, token: str) -> tuple[list[Path], list[Path]]:
    """The paths whose name or bytes hold the token, and the files and folders that could not be read."""
    needle = token.encode("utf-8")
    hits: list[Path] = []
    unchecked: list[Path] = []
    if not root.exists() and not root.is_symlink():
        return hits, unchecked
    if not root.is_dir():
        if token in root.name:
            hits.append(root)
        elif not root.is_symlink():
            found = _file_has(root, needle)
            (unchecked if found is None else hits if found else []).append(root)
        return hits, unchecked

    def could_not_open(err: OSError) -> None:
        unchecked.append(Path(err.filename) if err.filename else root)

    for dirpath, dirnames, filenames in os.walk(root, onerror=could_not_open):
        for name in dirnames + filenames:
            if token in name:
                hits.append(Path(dirpath, name))
        for name in filenames:
            path = Path(dirpath, name)
            if token in name or path.is_symlink() or not path.is_file():
                continue  # links point elsewhere; pipes and sockets hold no bytes
            found = _file_has(path, needle)
            if found is None:
                unchecked.append(path)
            elif found:
                hits.append(path)
    return sorted(set(hits)), sorted(set(unchecked))


def scan_for_token(root: Path, token: str) -> list[Path]:
    """Every path under root (or root itself) whose name or bytes contain the token. For tests and
    internal checks only: these paths may hold the token, so report them through check_for_token."""
    return _scan(Path(root), token)[0]


@dataclass
class LeakScan:
    hits: list[str]
    unchecked: list[str]

    @property
    def clean(self) -> bool:
        return not self.hits and not self.unchecked

    def as_dict(self) -> dict:
        return {"clean": self.clean, "hits": self.hits, "unchecked": self.unchecked}

    def message(self, where: str) -> str:
        parts = []
        if self.hits:
            parts.append(f"the token was found in {len(self.hits)} file(s) {where}: " + ", ".join(self.hits))
        if self.unchecked:
            parts.append(f"{len(self.unchecked)} file(s) or folder(s) {where} could not be read, so the token "
                         "could not be ruled out: " + ", ".join(self.unchecked))
        return "; ".join(parts)


def check_for_token(root: Path, token: str) -> LeakScan:
    """The scan of root, with paths relative to it and the token masked in every one of them."""
    root = Path(root)
    base = root if root.is_dir() else root.parent
    hits, unchecked = _scan(root, token)

    def show(path: Path) -> str:
        try:
            text = str(path.relative_to(base))
        except ValueError:
            text = str(path)
        return mask(text, token)

    return LeakScan([show(p) for p in hits], [show(p) for p in unchecked])


def assert_no_leak(root: Path, token: str, where: str = "in the output") -> None:
    scan = check_for_token(root, token)
    if not scan.clean:
        raise TokenLeak(scan.message(where))


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
    """Anything that would hand the judging environment a credential or a way out: a verifier that is not
    in its own environment or has network, environment settings for the verifier in the task or in a
    trial config, or credential names in the files the verifier gets."""
    task_dir = Path(task_dir)
    config = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    problems = _verifier_envs(config, "task.toml")
    verifier = config.get("verifier") if isinstance(config.get("verifier"), dict) else {}
    if verifier.get("environment_mode") != "separate":
        problems.append('task.toml: the verifier must run in its own environment (environment_mode = "separate")')
    if verifier.get("network_mode") != "no-network":
        problems.append('task.toml: the verifier must have no network (network_mode = "no-network")')
    for tests in [task_dir / "tests", *sorted((task_dir / "steps").glob("*/tests"))]:
        for path in sorted(p for p in tests.rglob("*") if p.is_file()):
            data = path.read_bytes()
            if any(name.encode() in data for name in CREDENTIAL_NAMES):
                problems.append(f"{path.relative_to(task_dir)}: names a credential variable")
    if job_dir is not None:
        for trial_config in sorted(Path(job_dir).rglob("config.json")):
            try:
                problems += _verifier_envs(json.loads(trial_config.read_text(encoding="utf-8")),
                                           str(trial_config.relative_to(job_dir)))
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
    """The rate-limit infos (in step order), model, Claude Code version, summed usage and the agent's own
    error results of the agent logs."""
    files = sorted(Path(job_dir).glob("*/steps/*/agent/claude-code.txt")) + sorted(Path(job_dir).glob("*/agent/claude-code.txt"))
    order = {name: i for i, name in enumerate(step_order or [])}
    files.sort(key=lambda p: (order.get(p.parent.parent.name, len(order)), p.parent.parent.name))
    out = {"rate_limit_infos": [], "model": None, "claude_code_version": None, "errors": [],
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
                subtype = str(obj.get("subtype") or "")
                if obj.get("is_error") is True or subtype.startswith("error"):
                    out["errors"].append({"step": path.parent.parent.name, "subtype": subtype or None,
                                          "is_error": obj.get("is_error")})
    return out


# --- the five-hour guard -----------------------------------------------------------------------------------------------

FIVE_HOURS = 5 * 3600
DEFAULT_GUARD_MINUTES = 30.0
GUARD_NOTE = ("a guard on this runner's own agent time per rolling five hours, not a measurement of the "
              "account's five-hour usage: the agent does not report that window, and the owner's own use "
              "of the account is not counted")


def agent_minutes(runs: Iterable[dict], now: float) -> float:
    """This runner's agent minutes in the five hours before now."""
    return sum(float(r.get("agent_seconds") or 0) for r in _inside(runs, now)) / 60.0


def _inside(runs: Iterable[dict], now: float) -> list[dict]:
    out = []
    for r in runs:
        try:
            started = float(r["started_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - FIVE_HOURS < started <= now:
            out.append(r)
    return out


def guard_decision(runs: Iterable[dict], limit_minutes: float, now: float) -> Decision:
    """Start only while this runner's agents worked less than the limit in the last five hours."""
    inside = sorted(_inside(runs, now), key=lambda r: float(r["started_at"]))
    used = sum(float(r.get("agent_seconds") or 0) for r in inside) / 60.0
    if used < limit_minutes:
        return Decision(True)
    remaining, wait = used, None
    for r in inside:  # the time when enough of the oldest runs have left the window
        remaining -= float(r.get("agent_seconds") or 0) / 60.0
        wait = float(r["started_at"]) + FIVE_HOURS
        if remaining < limit_minutes:
            break
    return Decision(False, f"five-hour guard: this runner's agents worked {used:.1f} minutes in the last five "
                           f"hours, at or above the {limit_minutes:g}-minute limit ({GUARD_NOTE})", wait)


def start_decision(windows: dict[str, dict], runs: list[dict], max_five_hour: float, max_weekly: float,
                   guard_minutes: float, now: float) -> Decision:
    stops = [d for d in (decide(windows, max_five_hour, max_weekly, now), guard_decision(runs, guard_minutes, now))
             if not d.start]
    if not stops:
        return Decision(True)
    wait = None if any(d.wait_until is None for d in stops) else max(d.wait_until for d in stops)
    return Decision(False, "; ".join(d.reason for d in stops), wait)


# --- state between runs and the batch ---------------------------------------------------------------------------------

def _read_state(out_root: Path) -> dict:
    try:
        data = json.loads((Path(out_root) / STATE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_state(out_root: Path) -> dict[str, dict]:
    windows = _read_state(out_root).get("windows")
    return windows if isinstance(windows, dict) else {}


def load_runs(out_root: Path) -> list[dict]:
    runs = _read_state(out_root).get("runs")
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []


def save_state(out_root: Path, windows: dict[str, dict], timestamp: str, runs: list[dict] | None = None,
               now: float | None = None) -> None:
    Path(out_root).mkdir(parents=True, exist_ok=True)
    kept = _inside(runs or [], now) if now is not None else list(runs or [])
    (Path(out_root) / STATE_FILE).write_text(
        json.dumps({"updated_by_batch": timestamp, "windows": windows, "runs": kept}, indent=2), encoding="utf-8")


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_record(run_dir: Path, batch: Path, record: dict) -> None:
    record["record"] = str((run_dir / "record.json").relative_to(batch))
    _write_json(run_dir / "record.json", record)


def _summary_entry(record: dict) -> dict:
    keys = ("language", "status", "reason", "reward", "steps", "wall_seconds", "rate_limits", "five_hour_guard",
            "record")
    entry = {k: record.get(k) for k in keys if k in record}
    entry["steps"] = [{"step": s["step"], "reward": s["reward"]} for s in record.get("steps", [])]
    entry["rate_limits"] = {k: record["rate_limits"][k] for k in ("before", "first", "after", "unreported")
                            if k in record["rate_limits"]}
    return entry


def _run_agent_seconds(record: dict) -> float:
    """The agent time a run used, for the guard: the steps' agent time when known, else the whole run."""
    known = [s["agent_seconds"] for s in record.get("steps", []) if s.get("agent_seconds") is not None]
    return float(sum(known)) if known else float(record.get("wall_seconds") or 0.0)


def run_pilot(task_path: Path, languages: list[str], out_root: Path, token_file: Path, harbor: Path,
              claude_version: str, max_five_hour: float = DEFAULT_MAX_PERCENT, max_weekly: float = DEFAULT_MAX_PERCENT,
              wait_for_reset: bool = False, model: str | None = None, base_env: dict[str, str] | None = None,
              now: Callable[[], float] = time.time, timestamp: str | None = None,
              image_check: Callable[[str], None] = image_present, runner: Runner = run_subprocess,
              sleep: Callable[[float], None] = time.sleep, timeout: float | None = None,
              allow_hosts: Iterable[str] = DEFAULT_AGENT_HOSTS,
              max_agent_minutes_per_5h: float = DEFAULT_GUARD_MINUTES, docker=None) -> dict:
    for name, value in (("five-hour", max_five_hour), ("weekly", max_weekly)):
        if not 0 < value <= 100:
            raise PilotError(f"the {name} limit must be a percent above 0 and at most 100, not {value:g}")
    if not max_agent_minutes_per_5h > 0:
        raise PilotError(f"the five-hour guard must allow some agent minutes, not {max_agent_minutes_per_5h:g}")
    task = load_task(Path(task_path))
    for lang in languages:
        if lang not in LANGUAGES or lang not in task.languages:
            raise PilotError(f"task {task.id} does not run in {lang}")
    docker = docker if docker is not None else DockerContainers()
    timeout = harbor_time_limit(task) if timeout is None else timeout
    token = read_token(token_file)
    env = child_env(dict(os.environ if base_env is None else base_env), token)
    timestamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = Path(out_root)
    batch = out_root / timestamp
    batch.mkdir(parents=True, exist_ok=True)
    windows = load_state(out_root)
    runs = load_runs(out_root)
    summary = {"timestamp": timestamp, "task": task.id, "languages": languages, "claude_code_pinned": claude_version,
               "model": model or "agent default", "agent_hosts": list(allow_hosts), "time_limit_seconds": timeout,
               "limits": {"max_five_hour_percent": max_five_hour, "max_weekly_percent": max_weekly,
                          "wait_for_reset": wait_for_reset,
                          "five_hour_guard": {"max_agent_minutes": max_agent_minutes_per_5h, "note": GUARD_NOTE}},
               "runs": []}
    stopped: str | None = None
    try:
        for lang in languages:
            before = json.loads(json.dumps(windows))
            if stopped is None:
                decision = start_decision(windows, runs, max_five_hour, max_weekly, max_agent_minutes_per_5h, now())
                waits = 0
                while not decision.start and wait_for_reset and decision.wait_until and waits < MAX_WAITS:
                    sleep(max(0.0, decision.wait_until - now()) + RESET_MARGIN_SECONDS)
                    waits += 1
                    decision = start_decision(windows, runs, max_five_hour, max_weekly, max_agent_minutes_per_5h,
                                              now())
                if not decision.start:
                    when = (f"; it clears at {utc_text(decision.wait_until)}" if decision.wait_until
                            else "; the reset time is unknown")
                    stopped = (decision.reason + when + "; not started (run again after the reset, "
                               "or pass --wait-for-reset to wait for it)")
            if stopped is None:
                left = docker.leftovers(compose_prefix(f"{task.id}__{lang}"))
                if left:
                    stopped = ("containers of an earlier run of this task and language are still in Docker ("
                               + ", ".join(left) + "); not started until they are removed")
            if stopped is not None:
                record = {"language": lang, "status": "not-started", "reason": stopped, "reward": None, "steps": [],
                          "rate_limits": {"before": before, "first": {}, "after": before}}
                summary["runs"].append(_summary_entry(record))
                continue
            minutes_before = agent_minutes(runs, now())
            started_at, t0 = now(), time.monotonic()
            try:
                record = run_one(task, lang, batch, env, token, harbor, claude_version, model, before, runner,
                                 image_check, timeout, now, allow_hosts, docker)
            except BaseException:
                # whatever stopped the run, its time still counts for the guard and its record is listed
                runs.append({"batch": timestamp, "language": lang, "started_at": started_at,
                             "agent_seconds": round(time.monotonic() - t0, 1)})
                save_state(out_root, windows, timestamp, runs, now())
                try:
                    summary["runs"].append(_summary_entry(
                        json.loads((batch / lang / "record.json").read_text(encoding="utf-8"))))
                except (OSError, ValueError, KeyError):
                    pass
                raise
            runs.append({"batch": timestamp, "language": lang, "started_at": started_at,
                         "agent_seconds": _run_agent_seconds(record)})
            windows = {**windows, **record["rate_limits"]["after"]}
            save_state(out_root, windows, timestamp, runs, now())
            record["five_hour_guard"] = {"note": GUARD_NOTE, "window_hours": 5,
                                         "limit_minutes": max_agent_minutes_per_5h,
                                         "minutes_before": round(minutes_before, 1),
                                         "minutes_after": round(agent_minutes(runs, now()), 1)}
            _write_record(batch / lang, batch, record)
            summary["runs"].append(_summary_entry(record))
            _write_json(batch / "summary.json", summary)
            if record["status"] == "timeout":
                stopped = (f"the {lang} run hit the time limit of {timeout:g} s; Harbor was stopped and its "
                           "containers removed; later languages were not started")
    finally:
        _write_json(batch / "summary.json", summary)
        # Last, whatever happened: everything the batch left behind, the records, the summary and the state.
        assert_no_leak(batch, token, "in the batch folder")
        assert_no_leak(out_root / STATE_FILE, token, "in the state file")
    return summary


def run_one(task, lang: str, batch: Path, env: dict[str, str], token: str, harbor: Path, claude_version: str,
            model: str | None, before: dict, runner: Runner, image_check: Callable[[str], None], timeout: float,
            now: Callable[[], float], allow_hosts: Iterable[str] = DEFAULT_AGENT_HOSTS, docker=None) -> dict:
    docker = docker if docker is not None else DockerContainers()
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
    job_dir = jobs_dir / job_name
    cmd = harbor_command(harbor, task_dir, jobs_dir, job_name, claude_version, model, allow_hosts=allow_hosts)
    record: dict = {"language": lang, "task": task.id, "image": image, "harbor_job": str(job_dir),
                    "harbor_returncode": None, "wall_seconds": None, "time_limit_seconds": timeout,
                    "claude_code_version": {"pinned": claude_version, "observed": None},
                    "model": model, "usage": None, "steps": [], "reward": None, "error": None, "status": None,
                    "leak_scan": None, "containers_left": [], "verifier_env_problems": [],
                    "rate_limits": {"before": before, "first": {}, "after": {}, "unreported": list(REGULATED_WINDOWS),
                                    "events": []}}
    started = time.monotonic()
    finished = False
    try:
        record["harbor_returncode"] = runner(cmd, env, run_dir / "harbor.log", timeout)
        finished = True
    finally:
        record["wall_seconds"] = round(time.monotonic() - started, 1)
        if not finished or record["harbor_returncode"] == TIMEOUT_RETURNCODE:
            # Harbor was cut short and may have left its containers running: remove them first, so nothing
            # keeps writing into the output or using the subscription, and the scan sees the final output.
            try:
                record["containers_left"] = docker.remove(compose_prefix(task_dir.name))
            except Exception as err:
                record["containers_left"] = [mask(f"could not check: {type(err).__name__}: {err}", token)]
        scan = check_for_token(run_dir, token)
        record["leak_scan"] = scan.as_dict()
        if not scan.clean:
            record["status"] = "token-leak" if scan.hits else "leak-scan-failed"
        elif not finished:
            record["status"] = "interrupted"
        if record["status"]:
            _write_record(run_dir, batch, record)
        if not scan.clean:
            raise TokenLeak(scan.message(f"in the {lang} run folder"))

    returncode = record["harbor_returncode"]
    status = "ok" if returncode == 0 else "timeout" if returncode == TIMEOUT_RETURNCODE else "harbor-failed"
    try:
        trial = read_trial(job_dir)
        agent = read_agent_output(job_dir, [s.name for s in task.steps])
    except Exception as err:  # anything in Harbor's output this runner cannot read
        if status == "ok":
            status = "no-result"
        record["error"] = mask(f"{type(err).__name__}: {err}", token)
    else:
        record.update(steps=trial["steps"], reward=trial["reward"], error=trial["error"], trial=trial["trial"],
                      harbor_wall_seconds=trial["harbor_wall_seconds"], usage=agent["usage"],
                      model=model or agent["model"], agent_errors=agent["errors"])
        record["claude_code_version"]["observed"] = agent["claude_code_version"]
        record["rate_limits"] = {"before": before, "first": first_windows(agent["rate_limit_infos"]),
                                 "after": latest_windows(agent["rate_limit_infos"]),
                                 "unreported": unreported_windows(latest_windows(agent["rate_limit_infos"])),
                                 "events": agent["rate_limit_infos"]}
        if status == "ok" and (not trial["steps"] or any(s["reward"] is None for s in trial["steps"])):
            status = "incomplete"
        if status in ("ok", "incomplete"):
            # Harbor judges a step even when the agent's command failed, so a reward alone proves nothing.
            if any(s["error"] for s in trial["steps"]) or agent["errors"]:
                status = "agent-error"
            elif trial["error"]:
                status = "trial-error"  # outside the agent, for example the judge's own time limit
    record["verifier_env_problems"] = verifier_env_problems(task_dir, job_dir) if job_dir.is_dir() else []
    if record["verifier_env_problems"]:
        status = "verifier-env"
    record["status"] = status
    _write_record(run_dir, batch, record)
    if record["containers_left"]:
        raise PilotError(f"the {lang} run was stopped but its containers are still in Docker ("
                         + ", ".join(record["containers_left"]) + "); not starting anything else")
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
    p.add_argument("--max-agent-minutes-per-5h", type=float, default=DEFAULT_GUARD_MINUTES,
                   help="five-hour guard: do not start when this runner's agents worked this many minutes or more "
                        "in the last five hours (default 30); a guard, not a measurement of the account")
    p.add_argument("--wait-for-reset", action="store_true", help="wait for the window to reset instead of stopping")
    p.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--harbor", type=Path, default=DEFAULT_HARBOR)
    p.add_argument("--claude-version", help="Claude Code version to pin (default: the host's `claude --version`)")
    p.add_argument("--model", help="model name for the agent (default: the CLI's own default)")
    p.add_argument("--timeout-sec", type=float,
                   help="stop Harbor after this many seconds (default: above the task's own agent and judge limits)")
    p.add_argument("--agent-host", action="append",
                   help="host the agent may reach while it works; repeat for several (default: api.anthropic.com)")
    args = p.parse_args(argv)
    try:
        summary = run_pilot(args.task, args.lang, args.out, args.token_file.expanduser(), args.harbor,
                            args.claude_version or host_claude_version(), args.max_five_hour, args.max_weekly,
                            args.wait_for_reset, args.model, timeout=args.timeout_sec,
                            allow_hosts=args.agent_host or DEFAULT_AGENT_HOSTS,
                            max_agent_minutes_per_5h=args.max_agent_minutes_per_5h)
    except TokenLeak as err:
        print(f"TOKEN LEAK CHECK FAILED: {err}", file=sys.stderr)
        return 3
    except PilotError as err:
        print(f"pilot: {err}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("pilot: interrupted; a running Harbor was stopped, its containers removed and the output scanned",
              file=sys.stderr)
        return 130
    for run in summary["runs"]:
        steps = ", ".join(f"{s['step']}={s['reward']}" for s in run["steps"])
        print(f"{run['language']}: {run['status']}" + (f" ({steps})" if steps else "") + (f" - {run['reason']}" if run.get("reason") else ""))
        unreported = (run.get("rate_limits") or {}).get("unreported")
        if run["status"] != "not-started" and unreported:
            print(f"  note: the agent reported no utilization for {', '.join(unreported)}; there only a rejected "
                  "status and the five-hour guard can stop the next run")
    return 0 if all(r["status"] == "ok" for r in summary["runs"]) else 1


if __name__ == "__main__":
    sys.exit(main())
