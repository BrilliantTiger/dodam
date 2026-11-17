"""Pilot runner: usage regulation, the harbor command, token safety and result recording.

None of these tests needs Docker, Harbor or a real token; the token below is a made-up string.
"""
import functools
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dodambench import pilot
from dodambench.convert import to_harbor
from dodambench.task import load_task

TASKS = Path(__file__).resolve().parents[1] / "tasks"
FAKE_TOKEN = "sk-ant-oat01-FAKE0123456789abcdefghijklmnopqrstuvwxyzFAKE"
NOW = 1_790_000_000.0


def event(window="five_hour", utilization=0.1, status="allowed", resets=NOW + 3600, **extra) -> dict:
    info = {"status": status, "resetsAt": resets, "rateLimitType": window, "utilization": utilization}
    info.update(extra)
    return {"type": "rate_limit_event", "rate_limit_info": info, "uuid": "u", "session_id": "s"}


def lines(*events) -> list[str]:
    return [json.dumps(e) if isinstance(e, dict) else e for e in events]


# --- parsing rate_limit_event lines -------------------------------------------------------------

def test_parse_keeps_only_rate_limit_events_and_their_raw_info():
    out = pilot.parse_rate_limit_events(lines(
        {"type": "system", "subtype": "init"},
        event("five_hour", 0.25),
        {"type": "assistant", "message": {}},
        event("seven_day", 0.4, status="allowed_warning"),
    ))
    assert [i["rateLimitType"] for i in out] == ["five_hour", "seven_day"]
    assert out[0]["utilization"] == 0.25
    assert out[1]["status"] == "allowed_warning"


def test_parse_skips_malformed_lines():
    out = pilot.parse_rate_limit_events(lines(
        "", "not json at all", "{broken", "[1, 2]", "null", '{"type": "rate_limit_event"}',
        '{"type": "rate_limit_event", "rate_limit_info": "oops"}',
        '{"type": "rate_limit_event", "rate_limit_info": {"status": 7}}',
        event("five_hour", 0.3),
    ))
    assert len(out) == 1 and out[0]["utilization"] == 0.3


def test_parse_accepts_carriage_returns_and_leading_noise():
    raw = "\x1b[0m" + json.dumps(event("five_hour", 0.2)) + "\r"
    assert len(pilot.parse_rate_limit_events([raw, json.dumps(event("five_hour", 0.2)) + "\r\n"])) == 2


def test_latest_windows_takes_the_last_event_of_each_window():
    windows = pilot.latest_windows(pilot.parse_rate_limit_events(lines(
        event("five_hour", 0.1), event("seven_day", 0.2), event("five_hour", 0.35))))
    assert windows["five_hour"]["utilization"] == 0.35
    assert windows["five_hour"]["utilization_pct"] == pytest.approx(35.0)
    assert windows["seven_day"]["utilization"] == 0.2
    assert windows["five_hour"]["resets_at"] == NOW + 3600
    assert windows["five_hour"]["status"] == "allowed"


def test_latest_windows_reads_the_unified_windows_block():
    info = event("five_hour", 0.1, unifiedWindows={
        "five_hour": {"utilization": 0.6, "resetsAt": 1, "status": "allowed_warning"},
        "seven_day": {"utilization": 0.3, "resetsAt": 2}})["rate_limit_info"]
    windows = pilot.latest_windows([info])
    assert windows["five_hour"]["utilization"] == 0.6 and windows["five_hour"]["status"] == "allowed_warning"
    assert windows["seven_day"]["utilization"] == 0.3 and windows["seven_day"]["resets_at"] == 2


def test_latest_windows_of_nothing_is_empty():
    assert pilot.latest_windows([]) == {}


def test_missing_utilization_stays_unknown():
    windows = pilot.latest_windows([{"status": "allowed", "rateLimitType": "five_hour"}])
    assert windows["five_hour"]["utilization"] is None
    assert windows["five_hour"]["utilization_pct"] is None


@pytest.mark.parametrize("raw,pct", [(0, 0.0), (0.5, 50.0), (0.99, 99.0), (1, 100.0), (1.0, 100.0), (1.2, 120.0),
                                     (37, 3700.0)])
def test_utilization_is_always_read_as_a_fraction(raw, pct):
    # Claude Code writes fractions (0.25 in the real run meant 25%); a value above 1 is past the limit,
    # never a small percent.
    assert pilot.normalize_percent(raw) == pytest.approx(pct)


def test_a_utilization_above_one_is_over_every_limit():
    assert not pilot.decide(win(1.2, 0.1), 100, 100, NOW).start
    assert not pilot.decide(win(0.1, 37), 100, 100, NOW).start


@pytest.mark.parametrize("raw", [None, "x", True, -1, float("nan")])
def test_utilization_that_makes_no_sense_is_unknown(raw):
    assert pilot.normalize_percent(raw) is None


def test_reset_times_in_milliseconds_are_read_as_seconds():
    assert pilot.epoch_seconds(1_790_000_000) == 1_790_000_000
    assert pilot.epoch_seconds(1_790_000_000_000) == 1_790_000_000
    assert pilot.epoch_seconds(None) is None


# --- the start / stop decision -------------------------------------------------------------------

def win(five=None, week=None, five_status="allowed", week_status="allowed", reset_five=NOW + 3600, reset_week=NOW + 86400):
    windows = {}
    if five is not None:
        windows["five_hour"] = {"utilization": five, "utilization_pct": pilot.normalize_percent(five),
                                "resets_at": reset_five, "status": five_status}
    if week is not None:
        windows["seven_day"] = {"utilization": week, "utilization_pct": pilot.normalize_percent(week),
                                "resets_at": reset_week, "status": week_status}
    return windows


def test_first_run_with_nothing_known_starts():
    assert pilot.decide({}, 50, 50, NOW).start


def test_below_both_thresholds_starts():
    assert pilot.decide(win(0.49, 0.49), 50, 50, NOW).start


def test_five_hour_at_the_threshold_stops_with_the_reset_time():
    d = pilot.decide(win(0.5, 0.1), 50, 50, NOW)
    assert not d.start and d.wait_until == NOW + 3600
    assert "five_hour" in d.reason and "50" in d.reason


def test_weekly_at_the_threshold_stops_with_the_weekly_reset_time():
    d = pilot.decide(win(0.1, 0.5), 50, 50, NOW)
    assert not d.start and d.wait_until == NOW + 86400
    assert "seven_day" in d.reason


def test_thresholds_are_options():
    assert pilot.decide(win(0.6, 0.1), 70, 50, NOW).start
    assert not pilot.decide(win(0.1, 0.6), 50, 40, NOW).start


def test_rejected_status_stops_even_below_the_thresholds():
    d = pilot.decide(win(0.1, 0.1, five_status="rejected"), 50, 50, NOW)
    assert not d.start and "rejected" in d.reason and d.wait_until == NOW + 3600


def test_other_weekly_windows_count_as_weekly():
    windows = win(0.1, 0.1)
    windows["seven_day_opus"] = {"utilization": 0.8, "utilization_pct": 80.0, "resets_at": NOW + 5, "status": "allowed"}
    assert not pilot.decide(windows, 50, 50, NOW).start


def test_a_window_that_has_already_reset_no_longer_counts():
    d = pilot.decide(win(0.9, 0.1, reset_five=NOW - 1), 50, 50, NOW)
    assert d.start


def test_using_extra_paid_usage_stops():
    windows = win(0.1, 0.1)
    windows["overage"] = {"utilization": None, "utilization_pct": None, "resets_at": NOW + 10,
                          "status": "allowed", "is_using_overage": True}
    d = pilot.decide(windows, 50, 50, NOW)
    assert not d.start and "extra" in d.reason


def test_unknown_reset_time_gives_no_wait_time():
    d = pilot.decide(win(0.9, 0.1, reset_five=None), 50, 50, NOW)
    assert not d.start and d.wait_until is None


# --- the token and the child environment -----------------------------------------------------------

def token_file(tmp_path: Path, text: str = FAKE_TOKEN + "\n", mode: int = 0o600) -> Path:
    path = tmp_path / "claude-token"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_read_token_strips_the_newline(tmp_path):
    assert pilot.read_token(token_file(tmp_path)) == FAKE_TOKEN


@pytest.mark.parametrize("text", ["", "\n", "two\nlines\n", "has space\n"])
def test_read_token_refuses_files_that_are_not_one_bare_token(tmp_path, text):
    with pytest.raises(pilot.PilotError) as err:
        pilot.read_token(token_file(tmp_path, text))
    assert "two" not in str(err.value) and "space" not in str(err.value)


def test_read_token_refuses_a_file_others_can_read(tmp_path):
    with pytest.raises(pilot.PilotError) as err:
        pilot.read_token(token_file(tmp_path, mode=0o644))
    assert FAKE_TOKEN not in str(err.value)


def test_read_token_names_the_missing_file_without_a_token(tmp_path):
    with pytest.raises(pilot.PilotError):
        pilot.read_token(tmp_path / "nope")


def test_child_env_has_the_token_oauth_flag_and_no_api_key():
    base = {"PATH": "/bin", "HOME": "/h", "ANTHROPIC_API_KEY": "sk-ant-api-x", "ANTHROPIC_AUTH_TOKEN": "t",
            "ANTHROPIC_BASE_URL": "http://x", "CLAUDE_CODE_OAUTH_TOKEN": "old", "CLAUDE_CODE_USE_BEDROCK": "1",
            "OPENAI_API_KEY": "o"}
    env = pilot.child_env(base, FAKE_TOKEN)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN
    assert env["CLAUDE_FORCE_OAUTH"] == "1"
    assert env["PATH"] == "/bin" and env["HOME"] == "/h"
    assert not [k for k in env if k.startswith("ANTHROPIC_")]
    assert "CLAUDE_CODE_USE_BEDROCK" not in env and "OPENAI_API_KEY" not in env
    assert base["ANTHROPIC_API_KEY"] == "sk-ant-api-x"  # the caller's environment is left alone


