"""Render the pilot's document in Korean and in English from the two data files.

The words are in the templates next to this script (Korean is the source, English follows it). Every figure
comes from the data: bench/records/pilot-2026-11-17.json (measured values) and tools/plan/pilot_replan.json
(what follows from them, with the assumptions). A template holds no digit of its own; a figure appears only
where a {{name}} stands.

Usage: python3 render.py            (rewrites docs/pilot/2026-11-17-pilot.ko.md and .en.md)
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DATA = ROOT / "bench" / "records" / "pilot-2026-11-17.json"
REPLAN = ROOT / "tools" / "plan" / "pilot_replan.json"
EARLIER = ROOT / "tools" / "plan" / "claude_only.json"  # the single-seed dates of charter version 0.7
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


def month_of(iso: str, lang: str) -> str:
    y, m, _ = (int(x) for x in iso.split("-"))
    return f"{y}년 {m}월" if lang == "ko" else f"{MONTHS[m - 1]} {y}"


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
        "name": NAMES[run["language"]], "status": run["status"], "started": run["batch_started_utc"][11:16],
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


def earlier_facts(earlier: dict, replan: dict, lang: str) -> dict:
    """The single-seed dates an earlier charter gave, and what explains their distance from today's ranges."""
    f = {"v07.seed": str(earlier["seed"]), "v07.sims": num(earlier["sims_per_case"])}
    for name, case in earlier["cases"].items():
        f[f"v07.{name}.p50"] = case["rel_10"]["p50"]
        f[f"v07.{name}.p50_month"] = month_of(case["rel_10"]["p50"], lang)
    found = earlier["comparison"]
    f["v07.n_seeds"] = num(len(found["seeds"]))
    f["v07.more_sims"] = num(found["more_sims"])
    for name in ("claude_alone", "claude_shared"):
        for way, spread in found[name].items():
            for end in ("earliest", "latest"):
                f[f"v07.{name}.{way}.{end}_month"] = month_of(spread[end], lang)
    for key, value in earlier["later_for_each_month"].items():
        if key != "from":
            f[f"v07.later.{key}"] = num(value)
    if found["seeds"][0] != earlier["seed"]:
        raise RenderError("the comparison's first seed is not the seed of the earlier dates")
    f["v07.claude_alone.drawn_first_month"] = month_of(found["claude_alone"]["own_scenario"]["by_seed"][0], lang)
    alone = found["claude_alone"]
    old = earlier["cases"]["claude_alone"]["rel_10"]["p50"]
    now = replan["forecast"]["claude_dedicated"]["rel_10_p50"]
    five = alone["plan_scenario_scaled_first_five_seeds"]
    if (five["earliest"], five["latest"]) != (now["earliest"], now["latest"]):
        raise RenderError("the comparison's first five seeds are not the seeds of the range the charter gives")
    if found["sims"] != earlier["sims_per_case"] or found["sims"] != replan["forecast"]["claude_dedicated"]["sims_per_seed"]:
        raise RenderError("the charter says the earlier dates and the ranges were run the same number of times")
    if now["earliest"] <= old <= now["latest"]:
        raise RenderError("the charter says the earlier date for one Claude account lies outside the range of five seeds")
    if not alone["plan_scenario_scaled"]["earliest"] <= old <= alone["plan_scenario_scaled"]["latest"]:
        raise RenderError("the charter says the earlier date lies inside the range once more seeds are run")
    if alone["own_scenario"]["earliest"] > alone["plan_scenario_scaled"]["latest"] or \
            alone["plan_scenario_scaled"]["earliest"] > alone["own_scenario"]["latest"]:
        raise RenderError("the charter says the way the capacity is set does not explain the difference")
    return f


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
        f[name + "_md"] = "/".join(str(int(x)) for x in iso.split("-")[1:])

    versions = {r["claude_code"]["observed"] for r in runs} | {r["claude_code"]["pinned"] for r in runs}
    models = {r["model"] for r in runs}
    asked = {b["model_asked_for"] for b in data["batches"]}
    if len(versions) != 1 or len(models) != 1 or asked != {"agent default"}:
        raise RenderError("the document is written for one version and one model that was not chosen")
    put_date("date", data["date_utc"])
    put_date("night_start", str(dt.date.fromisoformat(data["date_utc"]) + dt.timedelta(days=1)))
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
            if value is None:
                raise RenderError(f"the data does not say {group}.{key}, which the document states")
            f[f"{group}.{key}"] = one(value) if key.startswith(("guard_minutes", "hours_", "reading_interval")) \
                else num(value)
    for run in runs:
        f[f"run.{run['language']}.asum"] = one(sum(st["agent_seconds"] for st in run["steps"]))
        f[f"run.{run['language']}.wall"] = one(run["wall_seconds"])
        put(f"run.{run['language']}.turns", run["turns"])
    today = used["day"]
    for key, value in today.items():
        if not isinstance(value, list):
            put(f"day.{key}", value)
    if [(e["tokens"] > 0, e["steps_that_ended_with_an_error"] > 0, e["process_table_watched"], e["language"])
            for e in today["not_counted_starts"]] != [(False, True, False, "python"), (True, False, False, "python")]:
        raise RenderError("the document tells of two unwatched Python starts: one where the agent never worked, "
                          "then one complete run")
    for n, early in enumerate(today["not_counted_starts"], start=1):
        for key, value in early.items():
            if key == "rewards":
                f[f"e{n}.r1"], f[f"e{n}.r2"] = one(value[0]), one(value[1])
            elif isinstance(value, bool) or value is None:
                continue
            elif key.endswith("_seconds") or key.endswith("_seconds_counted"):
                f[f"e{n}.{key}"] = one(value)
            else:
                put(f"e{n}.{key}", value)
    put("e2.weekly_percent_at_its_end", today["not_counted_starts"][1]["weekly_percent_at_its_end"])
    if today["not_counted_starts"][1]["weekly_percent_at_its_end"] != used["weekly"]["first_batch_before"]:
        raise RenderError("the document says the figure stored before the counted Python run is the reading "
                          "of the uncounted run before it")
    if used["safety"]["watched_runs"] != len(runs) or used["safety"]["starts_without_a_watch"] != today["not_counted"]:
        raise RenderError("the document says the watch covered the counted runs and none of the others")

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
    for which, short in (("same_model", "same"), ("largest_model", "large")):
        for key, value in cap["next_measurements"][which].items():
            if isinstance(value, bool):
                continue
            if value is None:
                raise RenderError(f"the estimates do not say {which}.{key}, which the document states")
            f[f"next.{short}.{key}"] = f"{value:.3f}" if key == "one_run_below_points_if_unmoved" and which == \
                "same_model" else num(value)
    if not cap["next_measurements"]["same_model"]["unmoved_shows_the_baseline_fits_that_room"]:
        raise RenderError("the document says an unmoved figure after the first night shows the baseline fits the room")

    put("sims", sched["sims_per_seed"])
    put("n_seeds", len(sched["seeds"]))
    put("runs_per_case", sched["runs_per_case"])
    put("tasks_rolled", sched["tasks_rolled"])
    put("tasks_left_out", sched["tasks_added_later_and_left_out"])
    put("pct80", sched["upper_percent"])
    put_date("pilot_row_start", sched["pilot_row"]["start"])
    put_date("pilot_row_end", sched["pilot_row"]["end"])
    put("pilot_days_early", sched["pilot_row"]["days_before_its_start"])
    put("pilot_days_before_end", sched["pilot_row"]["days_before_its_end"])
    put_date("gate", sched["gate_day"])
    put("plan_working_days", sched["baseline_working_days_in_the_plan"])
    for key, value in sched["noise"].items():
        put(f"noise.{key}", value)
    for name, days in sched["working_days_a_week"].items():
        put(f"{name}.days_a_week", days)
    for name, cases in sched["capacities"].items():
        for case, result in cases.items():
            where = f"{name}.{case}"
            for key, value in result.items():
                if key == "seeds":
                    continue
                if isinstance(value, dict):
                    for q in ("p50", "p80"):
                        put_date(f"{where}.{key}.{q}", value[q])
                else:
                    f[f"{where}.{key}"] = percent(value)
                    shares = [s[key] for s in result["seeds"]]
                    f[f"{where}.{key}.lo"], f[f"{where}.{key}.hi"] = percent(min(shares)), percent(max(shares))
    plan = sched["capacities"]["planning"]
    if not plan["after_x1"]["b_baseline"]["p50"] <= cap["row"]["end"] <= plan["after_x2"]["b_baseline"]["p50"]:
        raise RenderError("the verdict says the row's end lies between the measured case and twice the working time")
    chance = lambda case: plan[case]["p_baseline_by_row_end"]
    if not (abs(chance("finished_only") - chance("before")) <= 0.03 < chance("numbers_only") - chance("before")):
        raise RenderError("the document says the rise in the chance comes from the pilot's numbers, not from "
                          "counting finished work as finished")
    for name in ("claude_dedicated", "claude_shared"):
        if sched["capacities"][name]["after_x1"]["p_baseline_by_row_end"] > 0.1:
            raise RenderError("the document says the chance under today's capacity is small")
    if used["weekly"]["moved_between_the_two_readings_points"] != 0 or used["five_hour"]["runs_that_reported_it"] != 0:
        raise RenderError("the document says the weekly figure did not move and the other limit was not reported")
    if used["safety"]["readings_with_the_token"] or used["safety"]["leak_hits"] or used["safety"]["containers_left"]:
        raise RenderError("the document says no token was seen, none was left and no container was left")

    fc = replan["forecast"]
    put("fc.n_seeds", len(fc["seeds"]))
    f["fc.plan_seed"] = str(fc["plan_seed"])
    for name in ("planning", "planning_with_every_task", "two_companies", "claude_dedicated", "claude_shared"):
        case = fc[name]
        put(f"fc.{name}.sims", case["sims_per_seed"])
        put(f"fc.{name}.tasks", case["tasks"])
        put(f"fc.{name}.days_a_week", case["working_days_a_week"])
        for key in ("g_dir_p50", "g_dir_p80", "rel_10_p50", "rel_10_p80"):
            found = case[key]
            for end in ("earliest", "latest"):
                put_date(f"fc.{name}.{key}.{end}", found[end])
                f[f"fc.{name}.{key}.{end}_month"] = month_of(found[end], lang)
            put(f"fc.{name}.{key}.weeks_apart", found["weeks_apart"])
            put(f"fc.{name}.{key}.days_apart", found["days_apart"])
    put_date("fc.plan.rel_10_p50", fc["planning"]["seeds"][0]["rel_10"]["p50"])
    put_date("fc.plan.rel_10_p80", fc["planning"]["seeds"][0]["rel_10"]["p80"])
    put_date("fc.plan.g_dir_p50", fc["planning"]["seeds"][0]["g_dir"]["p50"])
    f["fc.plan.rel_10_p50_month"] = month_of(fc["planning"]["seeds"][0]["rel_10"]["p50"], lang)
    f["fc.last_month"] = month_of(fc["claude_shared"]["last_day_of_the_simulation"], lang)
    for name in ("claude_dedicated", "claude_shared"):
        if fc[name]["rel_10_p80"]["seeds_at_the_last_day"] != len(fc["seeds"]):
            raise RenderError("the document says the 80 percent date under today's capacity is later than the "
                              "simulation's last day")
    every, made = fc["planning_with_every_task"], fc["planning"]
    for key in ("rel_10_p50", "rel_10_p80"):
        if every[key]["earliest"] > made[key]["latest"] or made[key]["earliest"] > every[key]["latest"]:
            raise RenderError("the document says the added task moves the dates no further than a change of seed")
    return f


