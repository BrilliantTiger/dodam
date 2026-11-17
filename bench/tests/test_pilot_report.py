"""The pilot's estimates, the changes to the schedule simulation they need, and the document people read.

The document is rendered from two data files; these tests fail when a figure in either language differs
from them, when a template carries a figure of its own, or when the estimates no longer follow from the
measured values.
"""
import copy
import datetime as dt
import json
import math
import random
import re
import shutil
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


def committed() -> dict:
    return json.loads(REPLAN.read_text(encoding="utf-8"))


def agent_time(run: dict) -> float:
    return sum(s["agent_seconds"] for s in run["steps"])


def test_usage_adds_up_the_measured_values():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    used = pilot_replan.usage(data)
    agent = [agent_time(r) for r in data["runs"]]
    assert used["agent_seconds_mean"] == round(sum(agent) / len(agent), 1)
    assert used["safety"]["readings"] == sum(r["process_watch"]["readings"] for r in data["runs"])
    assert used["weekly"]["later_runs"] == 6
    changed = copy.deepcopy(data)
    changed["runs"][0]["process_watch"]["readings_with_the_token"] = 1
    assert pilot_replan.usage(changed)["safety"]["readings_with_the_token"] == 1


def test_the_weekly_bound_rests_on_what_lies_wholly_between_the_two_readings():
    """A step reports the weekly figure once, early. The first step of the first later run and the last step
    of the last one are cut by a reading, so they do not count towards what the two readings bracket."""
    data = json.loads(DATA.read_text(encoding="utf-8"))
    weekly = pilot_replan.usage(data)["weekly"]
    later = [r for r in data["runs"] if r["batch_started_utc"] != data["runs"][0]["batch_started_utc"]]
    first = min(later, key=lambda r: r["guard"]["minutes_before"])
    last = max(later, key=lambda r: r["guard"]["minutes_after"])
    between = round(sum(agent_time(r) for r in later) - first["steps"][0]["agent_seconds"]
                    - last["steps"][-1]["agent_seconds"], 1)
    assert weekly.get("agent_seconds_wholly_between_the_readings") == between
    assert weekly.get("steps_wholly_between_the_readings") == sum(len(r["steps"]) for r in later) - 2
    mean = round(sum(agent_time(r) for r in data["runs"]) / len(data["runs"]), 1)
    assert weekly["one_run_below_points"] == round(mean / between, 2)
    assert weekly["baseline_below_points"] == round(weekly["one_run_below_points"] * 630, 1)
    assert weekly.get("first_reading_after_replies") == first["steps"][0]["limit_readings"]["replies_before_each_reading"][0]
    assert weekly.get("last_reading_step_replies") == last["steps"][-1]["limit_readings"]["replies"]
    assert weekly.get("moved_between_the_two_readings_points") == 0
    moved = copy.deepcopy(data)
    next(r for r in moved["runs"] if r["language"] == last["language"])["weekly"]["after"]["percent"] += 2
    assert pilot_replan.usage(moved)["weekly"].get("moved_between_the_two_readings_points") == 2


def test_usage_tells_the_whole_day_and_scopes_the_watch_to_the_runs_it_covered():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    used = pilot_replan.usage(data)
    day, safety = used.get("day"), used["safety"]
    assert day is not None, "the estimates do not tell what else was started that day"
    assert (day["starts_with_a_record"], day["counted"], day["not_counted"]) == (9, 7, 2)
    assert [e["started_hhmm_utc"] for e in day["not_counted_starts"]] == ["07:20", "07:31"]
    assert [e["process_table_watched"] for e in day["not_counted_starts"]] == [False, False]
    assert [e["tokens"] > 0 for e in day["not_counted_starts"]] == [False, True]
    # both starts came before the change that keeps the token off command lines was first committed
    assert all(e["minutes_before_the_fix_was_first_committed"] > 0 for e in day["not_counted_starts"])
    assert day["counted_runs_started_before_the_fix_was_merged"] + day["counted_runs_started_after_the_fix_was_merged"] == 7
    assert (safety["watched_runs"], safety["starts_without_a_watch"]) == (7, 2)
    assert safety["leak_scans"] == 9 and safety["clean_leak_scans"] == 9
    # a start that is not counted and was not watched adds no reading; one that was watched would
    assert safety["readings"] == sum(r["process_watch"]["readings"] for r in data["runs"])
    unscanned = copy.deepcopy(data)
    unscanned["starts_not_counted"][1]["leak_scan"].update(clean=False, hits=1)
    assert pilot_replan.usage(unscanned)["safety"]["leak_hits"] == 1


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