def test_harbor_command_never_carries_the_token_or_an_api_key(tmp_path):
    cmd = pilot.harbor_command(Path("/x/harbor"), tmp_path / "task", tmp_path / "jobs", "job1", "2.1.326", model=None)
    assert all(FAKE_TOKEN not in a for a in cmd)
    joined = " ".join(cmd)
    assert "OAUTH" not in joined and "API_KEY" not in joined and "--ae" not in cmd and "--ve" not in cmd
    assert cmd[0] == "/x/harbor" and cmd[1] == "run"
    assert cmd[cmd.index("-a") + 1] == "claude-code"
    assert "version=2.1.326" in cmd and cmd[cmd.index("version=2.1.326") - 1] == "--ak"
    assert "--bare" not in joined and "-m" not in cmd
    assert cmd[cmd.index("--job-name") + 1] == "job1"
    assert "--yes" in cmd


def test_harbor_command_lets_the_agent_reach_the_model_host(tmp_path):
    # With the subscription token Harbor cannot guess the agent's endpoint and adds no host to the
    # agent's network allowlist, so every request was dropped (ECONNRESET) until the host was named.
    cmd = pilot.harbor_command(Path("harbor"), tmp_path, tmp_path, "j", "2.1.326")
    assert cmd[cmd.index("--allow-agent-host") + 1] == "api.anthropic.com"
    cmd = pilot.harbor_command(Path("harbor"), tmp_path, tmp_path, "j", "2.1.326", allow_hosts=["a.example", "b.example"])
    hosts = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--allow-agent-host"]
    assert hosts == ["a.example", "b.example"]
    assert "--allow-environment-host" not in cmd  # the verifier and the setup get no extra host


def test_harbor_command_passes_a_model_only_when_asked(tmp_path):
    cmd = pilot.harbor_command(Path("harbor"), tmp_path, tmp_path, "j", "2.1.326", model="claude-sonnet-5-5")
    assert cmd[cmd.index("-m") + 1] == "claude-sonnet-5-5"


def test_the_pinned_version_is_read_from_claude_version_output():
    assert pilot.parse_claude_version("2.1.326 (Claude Code)\n") == "2.1.326"
    with pytest.raises(pilot.PilotError):
        pilot.parse_claude_version("garbage")


# --- the leak scan ------------------------------------------------------------------------------------

def test_scan_finds_the_token_in_any_file_without_printing_it(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "ok.txt").write_text("nothing here")
    (tmp_path / "a" / "b" / "claude-code.txt").write_text("log " + FAKE_TOKEN + " more")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01" + FAKE_TOKEN.encode() + b"\xff")
    hits = pilot.scan_for_token(tmp_path, FAKE_TOKEN)
    assert sorted(p.name for p in hits) == ["bin.dat", "claude-code.txt"]