def render(lang: str, data: dict | None = None, replan: dict | None = None, text: str | None = None,
           earlier: dict | None = None) -> str:
    data = json.loads(DATA.read_text(encoding="utf-8")) if data is None else data
    replan = json.loads(REPLAN.read_text(encoding="utf-8")) if replan is None else replan
    text = template(lang).read_text(encoding="utf-8") if text is None else text
    known = facts(data, replan, lang)
    if "{{v07." in text:  # only the lines that set an earlier charter's dates beside today's need them
        earlier = json.loads(EARLIER.read_text(encoding="utf-8")) if earlier is None else earlier
        known.update(earlier_facts(earlier, replan, lang))

    def fill(line: str, values: dict) -> str:
        def value(m):
            if m.group(1) not in values:
                raise RenderError(f"the {lang} template asks for {m.group(1)}, which the data does not give")
            return values[m.group(1)]
        return PLACE.sub(value, line)

    lines = text.splitlines()
    # The schedule table is written once, for the plan's capacity; "@table <capacity>" repeats it for another.
    first = next((n for n, line in enumerate(lines) if "{{planning.before.b_baseline.p50}}" in line), None)
    table = []
    if first is not None:
        last = first
        while last + 1 < len(lines) and lines[last + 1].startswith("|"):
            last += 1
        table = lines[first - 2:last + 1]
    out = []
    for line in lines:
        if line.startswith("@runs "):
            out.extend(fill(line[len("@runs "):], run_facts(run)) for run in data["runs"])
        elif line.startswith("@table "):
            name = line[len("@table "):].strip()
            if not table or name not in replan["schedule"]["capacities"]:
                raise RenderError(f"the {lang} template asks for a table the data does not give: {name}")
            out.extend(fill(row.replace("{{planning.", "{{" + name + "."), known) for row in table)
        else:
            out.append(fill(line, known))
    return "\n".join(out) + "\n"