def test_the_next_measurements_resolve_what_they_are_said_to_within_the_guard():
    used = pilot_replan.usage(json.loads(DATA.read_text(encoding="utf-8")))
    cap = pilot_replan.capacity(used, "2026-12-12", "2027-01-01")
    nxt = cap.get("next_measurements")
    assert nxt is not None, "the estimates do not work out what the next measurements can resolve"
    agent, wall = used["agent_seconds_mean"], used["wall_seconds_mean"]
    night = lambda factor: pilot_replan.runs_per_night(
        round(agent * factor, 1), round(wall + (factor - 1) * agent, 1), 1800, 18000, 28800)
    same, large = nxt["same_model"], nxt["largest_model"]
    # the first night is as many runs as the guard lets one night hold, and no more
    assert same["runs"] == night(1) == cap["cases"][0]["runs_per_night"]
    # one run less lies between the first and the last reading; an unmoved figure bounds 630 runs by this
    assert same["runs_wholly_between_first_and_last_reading"] == same["runs"] - 1
    assert same["baseline_below_points_if_unmoved"] == round(630 / (same["runs"] - 1), 1)
    room = used["weekly"]["room_to_the_stop_points"]
    assert same["unmoved_shows_the_baseline_fits_that_room"] is (630 / (same["runs"] - 1) < room)
    assert same["baseline_points_if_moved_one_step_at_most"] == round(2 * 630 / (same["runs"] - 1), 1) > room
    assert (same["models_left"], same["tasks_left"]) == (2, 9) and (large["models_left"], large["tasks_left"]) == (1, 9)
    # the largest model, once per language: up to which working time the seven runs start in one window, and
    # up to which they still end in one night
    starts = lambda factor: math.floor(1800 / round(agent * factor, 1) - 1e-9) + 1
    edge = large["all_start_in_one_window_up_to_factor"]
    assert starts(edge) >= 7 > starts(round(edge + 0.01, 2))
    edge = large["all_end_in_one_night_up_to_factor"]
    assert night(edge) >= 7 > night(round(edge + 0.01, 2))
    assert large["runs_in_the_night_at_factor_4"] == min(7, night(4))
    assert large["one_run_below_points_if_unmoved"] == round(1 / 6, 2)


# --- the committed estimates -------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rebuilt():
    """The estimates worked out again with the first of the committed seeds; the forecast to 1.0 is not run here."""
    try:
        return pilot_replan.build(with_forecast=False, seeds=pilot_replan.SEEDS[:1])
    except (TypeError, AttributeError):
        return None  # an estimates script that cannot be run for one seed


def test_the_committed_usage_and_capacity_are_what_the_script_computes(rebuilt):
    assert rebuilt is not None, "the estimates cannot be worked out again for one stated seed"
    assert committed()["usage"] == rebuilt["usage"]
    assert committed()["capacity"] == rebuilt["capacity"]


def close(a: dict, b: dict, where) -> None:
    """Dates may differ by one step of the simulation (a week) and shares by a few runs between machines."""
    for key in ("b_tasks10", "b_runner", "b_baseline", "g_dir"):
        for q in ("p50", "p80"):
            gap = dt.date.fromisoformat(a[key][q]) - dt.date.fromisoformat(b[key][q])
            assert abs(gap.days) <= 7, (where, key, q)
    for key in ("p_baseline_by_row_end", "p_gate_by_its_date"):
        assert abs(a[key] - b[key]) <= 0.03, (where, key)


