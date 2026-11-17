"""What the pilot of 17 November 2026 changes in the plan.

Reads the pilot's measured values (bench/records/pilot-2026-11-17.json) and writes pilot_replan.json:

- usage: what the seven counted runs used, what the weekly readings bracket, and what was started that day
  besides them;
- capacity: how many nights the first baseline measurement takes on this machine under the runner's guard,
  and what the next measurements can and cannot resolve within that guard;
- schedule: the December rows in the schedule simulation, for the plan's capacity and for the capacity the
  project works with today, with the pilot's two effects run apart and every case run with several seeds;
- forecast: how far the plan's own dates move from seed to seed, and the dates under today's capacity.

Every assumption is a named number below and is written into the output next to what follows from it.
Averages are rounded first and everything after them is computed from the rounded value, so a reader can
redo the arithmetic from the printed numbers.

Usage: python3 pilot_replan.py              (rewrites pilot_replan.json; a few minutes)
       python3 pilot_replan.py --no-forecast   (keeps the committed forecast part)
       python3 pilot_replan.py --no-schedule   (keeps the committed schedule part)
       python3 pilot_replan.py --check         (computes everything again and says whether the file is what it gives)
       python3 pilot_replan.py --check --part schedule   (or --part forecast: one of the two parts of that check)

The seeds are run side by side, one process each; every case starts from its seed afresh, so the result does
not depend on how many run at once.
"""
import datetime as dt
import json
import math
import os
import random
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent.parent / "bench" / "records" / "pilot-2026-11-17.json"
OUT = HERE / "pilot_replan.json"

# --- assumptions ------------------------------------------------------------------------------------------------
MODELS, TASKS, LANGUAGES, REPEATS = 3, 10, 7, 3   # the charter's first baseline: 630 runs
NIGHT_HOURS = 8                                   # runs only at night, eight hours a night, every night
FACTORS = (1, 2, 4)                               # agent time of an average run, as a multiple of the measured one;
#                                                   only 1 is measured (one model, one task)
AGENT_HOURS_PER_WORKING_DAY = 16                  # the simulation's unit: one AI working day
PILOT_DAY = "2026-11-17"
PILOT_ROW = ("2026-11-29", "2026-12-06")          # where the charter had planned the pilot
# From the repository's history (git log --format=%cI): when the change that keeps the login token off
# command lines was first committed, and when it was merged into main.
FIX_FIRST_COMMIT_UTC = "2026-11-17T07:56:46Z"
FIX_MERGED_UTC = "2026-11-17T11:10:18Z"
# Tasks the pilot shows to be finished: it could not have run without the task format, the sample task and
# the runner's skeleton, and re-planning after the first runs is this work itself.
FINISHED = ("b_format", "b_sample", "b_runner0", "b_pilot", "pm_replan_nov")
# AI working days a week. The plan counts 15 (one account at each of three companies). The two Claude-only
# figures are the charter's own counts and have not been measured.
CAPACITIES = (("planning", 15), ("claude_dedicated", 5), ("claude_shared", 4))
PLAN_DAYS_A_WEEK = 15
WATCHED = ("b_tasks10", "b_runner", "b_baseline", "g_dir")
# Every schedule case is run once per seed; the first seed is the one the first record of this work used.
SEEDS = (20261117, 1, 2, 3, 4)
SIMS = 2000
# The forecast to 1.0: the plan's own run (anchors.py: seed 2029, 2,000 runs) and four more seeds; the cases
# for other capacities, which run to the simulation's last day, with fewer runs.
FORECAST_SEEDS = (2029, 1, 2, 3, 4)
# (name, which of the simulation's capacity scenarios, working days a week, runs per seed)
FORECAST_CASES = (("planning", 0, 15, 2000), ("two_companies", 2, 10, 1200), ("double_capacity", 1, 30, 1200),
                  ("claude_dedicated", 0, 5, 1200), ("claude_shared", 0, 4, 1200))


