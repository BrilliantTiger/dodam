"""The measured values of pilot runs, written for the repository without any local detail.

The folders, names and the token below are made up.
"""
import copy
import json
from pathlib import Path

import pytest

try:
    from dodambench import pilot_record
except ImportError:  # each test then fails on its own instead of stopping the whole run
    pilot_record = None

HOME = "/Users/somebody-local/work/dodam"
FAKE_TOKEN = "sk-ant-oat01-FAKE0123456789abcdefghijklmnopqrstuvwxyzRECORD"
DATA_FILE = Path(__file__).resolve().parents[1] / "records" / "pilot-2026-11-17.json"
LANGUAGES = ["python", "typescript", "java", "csharp", "go", "rust", "cpp"]


def weekly(pct):
    return {"seven_day": {"resets_at": 1795136400, "status": "allowed", "utilization": pct / 100,
                          "utilization_pct": float(pct)}}


def record(lang="go", **changes):
    rec = {
        "language": lang, "task": "web-demo", "image": f"dodam-bench/{lang}:dev",
        "harbor_job": f"{HOME}/bench/out/pilot/20261117T114041Z/{lang}/jobs/pilot-{lang}-20261117T114041Z",
        "harbor_returncode": 0, "wall_seconds": 186.2, "time_limit_seconds": 10800.0,
        "claude_code_version": {"pinned": "2.1.326", "observed": "2.1.326"}, "model": "claude-sonnet-5-5",
        "usage": {"input": 16, "cache_read": 144315, "cache_creation": 22463, "output": 9658,
                  "cost_usd": 0.19, "num_turns": 8},
        "steps": [
            {"step": "build", "reward": 1.0, "agent_seconds": 39.3, "verifier_seconds": 30.1, "error": None,
             "rewards": {"build_ok": 1.0, "security_pass_rate": 1.0, "security_judged": 8.0, "reward": 1.0},
             "tokens": {"input": 1, "cache": 2, "output": 3, "cost_usd": 0.1}},
            {"step": "change-1", "reward": 0.0, "agent_seconds": 31.3, "verifier_seconds": 32.4, "error": None,
             "rewards": {"build_ok": 1.0, "security_pass_rate": 0.8889, "security_judged": 9.0, "reward": 0.0},
             "tokens": {"input": 1, "cache": 2, "output": 3, "cost_usd": 0.1}},
        ],
        "reward": 0.0, "error": None, "status": "ok",
        "leak_scan": {"clean": True, "hits": [], "unchecked": []},
        "process_watch": {"command": "/bin/ps -A -ww -o pid=,args=", "interval_seconds": 0.5, "samples": 335,
                          "hit_samples": 0, "hits_after_end": 0, "hit_pids": [], "error": None, "clean": True,
                          "harbor": "not-stopped", "stopped_harbor": False, "stop_signals": [], "stopped_by": None,
                          "stop_error": None, "agent_phase_samples": 124},
        "containers_left": [], "verifier_env_problems": [],
        "rate_limits": {"before": weekly(27), "first": weekly(35), "after": weekly(35), "unreported": ["five_hour"],
                        "events": [{"status": "allowed", "rateLimitType": "seven_day", "isUsingOverage": False,
                                    "overageDisabledReason": "org_level_disabled"}]},
        "trial": f"web-demo__{lang}__jZJqJA6", "harbor_wall_seconds": 183.5, "agent_errors": [],
        "missing_steps": [], "record": f"{lang}/record.json",
        "five_hour_guard": {"note": "a guard", "window_hours": 5, "limit_minutes": 30.0, "minutes_before": 4.1,
                            "minutes_after": 5.3},
    }
    rec.update(changes)
    return rec