def test_the_committed_schedule_is_what_the_simulation_gives_for_its_first_seed(rebuilt):
    assert rebuilt is not None, "the estimates cannot be worked out again for one stated seed"
    was, again = committed()["schedule"], rebuilt["schedule"]
    for key in ("sims_per_seed", "tasks_rolled", "tasks_added_later_and_left_out", "pilot_row", "baseline_row",
                "gate_day", "working_days_a_week", "upper_percent"):
        assert was[key] == again[key], key
    assert was["seeds"][0] == again["seeds"][0] and len(again["seeds"]) == 1
    assert was["capacities"].keys() == again["capacities"].keys()
    for name, cases in was["capacities"].items():
        assert cases.keys() == again["capacities"][name].keys()
        for case, result in cases.items():
            assert result["seeds"][0]["seed"] == was["seeds"][0]
            close(result["seeds"][0], again["capacities"][name][case], (name, case))


def test_the_schedule_is_run_for_the_plan_s_capacity_and_for_today_s_and_each_effect_apart():
    sched = committed()["schedule"]
    assert sched.get("working_days_a_week") == {"planning": 15, "claude_dedicated": 5, "claude_shared": 4}
    assert len(sched.get("seeds", [])) >= 5
    for name, cases in sched["capacities"].items():
        assert list(cases) == ["before", "finished_only", "numbers_only", "after_x1", "after_x2", "after_x4"], name
        for case, result in cases.items():
            assert [s["seed"] for s in result["seeds"]] == sched["seeds"]
            for key in ("p_baseline_by_row_end", "p_gate_by_its_date"):        # the pooled figure lies among the seeds'
                shares = [s[key] for s in result["seeds"]]
                assert min(shares) <= result[key] <= max(shares), (name, case, key)
    # less capacity never makes a date earlier
    for case in sched["capacities"]["planning"]:
        dates = [sched["capacities"][name][case]["g_dir"]["p50"] for name in ("planning", "claude_dedicated", "claude_shared")]
        assert dates == sorted(dates), case


def test_the_noise_published_is_the_widest_spread_between_the_seeds():
    sched = committed()["schedule"]
    middle = upper = chance = 0
    days = lambda values: (dt.date.fromisoformat(max(values)) - dt.date.fromisoformat(min(values))).days
    for cases in sched["capacities"].values():
        for result in cases.values():
            assert result.get("seeds"), "the schedule is not run with several seeds"
            for key in ("b_baseline", "g_dir"):
                middle = max(middle, days([s[key]["p50"] for s in result["seeds"]]))
                upper = max(upper, days([s[key]["p80"] for s in result["seeds"]]))
            for key in ("p_baseline_by_row_end", "p_gate_by_its_date"):
                shares = [s[key] for s in result["seeds"]]
                chance = max(chance, round((max(shares) - min(shares)) * 100))
    assert sched.get("noise") == {"middle_dates_move_up_to_days": middle, "upper_dates_move_up_to_days": upper,
                                  "chances_move_up_to_points": chance}


def test_the_forecast_states_its_seeds_and_the_plan_s_dates_are_one_of_them():
    fc = committed().get("forecast")
    assert fc is not None, "the estimates do not hold the forecast's dates seed by seed"
    anchors = json.loads((PLAN / "anchors.json").read_text(encoding="utf-8"))
    own = fc["planning"]["seeds"][0]
    assert own["seed"] == fc["plan_seed"] == 2029 and fc["planning"]["sims_per_seed"] == 2000
    for key in ("g_dir", "rel_10"):
        assert own[key] == {q: anchors[key][q] for q in ("p50", "p80")}, key
    assert fc["planning"]["tasks"] == committed()["schedule"]["tasks_rolled"]
    assert fc["planning_with_every_task"]["tasks"] == fc["planning"]["tasks"] + \
        committed()["schedule"]["tasks_added_later_and_left_out"]
    for name in ("planning", "two_companies", "claude_dedicated", "claude_shared", "planning_with_every_task"):
        case = fc[name]
        assert [s["seed"] for s in case["seeds"]] == fc["seeds"] and len(fc["seeds"]) >= 5
        for key in ("g_dir", "rel_10"):
            for q in ("p50", "p80"):
                values = [s[key][q] for s in case["seeds"]]
                found = case[f"{key}_{q}"]
                assert (found["earliest"], found["latest"]) == (min(values), max(values)), (name, key, q)
                apart = (dt.date.fromisoformat(max(values)) - dt.date.fromisoformat(min(values))).days
                assert found["days_apart"] == apart and found["weeks_apart"] == round(apart / 7)


