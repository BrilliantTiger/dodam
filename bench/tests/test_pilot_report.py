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


PART = " @part"   # a rendered line that ends so must stand inside a line of the document; any other, be a whole line


def not_carried(lines: list[str], text: str) -> list[str]:
    have = set(text.splitlines())
    return [line for line in lines if (line[:-len(PART)] not in text if line.endswith(PART) else line not in have)]


@pytest.mark.parametrize("name,lang", QUOTES)
def test_the_lines_about_the_pilot_in_a_hand_kept_document_are_rendered_from_the_data(name, lang):
    """The charter (its rows, its paragraphs and its history rows), the status line of the README and the
    owner's list carry these lines word for word, so a figure in them cannot drift from the data."""
    source = quote_template(name, lang)
    assert source.is_file(), f"the {lang} {name} lines about the pilot are not tied to the data"
    kept = {"charter": ROOT / "docs" / f"charter.{lang}.md", "owner_list": ROOT / "TODO.md",
            "readme": ROOT / ("README.ko.md" if lang == "ko" else "README.md")}[name]
    lines = [line for line in render.render(lang, text=source.read_text(encoding="utf-8")).splitlines() if line.strip()]
    assert lines and not_carried(lines, kept.read_text(encoding="utf-8")) == []
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
    have = (ROOT / "docs" / f"charter.{lang}.md").read_text(encoding="utf-8")
    for change in (lambda r: r["schedule"]["capacities"]["claude_shared"]["after_x1"].update(p_gate_by_its_date=0.31),
                   lambda r: r["usage"]["weekly"].update(one_run_below_points=0.5),
                   lambda r: r["forecast"]["claude_shared"]["rel_10_p50"].update(latest="2039-03-02"),
                   lambda r: r["capacity"]["next_measurements"]["same_model"].update(runs=21)):
        replan = copy.deepcopy(committed())
        change(replan)
        lines = render.render(lang, replan=replan, text=source.read_text(encoding="utf-8")).splitlines()
        assert not_carried([line for line in lines if line.strip()], have), "a changed figure went unnoticed"


# --- the dates an earlier charter gave, and the owner's item about a new token ----------------------------------

EARLIER = PLAN / "claude_only.json"
UNTRUE = ("could not be reproduced", "not kept in the repository and", "다시 낼 수 없", "저장소에 남지 않은 계산")