def test_scan_of_a_clean_tree_finds_nothing_and_reads_the_whole_tree(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.json").write_text(json.dumps({"a": 1}))
    assert pilot.scan_for_token(tmp_path, FAKE_TOKEN) == []


def test_scan_finds_a_token_in_a_file_name(tmp_path):
    (tmp_path / (FAKE_TOKEN + ".txt")).write_text("x")
    assert pilot.scan_for_token(tmp_path, FAKE_TOKEN)


def test_scan_finds_a_token_split_across_the_read_chunks(tmp_path):
    (tmp_path / "big.log").write_bytes(b"a" * (pilot.SCAN_CHUNK - 10) + FAKE_TOKEN.encode() + b"b" * 100)
    assert pilot.scan_for_token(tmp_path, FAKE_TOKEN)


def test_a_token_in_a_name_is_masked_in_every_report(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / (FAKE_TOKEN + ".txt")).write_text("x")
    (tmp_path / ("dir-" + FAKE_TOKEN)).mkdir()
    scan = pilot.check_for_token(tmp_path, FAKE_TOKEN)
    assert not scan.clean
    assert sorted(scan.hits) == ["d/<token>.txt", "dir-<token>"]
    assert FAKE_TOKEN not in json.dumps(scan.as_dict())
    with pytest.raises(pilot.TokenLeak) as err:
        pilot.assert_no_leak(tmp_path, FAKE_TOKEN)
    assert FAKE_TOKEN not in str(err.value) and "<token>.txt" in str(err.value)


skip_as_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads every file")


@skip_as_root
def test_a_file_the_scan_cannot_read_fails_the_scan(tmp_path):
    secret = tmp_path / "from-container.log"
    secret.write_text(FAKE_TOKEN)
    secret.chmod(0)
    try:
        scan = pilot.check_for_token(tmp_path, FAKE_TOKEN)
        assert not scan.clean and scan.hits == [] and scan.unchecked == ["from-container.log"]
        with pytest.raises(pilot.TokenLeak) as err:
            pilot.assert_no_leak(tmp_path, FAKE_TOKEN)
        assert "could not" in str(err.value) and FAKE_TOKEN not in str(err.value)
    finally:
        secret.chmod(0o600)


@skip_as_root
def test_a_folder_the_scan_cannot_open_fails_the_scan(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "inside.log").write_text(FAKE_TOKEN)
    locked.chmod(0)
    try:
        scan = pilot.check_for_token(tmp_path, FAKE_TOKEN)
        assert not scan.clean and scan.unchecked == ["locked"]
    finally:
        locked.chmod(0o700)


def test_assert_no_leak_fails_loudly_without_the_token_in_the_message(tmp_path):
    (tmp_path / "x.log").write_text(FAKE_TOKEN)
    with pytest.raises(pilot.TokenLeak) as err:
        pilot.assert_no_leak(tmp_path, FAKE_TOKEN)
    assert FAKE_TOKEN not in str(err.value) and "x.log" in str(err.value)
    pilot.assert_no_leak(tmp_path / "missing-dir-is-fine", FAKE_TOKEN)


# --- the verifier never gets credentials -----------------------------------------------------------------

def test_a_converted_task_gives_the_verifier_no_environment(tmp_path):
    task_dir = to_harbor(load_task(TASKS / "web-signup"), "python", tmp_path, "dodam-bench/python:dev")
    assert pilot.verifier_env_problems(task_dir) == []


def test_verifier_env_problems_are_found_in_the_task_and_in_the_trial_config(tmp_path):
    task_dir = to_harbor(load_task(TASKS / "web-signup"), "python", tmp_path / "t", "dodam-bench/python:dev")
    toml = task_dir / "task.toml"
    toml.write_text(toml.read_text().replace(
        '[verifier]\n', '[verifier]\nenv = { CLAUDE_CODE_OAUTH_TOKEN = "${CLAUDE_CODE_OAUTH_TOKEN}" }\n'))
    assert pilot.verifier_env_problems(task_dir)
    clean = to_harbor(load_task(TASKS / "web-signup"), "python", tmp_path / "c", "dodam-bench/python:dev")
    job = tmp_path / "job" / "trial"
    job.mkdir(parents=True)
    (job / "config.json").write_text(json.dumps({"agent": {"env": {"A": "b"}}, "verifier": {"env": {"ANTHROPIC_API_KEY": "x"}}}))
    assert pilot.verifier_env_problems(clean, tmp_path / "job")
    (job / "config.json").write_text(json.dumps({"agent": {"env": {"A": "b"}}, "verifier": {"env": {}}}))
    assert pilot.verifier_env_problems(clean, tmp_path / "job") == []


@pytest.mark.parametrize("old,new", [('environment_mode = "separate"', 'environment_mode = "shared"'),
                                     ('network_mode = "no-network"', 'network_mode = "public"')])
def test_the_verifier_must_stay_separate_and_without_network(tmp_path, old, new):
    task_dir = to_harbor(load_task(TASKS / "web-signup"), "python", tmp_path, "dodam-bench/python:dev")
    toml = task_dir / "task.toml"
    head, verifier = toml.read_text().split("[verifier]\n", 1)
    assert old in verifier
    toml.write_text(head + "[verifier]\n" + verifier.replace(old, new, 1))
    problems = pilot.verifier_env_problems(task_dir)
    assert problems and any(new.split(" = ")[0] in p for p in problems)


# --- reading what the agent and Harbor wrote -----------------------------------------------------------------

def make_trial(job_dir: Path, rewards=(1.0, 0.5), tokens=(100, 20, 30), five_status="allowed",
               result_error=False, week=0.1) -> None:
    trial = job_dir / "web-signup__python__abc"
    result = {"task_name": "dodam/web-signup-python", "trial_name": trial.name,
              "agent_info": {"name": "claude-code", "version": "2.1.326", "model_info": None},
              "exception_info": None,
              "started_at": "2026-11-17T07:13:20Z", "finished_at": "2026-11-17T07:20:20Z",
              "verifier_result": {"rewards": {"reward": rewards[-1]}},
              "step_results": []}
    for name, reward in zip(("build", "change-1"), rewards):
        result["step_results"].append({
            "step_name": name,
            "agent_result": {"n_input_tokens": tokens[0], "n_cache_tokens": tokens[1], "n_output_tokens": tokens[2],
                             "cost_usd": 0.5},
            "verifier_result": {"rewards": {"reward": reward, "build_ok": 1.0, "functional_pass_rate": 1.0}},
            "exception_info": None,
            "agent_execution": {"started_at": "2026-11-17T07:13:20Z", "finished_at": "2026-11-17T07:15:20Z"},
            "verifier": {"started_at": "2026-11-17T07:15:20Z", "finished_at": "2026-11-17T07:16:20Z"}})
        agent = trial / "steps" / name / "agent"
        agent.mkdir(parents=True)
        (agent / "claude-code.txt").write_text("\n".join(lines(
            {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "claude_code_version": "2.1.326"},
            event("five_hour", 0.3 if name == "build" else 0.4, status=five_status),
            event("seven_day", week),
            {"type": "result", "subtype": "success", "is_error": result_error, "total_cost_usd": 0.5, "num_turns": 7,
             "usage": {"input_tokens": 10, "output_tokens": 30, "cache_read_input_tokens": 20,
                       "cache_creation_input_tokens": 90}})) + "\n")
    (trial / "result.json").write_text(json.dumps(result))
    (job_dir / "result.json").write_text(json.dumps({"n_total_trials": 1, "stats": {}}))
    (trial / "config.json").write_text(json.dumps({"agent": {}, "verifier": {"env": {}}}))


def test_read_trial_gives_step_rewards_tokens_and_times(tmp_path):
    make_trial(tmp_path)
    trial = pilot.read_trial(tmp_path)
    assert [s["step"] for s in trial["steps"]] == ["build", "change-1"]
    assert [s["reward"] for s in trial["steps"]] == [1.0, 0.5]
    assert trial["steps"][0]["rewards"]["build_ok"] == 1.0
    assert trial["steps"][0]["agent_seconds"] == pytest.approx(120.0)
    assert trial["steps"][0]["verifier_seconds"] == pytest.approx(60.0)
    assert trial["steps"][0]["tokens"] == {"input": 100, "cache": 20, "output": 30, "cost_usd": 0.5}
    assert trial["error"] is None


def test_read_trial_reports_a_step_that_never_got_a_reward(tmp_path):
    make_trial(tmp_path)
    path = next(tmp_path.glob("*/result.json"))
    data = json.loads(path.read_text())
    data["step_results"][1]["verifier_result"] = None
    data["step_results"][1]["exception_info"] = {"exception_type": "AgentTimeoutError", "exception_message": "slow"}
    path.write_text(json.dumps(data))
    trial = pilot.read_trial(tmp_path)
    assert trial["steps"][1]["reward"] is None
    assert trial["steps"][1]["error"] == "AgentTimeoutError: slow"


def test_read_trial_without_any_trial_is_an_error(tmp_path):
    (tmp_path / "result.json").write_text("{}")
    with pytest.raises(pilot.PilotError):
        pilot.read_trial(tmp_path)


def test_read_agent_output_collects_events_model_version_and_usage(tmp_path):
    make_trial(tmp_path)
    out = pilot.read_agent_output(tmp_path)
    assert [i["rateLimitType"] for i in out["rate_limit_infos"]] == ["five_hour", "seven_day"] * 2
    assert out["model"] == "claude-sonnet-5-5"
    assert out["claude_code_version"] == "2.1.326"
    assert out["usage"] == {"input": 20, "cache_read": 40, "cache_creation": 180, "output": 60,
                            "cost_usd": 1.0, "num_turns": 14}


def test_read_agent_output_orders_steps_by_the_task_not_by_name(tmp_path):
    make_trial(tmp_path)
    out = pilot.read_agent_output(tmp_path, step_order=["build", "change-1"])
    assert out["rate_limit_infos"][-2]["utilization"] == 0.4  # change-1 came last


# --- one pilot run, with a fake Harbor ---------------------------------------------------------------------------

class FakeRunner:
    def __init__(self, leak=False, returncode=0, rewards=(1.0, 1.0), stray_leak=False, agent_error=False,
                 name_leak=False, unreadable=False, bad_time=False, interrupt=False, trial_error=False,
                 result_error=False, five_status="allowed", week=0.1, docker=None, containers=()):
        self.calls, self.leak, self.returncode, self.rewards = [], leak, returncode, rewards
        self.stray_leak, self.agent_error = stray_leak, agent_error
        self.name_leak, self.unreadable, self.bad_time, self.interrupt = name_leak, unreadable, bad_time, interrupt
        self.trial_error, self.result_error, self.five_status = trial_error, result_error, five_status
        self.timeouts, self.week, self.docker, self.containers = [], week, docker, list(containers)

    def __call__(self, cmd, env, log_path, timeout):
        self.calls.append((list(cmd), dict(env)))
        self.timeouts.append(timeout)
        jobs = Path(cmd[cmd.index("-o") + 1]) / cmd[cmd.index("--job-name") + 1]
        jobs.mkdir(parents=True)
        if self.docker is not None:
            self.docker.projects += self.containers
        make_trial(jobs, rewards=self.rewards, five_status=self.five_status, result_error=self.result_error,
                   week=self.week)
        token = env["CLAUDE_CODE_OAUTH_TOKEN"]
        path = next(jobs.glob("*/result.json"))
        data = json.loads(path.read_text())
        if self.bad_time:
            data["started_at"] = "not a time"
        if self.trial_error:
            data["exception_info"] = {"exception_type": "VerifierTimeoutError", "exception_message": "judge too slow"}
        path.write_text(json.dumps(data))
        if self.name_leak:
            (jobs / (token + ".txt")).write_text("x")
        if self.unreadable:
            locked = jobs / "from-container.log"
            locked.write_text(token)
            locked.chmod(0)
        if self.interrupt:
            if self.stray_leak:
                (Path(log_path).parents[1] / "stray.log").write_text(token)
            raise KeyboardInterrupt
        if self.agent_error:
            path = next(jobs.glob("*/result.json"))
            data = json.loads(path.read_text())
            for step in data["step_results"]:
                step["exception_info"] = {"exception_type": "UnknownApiError", "exception_message": "Command failed"}
            path.write_text(json.dumps(data))
        if self.leak:
            (jobs / "oops.log").write_text(env["CLAUDE_CODE_OAUTH_TOKEN"])
        if self.stray_leak:  # a file outside the run's own folder but inside the batch
            (Path(log_path).parents[1] / "stray.log").write_text(env["CLAUDE_CODE_OAUTH_TOKEN"])
        Path(log_path).write_text("harbor output\n")
        return self.returncode


class FakeDocker:
    """Containers of Harbor runs, by compose project name."""

    def __init__(self, leftovers=(), stuck=()):
        self.projects = list(leftovers)
        self.stuck = list(stuck)
        self.removed: list[str] = []

    def leftovers(self, prefix):
        return [p for p in self.projects if p.startswith(prefix)]

    def remove(self, prefix):
        gone = [p for p in self.projects if p.startswith(prefix) and p not in self.stuck]
        self.removed += gone
        self.projects = [p for p in self.projects if p not in gone]
        return self.leftovers(prefix)


def pilot_args(tmp_path, **kw):
    args = dict(task_path=TASKS / "web-signup", languages=["python"], out_root=tmp_path / "out",
                token_file=token_file(tmp_path), harbor=Path("harbor"), claude_version="2.1.326",
                max_five_hour=50.0, max_weekly=50.0, wait_for_reset=False, model=None,
                base_env={"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-api-x"},
                now=lambda: NOW, timestamp="20261117T070000Z", image_check=lambda image: None,
                docker=FakeDocker())
    args.update(kw)
    return args


def test_a_run_writes_one_record_and_a_summary(tmp_path):
    runner = FakeRunner()
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path))
    root = tmp_path / "out" / "20261117T070000Z"
    record = json.loads((root / "python" / "record.json").read_text())
    assert record["language"] == "python" and record["task"] == "web-signup"
    assert [(s["step"], s["reward"]) for s in record["steps"]] == [("build", 1.0), ("change-1", 1.0)]
    assert record["reward"] == 1.0
    assert record["wall_seconds"] >= 0
    assert record["claude_code_version"]["pinned"] == "2.1.326"
    assert record["claude_code_version"]["observed"] == "2.1.326"
    assert record["model"] == "claude-sonnet-5-5"
    assert record["usage"]["output"] == 60
    assert record["rate_limits"]["before"] == {}
    assert record["rate_limits"]["after"]["five_hour"]["utilization"] == 0.4
    assert record["rate_limits"]["after"]["seven_day"]["utilization"] == 0.1
    assert len(record["rate_limits"]["events"]) == 4
    assert record["leak_scan"] == {"clean": True, "hits": [], "unchecked": []}
    assert record["verifier_env_problems"] == []
    assert Path(record["harbor_job"]).is_dir()
    assert record["status"] == "ok"
    assert summary["runs"][0]["language"] == "python"
    assert json.loads((root / "summary.json").read_text())["runs"][0]["reward"] == 1.0


def test_the_child_environment_of_a_run_has_the_token_and_no_api_key(tmp_path):
    runner = FakeRunner()
    pilot.run_pilot(runner=runner, **pilot_args(tmp_path))
    cmd, env = runner.calls[0]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_TOKEN and env["CLAUDE_FORCE_OAUTH"] == "1"
    assert "ANTHROPIC_API_KEY" not in env
    assert all(FAKE_TOKEN not in a for a in cmd)


def test_the_token_is_not_in_any_file_the_runner_writes(tmp_path):
    pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path))
    assert pilot.scan_for_token(tmp_path / "out", FAKE_TOKEN) == []


def test_a_leak_is_reported_loudly_and_recorded_without_the_token(tmp_path):
    with pytest.raises(pilot.TokenLeak) as err:
        pilot.run_pilot(runner=FakeRunner(leak=True), **pilot_args(tmp_path))
    assert FAKE_TOKEN not in str(err.value) and "oops.log" in str(err.value)
    record = json.loads((tmp_path / "out" / "20261117T070000Z" / "python" / "record.json").read_text())
    assert record["leak_scan"]["clean"] is False and record["status"] == "token-leak"
    assert pilot.scan_for_token(tmp_path / "out" / "20261117T070000Z" / "python" / "record.json", FAKE_TOKEN) == []


def test_the_whole_batch_is_scanned_after_the_records_are_written(tmp_path):
    with pytest.raises(pilot.TokenLeak) as err:
        pilot.run_pilot(runner=FakeRunner(stray_leak=True), **pilot_args(tmp_path))
    assert FAKE_TOKEN not in str(err.value) and "stray.log" in str(err.value)