# --- the plan ----------------------------------------------------------------------------------------------------

PLAN_DOC = ROOT / "docs" / "plan" / "2026-10-29-plan-to-1.0.ko.md"


@pytest.fixture
def plan_copy(tmp_path):
    """A copy of the plan's scripts and data, so that running them again leaves the repository as it is."""
    folder = tmp_path / "tools" / "plan"
    shutil.copytree(PLAN, folder, ignore=shutil.ignore_patterns("__pycache__"))
    return folder


def test_the_plan_s_milestone_dates_are_what_one_command_gives(plan_copy):
    """anchors.py states its seed and its number of runs; run again, it gives the committed dates."""
    subprocess.run([sys.executable, "build.py"], cwd=plan_copy, check=True, capture_output=True)
    subprocess.run([sys.executable, "anchors.py"], cwd=plan_copy, check=True, capture_output=True)
    again = json.loads((plan_copy / "anchors.json").read_text(encoding="utf-8"))
    was = json.loads((PLAN / "anchors.json").read_text(encoding="utf-8"))
    assert again == was


def test_the_plan_document_is_what_its_scripts_write(plan_copy):
    out = plan_copy / "plan.md"
    subprocess.run([sys.executable, "build.py"], cwd=plan_copy, check=True, capture_output=True)
    subprocess.run([sys.executable, "build_doc.py", str(out)], cwd=plan_copy, check=True, capture_output=True)
    assert out.read_text(encoding="utf-8") == PLAN_DOC.read_text(encoding="utf-8")


def test_the_plan_says_which_tasks_its_dates_come_from_and_under_which_capacity():
    text = PLAN_DOC.read_text(encoding="utf-8")
    sched, fc = committed()["schedule"], committed().get("forecast") or {}
    import wbs_data
    tasks = wbs_data.ITEMS
    rolled, added = sched.get("tasks_rolled"), sched.get("tasks_added_later_and_left_out")
    assert rolled is not None and rolled + added == len(tasks)
    assert f"작업 {len(tasks)}개" in text and f"그 가운데 {rolled}개로 냈고" in text
    assert f"씨앗 {fc.get('plan_seed')}로 2,000번" in text
    today = sched["capacities"].get("claude_shared", {}).get("after_x1")
    assert today is not None, "the schedule is not simulated for today's capacity"
    y, m, d = (int(x) for x in today["g_dir"]["p50"].split("-"))
    assert f"지금 조건(Claude 계정 하나를 나눠 쓴다, 주 약 4작업일로 셈)" in text
    assert f"방향 고정 관문의 가운데 날짜가 {y}년 {m}월 {d}일" in text
    assert "잰 값이 아니라 가정" in text


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
    replan = committed()
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
    assert "claude_shared" in replan["schedule"]["capacities"], "the schedule is not simulated for today's capacity"
    assert "day" in replan["usage"], "the estimates do not tell what else was started that day"
    four = copy.deepcopy(replan)
    four["schedule"]["capacities"]["claude_shared"]["after_x1"]["g_dir"]["p80"] = "2027-12-01"
    assert render.render(lang, replan=four) != now
    five = copy.deepcopy(replan)
    five["usage"]["day"]["not_counted_starts"][1]["wall_seconds"] += 1
    assert render.render(lang, replan=five) != now


