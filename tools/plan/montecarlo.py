"""Monte Carlo of the plan to 1.0 under subscription-only AI capacity.

Every number below is an assumption written down on purpose; the report quotes them.
Usage: python3 montecarlo.py [n_sims]
"""
import datetime as dt
import json
import math
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
D = json.loads((HERE / "wbs.json").read_text(encoding="utf-8"))
# Tasks added to the work breakdown after the forecast was made carry the day they were added. The forecast
# rolls the tasks it was made with, so that its dates can be reproduced; DODAM_PLAN_ALL_TASKS=1 rolls every task.
ADDED_LATER = [i["id"] for i in D["items"] if i.get("added")]
if not os.environ.get("DODAM_PLAN_ALL_TASKS"):
    D["items"] = [i for i in D["items"] if not i.get("added")]
ITEMS = {i["id"]: i for i in D["items"]}
ORDER = sorted(D["items"], key=lambda x: (x["start"], 0 if x.get("milestone") else 1))
day = dt.date.fromisoformat
N = int(sys.argv[1]) if len(sys.argv) > 1 else 20000

# How much each kind of work tends to run over its planned length: (median multiplier, 90th percentile).
CATS = {
    "novel": (1.3, 2.5),      # new design and compiler work, nothing to copy
    "routine": (1.15, 1.8),   # libraries, tools, packages built after the design is set
    "bench": (1.2, 2.0),      # tasks, measuring tools, measurement
    "human": (1.2, 2.0),      # work that waits on the owner
    "external": (1.1, 1.5),   # outside auditors
    "fixed": (1.0, 1.1),      # calendar-bound (three months of live operation)
}
NOVEL = {"design", "front", "back", "data"}
ROUTINE = {"stdlib", "tools", "pkg", "report", "loop"}


def cat(it):
    i, g = it["id"], it["group"]
    if i in ("q_audit01", "q_audit"):
        return "external"
    if i == "rel_svc_run":
        return "fixed"
    if i in ("q_lean_model", "q_lean_proof", "k_mm_proto", "k_refrun"):
        return "novel"
    if g in NOVEL:
        return "novel"
    if g in ROUTINE:
        return "routine"
    if g == "bench":
        return "bench"
    if g == "qa":
        return "routine"
    return "human"


def lognorm(median, p90):
    s = math.log(p90 / median) / 1.2816
    return median * math.exp(random.gauss(0, s))


# Agent time that measurement runs eat from the subscriptions (in AI working days, one day about 16 agent hours).
# runs x hours per run / 16: baseline 630 x 0.5h, grammar candidates ~360 x 0.5h, memory prototypes 720 x 0.25h, ...
MEASURE = {"b_pilot": 0.3, "b_baseline": 20, "d_syntax": 11, "b_mm_measure": 11, "b_dodam1": 8, "d_syntax2": 3,
           "b_ref27": 85, "b_full28": 75, "b_final": 140}  # sized for subscriptions: 0.1 2,730 runs, 0.3 2,400, 1.0 4,500
LOOP_PER_WEEK = 3.75  # weekly judgement with one model + monthly judgement with three models, during the loop

# Demand per week in AI working days, from the planned windows.
demand = defaultdict(float)
def monday(d):
    return d - dt.timedelta(days=d.weekday())
for it in D["items"]:
    s, e = day(it["start"]), day(it["end"])
    n = (e - s).days + 1
    a = it.get("ai_days", 0) + MEASURE.get(it["id"], 0)
    if it["id"] in ("l_run1", "l_run2", "l_run3"):
        a += LOOP_PER_WEEK * n / 7
    if not a:
        continue
    for k in range(n):
        demand[monday(s + dt.timedelta(days=k))] += a / n  # each day adds its share; the week sums them
# demand[w] is AI working days needed in week w

def window_mean(it):
    s, e = day(it["start"]), day(it["end"])
    ws, w = [], monday(s)
    while w <= e:
        ws.append(demand.get(w, 0.0))
        w += dt.timedelta(days=7)
    return sum(ws) / len(ws)

WMEAN = {i: window_mean(it) for i, it in ITEMS.items()}

# Capacity scenarios: AI working days per week the subscriptions give (median, 10th, 90th percentile).
# One account per company: more accounts to raise limits goes against the companies' usage policies
# (subscription research, 2026-10-29), so capacity grows only through higher plans.
SCEN = {
    "회사마다 계정 하나(최상위 요금제, 월 약 650달러)": (15, 7, 32),
    "용량이 그 두 배일 때": (30, 14, 64),
    "Google 없이 두 회사(월 약 400달러)": (10, 5, 21),
}

