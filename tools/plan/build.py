"""Validate the Dodam plan-to-1.0 work breakdown and render it.

Usage: python3 build.py            -> checks, writes wbs.json and wbs_section.md
The data lives in wbs_data.py (GROUPS, ITEMS). Internal ids are never rendered.
"""
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import wbs_data as W  # noqa: E402

D = dt.date.fromisoformat
errors, warns = [], []

# Codes and cross-references the readers must never see.
BANNED = [
    (re.compile(r"\b[A-Z]{1,3}-?\d{1,3}\b"), "code-like token"),
    (re.compile(r"§"), "section sign"),
    (re.compile(r"\b(CWE|CVE|GHSA|RFC)\b"), "external code"),
    (re.compile(r"(참고하|참조하|참고:|참조:|[을를] 참고|[을를] 참조|자세한 내용은|문서를 보|아래 링크)"), "points to another document"),
    (re.compile(r"\b(Phase|Stage|Sprint|M\d|P\d)\b"), "stage code"),
]
ALLOWED_TOKENS = {"MCP", "LSP", "JSON", "WASM", "WASI", "LLVM", "CSV", "HTTP", "SQL", "HTML", "XSS",
                  "SSRF", "CLI", "API", "AI", "DB", "CI", "DNS", "URL", "IDE", "JS", "SMT", "UTF-8"}


def scan_text(where, text):
    for rx, why in BANNED:
        for m in rx.finditer(text or ""):
            tok = m.group(0)
            if tok in ALLOWED_TOKENS or re.fullmatch(r"[A-Z]{2,5}", tok):
                continue
            # allow model/version names like "p99", years etc. only via explicit whitelist below
            if tok in getattr(W, "TEXT_WHITELIST", set()):
                continue
            errors.append(f"{where}: {why}: {tok!r}")


ids = {}
for it in W.ITEMS:
    if it["id"] in ids:
        errors.append(f"duplicate id {it['id']}")
    ids[it["id"]] = it
    for k in ("group", "name", "start", "end", "out", "done", "who"):
        if not it.get(k):
            errors.append(f"{it['id']}: missing {k}")
    try:
        s, e = D(it["start"]), D(it["end"])
        if e < s:
            errors.append(f"{it['id']}: end before start")
        it["_s"], it["_e"] = s, e
    except Exception as ex:  # noqa: BLE001
        errors.append(f"{it['id']}: bad date {ex}")
    if it["group"] not in W.GROUPS:
        errors.append(f"{it['id']}: unknown group {it['group']}")
    for f in ("name", "out", "done", "who", "note"):
        scan_text(f"{it['id']}.{f}", it.get(f, ""))

for gid, g in W.GROUPS.items():
    scan_text(f"group {gid}", g["name"] + " " + g.get("desc", ""))

# Dependencies are finish-to-start: a successor starts after every predecessor ends.
succ = defaultdict(list)
for it in W.ITEMS:
    for p in it.get("after", []):
        if p not in ids:
            errors.append(f"{it['id']}: unknown predecessor {p}")
            continue
        succ[p].append(it["id"])
        if "_e" in ids[p] and "_s" in it and not ids[p]["_e"] < it["_s"]:
            if not (it.get("milestone") and ids[p]["_e"] <= it["_s"]):
                errors.append(f"{it['id']} starts {it['_s']} before predecessor {p} ends {ids[p]['_e']}")

lo, hi = D(W.PROJECT_START), D(W.RELEASE_DATE)
for it in W.ITEMS:
    if "_s" in it and (it["_s"] < lo or it["_e"] > hi):
        errors.append(f"{it['id']}: outside project window {it['_s']}..{it['_e']}")

# Slack: how many days an item can slip before the next fixed date behind it slips.
# Milestones are fixed dates (gates and releases); other items are counted back from them.
order = sorted(W.ITEMS, key=lambda x: x["_e"], reverse=True)
latest = {}
for it in order:
    if it.get("milestone"):
        latest[it["id"]] = it["_e"]
        continue
    lf = hi
    for sid in succ[it["id"]]:
        s_it = ids[sid]
        if s_it.get("milestone"):
            lf = min(lf, s_it["_s"])  # a gate may fall on the day its last input finishes
            continue
        dur = (s_it["_e"] - s_it["_s"]).days
        ls = latest[sid] - dt.timedelta(days=dur)
        lf = min(lf, ls - dt.timedelta(days=1))
    latest[it["id"]] = lf
