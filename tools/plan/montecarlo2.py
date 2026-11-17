"""Week-by-week Monte Carlo of the plan to 1.0: AI capacity from subscriptions is shared among running tasks.

A task ends when its calendar minimum has passed and its AI work is used up. Running tasks share the
weekly capacity in proportion to the pace each needs. Assumptions are the same as montecarlo.py.
Usage: python3 montecarlo2.py [n_sims]
"""
import datetime as dt
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import montecarlo as M  # noqa: E402  (reuses categories, risks, measurement load; its own run is skipped below)

D = M.D
T0 = dt.date(2026, 10, 29)
off = lambda s: (dt.date.fromisoformat(s) - T0).days
BS = lambda it: it.get("base_start") or it["start"]  # planned dates before the forecast stretched them
BE = lambda it: it.get("base_end") or it["end"]
ITEMS = [it for it in D["items"]]
IDX = {it["id"]: k for k, it in enumerate(ITEMS)}
PRED = [[IDX[p] for p in it.get("after", [])] for it in ITEMS]
SUCC = defaultdict(list)
for k, ps in enumerate(PRED):
    for p in ps:
        SUCC[p].append(k)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
HORIZON = 365 * 16
HUMAN_ITEMS = ["pm_budget", "pm_infra", "rel_names", "q_audit01_pick", "q_audit_pick", "rel_fund", "rel_gov"]


def run(scen, outcome, cap_scale=1.0, risk_scale=1.0, over_scale=1.0, measure=None, loop_week=None, defer=(),
        finished=None, min_days=None, until="rel_10"):
    """One run of the plan. `finished` maps tasks that are already done to the day they ended: they take no
    capacity and nothing waits for them any longer. `min_days` maps tasks to the fewest calendar days they can
    take, whatever the capacity (a measurement that runs only so many times a night). The run ends when the
    task `until` is done; tasks that end later are then not dated."""
    finished = finished or {}
    min_days = min_days or {}
    MEAS = M.MEASURE if measure is None else measure
    LOOPW = M.LOOP_PER_WEEK if loop_week is None else loop_week
    med, p10, p90 = M.SCEN[scen]
    s_cap = (math.log(p90 / med) + math.log(med / p10)) / 2 / 1.2816
    K = med * cap_scale * math.exp(random.gauss(0, s_cap))  # AI working days per week
    extra_cal = defaultdict(float)
    extra_work = defaultdict(float)
    for name, p, (lo, hi), target in M.RISKS:
        if random.random() < p * risk_scale:
            extra_cal[target] += random.uniform(lo, hi)
            if name == "rewrite":
                extra_work[target] += random.uniform(60, 200)
            if name == "min_gate_fail" and random.random() < 0.15:
                extra_cal[target] += random.uniform(84, 182)
    approve_extra = defaultdict(float)
    if random.random() < 0.5 * risk_scale:  # the owner is away: only the few owner-only tasks (money, contracts, accounts) wait
        extra_cal[random.choice(HUMAN_ITEMS)] += random.uniform(14, 56)

    n = len(ITEMS)
    start = [None] * n
    done = [None] * n
    work = [0.0] * n
    cal = [0.0] * n
    rate_fixed = [0.0] * n
    for k, it in enumerate(ITEMS):
        if it.get("milestone"):
            continue
        if it["id"] in finished:
            done[k] = off(finished[it["id"]])
            continue
        dur = off(BE(it)) - off(BS(it)) + 1
        a = it.get("ai_days", 0) + MEAS.get(it["id"], 0)
        if it["id"] in ("l_run1", "l_run2", "l_run3"):
            a += LOOPW * dur / 7
        if it["id"] in defer:
            work[k], cal[k] = 0.0, 1.0
            continue
        if it.get("ongoing"):
            work[k], cal[k] = a, dur
            rate_fixed[k] = a / (dur / 7)
            continue
        med_m, p90_m = M.CATS[M.cat(it)]
        m = 1 + (M.lognorm(med_m, p90_m) - 1) * over_scale
        work[k] = a * m + extra_work[it["id"]]
        cal[k] = max(dur * m * (0.8 if a else 1.0) + extra_cal[it["id"]], min_days.get(it["id"], 0))
    elapsed = [0.0] * n
    pending = {k for k in range(n) if done[k] is None}
    active = set()

    def try_start(t):
        changed = True
        while changed:
            changed = False
            for k in list(pending):
                it = ITEMS[k]
                if any(done[p] is None for p in PRED[k]):
                    continue
                ready = max([off(BS(it))] + [done[p] + (0 if it.get("milestone") else 1) for p in PRED[k]])
                if it.get("milestone"):
                    d = ready + random.uniform(0, 1) + approve_extra.get(it["id"], 0)  # gates are judged by AI against criteria written beforehand
                    if it["id"] == "g_10" and outcome:
                        tries = 0
                        while random.random() > (0.55 if tries == 0 else 0.5):
                            tries += 1
                            d += 56
                            if tries == 3:
                                d += 365
                                break
                    done[k] = d
                    pending.discard(k)
                    changed = True
                elif ready < t + 7:
                    start[k] = max(ready, t)
                    pending.discard(k)
                    active.add(k)
                    changed = True

    t = 0
    target_k = IDX[until]
    while done[target_k] is None and t < HORIZON:
        try_start(t)
        # pace each running task needs to finish inside its remaining calendar
        need = {}
        for k in active:
            if rate_fixed[k]:
                need[k] = rate_fixed[k]
            elif work[k] > 1e-9:
                left_weeks = max(1.0, (cal[k] - elapsed[k]) / 7)
                need[k] = work[k] / left_weeks
        total = sum(need.values())
        f = min(1.0, K / total) if total > 0 else 1.0
        for k in list(active):
            span = min(7, t + 7 - start[k]) if start[k] > t else 7
            frac = span / 7
            if k in need:
                work[k] -= need[k] * f * frac
            elapsed[k] += span
            it = ITEMS[k]
            if it.get("ongoing"):
                if elapsed[k] >= cal[k]:
                    done[k] = start[k] + cal[k] - 1
                    active.discard(k)
                continue
            if work[k] <= 1e-9 and elapsed[k] >= cal[k]:
                done[k] = start[k] + elapsed[k] - 1
                active.discard(k)
        t += 7
    try_start(t)
    return {ITEMS[k]["id"]: done[k] for k in range(n)}