RISKS = [  # (name, probability, (min days, max days), item that absorbs the delay)
    ("mm_inconclusive", 0.30, (14, 42), "b_mm_measure"),
    ("min_gate_fail", 0.40, (28, 70), "b_dodam1"),
    ("rewrite", 0.25, (84, 280), "lib_core"),
    ("perf_miss", 0.35, (42, 112), "k_perf"),
    ("svc_issue", 0.25, (28, 84), "rel_svc_run"),
    ("audit01_severe", 0.30, (14, 42), "q_audit01_fix"),
    ("audit_severe", 0.35, (28, 84), "q_audit_fix"),
    ("vendor_policy", 0.30, (28, 84), "b_full28"),
    ("rename", 0.30, (14, 42), "rel_docs"),
]
GATES = ["g_dir", "d_mm_decide", "g_min", "rel_01", "rel_03", "g_10"]


def simulate(scen, outcome):
    med, p10, p90 = SCEN[scen]
    s_cap = (math.log(p90 / med) / 1.2816 + math.log(med / p10) / 1.2816) / 2
    K = med * math.exp(random.gauss(0, s_cap))
    extra = defaultdict(float)
    for name, p, (lo, hi), target in RISKS:
        if random.random() < p:
            extra[target] += random.uniform(lo, hi)
            if name == "min_gate_fail" and random.random() < 0.15:  # pivot to a syntax AI already knows
                extra[target] += random.uniform(84, 182)
    if random.random() < 0.5:  # the owner is away for a while
        extra["@" + random.choice(GATES)] += random.uniform(14, 56)
    fin = {}
    for it in ORDER:
        i = it["id"]
        s0, e0 = day(it["start"]), day(it["end"])
        ready = s0
        for p in it.get("after", []):
            nxt = fin[p] + dt.timedelta(days=0 if it.get("milestone") else 1)
            ready = max(ready, nxt)
        if it.get("milestone"):
            approve = dt.timedelta(days=random.uniform(0, 7) + extra.get("@" + i, 0))
            fin[i] = max(ready, s0) + approve
            if i == "g_10" and outcome:  # the numbers may not reach the 1.0 bar on the first try
                tries = 0
                while random.random() > (0.55 if tries == 0 else 0.5):
                    tries += 1
                    fin[i] += dt.timedelta(days=56)
                    if tries == 3:
                        fin[i] += dt.timedelta(days=365)
                        break
            continue
        dur = (e0 - s0).days + 1
        if it.get("ongoing"):
            fin[i] = max(e0, ready + dt.timedelta(days=dur - 1))
            continue
        m = lognorm(*CATS[cat(it)])
        ai = it.get("ai_days", 0) + MEASURE.get(i, 0)
        stretch = max(1.0, WMEAN[i] / K) if ai else 1.0
        d = dur * m * stretch + extra.get(i, 0)
        fin[i] = ready + dt.timedelta(days=max(1, round(d)) - 1)
    return fin


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


if __name__ == "__main__":
    random.seed(20261029)
    target, latest = day("2029-09-28"), day("2029-12-28")
    report = {}
    for scen in SCEN:
        for outcome in (False, True):
            res = defaultdict(list)
            for _ in range(N):
                f = simulate(scen, outcome)
                for g in ("g_min", "rel_01", "rel_02", "rel_03", "rel_rc1", "g_10", "rel_10"):
                    res[g].append(f[g])
            key = f"{scen} / {'숫자 목표 포함' if outcome else '다 만들고 감사까지'}"
            r10 = res["rel_10"]
            row = {g: [str(pct(v, q)) for q in (0.1, 0.5, 0.8, 0.9)] for g, v in res.items()}
            row["p_target"] = sum(1 for x in r10 if x <= target) / N
            row["p_latest"] = sum(1 for x in r10 if x <= latest) / N
            report[key] = row
            print(key)
            for g in ("g_min", "rel_01", "rel_03", "rel_10"):
                print(f"  {g:7s} P10 {row[g][0]}  P50 {row[g][1]}  P80 {row[g][2]}  P90 {row[g][3]}")
            print(f"  P(1.0 <= 2029-09-28) = {row['p_target']:.1%}   P(<= 2029-12-28) = {row['p_latest']:.1%}")
    (HERE / "montecarlo.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    # Demand versus capacity, for the report
    tot_dev = sum(i.get("ai_days", 0) for i in D["items"])
    tot_meas = sum(MEASURE.values()) + LOOP_PER_WEEK * sum(((day(ITEMS[i]["end"]) - day(ITEMS[i]["start"])).days + 1) / 7 for i in ("l_run1", "l_run2", "l_run3"))
    weeks = (day("2029-09-28") - day("2026-11-02")).days / 7
    print(f"AI work: development {tot_dev:.0f} + measurement {tot_meas:.0f} = {tot_dev + tot_meas:.0f} working days over {weeks:.0f} weeks = {(tot_dev + tot_meas) / weeks:.1f} per week on average")
    print("peak weekly demand:", f"{max(demand.values()):.1f}", "weeks above 15:", sum(1 for v in demand.values() if v > 15), "above 30:", sum(1 for v in demand.values() if v > 30))