def earlier() -> dict:
    assert EARLIER.is_file(), "the computation behind the dates of charter version 0.7 is not recorded in the repository"
    return json.loads(EARLIER.read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_the_dates_in_the_history_row_of_charter_version_0_7_are_the_recorded_output(lang):
    recorded = earlier()
    rows = [line for line in (ROOT / "docs" / f"charter.{lang}.md").read_text(encoding="utf-8").splitlines()
            if line.startswith("| v0.7 |")]
    assert len(rows) == 1
    cases = recorded["cases"]
    assert cases["claude_alone"]["rel_10"]["p50"] in rows[0] and cases["two_companies"]["rel_10"]["p50"] in rows[0]
    assert f"{recorded['sims_per_case']:,}" in rows[0]
    known = {date for case in cases.values() for date in [case["rel_01_p50"], *case["rel_10"].values()]}
    found = set(re.findall(r"20[3-4]\d-\d\d-\d\d", rows[0]))
    assert found and found <= known          # no date of the forecast years in that row but the recorded ones
    kept = render.quote_template("charter", lang).read_text(encoding="utf-8") if hasattr(render, "quote_template") else ""
    for name in ("claude_alone", "claude_shared", "two_companies"):
        assert "{{v07.%s.p50}}" % name in kept, "the charter does not set the earlier dates beside the ranges"


def test_the_recorded_earlier_dates_are_what_their_script_gives():
    recorded = earlier()
    assert (PLAN / "claude_only.py").is_file(), "the script that gives the earlier dates is not in the repository"
    import claude_only
    again = claude_only.compute()
    assert {key: recorded[key] for key in again} == again
    assert (recorded["seed"], recorded["sims_per_case"]) == (11, 1200)
    found = recorded["comparison"]
    for name in ("claude_alone", "claude_shared"):
        for way, spread in found[name].items():
            dates = spread.get("by_seed") or found[name]["plan_scenario_scaled"]["by_seed"][1:6]
            assert (spread["earliest"], spread["latest"]) == (min(dates), max(dates)), (name, way)
            assert "by_seed" not in spread or len(spread["by_seed"]) == len(found["seeds"])
    one = claude_only.compare(seeds=found["seeds"][:1])       # the first seed again, every column
    for name in ("claude_alone", "claude_shared"):
        for way in ("own_scenario", "plan_scenario_scaled"):
            assert one[name][way]["by_seed"] == found[name][way]["by_seed"][:1], (name, way)
    days = lambda a, b: (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
    cases, said = recorded["cases"], recorded["later_for_each_month"]
    later = days(cases["two_companies"]["rel_10"]["p50"], cases["claude_alone"]["rel_10"]["p50"])
    work = days(said["from"], cases["claude_alone"]["rel_10"]["p50"])
    assert said["days_for_each_month"] == round(later / work * 30.44) and said["months_later"] == round(later / 30.44)
    text = (PLAN / "claude_only.py").read_text(encoding="utf-8") + EARLIER.read_text(encoding="utf-8")
    assert not re.search(r"/Users|/home/|/private|/tmp|@", text)


def test_no_document_says_the_earlier_dates_could_not_be_reproduced():
    files = [ROOT / "README.md", ROOT / "README.ko.md", ROOT / "TODO.md", *sorted((ROOT / "docs").rglob("*.md")),
             *sorted(PILOT.glob("*.tmpl")), *sorted(PLAN.glob("plan_content.py"))]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for phrase in UNTRUE:
            assert phrase not in text, f"{path.name} gives a reason that is not true: {phrase}"
    for lang, true in (("ko", "그것으로 세 날짜가 그대로 다시 나온다"), ("en", "it gives the three dates again exactly")):
        rows = [line for line in (ROOT / "docs" / f"charter.{lang}.md").read_text(encoding="utf-8").splitlines()
                if line.startswith("| v0.9 |")]
        assert len(rows) == 1 and true in rows[0], f"the {lang} history row does not give the true reason"


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_the_charter_says_what_explains_the_distance_between_the_earlier_date_and_the_range(lang):
    source = quote_template("charter", lang).read_text(encoding="utf-8")
    assert "{{v07.claude_alone.plan_scenario_scaled.latest_month}}" in source, \
        "the charter does not say what explains the earlier date lying outside the range"
    recorded = earlier()
    have = set((ROOT / "docs" / f"charter.{lang}.md").read_text(encoding="utf-8").splitlines())
    changed = copy.deepcopy(recorded)                      # another recorded date is missed in the charter
    changed["cases"]["claude_alone"]["rel_10"]["p50"] = "2038-10-06"
    lines = render.render(lang, text=source, earlier=changed).splitlines()
    assert [line for line in lines if line.strip() and line not in have]

    def refused(change) -> bool:
        other = copy.deepcopy(recorded)
        change(other["comparison"]["claude_alone"])
        try:
            render.render(lang, text=source, earlier=other)
        except render.RenderError:
            return True
        return False
    # it says the seed explains it: with more seeds the earlier date lies inside the range ...
    assert refused(lambda alone: alone["plan_scenario_scaled"].update(latest="2038-10-01"))
    # ... and the way the capacity is set does not: the two ranges overlap
    assert refused(lambda alone: alone["own_scenario"].update(earliest="2039-01-05", latest="2039-06-01"))
    # the first five seeds of the comparison are the seeds of the range the charter gives
    assert refused(lambda alone: alone["plan_scenario_scaled_first_five_seeds"].update(latest="2038-10-01"))


def test_the_owner_s_item_about_a_new_token_says_what_is_known_and_not_known_about_revoking():
    items = [line for line in (ROOT / "TODO.md").read_text(encoding="utf-8").splitlines()
             if line.startswith("- [ ]") and "토큰을 새로 만들지" in line]
    assert len(items) == 1
    item = items[0]
    version = json.loads(DATA.read_text(encoding="utf-8"))["runs"][0]["claude_code"]["observed"]
    for said in (f"Claude Code {version}에는 이런 토큰의 목록을 보는 명령도, 하나를 무효로 만드는 명령도 없고",
                 "모르는 것: 새 토큰을 만들면 옛 토큰이 끝나는지",
                 "새 토큰을 만드는 것만으로 노출이 없어진다고는 할 수 없다",
                 "모델에 요청을 보내는 데만 쓸 수 있고 한 해 동안 유효하다",
                 "기계 밖에서는 읽을 수 없다"):
        assert said in item, f"the owner's item does not say: {said}"
    assert not re.search(r"Mac mini|sk-ant|/Users|~/|@", item)


# --- the plan's rows and sentences about the pilot --------------------------------------------------------------


def pilot_rows():
    assert (PLAN / "pilot_rows.py").is_file(), "the plan's rows about the pilot are not written from the estimates"
    import pilot_rows as module
    return module


def test_the_plan_s_rows_about_the_pilot_carry_what_the_estimates_give():
    """The rows of the finished pilot, of the measurement after it and of the re-planning: their figures and
    dates in the task data are exactly what the estimates give, and the plan document carries those words."""
    import wbs_data
    expected = pilot_rows().rows(committed())
    assert set(expected) == {"b_pilot", "b_night", "pm_replan_nov"}
    tasks = {task["id"]: task for task in wbs_data.ITEMS}
    document = PLAN_DOC.read_text(encoding="utf-8")
    for name, fields in expected.items():
        for field, text in fields.items():
            assert tasks[name][field] == text, f"{name}.{field} is not what the estimates give"
            if field in ("out", "done"):
                assert text in document, f"the plan document does not carry {name}.{field}"
    bound = committed()["usage"]["weekly"]["one_run_below_points"]
    assert f"한 번에 {bound}%포인트보다 작고" in tasks["b_pilot"]["done"]


def test_the_words_of_the_plan_s_rows_about_the_pilot_hold_no_figure_of_their_own():
    module = pilot_rows()
    for name, fields in module.WORDS.items():
        for field, words in fields.items():
            assert not re.search(r"\d", re.sub(r"\{\w+\}", "", words)), f"{name}.{field} holds a figure of its own"
    now = module.rows(committed())
    for change, name, field in (
            (lambda r: r["usage"]["weekly"].update(one_run_below_points=0.5), "b_pilot", "done"),
            (lambda r: r["usage"].update(agent_seconds_mean=80.5), "b_pilot", "out"),
            (lambda r: r["capacity"]["next_measurements"]["same_model"].update(runs=21), "b_night", "out"),
            (lambda r: r["capacity"]["next_measurements"]["largest_model"].update(runs=9), "b_night", "out"),
            (lambda r: r["schedule"].update(pilot_day="2026-11-19"), "pm_replan_nov", "done"),
            (lambda r: r["schedule"].update(pilot_day="2026-11-19"), "b_night", "start"),
            (lambda r: r["schedule"]["pilot_row"].update(end="2026-12-09"), "b_night", "end")):
        other = copy.deepcopy(committed())
        change(other)
        assert module.rows(other)[name][field] != now[name][field], (name, field)


def test_the_plan_s_sentences_about_the_pilot_say_what_was_measured():
    text = PLAN_DOC.read_text(encoding="utf-8")
    bound = committed()["usage"]["weekly"]["one_run_below_points"]
    assert "회사별로 잰다" not in text and "부터 실제 용량을 재고" not in text      # the pilot is over; it measured Claude only
    assert f"Claude만 쟀고, 주간 한도는 한 번에 {bound}%포인트보다 작다는 위쪽 한계만 안다" in text
    # wherever the plan gives the weekly bound, it is the one the estimates give
    assert re.findall(r"한 번에 ([\d.]+)%포인트보다 작", text) == [str(bound)] * 2


# --- every place a figure is published; statements that were withdrawn; statements with their conditions -------


CHARTER = {"ko": ROOT / "docs" / "charter.ko.md", "en": ROOT / "docs" / "charter.en.md"}
README = {"ko": ROOT / "README.ko.md", "en": ROOT / "README.md"}
PILOT_DOC = {lang: ROOT / "docs" / "pilot" / f"2026-11-17-pilot.{lang}.md" for lang in ("ko", "en")}
PLAN_PAGE = ROOT / "docs" / "plan" / "2026-10-29-plan-to-1.0.ko.html"
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")


def month(iso: str, lang: str) -> str:
    y, m, _ = (int(x) for x in iso.split("-"))
    return f"{y}년 {m}월" if lang == "ko" else f"{MONTH_NAMES[m - 1]} {y}"


def full_day(iso: str, lang: str) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{y}년 {m}월 {d}일" if lang == "ko" else f"{MONTH_NAMES[m - 1]} {d}, {y}"


def anchors() -> dict:
    return json.loads((PLAN / "anchors.json").read_text(encoding="utf-8"))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def schedule_part(lang: str) -> str:
    """The charter's part on the schedule: from its heading to the next heading of the same level."""
    text = read(CHARTER[lang])
    start = text.index("\n## 일정\n" if lang == "ko" else "\n## Schedule\n")
    return text[start:text.index("\n## ", start + 4)]


MILESTONES = {"ko": (("g_dir", "방향 고정"), ("g_min", "최소 언어"), ("rel_01", "AI 개선 루프와 첫 공개"),
                     ("rel_03", "실제로 쓰기"), ("rel_10", "1.0")),
              "en": (("g_dir", "Direction fixing"), ("g_min", "Minimal language"),
                     ("rel_01", "AI improvement loop and first public release"), ("rel_03", "Real use"),
                     ("rel_10", "1.0"))}


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_the_dates_the_charter_keeps_are_the_plan_s_milestone_dates(lang):
    """The schedule table and the paragraph under it keep the plan's dates; they are what anchors.py gives."""
    part, dates = schedule_part(lang), anchors()
    for name, title in MILESTONES[lang]:
        assert f"~ {full_day(dates[name]['p50'], lang)} | {title} |" in part, (name, "the middle date in the table")
    last = full_day(dates["rel_10"]["p80"], lang)
    assert (f"80% 확률로 1.0이 끝나는 날은 {last}이다." if lang == "ko" else
            f"The date by which 1.0 is done with 80% probability is {last}.") in part
    decided = month(dates["d_mm_decide"]["p50"], lang)
    assert (f"결정({decided} 초)" if lang == "ko" else f"decision (early {decided})") in part
    assert f"~ {full_day(dates['g_dir']['p50'], lang)})\n" in read(CHARTER[lang]), "the heading of the next steps"


# What is left of the charter's part on the schedule once every line and sentence written from the estimates
# is taken out: figures that are decisions, scope or findings of research and have no computation here. They
# are listed so that changing one of them by hand fails as well, and so that a new figure has to be either
# written from the estimates or added here on purpose.
NOT_COMPUTED = {
    "ko": ["2026년", "11월", "30개(웹", "20", "10)와", "7개로", "1.0", "100개,", "0.1", "판(0.1)", "매니저(0.2),", "3개월",
           "0건),", "0.3과", "1.0", "1.0", "1.0", "\"2029년~", "1.0\"은", "1%가", "1.0까지", "3.7~4.9년이", "2027년",
           "1월"],
    "en": ["2026", "30", "(20", "10", "7", "1.0", "100", "0.1,", "(0.1)", "(0.2),", "3", "(0", "0.3", "1.0",
           "1.0,", "1.0", "\"1.0", "2029\"", "1%", "3.7", "4.9", "1.0", "2027,"],
}


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_every_figure_in_the_charter_s_part_on_the_schedule_is_written_from_the_estimates_or_named(lang):
    source = quote_template("charter", lang)
    assert "{{anchor." in read(source), "the dates the charter keeps are not written from the plan's milestone dates"
    left = schedule_part(lang)
    for line in sorted(render.render(lang, text=read(source)).splitlines(), key=len, reverse=True):
        piece = line[:-len(PART)] if line.endswith(PART) else line
        if piece.strip():
            left = left.replace(piece, " ")
    assert re.findall(r"\S*\d\S*", left) == NOT_COMPUTED[lang]


def test_the_plan_s_page_is_what_its_scripts_write(plan_copy):
    for command in (["build.py"], ["build_html.py", "page.html"], ["wrap_local.py", "page.html", "plan.html"]):
        subprocess.run([sys.executable, *command], cwd=plan_copy, check=True, capture_output=True)
    assert read(plan_copy / "plan.html") == read(PLAN_PAGE)


# Statements that charter versions 0.8 and 0.9 and the reviews withdrew or changed. A row of a version history
# says what an older version said and stays; so does a line that names the statement as an earlier one.
WITHDRAWN = (
    (r"한 달마다.{0,40}(2주|두 주)", "about two weeks later for every month with Claude alone"),
    (r"two weeks later|about two weeks", "about two weeks later for every month with Claude alone"),
    (r"2038-11-10|2040-07-11|2033-08-31", "the single dates for Claude alone and for two companies"),
    (r"6번을 돌리는 동안|while 6 runs", "the weekly bound over six runs"),
    (r"37%(에서|였고)|was 37%|from 37%", "the chance before the pilot from one seed"),
    (r"경우마다 2,000번, 회사마다 계정 하나", "the schedule run once for each case"),
    (r"7개 언어를 3번씩\)|21 runs", "the one-night measurement of 21 runs"),
    (r"묶음 측정 뒤에 (다시 )?(판단|잰다)|again after the one-night", "the dates judged after the one-night measurement"),
    (r"명령줄에 한 번도 보이지 않았고", "the token never shown, without the runs that were not watched"),
    (r"두 배면 \d{4}년 \d+월로 당겨진다\.|moves up to [A-Z][a-z]+ \d{4}\.", "the date at twice the capacity from one seed, unqualified"),
    (r"번 넘게 되풀이|repeated more than", "every language repeated more than six times"),
    (r"올리기만 하므로|can only raise the figure, so", "other work can only raise the figure, without its condition"),
    (r"기준 측정이 주간 한도에 든다는 것", "an unmoved figure shows the baseline measurement fits the weekly limit"),
    (r"부터 실제 용량을 재고|회사별로 잰다", "the pilot will measure the capacity of each company"),
)
EARLIER_OR_HISTORY = re.compile(r"^\| v\d|^\| \S+ 판 \||이전 판에 적었던|earlier version|version 0\.7|<h2>판 이력|<h2>문서 이력")


def published() -> list[Path]:
    found = [ROOT / "TODO.md", *README.values(), *CHARTER.values(), *PILOT_DOC.values(), PLAN_DOC, PLAN_PAGE,
             PLAN / "README.md"]
    found += sorted(PLAN.glob("*.py")) + sorted(PILOT.glob("*.tmpl")) + sorted(PILOT.glob("*.py"))
    return found


def test_no_statement_that_was_withdrawn_is_stated_as_current():
    weekly = committed()["usage"]["weekly"]["one_run_below_points"]
    stated = []
    for path in published():
        for number, line in enumerate(read(path).splitlines(), 1):
            if EARLIER_OR_HISTORY.search(line):
                continue
            for pattern, what in WITHDRAWN:
                if re.search(pattern, line):
                    stated.append((path.name, number, what))
            # wherever a line gives the bound of the weekly limit for one run of the pilot, it is today's
            for bound in re.findall(r"주간 (?:한도|사용률)[^.|]{0,80}한 번에 ([\d.]+)%포인트보다 작", line):
                if bound != str(weekly):
                    stated.append((path.name, number, f"the weekly bound {bound}"))
    assert stated == []


def test_the_owner_s_item_on_the_second_company_gives_what_the_charter_gives():
    item = next(line for line in read(ROOT / "TODO.md").splitlines() if "ChatGPT Pro(월" in line)
    assert "약 2주" not in item and "한 달마다" not in item
    fc = committed().get("forecast") or {}
    shared, two = fc["claude_shared"]["rel_10_p50"], fc["two_companies"]["rel_10_p50"]
    assert f"끝까지 가면 씨앗에 따라 {month(shared['earliest'], 'ko')}~{month(shared['latest'], 'ko')}이고" in item
    assert f"두 회사로 하면(주 약 {fc['two_companies']['working_days_a_week']}작업일) " \
           f"{month(two['earliest'], 'ko')}~{month(two['latest'], 'ko')}이다" in item
    assert "잰 값이 아니라 가정" in item
    gate = full_day(committed()["schedule"]["gate_day"], "ko")
    assert f'"방향 고정"({gate}까지)' in read(ROOT / "TODO.md")


def test_the_first_night_says_how_often_each_language_is_run():
    night = committed()["capacity"]["next_measurements"]["same_model"]
    languages = committed()["capacity"]["assumptions"]["languages"]
    rounds, beyond = divmod(night["runs"], languages)
    assert beyond, "this test is written for a night that does not run every language equally often"
    assert (night.get("languages_run_once_more"), night.get("runs_of_those_languages"),
            night.get("languages_run_in_whole_rounds_only"), night["whole_rounds_of_the_languages"]) == \
        (beyond, rounds + 1, languages - beyond, rounds)
    assert f"언어 {beyond}개는 {rounds + 1}번, 나머지 {languages - beyond}개는 {rounds}번 되풀이하므로" in read(PILOT_DOC["ko"])
    assert f"{beyond} languages are run {rounds + 1} times and the other {languages - beyond} are run {rounds} times" \
        in read(PILOT_DOC["en"])
    other = copy.deepcopy(committed())
    other["capacity"]["next_measurements"]["same_model"]["languages_run_once_more"] = 0
    with pytest.raises(render.RenderError):     # "some are run once more" would not be true
        render.render("ko", replan=other)


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_the_document_says_when_other_work_can_only_raise_the_weekly_figure(lang):
    text = read(PILOT_DOC[lang])
    if lang == "ko":
        assert "올리기만 하므로" not in text
        assert "한 주가 두 읽기 사이에 새로 시작하지 않는 한, 같은 계정의 다른 작업은 숫자를 올리기만 한다" in text
        assert "그 시각이 그 밤에 들지 않는 날에 재고, 마지막 읽기가 첫 읽기보다 작으면 그 밤의 결과는 쓰지 않는다" in text
        # what the pilot did not observe is not given as a fact
        assert "사용량 화면에 나오는, 주간 한도가 새로 시작하는" not in text and "숫자가 내려가므로" not in text
        assert "시범에서는 한 주가 새로 시작하는 것을 보지 못했다" in text
        assert "읽기에 든 시각으로 밤을 고르는 일은 아직 해 보지 않았다" in text
    else:
        assert "can only raise the figure, so" not in text
        assert "Unless the week of the weekly limit starts anew between the two readings, other work on the same " \
               "account can only raise the figure" in text
        assert "a night whose last reading is lower than its first is not used" in text
        assert "which the usage screen of the account shows" not in text and "the figure drops" not in text
        assert "The pilot did not see a week start anew" in text
        assert "Choosing a night by the time in the readings has not been tried yet" in text


SAME_ENDS = re.compile(r"(\d{4}-\d{2}-\d{2})~\1|between (\d{4}-\d{2}-\d{2}) and \2\b|(\d{4}년 \d+월 \d+일)~\3")


def test_where_every_seed_gives_the_same_date_the_documents_say_so_instead_of_giving_a_range():
    for path in (*CHARTER.values(), *README.values(), *PILOT_DOC.values(), PLAN_DOC, PLAN_PAGE, ROOT / "TODO.md"):
        assert not SAME_ENDS.search(read(path)), f"{path.name} gives a range whose two ends are the same date"
    gate = committed()["forecast"]["planning"]["g_dir_p50"]
    assert gate["earliest"] == gate["latest"], "this test is written for a gate date that every seed gives"
    assert f"어느 씨앗에서나 {gate['earliest']}이고" in read(CHARTER["ko"])
    assert f"at {gate['earliest']} with every seed" in read(CHARTER["en"])
    other = copy.deepcopy(committed())
    other["forecast"]["planning"]["g_dir_p50"]["latest"] = "2027-03-03"
    for lang in ("ko", "en"):
        with pytest.raises(render.RenderError):     # "with every seed" would not be true
            render.render(lang, replan=other, text=read(quote_template("charter", lang)))


def test_the_date_at_twice_the_capacity_is_given_as_a_range_over_the_seeds():
    fc = committed().get("forecast") or {}
    assert "double_capacity" in fc, "the estimates do not run twice the plan's capacity with several seeds"
    double, plan = fc["double_capacity"], fc["planning"]
    assert double["working_days_a_week"] == 2 * plan["working_days_a_week"]
    assert [s["seed"] for s in double["seeds"]] == fc["seeds"] and len(fc["seeds"]) >= 5
    middles = [s["rel_10"]["p50"] for s in double["seeds"]]
    assert (double["rel_10_p50"]["earliest"], double["rel_10_p50"]["latest"]) == (min(middles), max(middles))
    first, last = (month(double["rel_10_p50"][end], "ko") for end in ("earliest", "latest"))
    assert f"씨앗 {len(fc['seeds'])}개로 {double['sims_per_seed']:,}번씩 돌려 {first}~{last} 사이로 당겨진다" in read(CHARTER["ko"])
    first, last = (month(double["rel_10_p50"][end], "en") for end in ("earliest", "latest"))
    assert f"it moves up to between {first} and {last}, run {double['sims_per_seed']:,} times with each of " \
           f"{len(fc['seeds'])} seeds" in read(CHARTER["en"])
    plan_text = read(PLAN_DOC)
    first, last = (month(double["rel_10_p50"][end], "ko") for end in ("earliest", "latest"))
    assert plan_text.count(f"{first}~{last}") >= 3, "the plan gives the single-seed date without its range"
    assert "나머지 줄은 경우마다 씨앗 하나로" in plan_text


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_the_readme_gives_the_80_percent_dates_beside_the_middle_dates(lang):
    fc, text = committed().get("forecast") or {}, read(README[lang])
    upper = month(fc["planning"]["seeds"][0]["rel_10"]["p80"], lang)
    stops = month(fc["claude_shared"]["last_day_of_the_simulation"], lang)
    assert fc["claude_shared"]["rel_10_p80"]["seeds_at_the_last_day"] == len(fc["seeds"])
    middle, top = fc["planning"]["rel_10_p50"], fc["planning"]["rel_10_p80"]
    ends = [month(found[end], lang) for found in (middle, top) for end in ("earliest", "latest")]
    if lang == "ko":
        assert f"80% 확률로는 {upper}까지다" in text
        assert f"80% 날짜는 일정 시뮬레이션이 멈추는 {stops}보다 늦다" in text
        # the plan's two dates are one seed's; the README says so and gives the ranges over the seeds beside them
        assert f"둘 다 씨앗 하나로 낸 값이고, 씨앗 {len(fc['seeds'])}개로는 가운데 날짜가 {ends[0]}~{ends[1]}, " \
               f"80% 날짜가 {ends[2]}~{ends[3]} 사이에서 움직인다" in text
    else:
        assert f"with 80% probability by {upper}" in text
        assert f"the 80% date would lie beyond {stops}, where the schedule simulation stops" in text
        assert f"Both dates come from one seed of the schedule simulation; over {len(fc['seeds'])} seeds the median " \
               f"moves between {ends[0]} and {ends[1]} and the 80% date between {ends[2]} and {ends[3]}" in text


def test_the_plan_s_row_for_the_one_night_measurement_says_what_the_charter_says():
    import wbs_data
    row = next(task for task in wbs_data.ITEMS if task["id"] == "b_night")
    night = committed()["capacity"]["next_measurements"]["same_model"]
    runs = committed()["capacity"]["assumptions"]["runs"]
    said = f"기준 측정 {runs}번이 {night['baseline_below_points_if_unmoved']:g}%포인트보다 적게 쓴다는 것"
    assert "주간 한도에 든다는 것" not in row["done"]
    assert said in row["done"] and said in read(CHARTER["ko"]) and said in read(PLAN_DOC)
    assert f"(시범 날 남아 있던 여유는 {night['room_on_the_pilot_day_points']:g}%포인트였다)" in row["done"]


def test_the_seeds_run_side_by_side_give_what_each_gives_alone():
    """The estimates run their seeds at once; a seed's figures must not depend on which others run beside it."""
    cap = committed()["capacity"]
    try:
        both = pilot_replan.schedule(cap, seeds=(11, 12), sims=40)
        alone = pilot_replan.schedule(cap, seeds=(12,), sims=40)
    except TypeError:
        both = alone = None
    assert both is not None, "the schedule cannot be run for a stated number of runs"
    for name, cases in both["capacities"].items():
        for case, found in cases.items():
            assert found["seeds"][1] == alone["capacities"][name][case]["seeds"][0], (name, case)
    assert both["capacities"]["planning"]["before"]["seeds"][0] != both["capacities"]["planning"]["before"]["seeds"][1]


# --- the parts of a full check are the whole check ---------------------------------------------------------------


def changed(text: str, change) -> str:
    """A recorded file with one thing changed, written the way the scripts write it."""
    data = json.loads(text)
    change(data)
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def cut_to_the_first_seed(data: dict) -> None:
    """The comparison with one seed only and the ranges to match, as the review tried it."""
    found = data["comparison"]
    found["seeds"] = found["seeds"][:1]
    for name in ("claude_alone", "claude_shared"):
        for way in found[name].values():
            if "by_seed" in way:
                way["by_seed"] = way["by_seed"][:1]
                way["earliest"] = way["latest"] = way["by_seed"][0]
        found[name]["plan_scenario_scaled_first_five_seeds"] = dict.fromkeys(
            ("earliest", "latest"), found[name]["plan_scenario_scaled"]["by_seed"][0])


def other_date(way: dict) -> None:
    way["by_seed"][3] = way["earliest"]     # another date for one seed, inside the range, so the range still fits


EARLIER_CHANGES = {
    "the comparison cut down to its first seed": cut_to_the_first_seed,
    "another seed in the comparison's list": lambda d: d["comparison"]["seeds"].__setitem__(-1, 9),
    "one seed more": lambda d: [d["comparison"]["seeds"].append(9)] + [
        way["by_seed"].append(way["latest"]) for name in ("claude_alone", "claude_shared")
        for way in d["comparison"][name].values() if "by_seed" in way],
    "another number of runs": lambda d: d["comparison"].update(sims=1000),
    "another larger number of runs": lambda d: d["comparison"].update(more_sims=3000),
    "a case of the comparison left out": lambda d: d["comparison"]["claude_alone"].pop("own_scenario_more_runs"),
    "a case of the comparison added": lambda d: d["comparison"].update(two_companies=d["comparison"]["claude_shared"]),
    "another date for one seed": lambda d: other_date(d["comparison"]["claude_shared"]["plan_scenario_scaled"]),
    "another single-seed date": lambda d: d["cases"]["two_companies"]["rel_10"].update(p50="2033-09-07"),
    "another seed for the single-seed dates": lambda d: d.update(seed=12),
    "another delay for each month": lambda d: d["later_for_each_month"].update(days_for_each_month=14),
    "a field more": lambda d: d.update(note="by hand"),
}
ESTIMATES_CHANGES = {
    "a seed of the schedule left out": lambda d: d["schedule"]["seeds"].pop(),
    "another number of runs of the schedule": lambda d: d["schedule"].update(sims_per_seed=1000),
    "another chance for the fourth seed": lambda d: d["schedule"]["capacities"]["planning"]["after_x1"]["seeds"][3].update(
        p_baseline_by_row_end=0.5),
    "a case of the schedule left out": lambda d: d["schedule"]["capacities"]["planning"].pop("after_x4"),
    "another measured value": lambda d: d["usage"].update(agent_seconds_mean=80.0),
    "a seed of the forecast left out": lambda d: d["forecast"]["seeds"].pop(),
    "a case of the forecast left out": lambda d: d["forecast"].pop("double_capacity"),
    "another number of runs of the forecast": lambda d: d["forecast"]["two_companies"].update(sims_per_seed=1000),
    "another capacity in the forecast": lambda d: d["forecast"]["double_capacity"].update(working_days_a_week=20),
    "another date for one seed of the forecast": lambda d: d["forecast"]["planning"]["seeds"][2]["rel_10"].update(p50="2032-03-10"),
    "a field more": lambda d: d.update(note="by hand"),
}


def test_the_parts_of_a_full_check_reject_whatever_the_whole_check_rejects():
    """CI works the recorded outputs out again in parts. Here the long computation is replaced by one that
    gives the committed figures, so the test asks only this: the committed file passes every part, a file
    changed in its seeds, cases, numbers of runs or figures fails at least one part, and a part that would
    work out nothing fails. What defines the computation is the script's, never the file's."""
    import claude_only
    text = read(EARLIER)
    good = json.loads(text)
    try:
        jobs = claude_only._jobs(claude_only.SEEDS, claude_only.SIMS, claude_only.MORE_SIMS)
    except AttributeError:
        jobs = None
    assert jobs is not None, "the script does not name the comparison's cases from its own seeds and numbers of runs"
    name_of = lambda job: (job[0], job[1], job[2], tuple(sorted(job[3].items())))
    dates = {name_of(job): good["comparison"][key[0]][key[1]]["by_seed"][n]
             for key, group in jobs.items() for n, job in enumerate(group)}
    run = lambda some: [dates[name_of(job)] for job in some]
    single = lambda: copy.deepcopy({k: v for k, v in good.items() if k not in ("comparison", "later_for_each_month")})
    part = lambda k, n, t: claude_only.check_part(k, n, t, run=run, single=single)
    assert all(part(k, 3, text) for k in (1, 2, 3)), "the committed file does not pass its own parts"
    for what, change in EARLIER_CHANGES.items():
        other = changed(text, change)
        assert other != text, what
        assert not all(part(k, 3, other) for k in (1, 2, 3)), f"every part accepts the earlier dates with {what}"
    cases = sum(len(group) for group in jobs.values())
    for k, n in ((0, 3), (4, 3), (cases + 1, cases + 5), (1, 0)):
        assert not part(k, n, text), f"part {k} of {n} works out nothing and passes"
    assert not part(1, 3, text.replace("\n ", "\n  ")), "a file not written the way the script writes it passes"

    text = read(REPLAN)
    good = json.loads(text)
    pieces = {"schedule": {k: v for k, v in good.items() if k != "forecast"}, "forecast": good["forecast"]}
    check = getattr(pilot_replan, "check", None)
    assert check is not None, "the estimates cannot be checked in parts without the long computation"
    part = lambda name, t: check(name, t, work_out=lambda piece: copy.deepcopy(pieces[piece]))
    assert pilot_replan.PARTS == ("schedule", "forecast") and all(part(name, text) for name in pilot_replan.PARTS)
    for what, change in ESTIMATES_CHANGES.items():
        other = changed(text, change)
        assert other != text, what
        assert not part("all", other), what
        assert not all(part(name, other) for name in pilot_replan.PARTS), f"every part accepts the estimates with {what}"
    for name in ("", "nothing", "schedule,forecast"):
        assert not part(name, text), f"the part {name!r} does not exist and passes"
    assert not part("schedule", "") and not part("forecast", "not a file")