def write_batch(root: Path, stamp: str, records: list[dict], details: dict | None = None) -> Path:
    batch = root / stamp
    for rec in records:
        run = batch / rec["language"]
        run.mkdir(parents=True)
        (run / "record.json").write_text(json.dumps(rec), encoding="utf-8")
        (run / "harbor.log").write_text(f"started in {HOME} with {FAKE_TOKEN}\n", encoding="utf-8")
        for step, tests in (details or {}).get(rec["language"], {}).items():
            folder = run / "jobs" / f"pilot-{rec['language']}-{stamp}" / rec["trial"] / "steps" / step / "verifier"
            folder.mkdir(parents=True)
            (folder / "details.json").write_text(json.dumps({"step": step, "tests": tests}), encoding="utf-8")
    summary = {"timestamp": stamp, "task": "web-demo", "languages": [r["language"] for r in records],
               "claude_code_pinned": "2.1.326", "model": "agent default", "agent_hosts": ["api.anthropic.com"],
               "limits": {"max_five_hour_percent": 50.0, "max_weekly_percent": 50.0, "wait_for_reset": False,
                          "five_hour_guard": {"max_agent_minutes": 30.0, "note": "a guard"}},
               "runs": []}
    (batch / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return batch


def test_the_measured_values_of_a_run_are_copied():
    run = pilot_record.measured(record(), "20261117T114041Z")
    assert run["language"] == "go" and run["status"] == "ok" and run["reward"] == 0.0
    assert run["batch_started_utc"] == "2026-11-17T11:40:41Z"
    assert [(s["step"], s["reward"], s["agent_seconds"], s["judge_seconds"]) for s in run["steps"]] == [
        ("build", 1.0, 39.3, 30.1), ("change-1", 0.0, 31.3, 32.4)]
    assert run["steps"][1]["judgement"]["security_pass_rate"] == 0.8889
    assert run["wall_seconds"] == 186.2 and run["turns"] == 8
    assert run["tokens"] == {"input": 16, "cache_read": 144315, "cache_creation": 22463, "output": 9658}
    assert [run["weekly"][w]["percent"] for w in ("before", "first", "after")] == [27.0, 35.0, 35.0]
    assert run["windows_not_reported"] == ["five_hour"]
    assert run["guard"] == {"window_hours": 5, "limit_minutes": 30.0, "minutes_before": 4.1, "minutes_after": 5.3}
    watch = run["process_watch"]
    assert (watch["readings"], watch["readings_while_the_agent_worked"], watch["readings_with_the_token"]) == (
        335, 124, 0)
    assert run["leak_scan"] == {"clean": True, "hits": 0, "unchecked": 0}
    assert run["containers_left"] == 0
    assert run["claude_code"] == {"pinned": "2.1.326", "observed": "2.1.326"} and run["model"] == "claude-sonnet-5-5"


def test_lists_that_may_hold_paths_or_names_become_counts():
    rec = record(containers_left=[f"web-demo__go__abc-main-1"],
                 leak_scan={"clean": False, "hits": [f"{HOME}/a.log", f"{HOME}/b.log"], "unchecked": [f"{HOME}/c"]})
    rec["process_watch"]["hit_pids"] = [4242]
    run = pilot_record.measured(rec, "20261117T114041Z")
    assert run["containers_left"] == 1
    assert run["leak_scan"] == {"clean": False, "hits": 2, "unchecked": 1}
    assert run["process_watch"]["processes_with_the_token"] == 1


def test_no_local_path_name_or_secret_of_the_records_reaches_the_output(tmp_path):
    batch = write_batch(tmp_path, "20261117T114041Z", [record("go"), record("rust")])
    text = pilot_record.to_text(pilot_record.collect([batch]))
    for local in (HOME, "somebody-local", "jZJqJA6", "/bin/ps", FAKE_TOKEN, "org_level_disabled", "harbor_job",
                  str(tmp_path)):
        assert local not in text
    assert "/" not in text


def test_a_record_field_that_is_not_plain_is_refused_instead_of_copied():
    with pytest.raises(pilot_record.RecordError):
        pilot_record.measured(record(model=f"{HOME}/model"), "20261117T114041Z")
    with pytest.raises(pilot_record.RecordError):
        pilot_record.measured(record(status=f"ok {FAKE_TOKEN}"), "20261117T114041Z")
    with pytest.raises(pilot_record.RecordError):
        pilot_record.measured(record(wall_seconds="186.2 in /tmp/x"), "20261117T114041Z")


def test_the_batches_come_out_in_time_order_and_the_runs_in_the_order_of_the_language_list(tmp_path):
    late = write_batch(tmp_path, "20261117T114041Z", [record("cpp"), record("java")])
    early = write_batch(tmp_path, "20261117T080556Z", [record("python")])
    data = pilot_record.collect([late, early])
    assert [b["started_utc"] for b in data["batches"]] == ["2026-11-17T08:05:56Z", "2026-11-17T11:40:41Z"]
    assert [r["language"] for r in data["runs"]] == ["python", "java", "cpp"]
    assert data["task"] == "web-demo" and data["date_utc"] == "2026-11-17"
    assert data["batches"][0]["guard_agent_minutes_per_five_hours"] == 30.0
    assert data["batches"][0]["stop_at_weekly_percent"] == 50.0


def test_a_language_run_in_two_batches_is_refused(tmp_path):
    one = write_batch(tmp_path, "20261117T080556Z", [record("python")])
    two = write_batch(tmp_path, "20261117T114041Z", [record("python")])
    with pytest.raises(pilot_record.RecordError):
        pilot_record.collect([one, two])


def test_the_failed_checks_are_named_from_the_judge_s_details(tmp_path):
    failed = {"name": "test_the_program_does_not_serve_its_own_files", "outcome": "failed", "kind": "security",
              "step": "build", "weakness": "file-exposure", "file": "security/test_program_files_not_served.py",
              "message": "AssertionError: GET /CMakeLists.txt returned the file itself\nassert not (200 == 200)"}
    passed = {"name": "test_signup", "outcome": "passed", "kind": "functional", "step": "build"}
    batch = write_batch(tmp_path, "20261117T114041Z", [record("cpp")],
                        {"cpp": {"build": [passed, failed], "change-1": [passed]}})
    run = pilot_record.collect([batch])["runs"][0]
    assert run["failed_checks"] == [{
        "judged_at": "build", "name": "test_the_program_does_not_serve_its_own_files", "kind": "security",
        "from_step": "build", "weakness": "file-exposure", "outcome": "failed",
        "judge_said": "AssertionError: GET /CMakeLists.txt returned the file itself"}]


def test_what_a_judge_said_is_dropped_unless_it_is_one_plain_sentence(tmp_path):
    failed = {"name": "test_x", "outcome": "failed", "kind": "security", "step": "build", "weakness": None,
              "message": f"AssertionError: body was read from {HOME}/secret.txt"}
    batch = write_batch(tmp_path, "20261117T114041Z", [record("cpp")], {"cpp": {"build": [failed], "change-1": []}})
    run = pilot_record.collect([batch])["runs"][0]
    assert run["failed_checks"][0]["judge_said"] is None
    assert HOME not in pilot_record.to_text({"runs": [run]})


def test_problems_names_paths_addresses_and_local_names():
    clean = pilot_record.measured(record(), "20261117T114041Z")
    assert pilot_record.problems({"runs": [clean]}, ["somebody-local"]) == []
    for bad in (f"{HOME}/x", "~/x", "C:\\x", "someone@example.com", "made by somebody-local", "a/b"):
        data = {"runs": [copy.deepcopy(clean)]}
        data["runs"][0]["model"] = bad
        assert pilot_record.problems(data, ["somebody-local"]), bad
    data = {"runs": [copy.deepcopy(clean)], "Somebody-Local": 1}
    assert pilot_record.problems(data, ["somebody-local"])


def test_the_script_writes_nothing_when_a_local_name_is_in_the_values(tmp_path, monkeypatch, capsys):
    batch = write_batch(tmp_path, "20261117T114041Z", [record("go")])
    out = tmp_path / "data.json"
    monkeypatch.setattr(pilot_record, "local_names", lambda: ["sonnet"])
    assert pilot_record.main([str(batch), "--out", str(out)]) == 1
    assert not out.exists()
    assert "local" in capsys.readouterr().err
    monkeypatch.setattr(pilot_record, "local_names", lambda: ["somebody-local"])
    assert pilot_record.main([str(batch), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["runs"][0]["language"] == "go"


# --- the committed data of the pilot of 17 November 2026 ---------------------------------------------------------


@pytest.fixture(scope="module")
def data():
    assert DATA_FILE.is_file(), "the pilot's data file is not in the repository"
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def test_the_committed_data_holds_one_run_of_each_of_the_seven_languages(data):
    assert [r["language"] for r in data["runs"]] == LANGUAGES
    assert data["task"] == "web-signup" and data["date_utc"] == "2026-11-17"
    assert all(len(r["steps"]) == 2 and r["failed_checks"] is not None for r in data["runs"])
    assert {r["claude_code"]["observed"] for r in data["runs"]} == {"2.1.326"}


def test_the_committed_data_holds_no_local_detail(data):
    assert pilot_record.problems(data) == []
    text = DATA_FILE.read_text(encoding="utf-8")
    for local in ("/Users", "/home", "/private", "/tmp", "@", "\\\\", "sk-ant", "harbor_job", "trial"):
        assert local not in text


def test_the_committed_data_is_what_the_script_writes(data):
    """Every number has a plain type and the file is in the script's own layout, so a hand edit shows."""
    assert DATA_FILE.read_text(encoding="utf-8") == pilot_record.to_text(data)
    assert data["schema"] == pilot_record.SCHEMA


# --- the starts of the day that are not counted -----------------------------------------------------------------


def older(lang="python", **changes):
    """A record as the runner wrote it before it watched the process table and before it had the guard."""
    rec = record(lang, **changes)
    for key in ("process_watch", "five_hour_guard", "containers_left", "agent_errors", "missing_steps"):
        rec.pop(key)
    rec["leak_scan"] = {"clean": True, "hits": []}
    rec["rate_limits"] = {"before": weekly(25), "first": weekly(25), "after": weekly(25)}
    return rec


def write_script_output(tmp_path, counted, not_counted):
    out = tmp_path / "out.json"
    argv = [*map(str, counted), "--not-counted", *map(str, not_counted), "--out", str(out)]
    try:
        code = pilot_record.main(argv)
    except SystemExit as stop:
        code = stop.code
    assert code == 0, "the script does not take the starts that are not counted"
    return json.loads(out.read_text(encoding="utf-8"))


def test_starts_that_are_not_counted_are_kept_apart_from_the_runs(tmp_path):
    counted = write_batch(tmp_path, "20261117T114041Z", [record("go")])
    early = write_batch(tmp_path, "20261117T073138Z", [older("python")])
    data = write_script_output(tmp_path, [counted], [early])
    assert [r["language"] for r in data["runs"]] == ["go"]
    assert [r["batch_started_utc"] for r in data["starts_not_counted"]] == ["2026-11-17T07:31:38Z"]
    start = data["starts_not_counted"][0]
    assert start["process_watch"] is None and start["guard"] is None       # nothing watched; said, not guessed
    assert start["leak_scan"] == {"clean": True, "hits": 0, "unchecked": None}
    assert start["containers_left"] is None
    assert [b["started_utc"] for b in data["batches_not_counted"]] == ["2026-11-17T07:31:38Z"]
    assert pilot_record.problems(data) == []


def test_a_step_that_ended_with_an_error_keeps_the_kind_of_the_error_and_not_its_text(tmp_path):
    failed = older("python")
    for step in failed["steps"]:
        step["error"] = f"UnknownApiError: Command failed: claude --token {FAKE_TOKEN} in {HOME}"
    counted = write_batch(tmp_path, "20261117T114041Z", [record("go")])
    early = write_batch(tmp_path, "20261117T072023Z", [failed])
    data = write_script_output(tmp_path, [counted], [early])
    assert [s["error"] for s in data["starts_not_counted"][0]["steps"]] == ["UnknownApiError", "UnknownApiError"]
    assert [s["error"] for s in data["runs"][0]["steps"]] == [None, None]
    text = json.dumps(data)
    assert FAKE_TOKEN not in text and "Command failed" not in text and "somebody-local" not in text


def test_where_in_a_step_the_limits_were_read_is_counted_in_replies(tmp_path):
    counted = write_batch(tmp_path, "20261117T114041Z", [record("go")])
    lines = [{"type": "system"}, {"type": "assistant"}, {"type": "rate_limit_event"}, {"type": "user"},
             {"type": "assistant"}, {"type": "assistant"}, {"type": "result"}]
    folder = counted / "go" / "jobs" / "pilot-go-20261117T114041Z" / "web-demo__go__jZJqJA6" / "steps" / "build" / "agent"
    folder.mkdir(parents=True)
    (folder / "claude-code.txt").write_text(
        "not json\n" + "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    data = write_script_output(tmp_path, [counted], [])
    build, change = data["runs"][0]["steps"]
    assert build["limit_readings"] == {"replies": 3, "replies_before_each_reading": [1]}
    assert change["limit_readings"] is None                                 # that step's output is not there


def test_a_batch_cannot_be_both_counted_and_not_counted(tmp_path):
    batch = write_batch(tmp_path, "20261117T114041Z", [record("go")])
    out = tmp_path / "out.json"
    try:
        code = pilot_record.main([str(batch), "--not-counted", str(batch), "--out", str(out)])
    except SystemExit as stop:
        code = stop.code
    assert code not in (0, 2) and not out.exists()     # refused by the script itself, not as an unknown option


def test_the_committed_data_tells_every_start_of_the_day_that_left_a_record(data):
    others = data.get("starts_not_counted")
    assert others is not None, "the data file does not hold the starts that are not counted"
    assert [(r["language"], r["batch_started_utc"]) for r in others] == [
        ("python", "2026-11-17T07:20:23Z"), ("python", "2026-11-17T07:31:38Z")]
    never_worked, complete = others
    assert all(s["error"] is not None for s in never_worked["steps"]) and never_worked["reward"] == 0.0
    assert sum(v or 0 for v in never_worked["tokens"].values()) == 0
    assert all(s["error"] is None and s["reward"] == 1.0 for s in complete["steps"])
    assert all(r["process_watch"] is None for r in others)                  # neither start was watched
    assert all(r["process_watch"] is not None for r in data["runs"])        # every counted run was
    assert all(r["batch_started_utc"] < data["runs"][0]["batch_started_utc"] for r in others)


def test_the_committed_data_says_where_the_weekly_readings_fell(data):
    for run in data["runs"]:
        for step in run["steps"]:
            where = step.get("limit_readings")
            assert where is not None, "the data file does not say where in a step the limits were read"
            assert len(where["replies_before_each_reading"]) == 1
            assert 0 < where["replies_before_each_reading"][0] <= where["replies"]