def test_both_languages_carry_the_same_figures():
    """Every figure of the Korean document is in the English one and the other way round, as often."""
    figures = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
    dates = re.compile(r"(\d{4})년 (\d{1,2})월 (\d{1,2})일|(\d{1,2})월 (\d{1,2})일|(\d{1,2})월"
                       r"|(\d{1,2}) [A-Z][a-z]+ (\d{4})|(\d{1,2}) [A-Z][a-z]+\b")
    def bag(lang):
        text = dates.sub(" ", render.document(lang).read_text(encoding="utf-8"))
        return sorted(figures.findall(text))
    assert bag("ko") == bag("en")


def refused(lang: str, **changed) -> bool:
    try:
        render.render(lang, **changed)
    except render.RenderError:
        return True
    return False


def test_the_document_refuses_data_it_was_not_written_for():
    data = json.loads(DATA.read_text(encoding="utf-8"))
    leaked = copy.deepcopy(committed())
    leaked["usage"]["safety"]["readings_with_the_token"] = 1
    assert refused("ko", replan=leaked)
    passed = copy.deepcopy(data)
    for step in passed["runs"][-1]["steps"]:
        step["reward"] = 1.0
    assert refused("en", data=passed)


def test_the_document_refuses_data_that_would_make_its_scoped_statements_untrue():
    replan = committed()
    assert "day" in replan["usage"], "the estimates do not tell what else was started that day"
    watched = copy.deepcopy(replan)          # it says the two earlier starts were not watched
    watched["usage"]["day"]["not_counted_starts"][1]["process_table_watched"] = True
    assert refused("ko", replan=watched)
    fewer = copy.deepcopy(replan)            # it says the watch covered every counted run
    fewer["usage"]["safety"]["watched_runs"] -= 1
    assert refused("en", replan=fewer)
    likely = copy.deepcopy(replan)           # it says the chance under today's capacity is small
    likely["schedule"]["capacities"]["claude_shared"]["after_x1"]["p_baseline_by_row_end"] = 0.5
    assert refused("ko", replan=likely)
    other = copy.deepcopy(replan)            # it says the rise comes from the pilot's numbers
    plan = other["schedule"]["capacities"]["planning"]
    plan["finished_only"]["p_baseline_by_row_end"] = plan["after_x1"]["p_baseline_by_row_end"]
    assert refused("en", replan=other)
    open_question = copy.deepcopy(replan)    # it says an unmoved figure shows the baseline fits the room
    open_question["capacity"]["next_measurements"]["same_model"]["unmoved_shows_the_baseline_fits_that_room"] = False
    assert refused("ko", replan=open_question)


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_the_document_stands_on_its_own(lang):
    text = render.document(lang).read_text(encoding="utf-8")
    assert not re.search(r"§|\b[A-Z]{2,5}-\d+\b|\bCWE\b|\bCVE\b", text)                 # no codes, no tracker numbers
    assert not re.search(r"참고하|참조하|자세한 내용은|문서를 보|\bsee the\b|\brefer to\b|\.md\b|https?://", text)
    assert not re.search(r"/Users|/home/|@|~/", text)
    for word in ("b_baseline", "g_dir", "after_x", "p50", "seven_day", "five_hour", "claude_shared", "numbers_only"):
        assert word not in text


# --- every chance and every kept date names its conditions -------------------------------------------------------

PLACES = re.compile(r"\{\{(planning|claude_shared|claude_dedicated)\.(?:before|finished_only|numbers_only|after_x\d)"
                    r"\.(?:p_|b_baseline|g_dir)")
QUOTES = [("charter", "ko"), ("charter", "en"), ("readme", "ko"), ("readme", "en"), ("owner_list", "ko")]


def blocks(text: str, by_heading: bool) -> list[str]:
    """The places of a template: its sections when it has headings, else its runs of lines without a gap."""
    if by_heading:
        return re.split(r"(?m)^#{1,6} ", text)
    return re.split(r"\n\s*\n", text)


