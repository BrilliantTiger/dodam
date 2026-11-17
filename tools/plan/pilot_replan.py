"""What the pilot of 17 November 2026 changes in the plan.

Reads the pilot's measured values (bench/records/pilot-2026-11-17.json) and writes pilot_replan.json:

- usage: what the seven runs used, summed and averaged, and what the weekly figure can and cannot bound;
- capacity: how many nights the first baseline measurement takes on this machine under the runner's guard;
- schedule: the December rows in the schedule simulation, before and after the pilot's inputs.

Every assumption is a named number below and is written into the output next to what follows from it.
Averages are rounded first and everything after them is computed from the rounded value, so a reader can
redo the arithmetic from the printed numbers.

Usage: python3 pilot_replan.py          (rewrites pilot_replan.json)
"""
import datetime as dt
import json
import math
import random
import subprocess
import sys
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
SIMS, SEED = 2000, 20261117
PILOT_DAY = "2026-11-17"
PILOT_ROW = ("2026-11-29", "2026-12-06")              # where the charter had planned the pilot
# Tasks the pilot shows to be finished: it could not have run without the task format, the sample task and
# the runner's skeleton, and re-planning after the first runs is this work itself.
FINISHED = ("b_format", "b_sample", "b_runner0", "b_pilot", "pm_replan_nov")
# The one-night batch measurement the pilot led to: half a working day that nothing waits for. The simulation
# leaves it out, in the cases before the pilot (the plan did not have it) and after it alike.
LEFT_OUT = ("b_night",)
CAPACITIES = (("planning", 1.0), ("claude_only", 1 / 3))  # about 15 and about 5 AI working days a week
WATCHED = ("b_tasks10", "b_runner", "b_baseline", "g_dir")


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


def usage(data: dict) -> dict:
    runs = data["runs"]
    agent = [sum(s["agent_seconds"] for s in r["steps"]) for r in runs]
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
    per_run_below = round(step / len(later), 2)
    room = stop_at - later_last["weekly"]["after"]["percent"]
    tokens = {k: sum(r["tokens"][k] for r in runs) for k in ("input", "cache_read", "cache_creation", "output")}
    return {
        "runs": len(runs),
        "agent_seconds_total": round(sum(agent), 1),
        "agent_minutes_total": round(sum(agent) / 60, 1),
        "agent_seconds_mean": round(sum(agent) / len(agent), 1),
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
            "moved_during_later_runs_points": moved,
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
            "readings": sum(r["process_watch"]["readings"] for r in runs),
            "readings_later_runs": sum(r["process_watch"]["readings"] for r in later),
            "readings_while_the_agent_worked": sum(r["process_watch"]["readings_while_the_agent_worked"] for r in runs),
            "readings_with_the_token": sum(r["process_watch"]["readings_with_the_token"] for r in runs),
            "reading_interval_seconds": later_last["process_watch"]["interval_seconds"],
            "clean_leak_scans": sum(1 for r in runs if r["leak_scan"]["clean"]),
            "leak_hits": sum(r["leak_scan"]["hits"] for r in runs),
            "files_the_scan_could_not_read": sum(r["leak_scan"]["unchecked"] for r in runs),
            "containers_left": sum(r["containers_left"] for r in runs),
            "runs_where_harbor_was_stopped": sum(1 for r in runs if r["process_watch"]["stopped_harbor"]),
            "judge_environment_problems": sum(r["judge_environment_problems"] for r in runs),
            "agent_errors": sum(r["agent_errors"] for r in runs),
            "limit_events_using_extra_usage": sum(r["limit_events_using_extra_usage"] for r in runs),
        },
    }