def day(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def runs_per_night(agent_seconds: float, wall_seconds: float, limit_seconds: float, window_seconds: float,
                   night_seconds: float) -> int:
    """Runs one after another from the start of a night, by the runner's guard: a run starts only while the
    agent time of the runs that ended inside the last window is below the limit, and it must end in the night."""
    tenths = lambda seconds: round(seconds * 10)  # whole tenths of a second, so no time is lost to rounding
    agent_seconds, wall_seconds, limit_seconds = tenths(agent_seconds), tenths(wall_seconds), tenths(limit_seconds)
    window_seconds, night_seconds = tenths(window_seconds), tenths(night_seconds)
    ends, t, n = [], 0, 0
    while True:
        inside = [e for e in ends if e > t - window_seconds]
        if len(inside) * agent_seconds < limit_seconds:
            if t + wall_seconds > night_seconds:
                return n
            t += wall_seconds
            ends.append(t)
            n += 1
        else:
            t = min(inside) + window_seconds  # the earliest of them leaves the window
            if t >= night_seconds:
                return n


def _agent(run: dict) -> float:
    return sum(s["agent_seconds"] for s in run["steps"])


def _reading(step: dict):
    """Where a step's reading of the limits fell: (replies before it, replies in the step), or None."""
    where = step.get("limit_readings")
    if not where or not where["replies_before_each_reading"]:
        return None
    return where["replies_before_each_reading"][0], where["replies"]


def usage(data: dict) -> dict:
    runs = data["runs"]
    others = data.get("starts_not_counted", [])
    agent = [_agent(r) for r in runs]
    judge = [sum(s["judge_seconds"] for s in r["steps"]) for r in runs]
    wall = [r["wall_seconds"] for r in runs]
    starts = sorted({r["batch_started_utc"] for r in runs})
    first_batch = [r for r in runs if r["batch_started_utc"] == starts[0]]
    later = [r for r in runs if r["batch_started_utc"] != starts[0]]
    stamp = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    percents = [r["weekly"][w]["percent"] for r in runs for w in ("before", "first", "after")]
    step = 1 if all(float(p).is_integer() for p in percents) else None
    later_first = min(later, key=lambda r: r["guard"]["minutes_before"])
    later_last = max(later, key=lambda r: r["guard"]["minutes_after"])
    moved = later_last["weekly"]["after"]["percent"] - later_first["weekly"]["first"]["percent"]
    stop_at = data["batches"][-1]["stop_at_weekly_percent"]
    # The two readings that bracket the later runs: the first reading of the first of them and the last
    # reading of the last. Each step reports once; what lies wholly between the two readings is every step
    # of those runs but the first step of the first run and the last step of the last.
    first_at, last_at = _reading(later_first["steps"][0]), _reading(later_last["steps"][-1])
    between = sum(_agent(r) for r in later) - later_first["steps"][0]["agent_seconds"] \
        - later_last["steps"][-1]["agent_seconds"]
    between_steps = sum(len(r["steps"]) for r in later) - 2
    mean = round(sum(agent) / len(agent), 1)
    between = round(between, 1)
    per_run_below = round(step * mean / between, 2)
    room = stop_at - later_last["weekly"]["after"]["percent"]
    tokens = {k: sum(r["tokens"][k] for r in runs) for k in ("input", "cache_read", "cache_creation", "output")}
    watched = [r for r in runs if r["process_watch"] is not None]
    newer = [r for r in watched if r["process_watch"]["readings_with_the_token_after_the_end"] is not None]
    unwatched = [r for r in others if r["process_watch"] is None]
    return {
        "runs": len(runs),
        "agent_seconds_total": round(sum(agent), 1),
        "agent_minutes_total": round(sum(agent) / 60, 1),
        "agent_seconds_mean": mean,
        "agent_seconds_min": round(min(agent), 1),
        "agent_seconds_max": round(max(agent), 1),
        "judge_seconds_mean": round(sum(judge) / len(judge), 1),
        "wall_seconds_total": round(sum(wall), 1),
        "wall_minutes_total": round(sum(wall) / 60, 1),
        "wall_seconds_mean": round(sum(wall) / len(wall), 1),
        "wall_seconds_min": min(wall),
        "wall_seconds_max": max(wall),
        "turns_total": sum(r["turns"] for r in runs),
        "tokens_total": tokens,
        "cost_usd_as_the_cli_counts_it_total": round(sum(r["cost_usd_as_the_cli_counts_it"] for r in runs), 2),
        "day": {
            "starts_with_a_record": len(runs) + len(others),
            "counted": len(runs),
            "not_counted": len(others),
            "not_counted_starts": [{
                "started_utc": r["batch_started_utc"],
                "started_hhmm_utc": r["batch_started_utc"][11:16],
                "language": r["language"],
                "turns": r["turns"],
                "tokens": sum(v or 0 for v in r["tokens"].values()),
                "steps_that_ended_with_an_error": sum(1 for s in r["steps"] if s["error"] is not None),
                "steps": len(r["steps"]),
                "agent_seconds_counted": round(_agent(r), 1),
                "wall_seconds": r["wall_seconds"],
                "rewards": [s["reward"] for s in r["steps"]],
                "weekly_percent_at_its_end": (r["weekly"]["after"] or {}).get("percent"),
                "process_table_watched": r["process_watch"] is not None,
                "leak_scan_clean": r["leak_scan"]["clean"],
                "leak_hits": r["leak_scan"]["hits"],
                "minutes_before_the_fix_was_first_committed": round(
                    (stamp(FIX_FIRST_COMMIT_UTC) - stamp(r["batch_started_utc"])).total_seconds() / 60),
            } for r in others],
            "fix_first_committed_hhmm_utc": FIX_FIRST_COMMIT_UTC[11:16],
            "fix_merged_hhmm_utc": FIX_MERGED_UTC[11:16],
            "counted_runs_started_before_the_fix_was_merged": sum(
                1 for r in runs if stamp(r["batch_started_utc"]) < stamp(FIX_MERGED_UTC)),
            "counted_runs_started_after_the_fix_was_merged": sum(
                1 for r in runs if stamp(r["batch_started_utc"]) >= stamp(FIX_MERGED_UTC)),
        },
        "weekly": {
            "display_step_points": step,
            "first_batch_runs": len(first_batch),
            "first_batch_before": first_batch[0]["weekly"]["before"]["percent"],
            "first_batch_first": first_batch[0]["weekly"]["first"]["percent"],
            "first_batch_after": first_batch[-1]["weekly"]["after"]["percent"],
            "hours_between_first_and_later_runs": round((stamp(starts[1]) - stamp(starts[0])).total_seconds() / 3600, 1),
            "later_runs": len(later),
            "later_stored_before": later_first["weekly"]["before"]["percent"],
            "later_first_reading": later_first["weekly"]["first"]["percent"],
            "later_last_reading": later_last["weekly"]["after"]["percent"],
            "rise_between_the_batches_points": later_first["weekly"]["first"]["percent"]
            - later_first["weekly"]["before"]["percent"],
            "moved_between_the_two_readings_points": moved,
            "readings_per_step": 1 if all(len((s.get("limit_readings") or {}).get("replies_before_each_reading", []))
                                          == 1 for r in later for s in r["steps"]) else None,
            "first_reading_after_replies": first_at[0] if first_at else None,
            "first_reading_step_replies": first_at[1] if first_at else None,
            "last_reading_after_replies": last_at[0] if last_at else None,
            "last_reading_step_replies": last_at[1] if last_at else None,
            "steps_of_the_later_runs": between_steps + 2,
            "steps_wholly_between_the_readings": between_steps,
            "agent_seconds_wholly_between_the_readings": between,
            "agent_seconds_of_the_later_runs": round(sum(_agent(r) for r in later), 1),
            "one_run_below_points": per_run_below,
            "baseline_below_points": round(per_run_below * MODELS * TASKS * LANGUAGES * REPEATS, 1),
            "runner_stops_at_percent": stop_at,
            "room_to_the_stop_points": room,
            "runs_in_that_room_at_the_bound": int(room / per_run_below),
            "runs_whose_readings_moved_between_first_and_after": sum(
                1 for r in runs if r["weekly"]["first"]["percent"] != r["weekly"]["after"]["percent"]),
        },
        "five_hour": {
            "runs_that_reported_it": sum(1 for r in runs if "five_hour" not in r["windows_not_reported"]),
            "guard_window_hours": later_last["guard"]["window_hours"],
            "guard_limit_minutes": later_last["guard"]["limit_minutes"],
            "guard_minutes_before_later_runs": later_first["guard"]["minutes_before"],
            "guard_minutes_after_later_runs": later_last["guard"]["minutes_after"],
            "guard_minutes_after_first_batch": first_batch[-1]["guard"]["minutes_after"],
        },
        "safety": {
            "watched_runs": len(watched),
            "starts_without_a_watch": len(unwatched),
            "readings": sum(r["process_watch"]["readings"] for r in watched),
            "readings_later_runs": sum(r["process_watch"]["readings"] for r in later),
            "readings_while_the_agent_worked": sum(r["process_watch"]["readings_while_the_agent_worked"]
                                                   for r in watched),
            "readings_with_the_token": sum(r["process_watch"]["readings_with_the_token"] for r in watched),
            "runs_also_watched_after_their_end": len(newer),
            "reading_interval_seconds": later_last["process_watch"]["interval_seconds"],
            "leak_scans": len(runs) + len(others),
            "clean_leak_scans": sum(1 for r in runs + others if r["leak_scan"]["clean"]),
            "leak_hits": sum(r["leak_scan"]["hits"] for r in runs + others),
            "runs_whose_scan_says_what_it_could_not_read": sum(
                1 for r in runs + others if r["leak_scan"]["unchecked"] is not None),
            "files_the_scan_could_not_read": sum(r["leak_scan"]["unchecked"] or 0 for r in runs + others),
            "runs_that_recorded_containers": sum(1 for r in runs + others if r["containers_left"] is not None),
            "containers_left": sum(r["containers_left"] or 0 for r in runs + others),
            "runs_where_harbor_was_stopped": sum(1 for r in watched if r["process_watch"]["stopped_harbor"]),
            "judge_environment_problems": sum(r["judge_environment_problems"] or 0 for r in runs),
            "agent_errors": sum(r["agent_errors"] or 0 for r in runs),
            "limit_events_using_extra_usage": sum(r["limit_events_using_extra_usage"] or 0 for r in runs + others),
        },
    }


def capacity(used: dict, row_start: str, row_end: str) -> dict:
    total = MODELS * TASKS * LANGUAGES * REPEATS
    agent, wall = used["agent_seconds_mean"], used["wall_seconds_mean"]
    limit = used["five_hour"]["guard_limit_minutes"] * 60
    window = used["five_hour"]["guard_window_hours"] * 3600
    night = NIGHT_HOURS * 3600
    row_nights = (day(row_end) - day(row_start)).days + 1

    def starts_in_a_window(a: float) -> int:
        return math.floor(limit / a - 1e-9) + 1  # the last run starts just below the limit

    def case(factor: float) -> dict:
        a = round(agent * factor, 1)
        w = round(wall + (factor - 1) * agent, 1)  # the judge and the containers take what they took
        per_window = starts_in_a_window(a)
        per_night = runs_per_night(a, w, limit, window, night)
        nights = math.ceil(total / per_night) if per_night else None
        return {"factor": factor, "agent_seconds_per_run": a, "agent_minutes_per_run": round(a / 60, 2),
                "wall_seconds_per_run": w, "starts_per_window": per_window,
                "minutes_for_those_starts": round(per_window * w / 60, 1), "runs_per_night": per_night,
                "nights": nights, "fits_the_row": nights is not None and nights <= row_nights,
                "agent_hours_total": round(total * a / 3600, 1),
                "working_days": round(total * a / 3600 / AGENT_HOURS_PER_WORKING_DAY, 2)}

    scan = [n / 100 for n in range(100, 2001)]
    fitting = [f for f in scan if case(f)["fits_the_row"]]
    largest = max(fitting) if fitting else None  # None: even the measured working time does not fit the row

    # The first night: the same task with the same model, as many runs as the guard lets one night hold.
    # The weekly figure is read once in each step, early in it, so what lies wholly between the first and
    # the last reading of the night is one run less than the night's runs.
    n1 = case(1)["runs_per_night"]
    step = used["weekly"]["display_step_points"]
    room = used["weekly"]["room_to_the_stop_points"]
    between = n1 - 1
    # The second night: the largest model, once in each language.
    one_window = max([f for f in scan if starts_in_a_window(round(agent * f, 1)) >= LANGUAGES], default=None)
    one_night = max([f for f in scan
                     if runs_per_night(round(agent * f, 1), round(wall + (f - 1) * agent, 1), limit, window, night)
                     >= LANGUAGES], default=None)
    at_four = case(4)
    return {
        "assumptions": {"models": MODELS, "tasks": TASKS, "languages": LANGUAGES, "repeats": REPEATS, "runs": total,
                        "runs_per_model": total // MODELS,
                        "night_hours": NIGHT_HOURS, "guard_limit_minutes": used["five_hour"]["guard_limit_minutes"],
                        "guard_window_hours": used["five_hour"]["guard_window_hours"],
                        "agent_hours_per_working_day": AGENT_HOURS_PER_WORKING_DAY,
                        "measured_factor": 1, "plan_hours_per_run": 0.5,
                        "plan_working_days": round(total * 0.5 / AGENT_HOURS_PER_WORKING_DAY, 1)},
        "row": {"start": row_start, "end": row_end, "nights": row_nights,
                "runs_per_night_needed": math.ceil(total / row_nights)},
        "cases": [case(f) for f in FACTORS],
        "largest_factor_that_fits_the_row": largest,
        "agent_minutes_per_run_at_that_factor": case(largest)["agent_minutes_per_run"] if fitting else None,
        "next_measurements": {
            "same_model": {
                "runs": n1,
                "whole_rounds_of_the_languages": n1 // LANGUAGES,
                "runs_beyond_whole_rounds": n1 % LANGUAGES,
                "languages_run_once_more": n1 % LANGUAGES,
                "runs_of_those_languages": n1 // LANGUAGES + 1,
                "languages_run_in_whole_rounds_only": LANGUAGES - n1 % LANGUAGES,
                "agent_minutes": round(n1 * agent / 60, 1),
                "runs_wholly_between_first_and_last_reading": between,
                "one_run_below_points_if_unmoved": round(step / between, 3),
                "baseline_below_points_if_unmoved": round(step * total / between, 1),
                "room_on_the_pilot_day_points": room,
                "unmoved_shows_the_baseline_fits_that_room": step * total / between < room,
                "baseline_points_if_moved_one_step_at_most": round(2 * step * total / between, 1),
                "models_covered": 1, "tasks_covered": 1,
                "models_left": MODELS - 1, "tasks_left": TASKS - 1,
            },
            "largest_model": {
                "runs": LANGUAGES,
                "all_start_in_one_window_up_to_factor": one_window,
                "agent_minutes_at_that_factor": round(agent * one_window / 60, 1) if one_window else None,
                "all_end_in_one_night_up_to_factor": one_night,
                "agent_minutes_at_the_night_factor": round(agent * one_night / 60, 1) if one_night else None,
                "starts_in_the_first_window_at_factor_4": min(LANGUAGES, at_four["starts_per_window"]),
                "runs_in_the_night_at_factor_4": min(LANGUAGES, at_four["runs_per_night"]),
                "models_covered_with_the_first_night": 2, "tasks_covered": 1,
                "models_left": MODELS - 2, "tasks_left": TASKS - 1,
                "one_run_below_points_if_unmoved": round(step / (LANGUAGES - 1), 2),
            },
        },
    }


def _simulation(build: bool = True):
    if build:
        subprocess.run([sys.executable, str(HERE / "build.py")], check=True, capture_output=True)  # writes wbs.json
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    argv, sys.argv = sys.argv, [sys.argv[0], "1"]
    try:
        import montecarlo as M
        import montecarlo2 as MC
    finally:
        sys.argv = argv
    return M, MC


def _spread(values: list[str]) -> int:
    return (day(max(values)) - day(min(values))).days


def _cases(cap: dict, M) -> dict:
    """What each case of the schedule gives the simulation."""
    light = dict(M.MEASURE, b_ref27=85, b_full28=75, b_final=140)  # the planning scenario of the charter's dates
    finished = {i: PILOT_DAY for i in FINISHED}
    measured = {f"x{c['factor']}": dict(measure=dict(light, b_baseline=c["working_days"], b_pilot=0),
                                         min_days={"b_baseline": c["nights"]}) for c in cap["cases"]}
    # The pilot has two effects. One is counting what is finished as finished on the pilot's day. The other is
    # the pilot's numbers: the agent time the baseline measurement uses and the fewest nights it can take.
    return {
        "before": dict(measure=light),
        "finished_only": dict(measure=light, finished=finished),
        "numbers_only": dict(measure=dict(light, b_baseline=cap["cases"][0]["working_days"]),
                             min_days=measured["x1"]["min_days"]),
        "after_x1": dict(finished=finished, **measured["x1"]),
        "after_x2": dict(finished=finished, **measured["x2"]),
        "after_x4": dict(finished=finished, **measured["x4"]),
    }


def _one_seed(job) -> dict:
    """Every capacity and case with one seed: the day each watched task ended in each run. Each case starts
    from the seed afresh, so the seeds can be run side by side and give what they give one after another."""
    cap, seed, sims = job
    M, MC = _simulation(build=False)
    scen = list(M.SCEN)[0]
    out = {}
    for name, days in CAPACITIES:
        out[name] = {}
        for case, kw in _cases(cap, M).items():
            random.seed(seed)
            seen = {i: [] for i in WATCHED}
            for _ in range(sims):
                r = MC.run(scen, True, cap_scale=days / PLAN_DAYS_A_WEEK, until="g_dir", **kw)
                for i in WATCHED:
                    seen[i].append(r[i] if r[i] is not None else MC.HORIZON)
            out[name][case] = seen
    return out


def schedule(cap: dict, seeds=SEEDS, sims: int = SIMS) -> dict:
    """The December rows, for each capacity and each case, once per seed. A case's figures are those of all
    its runs taken together; `seeds` beside them is what each seed alone gives, which shows the noise."""
    M, MC = _simulation()
    scen = list(M.SCEN)[0]
    if M.SCEN[scen][0] != PLAN_DAYS_A_WEEK:
        raise SystemExit("the plan's capacity is no longer 15 working days a week")
    items = {it["id"]: it for it in MC.ITEMS}
    row_end, gate_day = cap["row"]["end"], items["g_dir"]["start"]
    cases = _cases(cap, M)
    with ProcessPoolExecutor(max_workers=min(len(seeds), os.cpu_count() or 1)) as pool:
        by_seed = list(pool.map(_one_seed, [(cap, seed, sims) for seed in seeds]))

    def stats(seen: dict, n: int) -> dict:
        out = {}
        for i, v in seen.items():
            v = sorted(v)
            out[i] = {"p50": str(MC.date_of(v[n // 2])), "p80": str(MC.date_of(v[int(0.8 * n)]))}
        out["p_baseline_by_row_end"] = round(sum(1 for x in seen["b_baseline"] if x <= MC.off(row_end)) / n, 2)
        out["p_gate_by_its_date"] = round(sum(1 for x in seen["g_dir"] if x <= MC.off(gate_day)) / n, 2)
        return out

    def one(name: str, case: str) -> dict:
        pooled, per_seed = {i: [] for i in WATCHED}, []
        for seed, found in zip(seeds, by_seed):
            seen = found[name][case]
            per_seed.append(dict(stats(seen, sims), seed=seed))
            for i in WATCHED:
                pooled[i].extend(seen[i])
        out = stats(pooled, sims * len(seeds))
        out["seeds"] = per_seed
        return out

    result = {"sims_per_seed": sims, "seeds": list(seeds), "runs_per_case": sims * len(seeds), "upper_percent": 80,
              "pilot_day": PILOT_DAY, "finished_tasks": len(FINISHED),
              "tasks_rolled": len(MC.ITEMS), "tasks_added_later_and_left_out": len(M.ADDED_LATER),
              "pilot_row": {"start": PILOT_ROW[0], "end": PILOT_ROW[1],
                            "days_before_its_start": (day(PILOT_ROW[0]) - day(PILOT_DAY)).days,
                            "days_before_its_end": (day(PILOT_ROW[1]) - day(PILOT_DAY)).days},
              "baseline_working_days_in_the_plan": M.MEASURE["b_baseline"],
              "baseline_row": {"start": cap["row"]["start"], "end": row_end},
              "gate_day": gate_day,
              "working_days_a_week": dict(CAPACITIES),
              "capacities": {name: {case: one(name, case) for case in cases} for name, _ in CAPACITIES}}
    # How far a figure moves from seed to seed, at the most, over all cases.
    middle, upper, chance = 0, 0, 0
    for per_capacity in result["capacities"].values():
        for found in per_capacity.values():
            for i in ("b_baseline", "g_dir"):
                middle = max(middle, _spread([s[i]["p50"] for s in found["seeds"]]))
                upper = max(upper, _spread([s[i]["p80"] for s in found["seeds"]]))
            for key in ("p_baseline_by_row_end", "p_gate_by_its_date"):
                shares = [s[key] for s in found["seeds"]]
                chance = max(chance, round((max(shares) - min(shares)) * 100))
    result["noise"] = {"middle_dates_move_up_to_days": middle, "upper_dates_move_up_to_days": upper,
                       "chances_move_up_to_points": chance}
    return result


FORECAST_CODE = r"""
import json, random, sys
sys.path.insert(0, sys.argv[1]); spec = json.loads(sys.argv[2]); sys.argv = [sys.argv[0], "1"]
import montecarlo as M, montecarlo2 as MC
scen = list(M.SCEN)[spec["scen"]]
light = dict(M.MEASURE, b_ref27=85, b_full28=75, b_final=140)
out = {"tasks": len(MC.ITEMS), "last_day": str(MC.date_of(MC.HORIZON)), "seeds": []}
for seed in spec["seeds"]:
    random.seed(seed)
    seen = {"g_dir": [], "rel_10": []}
    for _ in range(spec["sims"]):
        r = MC.run(scen, True, measure=light, cap_scale=spec["days"] / M.SCEN[scen][0])
        for i in seen:
            seen[i].append(r[i] if r[i] is not None else MC.HORIZON)
    row = {"seed": seed}
    for i, v in seen.items():
        v.sort()
        row[i] = {"p50": str(MC.date_of(v[int(0.5 * len(v))])), "p80": str(MC.date_of(v[int(0.8 * len(v))]))}
    out["seeds"].append(row)
print(json.dumps(out))
"""


def forecast() -> dict:
    """The dates of the direction-fixing gate and of 1.0, once per seed: for the plan's capacity with the
    tasks the forecast was made with and with every task, and for the other capacities."""
    _simulation()

    def run(scen: int, days: float, sims: int, all_tasks: bool = False) -> dict:
        env = dict(os.environ)
        env.pop("DODAM_PLAN_ALL_TASKS", None)
        if all_tasks:
            env["DODAM_PLAN_ALL_TASKS"] = "1"
        spec = json.dumps({"seeds": list(FORECAST_SEEDS), "sims": sims, "days": days, "scen": scen})
        done = subprocess.run([sys.executable, "-c", FORECAST_CODE, str(HERE), spec], check=True,
                              capture_output=True, text=True, env=env)
        found = json.loads(done.stdout)
        last = found["last_day"]
        out = {"working_days_a_week": days, "sims_per_seed": sims, "tasks": found["tasks"], "seeds": found["seeds"]}
        for i in ("g_dir", "rel_10"):
            for q in ("p50", "p80"):
                values = [s[i][q] for s in found["seeds"]]
                out[f"{i}_{q}"] = {"earliest": min(values), "latest": max(values), "days_apart": _spread(values),
                                   "weeks_apart": round(_spread(values) / 7),
                                   "seeds_at_the_last_day": sum(1 for v in values if v == last)}
        out["last_day_of_the_simulation"] = last
        return out

    result = {"seeds": list(FORECAST_SEEDS), "plan_seed": FORECAST_SEEDS[0]}
    jobs = [(name, (scen, days, sims, False)) for name, scen, days, sims in FORECAST_CASES]
    jobs.append(("planning_with_every_task", (0, PLAN_DAYS_A_WEEK, FORECAST_CASES[0][3], True)))
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:   # each case is a process of its own
        for (name, _), found in zip(jobs, pool.map(lambda job: run(*job[1]), jobs)):
            result[name] = found
    return result


def build(with_forecast: bool = True, seeds=SEEDS, with_schedule: bool = True) -> dict:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import wbs_data as W
    row = next(it for it in W.ITEMS if it["id"] == "b_baseline")
    used = usage(data)
    cap = capacity(used, row["start"], row["end"])
    result = {"what": "derived from the pilot's measured values; every assumption is named",
              "usage": used, "capacity": cap,
              "schedule": schedule(cap, seeds) if with_schedule else json.loads(OUT.read_text(encoding="utf-8"))["schedule"]}
    if with_forecast:
        result["forecast"] = forecast()
    elif OUT.is_file():
        result["forecast"] = json.loads(OUT.read_text(encoding="utf-8")).get("forecast")
    return result


def to_text(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=1) + "\n"


if __name__ == "__main__":
    if "--check" in sys.argv:
        # The whole check, or one of its two parts so that it can be spread over machines: "schedule" is the
        # usage, the capacity and the schedule; "forecast" is the forecast to 1.0. Together they are the file.
        part = sys.argv[sys.argv.index("--part") + 1] if "--part" in sys.argv else "all"
        text = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if part == "all":
            same = text == to_text(build())
        elif part in ("schedule", "forecast") and text:
            was = json.loads(text)
            again = build(with_forecast=False) if part == "schedule" else dict(was, forecast=forecast())
            same = to_text(again) == text
        else:
            same = False
        print(f"{part}: {OUT.name} is what the script gives" if same else
              f"{part}: {OUT.name} differs from what the script gives")
        sys.exit(0 if same else 1)
    OUT.write_text(to_text(build(with_forecast="--no-forecast" not in sys.argv,
                                 with_schedule="--no-schedule" not in sys.argv)), encoding="utf-8")
    print(f"wrote {OUT.name}")
