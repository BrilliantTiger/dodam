"""The pilot's estimates, the changes to the schedule simulation they need, and the document people read.

The document is rendered from two data files; these tests fail when a figure in either language differs
from them, when a template carries a figure of its own, or when the estimates no longer follow from the
measured values.
"""
import copy
import datetime as dt
import json
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "tools" / "plan"
PILOT = ROOT / "tools" / "pilot"
DATA = ROOT / "bench" / "records" / "pilot-2026-11-17.json"
REPLAN = PLAN / "pilot_replan.json"
for folder in (PLAN, PILOT):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

try:
    import pilot_replan
except ImportError:  # each test then fails on its own instead of stopping the whole run
    pilot_replan = None
try:
    import render
except ImportError:
    render = None


@pytest.fixture(scope="module")
def sim():
    """The schedule simulation, loaded the way its own scripts load it."""
    subprocess.run([sys.executable, str(PLAN / "build.py")], check=True, capture_output=True)
    argv, sys.argv = sys.argv, [sys.argv[0], "1"]
    try:
        import montecarlo as M
        import montecarlo2 as MC
    finally:
        sys.argv = argv
    return M, MC


def calm(M, MC, **kw):
    """One run with nothing left to chance: capacity without limit, no risks, no overruns."""
    random.seed(1)
    return MC.run(list(M.SCEN)[0], True, cap_scale=100, risk_scale=0, over_scale=0, **kw)


# --- the schedule simulation -------------------------------------------------------------------------------------


def test_a_finished_task_ends_on_its_day_and_nothing_waits_for_it(sim):
    M, MC = sim
    plain = calm(M, MC)
    late = calm(M, MC, finished={"b_sample": "2026-12-20"})
    assert late["b_sample"] == MC.off("2026-12-20")
    assert late["b_tasks10"] > plain["b_tasks10"]          # its successor starts the day after it ended
    early = calm(M, MC, finished={"b_tasks10": "2026-10-30", "b_runner": "2026-10-30", "b_pilot": "2026-10-30"})
    assert early["b_baseline"] <= plain["b_baseline"]


def test_a_finished_task_takes_no_capacity(sim):
    """Tasks that share weeks with the measuring tool and that nothing waits for: once they are finished,
    the measuring tool gets their share of a tight capacity and ends earlier."""
    M, MC = sim
    tool = MC.ITEMS[MC.IDX["b_runner"]]
    beside = [it["id"] for k, it in enumerate(MC.ITEMS)
              if not it.get("milestone") and not it.get("ongoing") and it.get("ai_days", 0) > 0 and not MC.SUCC[k]
              and it["id"] != "b_runner" and MC.BS(it) <= MC.BE(tool) and MC.BE(it) >= MC.BS(tool)]
    assert beside
    kw = dict(cap_scale=0.05, risk_scale=0, over_scale=0, until="b_runner")
    random.seed(3)
    tight = MC.run(list(M.SCEN)[0], True, **kw)
    random.seed(3)
    freed = MC.run(list(M.SCEN)[0], True, finished={i: "2026-10-30" for i in beside}, **kw)
    assert freed["b_runner"] < tight["b_runner"]


def test_a_task_never_takes_fewer_days_than_its_shortest_length(sim):
    M, MC = sim
    plain = calm(M, MC)
    held = calm(M, MC, min_days={"b_baseline": 90})
    assert held["b_baseline"] >= plain["b_baseline"] + 60
    assert held["g_dir"] > plain["g_dir"]


def test_a_run_can_stop_at_a_named_task_with_the_same_dates_up_to_it(sim):
    M, MC = sim
    random.seed(5)
    whole = MC.run(list(M.SCEN)[0], True)
    random.seed(5)
    short = MC.run(list(M.SCEN)[0], True, until="g_dir")
    assert short["g_dir"] == whole["g_dir"] and short["b_baseline"] == whole["b_baseline"]
    assert short["rel_10"] is None


# --- the guard's arithmetic --------------------------------------------------------------------------------------


def test_runs_per_night_follow_the_guard():
    night = pilot_replan.runs_per_night
    # 100 s of agent time per run, limit 300 s per window: a fourth run starts only when the first left the window
    assert night(100, 600, 300, 3600, 1800) == 3      # three runs fill the half hour and the limit
    assert night(100, 600, 300, 3600, 3600) == 3      # the guard holds the fourth until the hour is over
    assert night(100, 600, 300, 3600, 4800) == 4      # the first left the window at 4,200 s; one more fits
    assert night(100, 600, 250, 3600, 3600) == 3      # the third starts just below the limit
    assert night(100, 600, 200, 3600, 3600) == 2
    assert night(10, 4000, 300, 3600, 3600) == 0      # a run that does not end in the night does not start


def test_runs_per_night_end_for_every_working_time():
    for hundredths in range(100, 1001):
        factor = hundredths / 100
        assert pilot_replan.runs_per_night(79.2 * factor, 223.9 + 79.2 * (factor - 1), 1800, 18000, 28800) >= 1


def test_usage_adds_up_the_measured_values():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    used = pilot_replan.usage(data)
    agent = [sum(s["agent_seconds"] for s in r["steps"]) for r in data["runs"]]
    assert used["agent_seconds_mean"] == round(sum(agent) / len(agent), 1)
    assert used["safety"]["readings"] == sum(r["process_watch"]["readings"] for r in data["runs"])
    assert used["weekly"]["later_runs"] == 6 and used["weekly"]["moved_during_later_runs_points"] == 0
    changed = copy.deepcopy(data)
    changed["runs"][-1]["weekly"]["after"]["percent"] = 37.0
    assert pilot_replan.usage(changed)["weekly"]["moved_during_later_runs_points"] == 2.0
    changed["runs"][0]["process_watch"]["readings_with_the_token"] = 1
    assert pilot_replan.usage(changed)["safety"]["readings_with_the_token"] == 1


