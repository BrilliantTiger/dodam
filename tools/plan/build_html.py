"""Render the plan to one HTML page (artifact body, no doctype) from plan_content.py and wbs.json.

Usage: python3 build_html.py OUT.html
"""
import html
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import build_doc as B  # noqa: E402
import plan_content  # noqa: E402

D, ITEMS, GROUPS = B.D, B.ITEMS, B.GROUPS


def inline(t):
    t = html.escape(t, quote=False)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)


def table(head, rows, cls=""):
    th = "".join(f"<th scope=\"col\">{inline(h)}</th>" for h in head)
    body = []
    for r in rows:
        tds = "".join(f"<td data-label=\"{html.escape(head[k])}\">{inline(str(c))}</td>" for k, c in enumerate(r))
        body.append(f"<tr>{tds}</tr>")
    return f"<div class=\"tbl {cls}\"><table><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def milestones():
    head, rows = B.milestone_rows()
    return table(head, rows, "narrow")


def forecast():
    head, rows = B.forecast_rows()
    return table(head, rows)


def refclass():
    return table(["언어", "시작 → 1.0", "1.0 무렵 팀 · 자금"], B.REF)


def load_tables():
    lines = B.md_load()
    head = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = [[c.strip() for c in l.strip("|").split("|")] for l in lines[2:] if l.startswith("|")]
    return table(head, rows, "narrow")


def budget():
    lines = B.md_budget()
    tl = [l for l in lines if l.startswith("|")]
    head = [c.strip() for c in tl[0].strip("|").split("|")]
    rows = [[c.strip() for c in l.strip("|").split("|")] for l in tl[2:]]
    big = [l[2:] for l in lines if l.startswith("- ")]
    return table(head, rows) + "<p class=\"sub\">직접 비용이 큰 일</p><ul class=\"tight\">" + "".join(f"<li>{inline(b)}</li>" for b in big) + "</ul>"


def wbs_list():
    out = ["<div class=\"chips\" role=\"group\" aria-label=\"시기로 거르기\">"]
    out.append("<button type=\"button\" class=\"chip on\" data-period=\"all\" aria-pressed=\"true\">전체</button>")
    for p in D["periods"]:
        out.append(f"<button type=\"button\" class=\"chip\" data-period=\"{html.escape(p['start'])}|{html.escape(p['end'])}\" aria-pressed=\"false\">{html.escape(p['name'])}</button>")
    out.append("<button type=\"button\" class=\"chip\" id=\"only-crit\" aria-pressed=\"false\">주경로만</button></div>")
    for g in GROUPS:
        rows = sorted([i for i in ITEMS if i["group"] == g["id"]], key=lambda x: (x["start"], x["end"]))
        money = ""
        if g["cost_usd"]:
            money = " · 직접 비용 " + B.man(g["cost_usd"])
        out.append(f"<section class=\"wg\" aria-labelledby=\"wg-{g['id']}\"><h3 id=\"wg-{g['id']}\">{inline(g['name'])}</h3>")
        out.append(f"<p class=\"wg-desc\">{inline(g['desc'])} <span class=\"wg-sum\">사람 {g['human_h']:.0f}시간 · AI {g['ai_days']:.0f}작업일{money}</span></p><ol class=\"items\">")
        for it in rows:
            s, e = it["start"], it["end"]
            rng = s if s == e else (f"{s}~{e[5:]}" if s[:4] == e[:4] else f"{s}~{e}")
            tags = []
            if it.get("milestone"):
                tags.append("<span class=\"tag ms\">이정표</span>")
            if it.get("critical"):
                tags.append("<span class=\"tag crit\">주경로</span>")
            if it.get("ongoing"):
                tags.append("<span class=\"tag on\">상시</span>")
            eff = []
            if it.get("human_h"):
                eff.append(f"사람 {it['human_h']:g}h")
            if it.get("ai_days"):
                eff.append(f"AI {it['ai_days']:g}작업일")
            if it.get("cost_usd"):
                eff.append(B.man(it["cost_usd"]))
            after = ", ".join(it.get("after_names", [])) or "—"
            out.append(
                f"<li class=\"it\" data-s=\"{s}\" data-e=\"{e}\" data-crit=\"{1 if it.get('critical') else 0}\">"
                f"<div class=\"it-top\"><span class=\"it-date\">{rng}</span><span class=\"it-name\">{inline(it['name'])}</span>{''.join(tags)}</div>"
                f"<dl class=\"it-kv\"><div><dt>산출물</dt><dd>{inline(it['out'])}</dd></div>"
                f"<div><dt>끝났다는 표시</dt><dd>{inline(it['done'])}</dd></div>"
                f"<div><dt>앞선 일</dt><dd>{inline(after)}</dd></div>"
                f"<div><dt>맡는 쪽 · 공수</dt><dd>{inline(it['who'])} · {' · '.join(eff) or '—'}</dd></div></dl></li>")
        out.append("</ol></section>")
    out.append("<p class=\"empty\" id=\"wbs-empty\" hidden>이 조건에 맞는 일이 없습니다.</p>")
    return "".join(out)


def gantt():
    return ("<figure class=\"gantt\" aria-labelledby=\"gantt-cap\">"
            "<figcaption id=\"gantt-cap\">갈래별 일정. 막대를 누르면 그 일의 산출물과 끝났다는 표시가 아래에 나옵니다. 진한 파란 막대는 주경로, 점선 막대는 상시 일, 세로 점선은 관문, 세로 실선은 오늘입니다. 휴대폰에서는 차트를 옆으로 밀어 봅니다.</figcaption>"
            "<div class=\"g-wrap\"><div class=\"g-labels\" id=\"g-labels\" aria-hidden=\"true\"></div>"
            "<div class=\"g-scroll\" id=\"g-scroll\"><svg id=\"g-svg\" role=\"img\" aria-label=\"2026년 10월부터 2029년 10월까지 갈래별 작업 막대와 관문\"></svg></div></div>"
            "<div class=\"g-detail\" id=\"g-detail\" aria-live=\"polite\"><p class=\"g-hint\">막대를 누르거나 Tab으로 옮겨 Enter를 누르면 여기에 자세한 내용이 나옵니다.</p></div>"
            "</figure>")


