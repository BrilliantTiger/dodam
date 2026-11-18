"""The scripts whose tables the documents cite say so when a table does not come out."""
import importlib.util
import json
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1]


def script(name: str):
    spec = importlib.util.spec_from_file_location(name, BENCH / "experiments" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MEASURED = {"held_before_mb": 100.0, "high_before_mb": 110.0, "high_after_mb": 400.0, "limit": "memory",
            "ended_by_the_kernel": 0, "seconds": 0.1, "measurements": 10, "longest_gap": 0.002}
AS_MEANT = {"exit_code": 0, "outcomes": ["passed", "failed", "passed"], "kernel_ended_the_case_runner": False}


def memory_experiment(monkeypatch, capsys, run_of) -> tuple[bool, dict]:
    """The memory experiment in its quick mode with made-up runs: whether it ended in an error, and
    what it printed. run_of(stop) is what one run gives, with and without the judge's stop."""
    overshoot = script("memory_overshoot")
    monkeypatch.setattr(overshoot, "judged", lambda program, memory_mb, limit_mb, stop: dict(run_of(stop)))
    try:
        overshoot.main(1)
        failed = False
    except SystemExit as stop:
        failed = bool(stop.code)
    return failed, json.loads(capsys.readouterr().out)


def test_the_memory_experiment_ends_normally_when_every_run_was_measured_and_judged_as_meant(monkeypatch, capsys):
    failed, report = memory_experiment(monkeypatch, capsys, lambda stop: {**MEASURED, **AS_MEANT})
    assert not failed and report["rows_that_did_not_come_out"] == [] and len(report["rows"]) == 16


@pytest.mark.parametrize("exit_code", [3, 137])
def test_the_memory_experiment_ends_in_an_error_when_runs_without_the_stop_gave_no_judgement(monkeypatch, capsys, exit_code):
    """Without the judge's stop the kernel chooses what it ends. A run that did not start, or that
    gave no judgement at all, is not among what may happen there."""
    nothing = {"exit_code": exit_code, "outcomes": [], "kernel_ended_the_case_runner": False}
    failed, report = memory_experiment(monkeypatch, capsys, lambda stop: {**MEASURED, **AS_MEANT} if stop else nothing)
    assert failed, "four rows gave no judgement and the script ended normally"
    assert len(report["rows_that_did_not_come_out"]) == 4, report["rows_that_did_not_come_out"]


def test_without_the_stop_a_run_in_which_the_kernel_ended_the_case_runner_is_a_judgement(monkeypatch, capsys):
    """The case runner is what the kernel ends after the program. Then the hidden test hands back no
    measurement, but the case failed for memory, the others were judged and the result says what
    the kernel ended. With the stop in place the kernel has nothing to end: there it is an error."""
    runner_ended = {**AS_MEANT, "kernel_ended_the_case_runner": True}
    failed, report = memory_experiment(monkeypatch, capsys, lambda stop: {**MEASURED, **AS_MEANT} if stop else runner_ended)
    assert not failed, report["rows_that_did_not_come_out"]
    assert [row.get("kernel_ended_the_case_runner") for row in report["rows"] if not row["stop_128_mb_below_the_cap"]] == [1] * 4
    failed, report = memory_experiment(monkeypatch, capsys, lambda stop: runner_ended)
    assert failed and len(report["rows_that_did_not_come_out"]) == 12


def test_without_the_stop_a_run_that_handed_back_nothing_for_no_reason_is_an_error(monkeypatch, capsys):
    failed, report = memory_experiment(monkeypatch, capsys, lambda stop: {**MEASURED, **AS_MEANT} if stop else AS_MEANT)
    assert failed and len(report["rows_that_did_not_come_out"]) == 4