def test_capacity_follows_from_the_mean_working_time():
    used = pilot_replan.usage(json.loads(DATA.read_text(encoding="utf-8")))
    cap = pilot_replan.capacity(used, "2026-12-12", "2027-01-01")
    assert cap["assumptions"]["runs"] == 630 and cap["row"]["nights"] == 21
    for case in cap["cases"]:
        assert case["nights"] * case["runs_per_night"] >= 630 > (case["nights"] - 1) * case["runs_per_night"]
        assert (case["starts_per_window"] - 1) * case["agent_seconds_per_run"] < 30 * 60
        assert case["starts_per_window"] * case["agent_seconds_per_run"] >= 30 * 60
    slower = copy.deepcopy(used)
    slower["agent_seconds_mean"] *= 3
    assert pilot_replan.capacity(slower, "2026-12-12", "2027-01-01")["cases"][0]["nights"] > cap["cases"][0]["nights"]


# --- the committed estimates -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rebuilt():
    return pilot_replan.build()


def test_the_committed_usage_and_capacity_are_what_the_script_computes(rebuilt):
    committed = json.loads(REPLAN.read_text(encoding="utf-8"))
    assert committed["usage"] == rebuilt["usage"]
    assert committed["capacity"] == rebuilt["capacity"]


def test_the_committed_schedule_is_what_the_simulation_gives(rebuilt):
    """Dates may differ by one step of the simulation (a week) and shares by a few runs between machines."""
    committed = json.loads(REPLAN.read_text(encoding="utf-8"))["schedule"]
    again = rebuilt["schedule"]
    assert {k: v for k, v in committed.items() if k != "capacities"} == {
        k: v for k, v in again.items() if k != "capacities"}
    assert committed["capacities"].keys() == again["capacities"].keys()
    for name, cases in committed["capacities"].items():
        assert cases.keys() == again["capacities"][name].keys()
        for case, result in cases.items():
            for key, value in result.items():
                other = again["capacities"][name][case][key]
                if isinstance(value, dict):
                    for q in ("p50", "p80"):
                        gap = dt.date.fromisoformat(value[q]) - dt.date.fromisoformat(other[q])
                        assert abs(gap.days) <= 7, (name, case, key, q)
                else:
                    assert abs(value - other) <= 0.03, (name, case, key)


# --- the document ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_the_document_is_what_the_data_renders(lang):
    assert render.document(lang).read_text(encoding="utf-8") == render.render(lang)


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_a_template_holds_no_figure_of_its_own(lang):
    assert render.own_digits(lang) == []


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_a_changed_number_in_the_data_changes_the_document(lang):
    data = json.loads(DATA.read_text(encoding="utf-8"))
    replan = json.loads(REPLAN.read_text(encoding="utf-8"))
    now = render.document(lang).read_text(encoding="utf-8")
    one = copy.deepcopy(data)
    one["runs"][2]["wall_seconds"] += 1
    assert render.render(lang, data=one) != now
    two = copy.deepcopy(replan)
    two["capacity"]["cases"][0]["nights"] += 1
    assert render.render(lang, replan=two) != now
    three = copy.deepcopy(replan)
    three["schedule"]["capacities"]["planning"]["after_x1"]["p_baseline_by_row_end"] += 0.01
    assert render.render(lang, replan=three) != now


def test_both_languages_carry_the_same_figures():
    """Every figure of the Korean document is in the English one and the other way round, as often."""
    figures = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
    dates = re.compile(r"(\d{4})년 (\d{1,2})월 (\d{1,2})일|(\d{1,2})월 (\d{1,2})일|(\d{1,2})월"
                       r"|(\d{1,2}) [A-Z][a-z]+ (\d{4})|(\d{1,2}) [A-Z][a-z]+\b")
    def bag(lang):
        text = dates.sub(" ", render.document(lang).read_text(encoding="utf-8"))
        return sorted(figures.findall(text))
    assert bag("ko") == bag("en")


def test_the_document_refuses_data_it_was_not_written_for():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    leaked = copy.deepcopy(json.loads(REPLAN.read_text(encoding="utf-8")))
    leaked["usage"]["safety"]["readings_with_the_token"] = 1
    with pytest.raises(render.RenderError):
        render.render("ko", replan=leaked)
    passed = copy.deepcopy(data)
    for step in passed["runs"][-1]["steps"]:
        step["reward"] = 1.0
    with pytest.raises(render.RenderError):
        render.render("en", data=passed)


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_the_document_stands_on_its_own(lang):
    text = render.document(lang).read_text(encoding="utf-8")
    assert not re.search(r"§|\b[A-Z]{2,5}-\d+\b|\bCWE\b|\bCVE\b", text)                 # no codes, no tracker numbers
    assert not re.search(r"참고하|참조하|자세한 내용은|문서를 보|\bsee the\b|\brefer to\b|\.md\b|https?://", text)
    assert not re.search(r"/Users|/home/|@|~/", text)
    for word in ("b_baseline", "g_dir", "after_x", "p50", "seven_day", "five_hour"):
        assert word not in text
