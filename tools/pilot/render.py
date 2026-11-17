"""Render the pilot's document in Korean and in English from the two data files.

The words are in the templates next to this script (Korean is the source, English follows it). Every figure
comes from the data: bench/records/pilot-2026-11-17.json (measured values) and tools/plan/pilot_replan.json
(what follows from them, with the assumptions). A template holds no digit of its own; a figure appears only
where a {{name}} stands.

Usage: python3 render.py            (rewrites docs/pilot/2026-11-17-pilot.ko.md and .en.md)
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DATA = ROOT / "bench" / "records" / "pilot-2026-11-17.json"
REPLAN = ROOT / "tools" / "plan" / "pilot_replan.json"
LANGS = ("ko", "en")
NAMES = {"python": "Python", "typescript": "TypeScript", "java": "Java", "csharp": "C#", "go": "Go",
         "rust": "Rust", "cpp": "C++"}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")
PLACE = re.compile(r"\{\{([A-Za-z0-9_.]+)\}\}")


class RenderError(Exception):
    pass


def template(lang: str) -> Path:
    return HERE / f"pilot.{lang}.md.tmpl"


def document(lang: str) -> Path:
    return ROOT / "docs" / "pilot" / f"2026-11-17-pilot.{lang}.md"


def num(value) -> str:
    """A number as the document shows it: whole numbers without a point and with thousands separators."""
    if isinstance(value, bool):
        raise RenderError("a yes or no is not a figure")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}"


def one(value) -> str:
    return f"{value:.1f}"


def long_date(iso: str, lang: str) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{y}년 {m}월 {d}일" if lang == "ko" else f"{d} {MONTHS[m - 1]} {y}"


def short_date(iso: str, lang: str) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{m}월 {d}일" if lang == "ko" else f"{d} {MONTHS[m - 1]}"


def percent(share: float) -> str:
    return num(round(share * 100))


def passed(judgement: dict, kind: str) -> int:
    return round(judgement[f"{kind}_pass_rate"] * judgement[f"{kind}_judged"])


def run_facts(run: dict) -> dict:
    steps = run["steps"]
    if len(steps) != 2:
        raise RenderError("the document is written for runs of two steps")
    watch, weekly = run["process_watch"], run["weekly"]
    return {
        "name": NAMES[run["language"]], "status": run["status"],
        "r1": one(steps[0]["reward"]), "r2": one(steps[1]["reward"]),
        "wall": one(run["wall_seconds"]),
        "a1": one(steps[0]["agent_seconds"]), "a2": one(steps[1]["agent_seconds"]),
        "asum": one(steps[0]["agent_seconds"] + steps[1]["agent_seconds"]),
        "j1": one(steps[0]["judge_seconds"]), "j2": one(steps[1]["judge_seconds"]),
        "turns": num(run["turns"]),
        "tin": num(run["tokens"]["input"]), "tcr": num(run["tokens"]["cache_read"]),
        "tcw": num(run["tokens"]["cache_creation"]), "tout": num(run["tokens"]["output"]),
        "wb": num(weekly["before"]["percent"]), "wf": num(weekly["first"]["percent"]),
        "wa": num(weekly["after"]["percent"]),
        "gb": one(run["guard"]["minutes_before"]), "ga": one(run["guard"]["minutes_after"]),
        "readings": num(watch["readings"]), "areadings": num(watch["readings_while_the_agent_worked"]),
        "treadings": num(watch["readings_with_the_token"]),
        "leak_hits": num(run["leak_scan"]["hits"]), "leak_unread": num(run["leak_scan"]["unchecked"]),
        "containers": num(run["containers_left"]),
    }


def facts(data: dict, replan: dict, lang: str) -> dict:
    runs = data["runs"]
    used, cap, sched = replan["usage"], replan["capacity"], replan["schedule"]
    f = {}

    def put(name, value):
        f[name] = value if isinstance(value, str) else num(value)

    def put_date(name, iso):
        f[name] = iso
        f[name + "_long"] = long_date(iso, lang)
        f[name + "_short"] = short_date(iso, lang)

    versions = {r["claude_code"]["observed"] for r in runs} | {r["claude_code"]["pinned"] for r in runs}
    models = {r["model"] for r in runs}
    asked = {b["model_asked_for"] for b in data["batches"]}
    if len(versions) != 1 or len(models) != 1 or asked != {"agent default"}:
        raise RenderError("the document is written for one version and one model that was not chosen")
    put_date("date", data["date_utc"])
    put("cli_version", versions.pop())
    put("model", models.pop())
    put("task", data["task"])
    put("n_runs", len(runs))
    put("n_batches", len(data["batches"]))
    put("n_ok", sum(1 for r in runs if r["status"] == "ok" and r["harbor_returncode"] == 0 and not r["run_error"]))
    full = [r for r in runs if all(s["reward"] == 1.0 for s in r["steps"])]
    rest = [r for r in runs if r not in full]
    if [r["language"] for r in rest] != ["cpp"]:
        raise RenderError("the document explains one run that did not score in full, the C++ one")
    put("n_full", len(full))
    put("n_steps", len(runs[0]["steps"]))
    put("full_reward", one(1.0))
    put("no_reward", one(rest[0]["reward"]))
    put("time_limit_hours", runs[0]["time_limit_seconds"] / 3600)
    put("other_failed_checks", sum(len(r["failed_checks"]) for r in full))

    cpp = rest[0]
    for n, step in enumerate(cpp["steps"], start=1):
        j = step["judgement"]
        for kind, short in (("functional", "f"), ("boundary", "b"), ("security", "s")):
            put(f"cpp_{short}{n}", j[f"{kind}_judged"])
            put(f"cpp_{short}{n}_pass", passed(j, kind))
        put(f"cpp_vulns{n}", j["vulns_found"])
        put(f"cpp_broken{n}", j["regression_broken"])
        put(f"cpp_failed{n}", sum(1 for c in cpp["failed_checks"] if c["judged_at"] == step["step"]))
    said = {c["judge_said"] for c in cpp["failed_checks"]}
    kinds = {(c["kind"], c["weakness"], c["from_step"]) for c in cpp["failed_checks"]}
    if len(said) != 1 or kinds != {("security", "file-exposure", cpp["steps"][0]["step"])}:
        raise RenderError("the document explains one failed security check, about the program's own files")
    put("cpp_request", re.search(r"[A-Z]+ /[A-Za-z0-9._-]+", said.pop()).group(0))

    for key in ("agent_seconds_mean", "agent_seconds_min", "agent_seconds_max", "agent_minutes_total",
                "judge_seconds_mean", "wall_seconds_mean", "wall_seconds_min", "wall_seconds_max",
                "wall_minutes_total"):
        f[key] = one(used[key])
    put("turns_total", used["turns_total"])
    for key, value in used["tokens_total"].items():
        put(f"tokens_{key}", value)
    f["cost_total"] = f"{used['cost_usd_as_the_cli_counts_it_total']:.2f}"
    for group in ("weekly", "five_hour", "safety"):
        for key, value in used[group].items():
            f[f"{group}.{key}"] = one(value) if key.startswith(("guard_minutes", "hours_", "reading_interval")) \
                else num(value)

    for key, value in cap["assumptions"].items():
        put(f"plan.{key}", value)
    put_date("row_start", cap["row"]["start"])
    put_date("row_end", cap["row"]["end"])
    month = int(cap["row"]["start"].split("-")[1])
    f["row_month"] = f"{month}월" if lang == "ko" else MONTHS[month - 1]
    put("row_nights", cap["row"]["nights"])
    put("row_runs_per_night", cap["row"]["runs_per_night_needed"])
    for case in cap["cases"]:
        for key, value in case.items():
            if key != "fits_the_row":
                f[f"x{case['factor']}.{key}"] = num(value)
    fits = [c["factor"] for c in cap["cases"] if c["fits_the_row"]]
    if fits != [1]:
        raise RenderError("the document says that only the measured case fits the row")
    f["largest_factor"] = f"{cap['largest_factor_that_fits_the_row']:.2f}"
    f["largest_minutes"] = f"{cap['agent_minutes_per_run_at_that_factor']:.2f}"
    for key, value in cap["next_measurement"].items():
        put(f"next.{key}", value)

    put("sims", sched["sims"])
    put("pct80", sched["upper_percent"])
    put_date("pilot_row_start", sched["pilot_row"]["start"])
    put_date("pilot_row_end", sched["pilot_row"]["end"])
    put("pilot_days_early", sched["pilot_row"]["days_before_its_start"])
    put("pilot_days_before_end", sched["pilot_row"]["days_before_its_end"])
    put_date("gate", sched["gate_day"])
    put("plan_working_days", sched["baseline_working_days_in_the_plan"])
    for name, days in sched["working_days_a_week"].items():
        put(f"{name}.days_a_week", days)
    for name, cases in sched["capacities"].items():
        for case, result in cases.items():
            for key, value in result.items():
                where = f"{name}.{case}.{key}"
                if isinstance(value, dict):
                    f[where + ".p50"], f[where + ".p80"] = value["p50"], value["p80"]
                else:
                    f[where] = percent(value)
    plan = sched["capacities"]["planning"]
    if not plan["after_x1"]["b_baseline"]["p50"] <= cap["row"]["end"] <= plan["after_x2"]["b_baseline"]["p50"]:
        raise RenderError("the verdict says the row's end lies between the measured case and twice the working time")
    alone = sched["capacities"]["claude_only"]
    if not alone["after_x1"]["b_baseline"]["p50"] < alone["before"]["b_baseline"]["p50"]:
        raise RenderError("the verdict says the pilot moves the middle date earlier for one Claude account")
    if used["weekly"]["moved_during_later_runs_points"] != 0 or used["five_hour"]["runs_that_reported_it"] != 0:
        raise RenderError("the document says the weekly figure did not move and the other limit was not reported")
    if used["safety"]["readings_with_the_token"] or used["safety"]["leak_hits"] or used["safety"]["containers_left"]:
        raise RenderError("the document says no token was seen, none was left and no container was left")
    return f


def render(lang: str, data: dict | None = None, replan: dict | None = None, text: str | None = None) -> str:
    data = json.loads(DATA.read_text(encoding="utf-8")) if data is None else data
    replan = json.loads(REPLAN.read_text(encoding="utf-8")) if replan is None else replan
    text = template(lang).read_text(encoding="utf-8") if text is None else text
    known = facts(data, replan, lang)

    def fill(line: str, values: dict) -> str:
        def value(m):
            if m.group(1) not in values:
                raise RenderError(f"the {lang} template asks for {m.group(1)}, which the data does not give")
            return values[m.group(1)]
        return PLACE.sub(value, line)

    out = []
    for line in text.splitlines():
        if line.startswith("@runs "):
            out.extend(fill(line[len("@runs "):], run_facts(run)) for run in data["runs"])
        else:
            out.append(fill(line, known))
    return "\n".join(out) + "\n"


def own_digits(lang: str) -> list[str]:
    """Lines of a template that carry a digit outside a {{name}}: figures the data would not govern."""
    lines = template(lang).read_text(encoding="utf-8").splitlines()
    return [line for line in lines if re.search(r"\d", PLACE.sub("", line))]


if __name__ == "__main__":
    for lang in LANGS:
        document(lang).parent.mkdir(parents=True, exist_ok=True)
        document(lang).write_text(render(lang), encoding="utf-8")
        print(f"wrote {document(lang).relative_to(ROOT)}")
