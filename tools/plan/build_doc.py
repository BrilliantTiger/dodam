"""Render the plan to Markdown (repo document) from plan_content.py, wbs.json and the forecast runs.

Usage: python3 build_doc.py OUT.md
"""
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import plan_content  # noqa: E402

D = json.loads((HERE / "wbs.json").read_text(encoding="utf-8"))
ANCH = json.loads((HERE / "anchors.json").read_text(encoding="utf-8"))
MC2 = json.loads((HERE / "montecarlo2.json").read_text(encoding="utf-8"))
SC = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))
ITEMS = D["items"]
GROUPS = D["groups"]
BY = {i["id"]: i for i in ITEMS}
# Agent time that measurement runs take from the subscriptions (AI working days), as in the forecast.
MEASURE = {"b_pilot": 0.3, "b_baseline": 20, "d_syntax": 11, "b_mm_measure": 11, "b_dodam1": 8, "d_syntax2": 3,
           "b_ref27": 85, "b_full28": 75, "b_final": 140}
LOOP_PER_WEEK = 3.75


def man(x):
    """Dollars in Korean units: 197,730 -> '약 19.8만 달러'."""
    if x >= 10000:
        return f"약 {x / 10000:.1f}만 달러".replace(".0만", "만")
    return f"약 {int(x / 100 + 0.5) * 100:,.0f}달러"


def kdate(s):
    y, m, d = s.split("-")
    return f"{int(y)}년 {int(m)}월 {int(d)}일"


def kmonth(s):
    y, m, _ = s.split("-")
    return f"{int(y)}년 {int(m)}월"