def capacity(used: dict, row_start: str, row_end: str) -> dict:
    total = MODELS * TASKS * LANGUAGES * REPEATS
    agent, wall = used["agent_seconds_mean"], used["wall_seconds_mean"]
    limit = used["five_hour"]["guard_limit_minutes"] * 60
    window = used["five_hour"]["guard_window_hours"] * 3600
    night = NIGHT_HOURS * 3600
    row_nights = (day(row_end) - day(row_start)).days + 1

    def case(factor: float) -> dict:
        a = round(agent * factor, 1)
        w = round(wall + (factor - 1) * agent, 1)  # the judge and the containers take what they took
        per_window = math.floor(limit / a - 1e-9) + 1  # the last run starts just below the limit
        per_night = runs_per_night(a, w, limit, window, night)
        nights = math.ceil(total / per_night)
        return {"factor": factor, "agent_seconds_per_run": a, "agent_minutes_per_run": round(a / 60, 2),
                "wall_seconds_per_run": w, "starts_per_window": per_window,
                "minutes_for_those_starts": round(per_window * w / 60, 1), "runs_per_night": per_night,
                "nights": nights, "fits_the_row": nights <= row_nights,
                "agent_hours_total": round(total * a / 3600, 1),
                "working_days": round(total * a / 3600 / AGENT_HOURS_PER_WORKING_DAY, 2)}

    fitting = [f for f in (n / 100 for n in range(100, 1001)) if case(f)["nights"] <= row_nights]
    largest = max(fitting) if fitting else None  # None: even the measured working time does not fit the row
    same_model_runs = LANGUAGES * REPEATS
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
        "next_measurement": {
            "same_model_runs": same_model_runs,
            "agent_minutes": round(same_model_runs * agent / 60, 1),
            "wall_minutes": round(same_model_runs * wall / 60, 1),
            "weekly_points_per_run_it_can_tell": round(1 / same_model_runs, 2),
            "larger_model_runs": LANGUAGES,
        },
    }


def schedule(cap: dict) -> dict:
    subprocess.run([sys.executable, str(HERE / "build.py")], check=True, capture_output=True)  # writes wbs.json
    sys.path.insert(0, str(HERE))
    argv, sys.argv = sys.argv, [sys.argv[0], "1"]
    try:
        import montecarlo as M
        import montecarlo2 as MC
    finally:
        sys.argv = argv
    scen = list(M.SCEN)[0]
    light = dict(M.MEASURE, b_ref27=85, b_full28=75, b_final=140)  # the planning scenario of the charter's dates
    items = {it["id"]: it for it in MC.ITEMS}
    row_end = cap["row"]["end"]
    gate_day = items["g_dir"]["start"]
    outside = {i: PILOT_DAY for i in LEFT_OUT if i in items}
    finished = dict({i: PILOT_DAY for i in FINISHED}, **outside)

    def one(cap_scale: float, **kw) -> dict:
        random.seed(SEED)
        seen = {i: [] for i in WATCHED}
        for _ in range(SIMS):
            r = MC.run(scen, True, cap_scale=cap_scale, until="g_dir", **kw)
            for i in WATCHED:
                seen[i].append(r[i] if r[i] is not None else MC.HORIZON)
        out = {}
        for i, v in seen.items():
            v.sort()
            out[i] = {"p50": str(MC.date_of(v[SIMS // 2])), "p80": str(MC.date_of(v[int(0.8 * SIMS)]))}
        out["p_baseline_by_row_end"] = round(sum(1 for x in seen["b_baseline"] if x <= MC.off(row_end)) / SIMS, 2)
        out["p_gate_by_its_date"] = round(sum(1 for x in seen["g_dir"] if x <= MC.off(gate_day)) / SIMS, 2)
        return out

    result = {"sims": SIMS, "upper_percent": 80, "pilot_day": PILOT_DAY, "finished_tasks": len(FINISHED),
              "pilot_row": {"start": PILOT_ROW[0], "end": PILOT_ROW[1],
                            "days_before_its_start": (day(PILOT_ROW[0]) - day(PILOT_DAY)).days,
                            "days_before_its_end": (day(PILOT_ROW[1]) - day(PILOT_DAY)).days},
              "baseline_working_days_in_the_plan": M.MEASURE["b_baseline"],
              "baseline_row": {"start": cap["row"]["start"], "end": row_end},
              "gate_day": gate_day,
              "working_days_a_week": {name: round(M.SCEN[scen][0] * scale) for name, scale in CAPACITIES},
              "capacities": {}}
    for name, scale in CAPACITIES:
        cases = {"before": one(scale, measure=light, finished=outside)}
        for c in cap["cases"]:
            measure = dict(light, b_baseline=c["working_days"], b_pilot=0)
            cases[f"after_x{c['factor']}"] = one(scale, measure=measure, finished=finished,
                                                  min_days={"b_baseline": c["nights"]})
        result["capacities"][name] = cases
    return result


def build() -> dict:
    data = json.loads(DATA.read_text(encoding="utf-8"))
    sys.path.insert(0, str(HERE))
    import wbs_data as W
    row = next(it for it in W.ITEMS if it["id"] == "b_baseline")
    used = usage(data)
    cap = capacity(used, row["start"], row["end"])
    return {"what": "derived from the pilot's measured values; every assumption is named",
            "usage": used, "capacity": cap, "schedule": schedule(cap)}


def to_text(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=1) + "\n"


if __name__ == "__main__":
    OUT.write_text(to_text(build()), encoding="utf-8")
    print(f"wrote {OUT.name}")