def places_without_today(text: str, by_heading: bool) -> list[str]:
    missing = []
    for block in blocks(text, by_heading):
        named = set(PLACES.findall(block))
        if "@table claude_shared" in block:
            named |= {"claude_shared"}
        if "@table claude_dedicated" in block:
            named |= {"claude_dedicated"}
        if "planning" in named and not {"claude_shared", "claude_dedicated"} <= named:
            missing.append(block[:80])
    return missing


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_wherever_the_document_gives_a_chance_at_the_plan_s_capacity_it_gives_today_s_beside_it(lang):
    text = render.template(lang).read_text(encoding="utf-8")
    assert "{{claude_shared.after_x1.p_baseline_by_row_end}}" in text, "the document gives no chance for today's capacity"
    assert places_without_today(text, by_heading=True) == []


def quote_template(name: str, lang: str) -> Path:
    return PILOT / f"{name}.{lang}.tmpl"


@pytest.mark.parametrize("name,lang", QUOTES)
def test_the_lines_about_the_pilot_in_a_hand_kept_document_are_rendered_from_the_data(name, lang):
    """The charter (its rows, its paragraphs and its history rows), the status line of the README and the
    owner's list carry these lines word for word, so a figure in them cannot drift from the data."""
    source = quote_template(name, lang)
    assert source.is_file(), f"the {lang} {name} lines about the pilot are not tied to the data"
    kept = {"charter": ROOT / "docs" / f"charter.{lang}.md", "owner_list": ROOT / "TODO.md",
            "readme": ROOT / ("README.ko.md" if lang == "ko" else "README.md")}[name]
    have = set(kept.read_text(encoding="utf-8").splitlines())
    lines = [line for line in render.render(lang, text=source.read_text(encoding="utf-8")).splitlines() if line.strip()]
    assert lines and [line for line in lines if line not in have] == []
    digits = re.compile(r"(?<![A-Za-z\d.])v\d\.\d+(?![\d.])|(?<![\d.,])1\.0(?![\d.])|11월 일정")   # names, not figures
    bare = render.PLACE.sub("", digits.sub("", source.read_text(encoding="utf-8")))
    assert not re.search(r"\d", bare), "a figure stands in the template instead of coming from the data"


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_wherever_the_charter_gives_a_chance_at_the_plan_s_capacity_it_gives_today_s_beside_it(lang):
    source = quote_template("charter", lang)
    assert source.is_file(), f"the {lang} charter's lines about the pilot are not tied to the data"
    text = source.read_text(encoding="utf-8")
    assert places_without_today(text, by_heading=False) == []
    rows = [line for line in text.splitlines() if line.startswith("| v0.8 |")]
    assert len(rows) == 1 and {"planning", "claude_shared", "claude_dedicated"} <= set(PLACES.findall(rows[0]))


def test_the_owner_s_list_scopes_what_it_says_about_the_token():
    text = (ROOT / "TODO.md").read_text(encoding="utf-8")
    assert "실행하는 동안 명령줄에 한 번도 보이지 않았고" not in text
    safety = committed()["usage"]["safety"]
    assert f"지켜본 {safety.get('watched_runs')}번에서는 토큰이 명령줄에 보이지 않았다" in text
    assert f"지켜보지 않은 앞의 {safety.get('starts_without_a_watch')}번에서는 보였을 것이다" in text


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_a_changed_figure_in_the_data_is_missed_in_the_charter(lang):
    source = quote_template("charter", lang)
    assert source.is_file(), f"the {lang} charter's lines about the pilot are not tied to the data"
    have = set((ROOT / "docs" / f"charter.{lang}.md").read_text(encoding="utf-8").splitlines())
    for change in (lambda r: r["schedule"]["capacities"]["claude_shared"]["after_x1"].update(p_gate_by_its_date=0.31),
                   lambda r: r["usage"]["weekly"].update(one_run_below_points=0.5),
                   lambda r: r["forecast"]["claude_shared"]["rel_10_p50"].update(latest="2039-03-02"),
                   lambda r: r["capacity"]["next_measurements"]["same_model"].update(runs=21)):
        replan = copy.deepcopy(committed())
        change(replan)
        lines = render.render(lang, replan=replan, text=source.read_text(encoding="utf-8")).splitlines()
        assert [line for line in lines if line.strip() and line not in have], "a changed figure went unnoticed"
