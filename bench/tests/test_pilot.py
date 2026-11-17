"""Pilot runner: usage regulation, the harbor command, token safety and result recording.

None of these tests needs Docker, Harbor or a real token; the token below is a made-up string.
"""
import json
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


@pytest.mark.parametrize("raw,pct", [(0, 0.0), (0.5, 50.0), (0.99, 99.0), (1, 100.0), (1.0, 100.0), (37, 37.0), (100, 100.0)])
def test_utilization_is_read_as_a_fraction_up_to_one_and_a_percent_above(raw, pct):
    assert pilot.normalize_percent(raw) == pytest.approx(pct)


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


# --- reading what the agent and Harbor wrote -----------------------------------------------------------------

def make_trial(job_dir: Path, rewards=(1.0, 0.5), tokens=(100, 20, 30)) -> None:
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
            event("five_hour", 0.3 if name == "build" else 0.4),
            event("seven_day", 0.1),
            {"type": "result", "subtype": "success", "total_cost_usd": 0.5, "num_turns": 7,
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
    def __init__(self, leak=False, returncode=0, rewards=(1.0, 1.0), stray_leak=False, agent_error=False):
        self.calls, self.leak, self.returncode, self.rewards = [], leak, returncode, rewards
        self.stray_leak, self.agent_error = stray_leak, agent_error

    def __call__(self, cmd, env, log_path, timeout):
        self.calls.append((list(cmd), dict(env)))
        jobs = Path(cmd[cmd.index("-o") + 1]) / cmd[cmd.index("--job-name") + 1]
        jobs.mkdir(parents=True)
        make_trial(jobs, rewards=self.rewards)
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


def pilot_args(tmp_path, **kw):
    args = dict(task_path=TASKS / "web-signup", languages=["python"], out_root=tmp_path / "out",
                token_file=token_file(tmp_path), harbor=Path("harbor"), claude_version="2.1.326",
                max_five_hour=50.0, max_weekly=50.0, wait_for_reset=False, model=None,
                base_env={"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-api-x"},
                now=lambda: NOW, timestamp="20261117T070000Z", image_check=lambda image: None)
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
    assert record["leak_scan"] == {"clean": True, "hits": []}
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


def test_languages_run_one_after_the_other_and_stop_at_the_first_refusal(tmp_path):
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