def test_a_step_the_agent_failed_is_not_recorded_as_ok(tmp_path):
    # Harbor still judges the step and gives a reward when the agent's command failed (an API error),
    # so a reward alone does not mean the agent ran.
    summary = pilot.run_pilot(runner=FakeRunner(agent_error=True), **pilot_args(tmp_path))
    run = summary["runs"][0]
    assert run["status"] == "agent-error"
    record = json.loads((tmp_path / "out" / "20261117T070000Z" / "python" / "record.json").read_text())
    assert record["steps"][0]["error"] == "UnknownApiError: Command failed"


def test_the_record_keeps_the_values_seen_at_the_start_of_the_run(tmp_path):
    # Nothing is known before the very first run, so the first event of the run stands for the start.
    pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path))
    record = json.loads((tmp_path / "out" / "20261117T070000Z" / "python" / "record.json").read_text())
    assert record["rate_limits"]["first"]["five_hour"]["utilization"] == 0.3
    assert record["rate_limits"]["after"]["five_hour"]["utilization"] == 0.4
    summary = json.loads((tmp_path / "out" / "20261117T070000Z" / "summary.json").read_text())
    assert summary["runs"][0]["rate_limits"]["first"]["five_hour"]["utilization"] == 0.3


def test_the_first_values_of_each_window_come_from_its_first_event():
    first = pilot.first_windows(pilot.parse_rate_limit_events(lines(
        event("five_hour", 0.1), event("seven_day", 0.2), event("five_hour", 0.35))))
    assert first["five_hour"]["utilization"] == 0.1 and first["seven_day"]["utilization"] == 0.2


# The shape Claude Code 2.1.326 wrote in the first real run: the representative window has no utilization
# of its own; the value is in unifiedWindows, as a fraction, with the reset time in epoch seconds.
REAL_INFO = {"status": "allowed", "resetsAt": 1795136400, "rateLimitType": "seven_day", "overageStatus": "rejected",
             "overageDisabledReason": "org_level_disabled", "isUsingOverage": False,
             "unifiedWindows": {"seven_day": {"utilization": 0.25, "resetsAt": 1795136400, "status": "allowed"}}}


def real_event(utilization):
    info = json.loads(json.dumps(REAL_INFO))
    info["unifiedWindows"]["seven_day"]["utilization"] = utilization
    return {"type": "rate_limit_event", "rate_limit_info": info, "uuid": "u", "session_id": "s"}


def test_the_first_values_take_the_whole_first_event_as_claude_code_writes_it():
    infos = pilot.parse_rate_limit_events(lines(real_event(0.25), real_event(0.3)))
    assert pilot.first_windows(infos)["seven_day"]["utilization"] == 0.25
    latest = pilot.latest_windows(infos)
    assert latest["seven_day"]["utilization"] == 0.3 and latest["seven_day"]["resets_at"] == 1795136400
    assert not pilot.decide(latest, 50, 30, 1795136400 - 10).start
    assert pilot.decide(latest, 50, 50, 1795136400 - 10).start


def test_regulated_windows_the_agent_never_reported_are_named():
    infos = pilot.parse_rate_limit_events(lines(real_event(0.25)))
    assert pilot.unreported_windows(pilot.latest_windows(infos)) == ["five_hour"]
    assert pilot.unreported_windows({}) == ["five_hour", "seven_day"]
    assert pilot.unreported_windows(win(0.1, 0.1)) == []


def test_the_record_names_the_windows_the_agent_never_reported(tmp_path):
    pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path))
    record = json.loads((tmp_path / "out" / "20261117T070000Z" / "python" / "record.json").read_text())
    assert record["rate_limits"]["unreported"] == []


def test_the_stop_message_says_when_the_window_resets(tmp_path):
    state = tmp_path / "out" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"windows": win(0.55, 0.1)}))
    summary = pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path))
    reason = summary["runs"][0]["reason"]
    assert "2026-09-21T15:13:20Z" in reason  # NOW + 3600 in UTC
    assert "--wait-for-reset" in reason


def test_a_failed_harbor_command_is_recorded_as_failed(tmp_path):
    summary = pilot.run_pilot(runner=FakeRunner(returncode=1), **pilot_args(tmp_path))
    assert summary["runs"][0]["status"] == "harbor-failed"


def test_a_run_over_the_threshold_does_not_start(tmp_path):
    runner = FakeRunner()
    state = tmp_path / "out" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"windows": win(0.55, 0.1)}))
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path))
    assert runner.calls == []
    assert summary["runs"][0]["status"] == "not-started"
    assert "five_hour" in summary["runs"][0]["reason"]
    assert summary["runs"][0]["rate_limits"]["before"]["five_hour"]["utilization"] == 0.55


def test_the_last_values_of_one_run_are_the_before_values_of_the_next(tmp_path):
    runner = FakeRunner()
    pilot.run_pilot(runner=runner, **pilot_args(tmp_path))
    state = json.loads((tmp_path / "out" / "state.json").read_text())
    assert state["windows"]["five_hour"]["utilization"] == 0.4
    again = pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path, timestamp="20261117T080000Z"))
    assert again["runs"][0]["rate_limits"]["before"]["five_hour"]["utilization"] == 0.4


def test_languages_run_one_after_the_other(tmp_path):
    runner = FakeRunner()
    args = pilot_args(tmp_path, languages=["python", "go"])
    summary = pilot.run_pilot(runner=runner, **args)
    # make_trial writes five_hour 0.4 and weekly 0.1: below the thresholds, so both languages start
    assert [r["language"] for r in summary["runs"]] == ["python", "go"]
    assert len(runner.calls) == 2


def test_the_batch_stops_when_a_run_leaves_the_account_over_a_threshold(tmp_path):
    runner = FakeRunner()
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path, languages=["python", "go"], max_five_hour=40.0))
    assert [r["status"] for r in summary["runs"]] == ["ok", "not-started"]
    assert len(runner.calls) == 1


def test_a_language_the_task_does_not_cover_is_refused_before_anything_runs(tmp_path):
    runner = FakeRunner()
    with pytest.raises(pilot.PilotError):
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path, languages=["cobol"]))
    assert runner.calls == []


def test_the_token_file_must_exist_before_anything_runs(tmp_path):
    runner = FakeRunner()
    with pytest.raises(pilot.PilotError):
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path, token_file=tmp_path / "missing"))
    assert runner.calls == []


def test_waiting_for_the_reset_sleeps_until_then_and_starts(tmp_path):
    runner = FakeRunner()
    slept = []
    clock = {"t": NOW}

    def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    state = tmp_path / "out" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"windows": win(0.9, 0.1, reset_five=NOW + 600)}))
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path, wait_for_reset=True, sleep=sleep,
                                                          now=lambda: clock["t"]))
    assert summary["runs"][0]["status"] == "ok"
    assert len(slept) == 1 and 600 <= slept[0] <= 700


# --- the leak scans hold on every path out of a run --------------------------------------------------------------

def read_record(tmp_path, lang="python"):
    return json.loads((tmp_path / "out" / "20261117T070000Z" / lang / "record.json").read_text())


def test_a_token_in_a_harbor_file_name_never_reaches_a_record_or_a_message(tmp_path):
    with pytest.raises(pilot.TokenLeak) as err:
        pilot.run_pilot(runner=FakeRunner(name_leak=True), **pilot_args(tmp_path))
    assert FAKE_TOKEN not in str(err.value) and "<token>.txt" in str(err.value)
    record_path = tmp_path / "out" / "20261117T070000Z" / "python" / "record.json"
    assert FAKE_TOKEN not in record_path.read_text() and "<token>.txt" in record_path.read_text()
    assert read_record(tmp_path)["status"] == "token-leak"
    for path in (tmp_path / "out").rglob("*"):
        if path.is_file() and FAKE_TOKEN not in path.name:
            assert FAKE_TOKEN.encode() not in path.read_bytes(), path.name


@skip_as_root
def test_an_unreadable_file_in_harbors_output_fails_the_run(tmp_path):
    with pytest.raises(pilot.TokenLeak) as err:
        pilot.run_pilot(runner=FakeRunner(unreadable=True), **pilot_args(tmp_path))
    assert "could not" in str(err.value)
    record = read_record(tmp_path)
    assert record["status"] == "leak-scan-failed"
    assert [u.rsplit("/", 1)[-1] for u in record["leak_scan"]["unchecked"]] == ["from-container.log"]


def test_a_leak_is_raised_even_when_harbors_output_cannot_be_read(tmp_path):
    with pytest.raises(pilot.TokenLeak):
        pilot.run_pilot(runner=FakeRunner(leak=True, bad_time=True), **pilot_args(tmp_path))
    assert read_record(tmp_path)["status"] == "token-leak"


def test_harbor_output_that_cannot_be_read_is_recorded_not_raised(tmp_path):
    summary = pilot.run_pilot(runner=FakeRunner(bad_time=True), **pilot_args(tmp_path))
    assert summary["runs"][0]["status"] == "no-result"
    assert "ValueError" in read_record(tmp_path)["error"]


def test_an_interrupted_run_still_gets_the_batch_scan(tmp_path):
    with pytest.raises(pilot.TokenLeak) as err:
        pilot.run_pilot(runner=FakeRunner(interrupt=True, stray_leak=True), **pilot_args(tmp_path))
    assert "stray.log" in str(err.value)


def test_an_interrupted_run_stops_its_containers_and_passes_the_interrupt_on(tmp_path):
    docker = FakeDocker()
    runner = FakeRunner(interrupt=True, docker=docker, containers=["web-signup__python__abc__env"])
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path, docker=docker, languages=["python", "go"]))
    assert docker.removed == ["web-signup__python__abc__env"]
    assert read_record(tmp_path)["status"] == "interrupted"
    summary = json.loads((tmp_path / "out" / "20261117T070000Z" / "summary.json").read_text())
    assert [r["language"] for r in summary["runs"]] == ["python"]
    assert len(runner.calls) == 1


# --- time limits and Harbor's containers -------------------------------------------------------------------------------