def date_of(d):
    return T0 + dt.timedelta(days=int(d)) if d is not None else None


def summary(scen, outcome, sims=N, **kw):
    out = defaultdict(list)
    for _ in range(sims):
        r = run(scen, outcome, **kw)
        for g in ("g_min", "rel_01", "rel_02", "rel_03", "g_10", "rel_10"):
            out[g].append(int(r[g]) if r[g] is not None else HORIZON)
    res = {}
    for g, v in out.items():
        v.sort()
        res[g] = [str(date_of(v[int(q * len(v))])) for q in (0.1, 0.5, 0.8, 0.9)]
    v = sorted(out["rel_10"])
    res["p_target"] = sum(1 for x in v if x <= off("2029-09-28")) / len(v)
    res["p_latest"] = sum(1 for x in v if x <= off("2029-12-28")) / len(v)
    res["p_2030"] = sum(1 for x in v if x <= off("2030-12-31")) / len(v)
    return res


if __name__ == "__main__":
    random.seed(7)
    report = {}
    for scen in M.SCEN:
        for outcome in (False, True):
            key = f"{scen} / {'숫자 목표 포함' if outcome else '다 만들고 감사까지'}"
            r = summary(scen, outcome)
            report[key] = r
            print(key)
            for g in ("g_min", "rel_01", "rel_03", "rel_10"):
                print(f"  {g:7s} P10 {r[g][0]}  P50 {r[g][1]}  P80 {r[g][2]}  P90 {r[g][3]}")
            print(f"  P(<=2029-09-28) {r['p_target']:.1%}  P(<=2029-12-28) {r['p_latest']:.1%}  P(<=2030-12-31) {r['p_2030']:.1%}")
    # what it takes: unlimited capacity, no risks, no overruns (the plan as written)
    for label, kw in [("용량 무제한 · 위험 없음 · 초과 없음(계획 그대로)", dict(cap_scale=100, risk_scale=0, over_scale=0)),
                      ("용량 무제한 · 위험과 초과는 그대로", dict(cap_scale=100)),
                      ("계정 하나 · 위험 절반", dict(risk_scale=0.5))]:
        scen = list(M.SCEN)[0]
        r = summary(scen, True, sims=max(500, N // 2), **kw)
        report[label] = r
        print(label, "1.0 P10/P50/P80/P90", r["rel_10"], f"P(<=2029-09-28) {r['p_target']:.1%}")
    (HERE / "montecarlo2.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