def load_charts():
    return ("<div class=\"charts\">"
            "<figure class=\"bars\"><figcaption>소유자 시간(달마다, 시간)</figcaption><svg id=\"c-hours\" role=\"img\" aria-label=\"달마다 소유자 시간 막대 그래프\"></svg></figure>"
            "<figure class=\"bars\"><figcaption>AI 작업 수요(개발 + 측정, 주 평균 작업일)</figcaption><svg id=\"c-agents\" role=\"img\" aria-label=\"달마다 AI 작업 수요 막대 그래프. 점선은 구독 용량 가정 주 15작업일(기준)과 10작업일(두 회사)\"></svg></figure>"
            "</div><div class=\"tip\" id=\"tip\" role=\"status\" hidden></div>")


def demand_months():
    from collections import defaultdict
    m = defaultdict(list)
    for w, v in B.demand_weeks().items():
        m[w.strftime("%Y-%m")].append(v)
    return {k: round(sum(v) / len(v), 2) for k, v in sorted(m.items())}


def body_html():
    S = B.stats()
    parts = []
    for b in plan_content.content(S):
        k = b[0]
        if k == "h1":
            parts.append(None)  # header is drawn separately
        elif k == "h2":
            parts.append(f"</section><section class=\"sec\"><h2>{inline(b[1])}</h2>")
        elif k == "h3":
            parts.append(f"<h3>{inline(b[1])}</h3>")
        elif k == "p":
            parts.append(f"<p>{inline(b[1])}</p>")
        elif k == "ul":
            parts.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in b[1]) + "</ul>")
        elif k == "table":
            parts.append(table(b[1], b[2]))
        elif k == "gantt":
            parts.append(gantt())
        elif k == "milestones":
            parts.append(milestones())
        elif k == "forecast":
            parts.append(forecast())
        elif k == "refclass":
            parts.append(refclass())
        elif k == "load":
            parts.append(load_charts() + load_tables())
        elif k == "budget":
            parts.append(budget())
        elif k == "wbs":
            parts.append(wbs_list())
    meta_list = plan_content.content(S)[1][1]
    parts = [p for p in parts if p is not None]
    # the first block after h1 is the meta list; drop it from the flow (shown in the header)
    first_ul = next(n for n, p in enumerate(parts) if p.startswith("<ul>"))
    parts.pop(first_ul)
    body = "".join(parts)
    body = body.replace("</section>", "", 1) + "</section>"
    return S, meta_list, body


def page():
    S, meta, body = body_html()
    data = {
        "start": "2026-10-01", "end": "2031-03-31", "today": "2026-10-29",
        "periods": D["periods"],
        "groups": [{"id": g["id"], "name": g["name"]} for g in GROUPS],
        "items": [{k: it.get(k) for k in ("id", "group", "name", "start", "end", "milestone", "ongoing", "critical", "out", "done", "who", "human_h", "ai_days", "cost_usd", "after_names", "slack")} for it in ITEMS],
        "hours": D["owner_hours_month"], "demand": demand_months(),
    }
    gates = [("g_dir", "방향"), ("d_mm_decide", "메모리 모델"), ("g_min", "최소 언어"), ("rel_01", "0.1"), ("rel_03", "0.3"), ("g_10", "1.0 판정"), ("rel_10", "1.0")]
    data["gates"] = [{"id": i, "label": l, "date": next(x["start"] for x in ITEMS if x["id"] == i)} for i, l in gates]
    tiles = [
        ("1.0 배포(가운데 추정)", S["rel_p50"], f"80%는 {S['rel_p80']} 안"),
        ("작업", f"{S['n_items']}개", f"{S['n_groups']}갈래 · 이정표 {sum(1 for i in ITEMS if i.get('milestone'))}개"),
        ("소유자 시간", f"약 {S['human_total']:,.0f}시간", f"주 평균 약 {S['human_week_avg']:.1f}시간"),
        ("AI 작업", f"약 {S['work_total']:,.0f}작업일", "개발 + 측정, 구독으로만"),
        ("돈", S["direct_k"].replace("약 ", ""), "구독 · 서버 · 감사 두 번"),
    ]
    tiles_html = "".join(f"<div class=\"tile\"><p class=\"t-k\">{html.escape(a)}</p><p class=\"t-v\">{html.escape(b)}</p><p class=\"t-n\">{html.escape(c)}</p></div>" for a, b, c in tiles)
    meta_html = "".join(f"<li>{inline(m)}</li>" for m in meta)
    css = (HERE / "page.css").read_text(encoding="utf-8")
    js = (HERE / "page.js").read_text(encoding="utf-8")
    return f"""<title>도담 1.0 계획</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Black+Han+Sans&family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans+KR:wght@400;600;700&display=swap">
<style>
{css}
</style>
<main class="wrap">
<header class="head">
<p class="eyebrow">DODAM · 1.0까지</p>
<h1>도담 1.0 계획</h1>
<ul class="meta">{meta_html}</ul>
<div class="tiles">{tiles_html}</div>
</header>
{body}
</main>
<script>
const PLAN = {json.dumps(data, ensure_ascii=False)};
{js}
</script>
"""


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(page(), encoding="utf-8")
    print("wrote", sys.argv[1])