for it in W.ITEMS:
    it["slack"] = (latest[it["id"]] - it["_e"]).days
    if it["slack"] < 0:
        errors.append(f"{it['id']}: negative slack {it['slack']}")


def fmt_range(s, e):
    if s == e:
        return f"{s.year}-{s.month:02d}-{s.day:02d}"
    if s.year == e.year:
        return f"{s.year}-{s.month:02d}-{s.day:02d}~{e.month:02d}-{e.day:02d}"
    return f"{s.isoformat()}~{e.isoformat()}"


def names(lst):
    return ", ".join(ids[p]["name"] for p in lst) if lst else "—"


# Effort totals per group and per period.
periods = W.PERIODS
def period_of(day):
    for p in periods:
        if D(p["start"]) <= day <= D(p["end"]):
            return p["name"]
    return "?"

tot = defaultdict(lambda: [0.0, 0.0, 0.0])
tot_p = defaultdict(lambda: [0.0, 0.0, 0.0])
for it in W.ITEMS:
    h, a, c = it.get("human_h", 0), it.get("ai_days", 0), it.get("cost_usd", 0)
    for bucket in (tot[it["group"]], tot_p[period_of(it["_s"])]):
        bucket[0] += h; bucket[1] += a; bucket[2] += c

if errors:
    print("ERRORS:")
    for e in errors:
        print(" -", e)
    sys.exit(1)

# Render the WBS section in Markdown.
lines = []
for gid, g in W.GROUPS.items():
    rows = sorted([i for i in W.ITEMS if i["group"] == gid], key=lambda x: (x["_s"], x["_e"]))
    if not rows:
        continue
    h, a, c = tot[gid]
    lines.append(f"### {g['name']}\n")
    if g.get("desc"):
        lines.append(g["desc"] + "\n")
    money = (f"약 {c/10000:.1f}만 달러".replace(".0만", "만") if c >= 10000 else f"약 {int(c / 100 + 0.5) * 100:,.0f}달러")
    lines.append(f"공수 합계: 사람 {h:.0f}시간 · AI {a:.0f}작업일" + (f" · 직접 비용 {money}" if c else "") + "\n")
    lines.append("| 기간 | 할 일 | 산출물 | 끝났다는 표시 | 앞선 일 | 맡는 쪽 | 공수 |")
    lines.append("|---|---|---|---|---|---|---|")
    for it in rows:
        tight = it["slack"] <= W.CRITICAL_SLACK_DAYS and not it.get("ongoing") and not it.get("milestone")
        it["critical"] = tight
        name = ("◆ " if it.get("milestone") else "") + it["name"] + (" (주경로)" if tight else "")
        eff = []
        if it.get("human_h"): eff.append(f"사람 {it['human_h']:g}h")
        if it.get("ai_days"): eff.append(f"AI {it['ai_days']:g}일")
        if it.get("cost_usd"): eff.append(f"{it['cost_usd']:,.0f}달러")
        lines.append("| " + " | ".join([
            fmt_range(it["_s"], it["_e"]), name, it["out"], it["done"], names(it.get("after", [])),
            it["who"], " · ".join(eff) or "—"]) + " |")
    lines.append("")
(HERE / "wbs_section.md").write_text("\n".join(lines), encoding="utf-8")