def test_a_timeout_stops_the_containers_and_the_batch(tmp_path):
    docker = FakeDocker()
    projects = ["web-signup__python__abc__env", "web-signup__python__abc__verifier-build"]
    runner = FakeRunner(returncode=pilot.TIMEOUT_RETURNCODE, docker=docker, containers=projects)
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path, docker=docker, languages=["python", "go"]))
    assert [r["status"] for r in summary["runs"]] == ["timeout", "not-started"]
    assert "time limit" in summary["runs"][1]["reason"]
    assert sorted(docker.removed) == sorted(projects)
    assert len(runner.calls) == 1


def test_containers_that_will_not_stop_hold_the_batch(tmp_path):
    docker = FakeDocker(stuck=["web-signup__python__abc__env"])
    runner = FakeRunner(returncode=pilot.TIMEOUT_RETURNCODE, docker=docker, containers=["web-signup__python__abc__env"])
    with pytest.raises(pilot.PilotError) as err:
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path, docker=docker, languages=["python", "go"]))
    assert "web-signup__python__abc__env" in str(err.value)
    assert len(runner.calls) == 1
    assert read_record(tmp_path)["containers_left"] == ["web-signup__python__abc__env"]


def test_leftover_containers_of_an_earlier_run_keep_a_language_from_starting(tmp_path):
    runner = FakeRunner()
    docker = FakeDocker(leftovers=["web-signup__python__old__env"])
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path, docker=docker))
    assert runner.calls == [] and summary["runs"][0]["status"] == "not-started"
    assert "web-signup__python__old__env" in summary["runs"][0]["reason"]


def test_the_default_time_limit_is_above_harbors_own_limits(tmp_path):
    task = load_task(TASKS / "web-signup")
    harbor_own = sum(task.limits["agent_timeout_sec"] + task.limits["verifier_timeout_sec"] for _ in task.steps)
    assert pilot.harbor_time_limit(task) > harbor_own
    runner = FakeRunner()
    pilot.run_pilot(runner=runner, **pilot_args(tmp_path))
    assert runner.timeouts == [pilot.harbor_time_limit(task)]


HARBOR_STAND_IN = """
import signal, subprocess, sys, time
out, mode = sys.argv[1], sys.argv[2]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(out + "/child.pid", "w").write(str(child.pid))
if mode == "exit":
    sys.exit(0)  # exits on its own and leaves its child running
if mode == "polite":
    def stop(*_):
        open(out + "/stopped-politely", "w").write("1")
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
else:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(out + "/ready", "w").write("1")
time.sleep(120)
"""


def gone(pid: int, within: float = 10.0) -> bool:
    end = time.monotonic() + within
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def start_stand_in(tmp_path, mode):
    script = tmp_path / "stand_in.py"
    script.write_text(HARBOR_STAND_IN)
    return [sys.executable, str(script), str(tmp_path), mode]


def wait_ready(tmp_path, within=10.0):
    end = time.monotonic() + within
    while not (tmp_path / "ready").exists() and time.monotonic() < end:
        time.sleep(0.05)


def test_a_timeout_asks_harbor_to_stop_and_ends_its_whole_process_group(tmp_path):
    started = time.monotonic()
    code = pilot.run_subprocess(start_stand_in(tmp_path, "polite"), dict(os.environ), tmp_path / "h.log", 2.0,
                                grace=10.0, term_grace=2.0)
    assert code == pilot.TIMEOUT_RETURNCODE
    assert (tmp_path / "stopped-politely").exists()  # Harbor gets the chance to clean up first
    assert gone(int((tmp_path / "child.pid").read_text()))
    assert time.monotonic() - started < 20
    assert "time limit" in (tmp_path / "h.log").read_text()


def test_a_harbor_that_ignores_the_stop_is_killed_with_its_group(tmp_path):
    started = time.monotonic()
    code = pilot.run_subprocess(start_stand_in(tmp_path, "stubborn"), dict(os.environ), tmp_path / "h.log", 2.0,
                                grace=1.0, term_grace=1.0)
    assert code == pilot.TIMEOUT_RETURNCODE
    assert gone(int((tmp_path / "child.pid").read_text()))
    assert time.monotonic() - started < 20


def test_ctrl_c_is_passed_to_harbor_and_its_group_is_ended(tmp_path):
    def interrupt(*_):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGALRM, interrupt)
    try:
        cmd = start_stand_in(tmp_path, "polite")
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        with pytest.raises(KeyboardInterrupt):
            pilot.run_subprocess(cmd, dict(os.environ), tmp_path / "h.log", 60.0, grace=10.0, term_grace=2.0)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert (tmp_path / "stopped-politely").exists()
    assert gone(int((tmp_path / "child.pid").read_text()))


# --- usage regulation, end to end -----------------------------------------------------------------------------------

def test_a_rejected_window_in_a_run_keeps_the_next_language_from_starting(tmp_path):
    runner = FakeRunner(five_status="rejected")
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path, languages=["python", "go"]))
    assert [r["status"] for r in summary["runs"]] == ["ok", "not-started"]
    assert "rejected" in summary["runs"][1]["reason"] and len(runner.calls) == 1


def test_a_utilization_above_one_in_a_run_keeps_the_next_language_from_starting(tmp_path):
    runner = FakeRunner(week=1.2)
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path, languages=["python", "go"], max_weekly=100.0))
    assert [r["status"] for r in summary["runs"]] == ["ok", "not-started"]
    assert "120%" in summary["runs"][1]["reason"]


@pytest.mark.parametrize("kw", [{"max_five_hour": 0}, {"max_weekly": 101}, {"max_agent_minutes_per_5h": 0}])
def test_limits_outside_their_range_are_refused_before_anything_runs(tmp_path, kw):
    runner = FakeRunner()
    with pytest.raises(pilot.PilotError):
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path, **kw))
    assert runner.calls == []


def test_the_five_hour_guard_counts_this_runners_agent_minutes_of_the_last_five_hours():
    runs = [{"started_at": NOW - 7 * 3600, "ended_at": NOW - 5 * 3600 - 60, "agent_seconds": 5000},  # ended outside
            {"started_at": NOW - 3600, "ended_at": NOW - 2100, "agent_seconds": 1500}]
    assert pilot.guard_decision(runs, 30, NOW).start  # 25 minutes
    runs.append({"started_at": NOW - 600, "ended_at": NOW - 200, "agent_seconds": 400})
    d = pilot.guard_decision(runs, 30, NOW)  # 31.7 minutes
    assert not d.start and d.wait_until == NOW - 2100 + 5 * 3600  # when the older run's end leaves the window
    assert "guard" in d.reason and "not a measurement" in d.reason


def test_the_guard_counts_a_run_until_its_end_leaves_the_window():
    # started 5 h 1 min ago, its agent worked 120 minutes, it ended 3 hours ago: that work is recent
    runs = [{"started_at": NOW - 5 * 3600 - 60, "ended_at": NOW - 3 * 3600, "agent_seconds": 7200}]
    assert pilot.agent_minutes(runs, NOW) == pytest.approx(120)
    d = pilot.guard_decision(runs, 30, NOW)
    assert not d.start and d.wait_until == NOW - 3 * 3600 + 5 * 3600


def test_a_run_without_an_end_time_counts_and_gives_no_wait_time():
    runs = [{"started_at": NOW - 6 * 3600, "agent_seconds": 600}]
    d = pilot.guard_decision(runs, 5, NOW)
    assert not d.start and d.wait_until is None


def test_a_run_cut_short_counts_its_whole_wall_time_for_the_guard():
    # a step without an agent time was cut short; its agent may have worked for most of the run
    cut = [{"agent_seconds": 60.0}, {"agent_seconds": None}]
    assert pilot._run_agent_seconds({"status": "timeout", "wall_seconds": 3000.0, "steps": cut}) == 3000.0
    assert pilot._run_agent_seconds({"status": "ok", "wall_seconds": 3000.0, "steps": cut}) == 3000.0
    whole = [{"agent_seconds": 60.0}, {"agent_seconds": 90.0}]
    assert pilot._run_agent_seconds({"status": "agent-error", "wall_seconds": 3000.0, "steps": whole}) == 3000.0
    assert pilot._run_agent_seconds({"status": "ok", "wall_seconds": 3000.0, "steps": whole}) == 150.0


def test_a_run_adds_its_agent_time_to_the_guard_and_the_record_says_it_is_a_guard(tmp_path):
    pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path))
    guard_state = json.loads((tmp_path / "pilot-guard.json").read_text())  # beside the token file
    assert guard_state["version"] == 1
    [run] = guard_state["runs"]
    assert {k: run[k] for k in ("batch", "language", "started_at", "ended_at", "agent_seconds")} == {
        "batch": "20261117T070000Z", "language": "python", "started_at": NOW, "ended_at": NOW, "agent_seconds": 240.0}
    assert run["max_seconds"] >= pilot.harbor_time_limit(load_task(TASKS / "web-signup"))
    guard = read_record(tmp_path)["five_hour_guard"]
    assert guard["limit_minutes"] == 30 and guard["minutes_before"] == 0 and guard["minutes_after"] == 4
    assert "not a measurement" in guard["note"]


def test_the_guard_stops_the_batch_when_this_runners_agent_time_reaches_it(tmp_path):
    runner = FakeRunner()
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path, languages=["python", "go"],
                                                          max_agent_minutes_per_5h=3))
    assert [r["status"] for r in summary["runs"]] == ["ok", "not-started"]
    assert "guard" in summary["runs"][1]["reason"] and len(runner.calls) == 1


def test_waiting_for_the_guard_waits_until_old_runs_leave_the_window(tmp_path):
    slept = []
    clock = {"t": NOW}

    def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    runs = [{"batch": "b", "language": "go", "started_at": NOW - 5 * 3600 - 1300, "ended_at": NOW - 5 * 3600 + 600,
             "agent_seconds": 1900}]
    state = tmp_path / "out" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"windows": {}, "runs": runs}))
    (tmp_path / "pilot-guard.json").write_text(json.dumps({"version": 1, "runs": [
        {"id": "old", "pid": 1, "max_seconds": 10800.0, **r} for r in runs]}))
    summary = pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path, wait_for_reset=True, sleep=sleep,
                                                                now=lambda: clock["t"]))
    assert summary["runs"][0]["status"] == "ok"
    assert len(slept) == 1 and 600 <= slept[0] <= 700


