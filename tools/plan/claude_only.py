"""The dates that charter version 0.7 gave for going on with Claude alone and with two companies, and what
explains the difference between them and the ranges the charter gives now.

The dates: one seed (11), 1,200 runs for each case, the four cases drawn one after another from that one seed
in the order below, with the measurement sized for subscriptions. The two single-account cases are capacity
scenarios of their own: about 5 working days a week (10th to 90th percentile 2.5 to 10.5) and about 4
(2 to 8.4). Tasks added to the work breakdown after the forecast was made are left out.

The comparison: the single-account cases alone, 1,200 runs with each of ten seeds, once with those capacity
scenarios and once the way pilot_replan.py sets the capacity (the plan's scenario scaled down), and the first
of them with 2,000 runs as well. Nothing but the seed, the way the capacity is set and the number of runs
differs between the columns; neither side has the pilot's numbers or the finished work in it.

Usage: python3 claude_only.py           (rewrites claude_only.json and prints the dates; a few minutes)
       python3 claude_only.py --check   (computes everything again and says whether the file is what it gives)
"""
import json
import random
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED, SIMS = 11, 1200
ADDED = {"claude_alone": ("Claude 하나(월 200달러)", (5, 2.5, 10.5)),
         "claude_shared": ("Claude 하나, 다른 개인 작업과 나눠 씀", (4, 2, 8.4))}


def compute(seed: int = SEED, sims: int = SIMS) -> dict:
    subprocess.run([sys.executable, str(HERE / "build.py")], check=True, capture_output=True)  # writes wbs.json
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    argv, sys.argv = sys.argv, [sys.argv[0], str(sims)]
    try:
        import montecarlo as M
        import montecarlo2 as MC
    finally:
        sys.argv = argv
    plan, _, two = list(M.SCEN)[:3]
    for label, capacity in ADDED.values():
        M.SCEN[label] = capacity
    light = dict(M.MEASURE, b_ref27=85, b_full28=75, b_final=140)
    random.seed(seed)
    cases = {}
    for name, label in (("plan", plan), ("two_companies", two), ("claude_alone", ADDED["claude_alone"][0]),
                        ("claude_shared", ADDED["claude_shared"][0])):
        r = MC.summary(label, True, sims=sims, measure=light)
        cases[name] = {"working_days_a_week": list(M.SCEN[label]),
                       "rel_01_p50": r["rel_01"][1],
                       "rel_10": dict(zip(("p10", "p50", "p80", "p90"), r["rel_10"]))}
    return {"what": "single-seed dates of charter version 0.7; the cases are drawn in this order from one seed",
            "seed": seed, "sims_per_case": sims, "tasks": len(MC.ITEMS),
            "last_day_of_the_simulation": str(MC.date_of(MC.HORIZON)), "cases": cases}


SEEDS = (11, 2029, 1, 2, 3, 4, 5, 6, 7, 8)
PLAN_DAYS = 15


def compare(seeds=SEEDS, sims: int = SIMS, more_sims: int = 2000) -> dict:
    """The middle date of 1.0 for each single-account case, drawn first from each seed."""
    import montecarlo as M
    import montecarlo2 as MC
    plan = list(M.SCEN)[0]
    light = dict(M.MEASURE, b_ref27=85, b_full28=75, b_final=140)

    def middle(seed, scen, n, **kw):
        random.seed(seed)
        days = sorted(r if r is not None else MC.HORIZON
                      for r in (MC.run(scen, True, measure=light, **kw)["rel_10"] for _ in range(n)))
        return str(MC.date_of(days[int(0.5 * len(days))]))

    def spread(values):
        return {"earliest": min(values), "latest": max(values)}

    out = {"seeds": list(seeds), "sims": sims, "more_sims": more_sims}
    for name, (label, capacity) in ADDED.items():
        own = [middle(s, label, sims) for s in seeds]
        scaled = [middle(s, plan, sims, cap_scale=capacity[0] / PLAN_DAYS) for s in seeds]
        out[name] = {"own_scenario": dict(spread(own), by_seed=own),
                     "plan_scenario_scaled": dict(spread(scaled), by_seed=scaled),
                     "plan_scenario_scaled_first_five_seeds": spread(scaled[1:6] or scaled)}
        if name == "claude_alone":
            more = [middle(s, label, more_sims) for s in seeds]
            out[name]["own_scenario_more_runs"] = dict(spread(more), by_seed=more)
    return out


def build() -> dict:
    result = compute()
    result["comparison"] = compare()
    cases = result["cases"]
    day = lambda text: __import__("datetime").date.fromisoformat(text)
    later = (day(cases["claude_alone"]["rel_10"]["p50"]) - day(cases["two_companies"]["rel_10"]["p50"])).days
    work = (day(cases["claude_alone"]["rel_10"]["p50"]) - day("2026-11-17")).days
    # "About two weeks later for each month with Claude alone" in charter version 0.7: the delay against two
    # companies spread evenly over the months of work, which assumes the delay grows in proportion.
    result["later_for_each_month"] = {"from": "2026-11-17", "months_later": round(later / 30.44),
                                      "months_of_work": round(work / 30.44),
                                      "days_for_each_month": round(later / work * 30.44)}
    return result


def to_text(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=1) + "\n"


if __name__ == "__main__":
    result, out = build(), HERE / "claude_only.json"
    if "--check" in sys.argv:
        same = out.is_file() and out.read_text(encoding="utf-8") == to_text(result)
        print("claude_only.json is what the script gives" if same else "claude_only.json differs from what the script gives")
        sys.exit(0 if same else 1)
    out.write_text(to_text(result), encoding="utf-8")
    for name, case in result["cases"].items():
        print(name, case["rel_01_p50"], case["rel_10"])
    for name in ADDED:
        print(name, {k: (v["earliest"], v["latest"]) for k, v in result["comparison"][name].items()})
    print(result["later_for_each_month"])