# Templates whose rendered lines must stand word for word, each as a whole line, in a document kept by hand.
QUOTES = {
    "charter": {"ko": "docs/charter.ko.md", "en": "docs/charter.en.md"},
    "readme": {"ko": "README.ko.md", "en": "README.md"},
    "owner_list": {"ko": "TODO.md"},
}
# Names that carry a digit and are not figures: a version of the charter, the release, the name of a task.
NAMES_WITH_DIGITS = re.compile(r"(?<![A-Za-z\d.])v\d\.\d+(?![\d.])|(?<![\d.,])1\.0(?![\d.])|11월 일정")


def quote_template(name: str, lang: str) -> Path:
    return HERE / f"{name}.{lang}.tmpl"


def quoted(name: str, lang: str, data: dict | None = None, replan: dict | None = None) -> list[str]:
    """The lines a hand-kept document must carry, rendered from the data."""
    text = quote_template(name, lang).read_text(encoding="utf-8")
    return [line for line in render(lang, data, replan, text).splitlines() if line.strip()]


def missing_quotes(name: str, lang: str, data: dict | None = None, replan: dict | None = None) -> list[str]:
    """Rendered lines that the hand-kept document does not carry as they are."""
    have = set((ROOT / QUOTES[name][lang]).read_text(encoding="utf-8").splitlines())
    return [line for line in quoted(name, lang, data, replan) if line not in have]


def own_digits(lang: str, path: Path | None = None) -> list[str]:
    """Lines of a template that carry a digit outside a {{name}}: figures the data would not govern."""
    lines = (path or template(lang)).read_text(encoding="utf-8").splitlines()
    return [line for line in lines if re.search(r"\d", NAMES_WITH_DIGITS.sub("", PLACE.sub("", line)))]


if __name__ == "__main__":
    for lang in LANGS:
        document(lang).parent.mkdir(parents=True, exist_ok=True)
        document(lang).write_text(render(lang), encoding="utf-8")
        print(f"wrote {document(lang).relative_to(ROOT)}")