# --- naming what went wrong -----------------------------------------------------------------------------------------

def test_an_error_outside_the_agent_is_a_trial_error(tmp_path):
    summary = pilot.run_pilot(runner=FakeRunner(trial_error=True), **pilot_args(tmp_path))
    assert summary["runs"][0]["status"] == "trial-error"


def test_the_agents_own_error_line_makes_an_agent_error(tmp_path):
    summary = pilot.run_pilot(runner=FakeRunner(result_error=True), **pilot_args(tmp_path))
    assert summary["runs"][0]["status"] == "agent-error"


# --- the review of the guard and the cleanup ---------------------------------------------------------------------------

def test_the_guard_is_shared_by_batches_with_different_output_folders(tmp_path):
    first = pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path, max_agent_minutes_per_5h=3))
    assert first["runs"][0]["status"] == "ok"
    runner = FakeRunner()
    second = pilot.run_pilot(runner=runner, **pilot_args(tmp_path, out_root=tmp_path / "elsewhere",
                                                         max_agent_minutes_per_5h=3))
    assert second["runs"][0]["status"] == "not-started" and "guard" in second["runs"][0]["reason"]
    assert runner.calls == []


def test_the_cleanup_leaves_other_trials_of_the_same_task_and_language_alone(tmp_path):
    docker = FakeDocker()
    runner = FakeRunner(interrupt=True, docker=docker,
                        containers=["web-signup__python__abc__env", "web-signup__python__someoneelse__env"])
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path, docker=docker))
    assert docker.removed == ["web-signup__python__abc__env"]
    assert docker.projects == ["web-signup__python__someoneelse__env"]


def test_nothing_is_removed_when_harbor_never_started(tmp_path):
    docker = FakeDocker()

    def harbor_missing(cmd, env, log_path, timeout):
        docker.projects.append("web-signup__python__someoneelse__env")  # another run, started meanwhile
        raise FileNotFoundError("harbor")

    with pytest.raises(FileNotFoundError):
        pilot.run_pilot(runner=harbor_missing, **pilot_args(tmp_path, docker=docker))
    assert docker.removed == []


def test_containers_left_after_harbor_exits_on_its_own_are_removed(tmp_path):
    docker = FakeDocker()
    runner = FakeRunner(docker=docker, containers=["web-signup__python__abc__env"])
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path, docker=docker))
    assert docker.removed == ["web-signup__python__abc__env"]
    assert summary["runs"][0]["status"] == "ok"


def test_containers_that_stay_after_harbor_exits_on_its_own_stop_the_batch(tmp_path):
    docker = FakeDocker(stuck=["web-signup__python__abc__env"])
    runner = FakeRunner(returncode=1, docker=docker, containers=["web-signup__python__abc__env"])
    with pytest.raises(pilot.PilotError) as err:
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path, docker=docker, languages=["python", "go"]))
    assert "web-signup__python__abc__env" in str(err.value)
    assert len(runner.calls) == 1


def test_leftover_containers_of_another_language_of_the_task_keep_a_run_from_starting(tmp_path):
    runner = FakeRunner()
    docker = FakeDocker(leftovers=["web-signup__go__old__env"])
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path, docker=docker))
    assert runner.calls == [] and summary["runs"][0]["status"] == "not-started"
    assert "web-signup__go__old__env" in summary["runs"][0]["reason"]


def test_processes_harbor_leaves_behind_when_it_exits_on_its_own_are_ended(tmp_path):
    code = pilot.run_subprocess(start_stand_in(tmp_path, "exit"), dict(os.environ), tmp_path / "h.log", 30.0,
                                grace=2.0, term_grace=2.0)
    pid = int((tmp_path / "child.pid").read_text())
    try:
        assert code == 0
        assert gone(pid, within=5.0)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_ctrl_c_reports_what_the_record_says(tmp_path):
    docker = FakeDocker(stuck=["web-signup__python__abc__env"])
    runner = FakeRunner(interrupt=True, docker=docker, containers=["web-signup__python__abc__env"])
    with pytest.raises(KeyboardInterrupt) as err:
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path, docker=docker))
    notes = "\n".join(getattr(err.value, "__notes__", []))
    assert "python: interrupted" in notes and "web-signup__python__abc__env" in notes
    assert "python/record.json" in notes and "scanned" in notes


def test_main_prints_what_happened_after_ctrl_c_and_claims_nothing_else(monkeypatch, capsys):
    def interrupted(*args, **kwargs):
        err = KeyboardInterrupt()
        err.add_note("python: interrupted; containers left: web-signup__python__abc__env")
        raise err

    monkeypatch.setattr(pilot, "run_pilot", interrupted)
    code = pilot.main([str(TASKS / "web-signup"), "--lang", "python", "--claude-version", "2.1.326"])
    out = capsys.readouterr().err
    assert code == 130
    assert "containers left: web-signup__python__abc__env" in out and "containers removed" not in out


def test_the_final_scans_run_even_when_the_summary_cannot_be_written(tmp_path):
    (tmp_path / "out" / "20261117T070000Z" / "summary.json").mkdir(parents=True)  # the write will fail
    with pytest.raises(pilot.TokenLeak) as err:
        pilot.run_pilot(runner=FakeRunner(stray_leak=True), **pilot_args(tmp_path))
    assert "stray.log" in str(err.value)


# --- the five-hour guard as a whole: no lost time, one step, fail closed, whole writes, signals ----------------------

BENCH = TASKS.parent


def guard_entry(started, ended=None, agent=None, max_seconds=10800.0, ident="r1"):
    return {"id": ident, "batch": "b", "language": "go", "pid": 1, "started_at": started, "max_seconds": max_seconds,
            "ended_at": ended, "agent_seconds": agent}


def write_guard(folder: Path, *runs, raw=None) -> Path:
    path = folder / "pilot-guard.json"
    path.write_text(raw if raw is not None else json.dumps({"version": 1, "runs": list(runs)}))
    return path


def test_a_run_with_no_recorded_end_counts_its_whole_time_limit():
    runs = [guard_entry(NOW - 3600, max_seconds=10800.0)]
    assert pilot.agent_minutes(runs, NOW) == pytest.approx(180)
    d = pilot.guard_decision(runs, 30, NOW)
    assert not d.start and d.wait_until == NOW - 3600 + 10800 + 5 * 3600
    assert "no recorded end" in d.reason


def test_a_start_with_no_end_in_the_guard_file_keeps_a_run_from_starting(tmp_path):
    write_guard(tmp_path, guard_entry(NOW - 3600))
    runner = FakeRunner()
    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path))
    assert runner.calls == [] and summary["runs"][0]["status"] == "not-started"
    assert "no recorded end" in summary["runs"][0]["reason"]


def test_a_pilot_sees_the_run_of_another_pilot_that_is_still_going(tmp_path):
    inner = {}

    class OverlappingRunner(FakeRunner):
        def __call__(self, cmd, env, log_path, timeout):
            other = FakeRunner()
            inner["summary"] = pilot.run_pilot(runner=other, **pilot_args(
                tmp_path, out_root=tmp_path / "elsewhere", timestamp="20261117T070100Z"))
            inner["calls"] = other.calls
            return super().__call__(cmd, env, log_path, timeout)

    pilot.run_pilot(runner=OverlappingRunner(), **pilot_args(tmp_path))
    assert inner["calls"] == [] and inner["summary"]["runs"][0]["status"] == "not-started"
    assert "guard" in inner["summary"]["runs"][0]["reason"]


def test_two_overlapping_pilots_both_keep_their_runs_in_the_guard(tmp_path):
    class OverlappingRunner(FakeRunner):
        def __call__(self, cmd, env, log_path, timeout):
            pilot.run_pilot(runner=FakeRunner(), **pilot_args(
                tmp_path, out_root=tmp_path / "elsewhere", timestamp="20261117T070100Z", max_agent_minutes_per_5h=1000))
            return super().__call__(cmd, env, log_path, timeout)

    pilot.run_pilot(runner=OverlappingRunner(), **pilot_args(tmp_path, max_agent_minutes_per_5h=1000))
    runs = json.loads((tmp_path / "pilot-guard.json").read_text())["runs"]
    assert sorted(r["batch"] for r in runs) == ["20261117T070000Z", "20261117T070100Z"]
    assert all(r["ended_at"] is not None for r in runs)


def test_two_pilots_cannot_both_pass_on_the_same_allowance(tmp_path, monkeypatch):
    real = pilot.start_decision
    other = {}

    def run_other():
        runner = FakeRunner()
        other["summary"] = pilot.run_pilot(runner=runner, **pilot_args(
            tmp_path, out_root=tmp_path / "elsewhere", timestamp="20261117T070100Z"))
        other["calls"] = runner.calls

    def decide_then_let_the_other_try(*args, **kwargs):
        decision = real(*args, **kwargs)
        if "thread" not in other:  # the other pilot decides while this one has decided but not yet recorded
            other["thread"] = threading.Thread(target=run_other)
            other["thread"].start()
            other["thread"].join(timeout=1.0)
        return decision

    monkeypatch.setattr(pilot, "start_decision", decide_then_let_the_other_try)
    pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path))
    other["thread"].join(timeout=60)
    assert other["calls"] == [] and other["summary"]["runs"][0]["status"] == "not-started"