def months_between(a, b):
    """Whole months from date a to date b, rounded."""
    return round((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days / 30.44)


def demand_weeks():
    dem = defaultdict(float)
    for it in ITEMS:
        s, e = dt.date.fromisoformat(it["start"]), dt.date.fromisoformat(it["end"])
        n = (e - s).days + 1
        a = it.get("ai_days", 0) + MEASURE.get(it["id"], 0)
        if it["id"] in ("l_run1", "l_run2", "l_run3"):
            a += LOOP_PER_WEEK * n / 7
        for k in range(n):
            d = s + dt.timedelta(days=k)
            dem[d - dt.timedelta(days=d.weekday())] += a / n
    return dem


def stats():
    ai = sum(i.get("ai_days", 0) for i in ITEMS)
    meas = sum(MEASURE.values()) + sum(LOOP_PER_WEEK * ((dt.date.fromisoformat(BY[i]["end"]) - dt.date.fromisoformat(BY[i]["start"])).days + 1) / 7 for i in ("l_run1", "l_run2", "l_run3"))
    direct = sum(i.get("cost_usd", 0) for i in ITEMS)
    hm = D["owner_hours_month"]
    dem = demand_weeks()
    weeks = (dt.date.fromisoformat(D["release"]) - dt.date(2026, 11, 2)).days / 7
    by_month = defaultdict(list)
    for w, v in dem.items():
        by_month[w.strftime("%Y-%m")].append(v)
    peak_month = max(by_month, key=lambda m: sum(by_month[m]) / len(by_month[m]))
    gate = {i: {"p50": BY[i]["start"], "p80": ANCH[i]["p80"], "float": BY[i].get("float_to_release")} for i in ANCH}
    return {
        "n_items": len(ITEMS), "n_groups": len(GROUPS), "n_ms": sum(1 for i in ITEMS if i.get("milestone")),
        "human_total": sum(i.get("human_h", 0) for i in ITEMS),
        "human_max_month": max(hm.values()),
        "human_max_month_name": (lambda k: f"{k[:4]}년 {int(k[5:])}월")(max(hm, key=hm.get)),
        "human_max_week": max(hm.values()) / 4.33,
        "human_week_avg": sum(hm.values()) / weeks,
        "ai_total": ai, "meas_total": meas, "work_total": ai + meas,
        "need_avg": (ai + meas) / weeks, "need_peak_month": kmonth(peak_month + "-01"),
        "need_peak": sum(by_month[peak_month]) / len(by_month[peak_month]),
        "direct_total": direct, "direct_k": man(direct),
        "subs_k": man(BY["pm_subs"]["cost_usd"]), "infra_k": man(BY["pm_infra_run"]["cost_usd"]),
        "sub_months": months_between("2026-11-01", ANCH["rel_10"]["p50"]),
        "pilot_k": kdate(BY["b_pilot"]["start"]), "reforecast_k": kdate(BY["pm_reforecast"]["start"]),
        "base_p2030": SC["계정 하나, 측정을 구독에 맞게 줄임"]["p_2030"],
        "two_p50_k": kmonth(SC["두 회사, 측정을 구독에 맞게 줄임"]["rel_10"][1]),
        "two_p80_k": kmonth(SC["두 회사, 측정을 구독에 맞게 줄임"]["rel_10"][2]),
        "double_p50_k": kmonth(SC["용량 두 배, 측정을 구독에 맞게 줄임"]["rel_10"][1]),
        "double_p80_k": kmonth(SC["용량 두 배, 측정을 구독에 맞게 줄임"]["rel_10"][2]),
        "m_double": months_between(SC["용량 두 배, 측정을 구독에 맞게 줄임"]["rel_10"][1],
                                   SC["계정 하나, 측정을 구독에 맞게 줄임"]["rel_10"][1]),
        "m_two": months_between(SC["계정 하나, 측정을 구독에 맞게 줄임"]["rel_10"][1],
                                SC["두 회사, 측정을 구독에 맞게 줄임"]["rel_10"][1]),
        "audit_k": man(BY["q_audit01"]["cost_usd"] + BY["q_audit"]["cost_usd"]),
        "rel_p50": ANCH["rel_10"]["p50"], "rel_p80": ANCH["rel_10"]["p80"], "rel_p10": ANCH["rel_10"]["p10"],
        "rel_p50_k": kdate(ANCH["rel_10"]["p50"]), "rel_p80_k": kdate(ANCH["rel_10"]["p80"]),
        "years_p50": (dt.date.fromisoformat(ANCH["rel_10"]["p50"]) - dt.date(2026, 11, 2)).days / 365.25,
        "gate": gate, "k": kdate, "km": kmonth,
    }


def md_table(head, rows):
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return lines + [""]


def quarters():
    out, (y, q) = [], (2026, 4)
    end = dt.date.fromisoformat(D["release"])
    while dt.date(y, q * 3 - 2, 1) <= end:
        out.append((y, q))
        q += 1
        if q == 5:
            y, q = y + 1, 1
    return out


def in_quarter(it, y, q):
    s, e = dt.date.fromisoformat(it["start"]), dt.date.fromisoformat(it["end"])
    qs = dt.date(y, q * 3 - 2, 1)
    qe = dt.date(y + (q == 4), (q * 3) % 12 + 1, 1) - dt.timedelta(days=1)
    return s <= qe and e >= qs


def years():
    return sorted({y for y, _ in quarters()})


def md_gantt():
    qs = quarters()
    head = ["갈래"] + [f"{str(y)[2:]}년 {q}분기" for y, q in qs]
    rows = []
    for g in GROUPS:
        cells = [g["name"]]
        for y, q in qs:
            work = [i for i in ITEMS if i["group"] == g["id"] and not i.get("milestone") and in_quarter(i, y, q)]
            ms = [i for i in ITEMS if i["group"] == g["id"] and i.get("milestone") and in_quarter(i, y, q)]
            cells.append("◆" if ms else ("■" if work else ""))
        rows.append(cells)
    note = ("갈래마다 분기별로 일이 있는지를 표시했다. ■는 그 분기에 진행되는 일이 있다는 뜻, ◆는 그 분기에 이정표가 있다는 뜻이다. "
            "상시 일(퍼징 · 개선 루프 · 코드 정리 · 인프라 · 구독)도 넣었다. '26년 4분기'는 2026년 10~12월이다.")
    return [note, ""] + md_table(head, rows)


def milestone_rows():
    ms = sorted([i for i in ITEMS if i.get("milestone")], key=lambda x: x["start"])
    rows = []
    for i in ms:
        fl = f"{i['float_to_release']}일" if i["id"] != "rel_10" else "—"
        rows.append([i["start"], i["name"], ANCH[i["id"]]["p80"], fl])
    return ["날짜(가운데 추정)", "이정표", "늦어도(80%)", "1.0까지 여유"], rows


def md_milestones():
    return md_table(*milestone_rows())


def forecast_rows():
    def r(src, key):
        x = src[key]
        return x["rel_10"][1], x["rel_10"][2], f"{x['p_target']:.0%}", f"{x['p_2030']:.0%}"
    rows = [
        ["계획에 적은 길이 그대로(넘침 · 위험 없음, 용량 무제한)", *r(MC2, "용량 무제한 · 위험 없음 · 초과 없음(계획 그대로)")],
        ["용량 무제한, 넘침 · 위험은 있음", *r(MC2, "용량 무제한 · 위험과 초과는 그대로")],
        ["기준: 회사마다 계정 하나, 최상위 요금제(주 약 15작업일)", ANCH["rel_10"]["p50"], ANCH["rel_10"]["p80"],
         *r(SC, "계정 하나, 측정을 구독에 맞게 줄임")[2:]],
        ["같은 조건 + 패키지 저장소 · LLVM 배포 빌드를 1.0 뒤로", *r(SC, "계정 하나, 측정 줄임 + 저장소 · LLVM 1.0 뒤로")],
        ["같은 조건, 측정을 헌장 원안 크기로", *r(SC, "계정 하나, 측정 원안")],
        ["Google을 못 써서 두 회사(주 약 10작업일)", *r(SC, "두 회사, 측정을 구독에 맞게 줄임")],
        ["용량이 기준의 두 배(주 약 30작업일)", *r(SC, "용량 두 배, 측정을 구독에 맞게 줄임")],
        ["같은 조건 + 패키지 저장소 · LLVM 배포 빌드를 1.0 뒤로", *r(SC, "용량 두 배, 측정 줄임 + 저장소 · LLVM 1.0 뒤로")],
    ]
    return ["경우", "1.0 가운데 날짜", "80%가 끝나는 날", "2029-09-28까지 끝날 확률", "2030년 안에 끝날 확률"], rows


def md_forecast():
    return md_table(*forecast_rows())


REF = [
    ["Elixir", "3.7년", "1.0까지 기여자 189명, 이미 있던 BEAM 위에 지음"],
    ["Go", "4.5년", "Google, 숙련자 3명이 설계를 시작"],
    ["Mojo", "4.6년(회사 설립부터)", "투자 약 3.8억 달러"],
    ["Rust", "4.9년(첫 커밋부터)", "Mozilla 개발자 10여 명"],
    ["Kotlin", "약 5~6년", "JetBrains 정규 개발자 20명 넘게"],
    ["Gleam", "7.7년", "정규 개발자 1명"],
    ["Crystal", "8.5년", "기여자 450명 넘게"],
    ["Julia", "9.0년", "기여자 700명 넘게"],
    ["Nim", "11.3년", "기여자 500명 넘게"],
    ["Unison", "12.2년", "회사(공익법인)"],
    ["Zig · Elm · Roc · Carbon · Odin · Koka", "아직 1.0 없음(6.5~14.5년째)", "Carbon은 1.0을 '2028년 이후'로 잡았다. MoonBit은 스스로 정한 1.0 목표를 두 번 넘겼다"],
]


def md_refclass():
    return md_table(["언어", "시작 → 1.0", "1.0 무렵 팀 · 자금"], REF)


def load_rows():
    hm = D["owner_hours_month"]
    dem = demand_weeks()
    rows = []
    for y, q in quarters():
        months = [f"{y}-{m:02d}" for m in range(q * 3 - 2, q * 3 + 1)]
        h = sum(hm.get(m, 0) for m in months)
        wk = [v for w, v in dem.items() if w.strftime("%Y-%m") in months]
        rows.append([f"{y}년 {q * 3 - 2}~{q * 3}월", f"{h:.0f}시간", f"{sum(wk) / len(wk):.1f}작업일" if wk else "—"])
    return ["분기", "소유자 시간", "AI 작업 수요(주 평균, 개발 + 측정)"], rows


def md_load():
    return md_table(*load_rows())


def budget_rows():
    rows = []
    for y in map(str, years()):
        c, a = D["year_cost"].get(y, 0), D["year_ai_days"].get(y, 0)
        rows.append([f"{y}년", man(c), f"{a:,.0f}작업일"])
    tc = sum(i.get("cost_usd", 0) for i in ITEMS)
    ta = sum(i.get("ai_days", 0) for i in ITEMS)
    rows.append(["합계", man(tc), f"{ta:,.0f}작업일"])
    big = sorted([i for i in ITEMS if i.get("cost_usd", 0) >= 4000], key=lambda x: -x["cost_usd"])
    bl = []
    for i in big:
        span = i["start"][:7] if i["start"][:7] == i["end"][:7] else f"{i['start'][:7]}~{i['end'][:7]}"
        bl.append(f"{i['name']} ({span}): {man(i['cost_usd'])}")
    return ["해", "직접 비용(구독 · 서버 · 감사)", "AI 작업(개발)"], rows, bl


def md_budget():
    head, rows, bl = budget_rows()
    return md_table(head, rows) + ["돈이 큰 일:", ""] + [f"- {b}" for b in bl] + [""]


def md_wbs():
    return (HERE / "wbs_section.md").read_text(encoding="utf-8").rstrip("\n").split("\n") + [""]


def render_md():
    S = stats()
    out = []
    R = {"gantt": md_gantt, "milestones": md_milestones, "load": md_load, "budget": md_budget,
         "wbs": md_wbs, "forecast": md_forecast, "refclass": md_refclass}
    for b in plan_content.content(S):
        k = b[0]
        if k == "h1":
            out += [f"# {b[1]}", ""]
        elif k == "h2":
            out += [f"## {b[1]}", ""]
        elif k == "h3":
            out += [f"### {b[1]}", ""]
        elif k == "p":
            out += [b[1], ""]
        elif k == "ul":
            out += [f"- {x}" for x in b[1]] + [""]
        elif k == "table":
            out += md_table(b[1], b[2])
        elif k in R:
            out += R[k]()
        else:
            raise ValueError(k)
    text = "\n".join(out).rstrip("\n") + "\n"
    return re.sub(r"\n{3,}", "\n\n", text)


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(render_md(), encoding="utf-8")
    print("wrote", sys.argv[1])