out = {
    "project_start": W.PROJECT_START, "release": W.RELEASE_DATE,
    "periods": periods,
    "groups": [{"id": k, **v, "human_h": tot[k][0], "ai_days": tot[k][1], "cost_usd": tot[k][2]} for k, v in W.GROUPS.items()],
    "items": [{k: v for k, v in it.items() if not k.startswith("_")} | {"after_names": [ids[p]["name"] for p in it.get("after", [])]} for it in W.ITEMS],
    "period_totals": {k: v for k, v in tot_p.items()},
}
(HERE / "wbs.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

print(f"items {len(W.ITEMS)} · groups {len(W.GROUPS)} · milestones {sum(1 for i in W.ITEMS if i.get('milestone'))}")
crit = [i for i in sorted(W.ITEMS, key=lambda x: x['_s']) if i["slack"] <= W.CRITICAL_SLACK_DAYS and not i.get("ongoing") and not i.get("milestone")]
print("critical path:")
for i in crit:
    print(f"  {fmt_range(i['_s'], i['_e'])}  {i['name']}  (slack {i['slack']}d)")
print("totals by group (human h, AI days, USD):")
for k, v in tot.items():
    print(f"  {W.GROUPS[k]['name']}: {v[0]:.0f}h · {v[1]:.0f}d · ${v[2]:,.0f}")
print("totals by period:")
for p in periods:
    v = tot_p[p["name"]]
    print(f"  {p['name']}: {v[0]:.0f}h · {v[1]:.0f}d · ${v[2]:,.0f}")

# Load: AI agent-days spread evenly over each item's days, summed per week, shown as agents running at once
# (an agent works 6 days a week).
load = defaultdict(float)
for it in W.ITEMS:
    a = it.get("ai_days", 0)
    if not a:
        continue
    days = (it["_e"] - it["_s"]).days + 1
    for k in range(days):
        day = it["_s"] + dt.timedelta(days=k)
        monday = day - dt.timedelta(days=day.weekday())
        load[monday] += a / days
weeks = sorted(load)
agents = {w: load[w] / 6 for w in weeks}
peak = max(agents.values())
print(f"peak agents at once: {peak:.1f}")
by_month = defaultdict(list)
for w in weeks:
    by_month[w.strftime("%Y-%m")].append(agents[w])
print("monthly average agents at once:")
print("  " + "  ".join(f"{m}:{sum(v)/len(v):.1f}" for m, v in sorted(by_month.items())))
over = [w for w in weeks if agents[w] > W.MAX_AGENTS]
print(f"weeks above {W.MAX_AGENTS} agents: {len(over)}", ", ".join(w.isoformat() for w in over[:30]))
out["load"] = {w.isoformat(): round(agents[w], 2) for w in weeks}
(HERE / "wbs.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

# Owner hours per month (the project owner works on this beside other work).
hm = defaultdict(float)
for it in W.ITEMS:
    h = it.get("human_h", 0)
    if not h:
        continue
    days = (it["_e"] - it["_s"]).days + 1
    for k in range(days):
        day = it["_s"] + dt.timedelta(days=k)
        hm[day.strftime("%Y-%m")] += h / days
print("owner hours per month:")
print("  " + "  ".join(f"{m}:{v:.0f}" for m, v in sorted(hm.items())))
print(f"owner hours total {sum(hm.values()):.0f}, max month {max(hm.values()):.0f}")
out["owner_hours_month"] = {m: round(v, 1) for m, v in sorted(hm.items())}
out["agents_month"] = {m: round(sum(v)/len(v), 2) for m, v in sorted(by_month.items())}
(HERE / "wbs.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

# How far each gate can slip before 1.0 slips (milestones not pinned, work keeps its length).
free = {}
for it in sorted(W.ITEMS, key=lambda x: (x["_e"], 1 if x.get("milestone") else 0), reverse=True):
    lf = hi
    for sid in succ[it["id"]]:
        s_it = ids[sid]
        dur = (s_it["_e"] - s_it["_s"]).days
        lf = min(lf, free[sid] - dt.timedelta(days=dur) - dt.timedelta(days=0 if s_it.get("milestone") else 1))
    free[it["id"]] = lf
print("gate float to 1.0 (days):")
for it in sorted([i for i in W.ITEMS if i.get("milestone")], key=lambda x: x["_s"]):
    it["float_to_release"] = (free[it["id"]] - it["_e"]).days
    print(f"  {it['_s']}  {it['name']}: {it['float_to_release']}")
out["items"] = [{k: v for k, v in it.items() if not k.startswith("_")} | {"after_names": [ids[p]["name"] for p in it.get("after", [])]} for it in W.ITEMS]
(HERE / "wbs.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

# Money and AI work per calendar year (spread evenly over each item's days).
cy, ay = defaultdict(float), defaultdict(float)
for it in W.ITEMS:
    days = (it["_e"] - it["_s"]).days + 1
    for k in range(days):
        y = (it["_s"] + dt.timedelta(days=k)).year
        cy[y] += it.get("cost_usd", 0) / days
        ay[y] += it.get("ai_days", 0) / days
print("per year: direct USD / AI days")
for y in sorted(cy):
    print(f"  {y}: ${cy[y]:,.0f} / {ay[y]:,.0f}d")
print(f"  total: ${sum(cy.values()):,.0f} / {sum(ay.values()):,.0f}d")
out["year_cost"] = {str(y): round(v) for y, v in cy.items()}
out["year_ai_days"] = {str(y): round(v) for y, v in ay.items()}
(HERE / "wbs.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