@pytest.mark.parametrize("raw", [
    '{"version": 1, "runs": [',  # cut off
    "[]",
    '{"version": 1, "runs": null}',
    '{"runs": []}',  # no version: not this runner's format
    json.dumps({"version": 1, "runs": [guard_entry(NOW - 600, ended=float("nan"), agent=99999)]}),
])
def test_a_damaged_guard_file_starts_nothing_and_says_what_to_do(tmp_path, raw):
    write_guard(tmp_path, raw=raw)
    runner = FakeRunner()
    with pytest.raises(pilot.PilotError) as err:
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path))
    assert runner.calls == []
    assert "pilot-guard.json" in str(err.value) and "No run was started" in str(err.value)


@skip_as_root
def test_an_unreadable_guard_file_starts_nothing(tmp_path):
    path = write_guard(tmp_path, guard_entry(NOW - 600, ended=NOW - 60, agent=99999))
    path.chmod(0)
    runner = FakeRunner()
    try:
        with pytest.raises(pilot.PilotError) as err:
            pilot.run_pilot(runner=runner, **pilot_args(tmp_path))
    finally:
        path.chmod(0o600)
    assert runner.calls == [] and "No run was started" in str(err.value)


@skip_as_root
def test_a_guard_file_that_cannot_be_written_starts_nothing(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    tf = token_file(keys)
    write_guard(keys)
    (keys / "pilot-guard.json.lock").write_text("")
    keys.chmod(0o500)
    runner = FakeRunner()
    try:
        with pytest.raises(pilot.PilotError) as err:
            pilot.run_pilot(runner=runner, **pilot_args(tmp_path, token_file=tf))
    finally:
        keys.chmod(0o700)
    assert runner.calls == [] and "cannot be written" in str(err.value) and "No run was started" in str(err.value)


def test_the_guard_file_is_private_and_reaches_the_disk(tmp_path, monkeypatch):
    synced = []
    real_fsync = os.fsync

    def fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(pilot.os, "fsync", fsync)
    pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path))
    assert stat.S_IMODE((tmp_path / "pilot-guard.json").stat().st_mode) == 0o600
    assert len(synced) >= 2  # the run's start and its end


def test_a_guard_write_that_fails_leaves_the_old_file_whole_and_starts_nothing(tmp_path, monkeypatch):
    before = json.dumps({"version": 1, "runs": [guard_entry(NOW - 7 * 3600, ended=NOW - 6 * 3600, agent=60)]})
    path = write_guard(tmp_path, raw=before)

    def no_room(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pilot.os, "replace", no_room)
    runner = FakeRunner()
    with pytest.raises(pilot.PilotError):
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path))
    assert runner.calls == [] and path.read_text() == before
    assert not [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


HARBOR_FAKE = """#!{python}
import os, signal, sys, time
args = sys.argv[1:]
out = os.environ["STAND_IN_DIR"]
mode = os.environ.get("STAND_IN_MODE", "polite")
jobs, job = args[args.index("-o") + 1], args[args.index("--job-name") + 1]
os.makedirs(os.path.join(jobs, job, "web-signup__python__abc"), exist_ok=True)
open(os.path.join(out, "harbor.pid"), "w").write(str(os.getpid()))
def finish(code):
    open(os.path.join(out, "harbor-exit"), "w").write(repr(time.time()))
    sys.exit(code)
def stop(*_):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if mode == "slow-stop":
        time.sleep(2.0)  # Harbor removing its containers
    open(os.path.join(out, "harbor-stopped"), "w").write("1")
    finish(130)
signal.signal(signal.SIGINT, stop)
open(os.path.join(out, "harbor-ready"), "w").write("1")
if mode == "leave-child":
    import subprocess
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(os.path.join(out, "child.pid"), "w").write(str(child.pid))
    finish(0)
if mode == "exit":
    finish(0)
time.sleep(120)
"""

PILOT_DRIVER = """
import functools, os, signal, sys
from pathlib import Path
bench, tmp = sys.argv[1], Path(sys.argv[2])
stage = sys.argv[3] if len(sys.argv) > 3 else ""
sig = int(sys.argv[4]) if len(sys.argv) > 4 else int(signal.SIGTERM)
sys.path.insert(0, bench)
from dodambench import pilot


def poke():
    os.kill(os.getpid(), sig)


class NoContainers:
    def leftovers(self, prefix):
        return []

    def remove(self, prefix):
        (tmp / "containers-step").write_text(prefix)
        return []


runner = functools.partial(pilot.run_subprocess, grace=10.0, term_grace=2.0)
if stage == "before-harbor":
    real_runner = runner

    def runner(*args, **kwargs):
        poke()
        return real_runner(*args, **kwargs)
if stage == "scans":
    real_check = pilot.check_for_token

    def check(*args, **kwargs):
        poke()
        return real_check(*args, **kwargs)
    pilot.check_for_token = check
if stage == "leftover":
    real_leftover = pilot.end_leftover_processes

    def leftover(*args, **kwargs):
        poke()
        return real_leftover(*args, **kwargs)
    pilot.end_leftover_processes = leftover
if stage == "guard-end":
    real_end = pilot.end_guard_run

    def end(*args, **kwargs):
        poke()
        return real_end(*args, **kwargs)
    pilot.end_guard_run = end

try:
    pilot.run_pilot(task_path=Path(bench) / "tasks" / "web-signup", languages=["python"], out_root=tmp / "out",
                    token_file=tmp / "claude-token", harbor=tmp / "harbor", claude_version="2.1.326",
                    base_env={"PATH": os.environ.get("PATH", ""), "STAND_IN_DIR": str(tmp),
                              "STAND_IN_MODE": os.environ.get("STAND_IN_MODE", "polite")},
                    image_check=lambda image: None, docker=NoContainers(), timestamp="20261117T070000Z",
                    runner=runner)
except KeyboardInterrupt as err:
    (tmp / "notes.txt").write_text("\\n".join(getattr(err, "__notes__", [])))
    sys.exit(128 + getattr(err, "signum", 2))
"""


def make_harbor(tmp_path) -> Path:
    harbor = tmp_path / "harbor"
    harbor.write_text(HARBOR_FAKE.replace("{python}", sys.executable))
    harbor.chmod(0o755)
    return harbor


def launch_pilot(tmp_path, stage="", sig=signal.SIGTERM, mode="polite"):
    token_file(tmp_path)
    make_harbor(tmp_path)
    (tmp_path / "driver.py").write_text(PILOT_DRIVER)
    with open(tmp_path / "pilot-stderr.txt", "wb") as said:
        return subprocess.Popen([sys.executable, str(tmp_path / "driver.py"), str(BENCH), str(tmp_path), stage,
                                 str(int(sig))], env={**os.environ, "STAND_IN_MODE": mode},
                                stdout=subprocess.DEVNULL, stderr=said)


def start_pilot_process(tmp_path):
    proc = launch_pilot(tmp_path)
    end = time.monotonic() + 60
    while not (tmp_path / "harbor-ready").exists():
        assert proc.poll() is None and time.monotonic() < end, "the pilot did not start the stand-in Harbor"
        time.sleep(0.05)
    return proc


def stop_stray_harbor(tmp_path, proc):
    if proc.poll() is None:
        proc.kill()
        proc.wait()
    for name in ("harbor.pid", "child.pid"):
        try:
            os.kill(int((tmp_path / name).read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_sigterm_and_hangup_are_handled_like_ctrl_c(tmp_path, sig):
    proc = start_pilot_process(tmp_path)
    try:
        proc.send_signal(sig)
        assert proc.wait(timeout=120) == 128 + sig
        assert (tmp_path / "harbor-stopped").exists()  # Harbor was asked to stop, as on Ctrl-C
        assert gone(int((tmp_path / "harbor.pid").read_text()))
        [run] = json.loads((tmp_path / "pilot-guard.json").read_text())["runs"]
        assert run["ended_at"] is not None and run["agent_seconds"] is not None
        record = json.loads((tmp_path / "out" / "20261117T070000Z" / "python" / "record.json").read_text())
        assert record["status"] == "interrupted"
        assert "scanned" in (tmp_path / "notes.txt").read_text()
    finally:
        stop_stray_harbor(tmp_path, proc)


def test_a_pilot_killed_outright_leaves_its_run_counted(tmp_path):
    proc = start_pilot_process(tmp_path)
    try:
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)
        runner = FakeRunner()
        summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path, timestamp="20261117T080000Z", now=time.time))
        assert runner.calls == [] and summary["runs"][0]["status"] == "not-started"
        assert "no recorded end" in summary["runs"][0]["reason"]
    finally:
        stop_stray_harbor(tmp_path, proc)


# --- the smaller points of the third review -------------------------------------------------------------------------------

def test_every_final_scan_runs_and_every_failure_is_reported(tmp_path):
    def leaky(cmd, env, log_path, timeout):
        token = env["CLAUDE_CODE_OAUTH_TOKEN"]
        (Path(log_path).parents[1] / "stray.log").write_text(token)
        (tmp_path / "out" / "state.json").write_text(token)
        raise KeyboardInterrupt

    with pytest.raises(pilot.TokenLeak) as err:
        pilot.run_pilot(runner=leaky, **pilot_args(tmp_path))
    assert "batch folder" in str(err.value) and "state file" in str(err.value)
    assert FAKE_TOKEN not in str(err.value)


@pytest.mark.parametrize("damage", ["step result", "agent log"])
def test_a_run_without_every_step_of_the_task_is_not_ok(tmp_path, damage):
    def runner(cmd, env, log_path, timeout):
        code = FakeRunner()(cmd, env, log_path, timeout)
        jobs = Path(cmd[cmd.index("-o") + 1]) / cmd[cmd.index("--job-name") + 1]
        trial = next(jobs.glob("*/result.json"))
        if damage == "step result":
            data = json.loads(trial.read_text())
            data["step_results"] = data["step_results"][:1]
            trial.write_text(json.dumps(data))
        else:
            (trial.parent / "steps" / "change-1" / "agent" / "claude-code.txt").unlink()
        return code

    summary = pilot.run_pilot(runner=runner, **pilot_args(tmp_path))
    assert summary["runs"][0]["status"] == "incomplete"


@skip_as_root
@pytest.mark.parametrize("what", ["state", "guard"])
def test_a_failed_save_keeps_the_finished_run_in_the_summary_and_stops_the_batch(tmp_path, what):
    keys = tmp_path / "keys"
    keys.mkdir()
    tf = token_file(keys)
    if what == "state":
        (tmp_path / "out" / "state.json").mkdir(parents=True)

    class Runner(FakeRunner):
        def __call__(self, cmd, env, log_path, timeout):
            code = super().__call__(cmd, env, log_path, timeout)
            if what == "guard":
                keys.chmod(0o500)  # the guard can no longer be written when the run ends
            return code

    runner = Runner()
    try:
        with pytest.raises(pilot.PilotError):
            pilot.run_pilot(runner=runner, **pilot_args(tmp_path, token_file=tf, languages=["python", "go"]))
    finally:
        keys.chmod(0o700)
    summary = json.loads((tmp_path / "out" / "20261117T070000Z" / "summary.json").read_text())
    assert [(r["language"], r["status"]) for r in summary["runs"]] == [("python", "ok")]
    assert len(runner.calls) == 1


# --- the stop path, signal by signal, and the guard's remaining holes ----------------------------------------------------

def wait_for_file(path: Path, proc, within=60.0):
    end = time.monotonic() + within
    while not path.exists():
        assert proc.poll() is None and time.monotonic() < end, f"{path.name} did not appear"
        time.sleep(0.02)


@pytest.mark.parametrize("stage,sig", [
    ("before-harbor", signal.SIGTERM),
    ("running", signal.SIGTERM),
    ("stopping", signal.SIGTERM),
    ("stopping", signal.SIGHUP),
    ("stopping", signal.SIGINT),
    ("leftover", signal.SIGTERM),
    ("scans", signal.SIGTERM),
    ("guard-end", signal.SIGHUP),
])
def test_a_signal_at_any_stage_never_cuts_the_stop_short(tmp_path, stage, sig):
    mode = {"stopping": "slow-stop", "leftover": "leave-child", "scans": "exit", "guard-end": "exit"}.get(stage, "polite")
    proc = launch_pilot(tmp_path, stage=stage if stage in ("before-harbor", "leftover", "scans", "guard-end") else "",
                        sig=sig, mode=mode)
    try:
        if stage in ("running", "stopping"):
            wait_for_file(tmp_path / "harbor-ready", proc)
            proc.send_signal(sig)
        if stage == "stopping":  # a second and a third signal while Harbor takes 2 s to stop
            time.sleep(0.5)
            proc.send_signal(sig)
            time.sleep(0.5)
            proc.send_signal(sig)
        assert proc.wait(timeout=120) == 128 + sig
        harbor_started = (tmp_path / "harbor.pid").exists()
        assert harbor_started == (stage != "before-harbor")
        if harbor_started:
            assert gone(int((tmp_path / "harbor.pid").read_text()), within=0.5)  # gone before the pilot ended
            assert (tmp_path / "containers-step").exists()
        if (tmp_path / "child.pid").exists():  # what Harbor left in its group is gone too
            assert gone(int((tmp_path / "child.pid").read_text()), within=0.5)
        assert "scanned" in (tmp_path / "notes.txt").read_text()
        [run] = json.loads((tmp_path / "pilot-guard.json").read_text())["runs"]
        assert run["ended_at"] is not None
        if (tmp_path / "harbor-exit").exists():  # closed only once Harbor was gone
            assert run["ended_at"] >= float((tmp_path / "harbor-exit").read_text())
    finally:
        stop_stray_harbor(tmp_path, proc)


def test_the_guard_entry_stays_open_while_harbor_is_not_confirmed_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(pilot, "_group_gone", lambda pgid, within: False, raising=False)
    runner = functools.partial(pilot.run_subprocess, grace=1.0, term_grace=1.0)
    env = {"PATH": os.environ.get("PATH", ""), "STAND_IN_DIR": str(tmp_path), "STAND_IN_MODE": "exit"}
    with pytest.raises(pilot.PilotError) as err:
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path, harbor=make_harbor(tmp_path), base_env=env))
    assert "confirmed gone" in str(err.value)
    [run] = json.loads((tmp_path / "pilot-guard.json").read_text())["runs"]
    assert run["ended_at"] is None  # still counted at its whole possible duration


def test_an_open_entry_counts_the_longest_time_harbor_could_run_without_the_pilot(tmp_path):
    seen = {}

    class Peek(FakeRunner):
        def __call__(self, cmd, env, log_path, timeout):
            seen["entry"] = json.loads((tmp_path / "pilot-guard.json").read_text())["runs"][-1]
            return super().__call__(cmd, env, log_path, timeout)

    pilot.run_pilot(runner=Peek(), **pilot_args(tmp_path, timeout=600.0))
    harbor_own = pilot.harbor_time_limit(load_task(TASKS / "web-signup"))
    assert seen["entry"]["max_seconds"] >= harbor_own + pilot.STOP_GRACE_SECONDS + pilot.TERM_GRACE_SECONDS


def test_a_clock_set_back_during_a_run_keeps_the_guard_file_usable(tmp_path):
    clock = {"t": NOW}

    class ClockBack(FakeRunner):
        def __call__(self, cmd, env, log_path, timeout):
            clock["t"] = NOW - 3600
            return super().__call__(cmd, env, log_path, timeout)

    pilot.run_pilot(runner=ClockBack(), **pilot_args(tmp_path, now=lambda: clock["t"]))
    [run] = json.loads((tmp_path / "pilot-guard.json").read_text())["runs"]
    assert run["ended_at"] >= run["started_at"]
    again = pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path, timestamp="20261117T080000Z",
                                                              now=lambda: clock["t"]))
    assert again["runs"][0]["status"] == "ok"


@skip_as_root
def test_a_failed_guard_end_is_reported_whatever_stopped_the_run(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    tf = token_file(keys)
    docker = FakeDocker(stuck=["web-signup__python__abc__env"])

    class Runner(FakeRunner):
        def __call__(self, cmd, env, log_path, timeout):
            code = super().__call__(cmd, env, log_path, timeout)
            keys.chmod(0o500)  # the guard can no longer be written when the run ends
            return code

    runner = Runner(docker=docker, containers=["web-signup__python__abc__env"])
    try:
        with pytest.raises(pilot.PilotError) as err:
            pilot.run_pilot(runner=runner, **pilot_args(tmp_path, token_file=tf, docker=docker))
    finally:
        keys.chmod(0o700)
    said = str(err.value) + "\n" + "\n".join(getattr(err.value, "__notes__", []))
    assert "web-signup__python__abc__env" in said and "pilot-guard.json" in said


def test_waiting_says_why_and_until_when_and_reads_the_guard_again(tmp_path, capsys):
    other = guard_entry(NOW - 60, max_seconds=11160.0, ident="other")
    write_guard(tmp_path, other)
    slept = []
    clock = {"t": NOW}

    def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds
        if len(slept) == 1:  # the other pilot finishes meanwhile
            write_guard(tmp_path, {**other, "ended_at": NOW, "agent_seconds": 60.0})

    summary = pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path, wait_for_reset=True, sleep=sleep,
                                                                now=lambda: clock["t"]))
    assert summary["runs"][0]["status"] == "ok"
    assert slept and slept[0] <= 900  # a short step, then the guard file is read again
    said = capsys.readouterr().err
    assert "waiting until" in said and "no recorded end" in said


def test_a_signal_while_the_start_is_decided_stops_the_batch_before_any_wait(tmp_path, monkeypatch):
    clock = {"t": NOW}
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    def decide_and_get_a_signal(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)  # held while the start is decided
        return pilot.Decision(False, "not yet", clock["t"] + 600)

    monkeypatch.setattr(pilot, "start_decision", decide_and_get_a_signal)
    runner = FakeRunner()
    with pytest.raises(KeyboardInterrupt):
        pilot.run_pilot(runner=runner, **pilot_args(tmp_path, wait_for_reset=True, sleep=sleep,
                                                    now=lambda: clock["t"]))
    assert slept == [] and runner.calls == []



def test_the_stop_says_what_it_is_doing(tmp_path):
    proc = launch_pilot(tmp_path, mode="slow-stop")
    try:
        wait_for_file(tmp_path / "harbor-ready", proc)
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)  # held: Harbor takes 2 s to stop
        assert proc.wait(timeout=120) == 128 + signal.SIGTERM
        said = (tmp_path / "pilot-stderr.txt").read_text()
        assert "stopping Harbor" in said and "exit when the cleanup is done" in said
        assert "SIGINT" in said and "held" in said
    finally:
        stop_stray_harbor(tmp_path, proc)


def test_a_signal_as_the_last_run_ends_still_gets_the_summary_and_the_final_scans(tmp_path, monkeypatch):
    real_enter = pilot.SignalGate.enter
    sent = []

    def enter(self):
        if not sent and sys._getframe(1).f_code.co_name == "run_pilot":
            sent.append(True)  # the first time run_pilot itself starts holding, after its last run
            os.kill(os.getpid(), signal.SIGTERM)
        return real_enter(self)

    monkeypatch.setattr(pilot.SignalGate, "enter", enter)
    with pytest.raises(KeyboardInterrupt) as err:
        pilot.run_pilot(runner=FakeRunner(), **pilot_args(tmp_path))
    assert sent
    assert "scanned" in "\n".join(getattr(err.value, "__notes__", []))


def test_a_task_without_time_limits_gets_a_clear_message(tmp_path):
    task = load_task(TASKS / "web-signup")
    task.limits.pop("agent_timeout_sec")
    with pytest.raises(pilot.PilotError) as err:
        pilot.harbor_time_limit(task)
    assert "agent_timeout_sec" in str(err.value) and task.id in str(err.value)
    with pytest.raises(pilot.PilotError):
        pilot.open_run_seconds(task, 600.0)
