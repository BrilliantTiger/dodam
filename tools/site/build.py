#!/usr/bin/env python3
"""Builds the introduction page in four languages from one template.

    python3 tools/site/build.py              write site/index.html, site/<lang>/index.html, site/sitemap.xml
    python3 tools/site/build.py --check      fail if the strings disagree or those files are stale
    python3 tools/site/build.py --live DIR   build what is published: the pages with the section about
                                             now filled in, status.json and llms.txt (see below)

The structure, every link and every element live once, in template.html. Each language has one
strings file. Dates and large numbers are written in the strings as {d:2027-02-24}, {m:2041-01}
and {n:2000} and are spelled here in each language's usual form, so a date cannot differ between
languages. problems() reports any string that a language lacks or adds, and any string whose
numbers, dates or markup differ from the English one, and any count written as a word instead of
in digits. The address of the page is set here and nowhere else. Standard library only.

The section about now holds no value of its own. status() computes every value in it: the charter's
version, the step and its gate from the charter's text, the benchmark tasks from bench/tasks, the
changes merged to main from the git history, the open issues from GitHub's public information. The
committed pages carry that section empty and hidden; the published build (--live) fills it in and
writes the same values to status.json, which an open page reads again from its own address, and to
llms.txt, the guide for AI agents. Whatever comes from outside the template is escaped.

    --live DIR [--issues N] [--status FILE] [--compare]
        --issues N     use N instead of asking GitHub for the number of open issues
        --status FILE  use this status instead of computing one (the layout check does)
        --compare      print "changed=false" when the published status.json shows the same digest
        --analytics ID build with this measurement ID instead of the setting ANALYTICS_ID (the layout check does)
        --open WAYS    build as if these ways to the desk were open, github or github,live, with made-up
                       dates and times (the layout check does; what is published is never built this way)

Talking with the project's AI in real time is on the page in every state. The facts of the desk (which
way is open and since when, its limits, the measured time to its first answer, whether the version of
AI coworker it needs is released) live in tools/desk/desk.toml and nowhere else. The page's section,
the question about it, the part of the guide and the part of the status file are built from that file.
A closed way says first that it is not open yet and then how it will work, in sentences that are true
while it is closed; it shows no opening date, no measured time and no link to reach the desk.
"""

import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "site"
CHARTER = ROOT / "docs" / "charter.ko.md"
TASKS = ROOT / "bench" / "tasks"
DESK = ROOT / "tools" / "desk" / "desk.toml"      # the public facts of the desk, and the only place they are written
FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"
WAYS = ["github", "live"]                          # on GitHub; session to session over AI coworker
SITE_URL = "https://brillianttigercorp.com/dodam/"
REPO_URL = "https://github.com/BrilliantTiger/dodam"
# The measurement ID of Google Analytics: this one setting, the owner's property. Google's tag is loaded
# by this ID, never through a container: a container can be given other tags later, and the page promises
# measurement without advertising. With the setting empty the built pages hold no analytics code and
# name no other host.
ANALYTICS_ID = "G-DHJKG68707"
MEASURED = re.compile(r"<!--measured-->\n(.*?)<!--/measured-->\n", re.S)   # parts of the template that exist only with an ID
# Parts of the template that exist only in some states of the desk: a way open or closed, the version of
# AI coworker released or not while the live way is closed, any way open or none, some way still closed.
STATE_PARTS = ["github", "github_closed", "live", "live_closed", "released", "unreleased", "any", "none", "waiting"]
OPEN_ONLY = {name: re.compile(rf"<!--{name}-->\n(.*?)<!--/{name}-->\n", re.S) for name in STATE_PARTS}
# What Google's measurement needs, from Google's guide to content security policies for the Google tag.
GOOGLE_NEEDS = {
    "script-src": ["https://www.googletagmanager.com"],
    "img-src": ["https://www.googletagmanager.com", "https://*.google-analytics.com"],
    "connect-src": ["https://www.googletagmanager.com", "https://*.google-analytics.com", "https://*.google.com"],
}
REPO = REPO_URL.split("github.com/", 1)[1]
WELCOME = "ai-welcome"   # the label of issues open to outside contributors
STATUS_VERSION = 2
CHANGES_SHOWN = 5
QUESTIONS = 6
CARD = (1200, 630)       # the image shown when the page is shared
LANGS = ["en", "ko", "ja", "es"]
NAMES = {"en": "English", "ko": "한국어", "ja": "日本語", "es": "Español"}
LOCALES = {"en": "en_US", "ko": "ko_KR", "ja": "ja_JP", "es": "es_ES"}   # as pages are shared: language_TERRITORY
MONTHS = {
    "en": "January February March April May June July August September October November December".split(),
    "es": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(),
}

KEY = re.compile(r"\{\{([a-z0-9_]+)\}\}")
PLACE = re.compile(r"\{(?:(d|m|n):([0-9-]+)|(repo))\}")
FACT = re.compile(r"\{desk:([a-z_.]+)\}")   # a fact of the desk, as {desk:code_minutes}
LIMITS = ["answers_per_item_per_hour", "answers_per_day", "code_uses", "code_minutes", "codes_per_author_per_day", "live_sessions"]
MEASURES = ["median_seconds", "longest_seconds", "trials", "date"]
FACT_NAMES = set(LIMITS) - {"code_uses"} | {"name", "min_version", "label", "as_of"} | {
    f"{way}.{part}" for way in WAYS for part in ("since", "median", "longest", "trials", "measured")}
LIVE = re.compile(r"\[([a-z_]+(?:\.[a-z_]+)?)\]")   # a value of the status, as [tasks.ready]
LIVE_DATES = {"charter.date", "step.gate_plan", "step.gate_today"}
LIVE_NUMBERS = {"charter.version", "step.gate_today_chance", "tasks.ready", "tasks.first", "open_issues"}
COMPUTED = {"changes", "gate_today_hidden", "live_hidden", "structured", "csp", "analytics_id",
            "a7", "desk_form", "coworker_repo"}   # filled in by render()
# The sentences about the desk, by the state in which they are shown: always; a way open, or closed and
# described as what is coming; the note on AI coworker's version while the live way is closed; with no
# way open, what holds today; with a way open, what to know before writing; with a way closed, that the
# limits are what will hold. The page's section holds exactly these, and the guide repeats them.
DESK_TEXT = {
    "always": ["desk_k", "desk_h", "desk_p1", "desk_p2", "desk_gh_n", "desk_live_n", "desk_limits_h", "desk_limit1", "desk_limit2",
               "desk_limit3", "desk_live_cw", "q7"],
    "github": ["desk_gh_h", "desk_gh_state", "desk_gh_s1", "desk_gh_s2", "desk_gh_s3", "desk_gh_link"],
    "github_closed": ["desk_closed", "desk_soon", "desk_gh_h_closed", "desk_gh_c1", "desk_gh_c2", "desk_gh_c3"],
    "live": ["desk_live_h", "desk_live_state", "desk_live_s1", "desk_live_s2", "desk_live_s3", "desk_live_s4", "desk_live_s5",
             "desk_live_s6", "desk_live_form", "desk_know6", "desk_know7", "desk_know8", "desk_know9"],
    "live_closed": ["desk_closed", "desk_soon", "desk_live_h_closed", "desk_live_c1", "desk_live_c2", "desk_live_c3", "desk_live_c4",
                    "desk_live_c5", "desk_live_c6"],
    "released": ["desk_live_need"],
    "unreleased": ["desk_live_need_wait"],
    "none": ["desk_p3", "desk_today", "desk_gh_link"],
    "any": ["desk_know_h", "desk_know1", "desk_know2", "desk_know3", "desk_know4", "desk_know5"],
    "waiting": ["desk_limits_p"],
}
# The answer to the question about talking in real time, by which ways are open.
ANSWERS = {(): ["a7_closed"], ("github",): ["a7_desk", "a7_github", "a7_live_closed"], ("github", "live"): ["a7_desk", "a7_github", "a7_live"]}
ANSWER_KEYS = {key for keys in ANSWERS.values() for key in keys}
LIVE_NAMES = LIVE_DATES | LIVE_NUMBERS | {"step.name", "built"}
ALLOWED_TAGS = {"strong", "code", "span"}
# Counts are written in digits so that a differing count shows up as a differing number.
COUNT_WORDS = {
    "en": r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b",
    "ko": r"둘|셋|넷|다섯|여섯|일곱|여덟|아홉|[두세네] (?:곳|개|가지|명)",
    "ja": r"[二三四五六七八九十][つ社個件]",
    "es": r"\b(?:dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\b",
}


def load_strings(lang: str) -> dict:
    return json.loads((HERE / "strings" / f"{lang}.json").read_text(encoding="utf-8"))


def template() -> str:
    return (HERE / "template.html").read_text(encoding="utf-8")


def page_url(lang: str) -> str:
    return SITE_URL if lang == "en" else f"{SITE_URL}{lang}/"


def page_file(lang: str) -> str:
    return "index.html" if lang == "en" else f"{lang}/index.html"


def href(here: str, there: str) -> str:
    """Relative link from one language's page to another's."""
    if here == there:
        return "./"
    up = "" if here == "en" else "../"
    return (up or "./") if there == "en" else f"{up}{there}/"


def spell(lang: str, kind: str, value: str) -> str:
    if kind == "n":
        return value if lang == "es" else f"{int(value):,}"
    parts = [int(x) for x in value.split("-")]
    year, month, day = (parts + [0])[:3]
    if lang == "ko":
        return f"{year}년 {month}월" + (f" {day}일" if day else "")
    if lang == "ja":
        return f"{year}年{month}月" + (f"{day}日" if day else "")
    name = MONTHS[lang][month - 1]
    if lang == "es":
        return f"{day} de {name} de {year}" if day else f"{name} de {year}"
    return f"{name} {day}, {year}" if day else f"{name} {year}"


def expand(lang: str, text: str, desk: dict | None = None) -> str:
    if "{desk:" in text:
        facts = desk_facts(lang, desk if desk is not None else load_desk())
        text = FACT.sub(lambda m: html.escape(facts[m.group(1)]), text)
    return PLACE.sub(lambda m: REPO_URL if m.group(3) else spell(lang, m.group(1), m.group(2)), text)


# --- the desk: the project's AI session that talks with sessions from outside ---------------------


def load_desk(path: Path | str | None = None) -> dict:
    """The facts of the desk as tools/desk/desk.toml states them."""
    return tomllib.loads(Path(path or DESK).read_text(encoding="utf-8"))


def is_day(value) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\d", value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def form_labels(text: str) -> list[str]:
    """The labels an issue form puts on its issue, from its `labels:` entry at the top level."""
    found = re.search(r"(?m)^labels:[ \t]*(.*)\n((?:[ \t]+-.*\n?)*)", text)
    if not found:
        return []
    inline, listed = found.groups()
    names = inline.strip().strip("[]").split(",") if inline.strip() else [row.split("-", 1)[1] for row in listed.splitlines()]
    return [name.strip().strip("\"'") for name in names if name.strip().strip("\"'")]


def desk_problems(desk: dict | None = None, forms: Path | None = None, made_up: bool = False) -> list[str]:
    """Everything that keeps the facts of the desk from being stated: a missing or misshapen value, a
    way that is open without what it needs, a closed way that carries an opening date. Made-up facts,
    which the layout check builds with, are not asked for the request form."""
    desk = desk if desk is not None else load_desk()
    forms = forms if forms is not None else FORMS
    found = []
    if set(desk) != {"name", "as_of", "ways", "limits", "coworker", "form"}:
        return ["the facts of the desk are: name, as_of, ways, limits, coworker, form"]
    ways, limits, coworker, form = desk["ways"], desk["limits"], desk["coworker"], desk["form"]
    if not all(isinstance(part, dict) for part in (ways, limits, coworker, form)) or set(ways) != set(WAYS):
        return ["the desk has two ways, github and live, and its limits, coworker and form are tables"]
    if not isinstance(desk["name"], str) or not desk["name"].strip():
        found.append("the desk has no name")
    if not is_day(desk["as_of"]):
        found.append("as_of is the day the facts were last checked, as 2026-11-18")
    if set(limits) != set(LIMITS) or not all(is_count(limits[name]) for name in limits):
        found.append("the limits are whole numbers above zero: " + ", ".join(LIMITS))
    elif limits["code_uses"] != 1:
        found.append("a connection code of AI coworker works once, and the page says once")
    if set(coworker) != {"min_version", "released", "repository"} or not isinstance(coworker.get("released"), bool) \
            or not re.fullmatch(r"\d+\.\d+\.\d+", str(coworker.get("min_version"))) \
            or not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", str(coworker.get("repository"))):
        found.append("coworker names the lowest version, as 0.13.0, whether it is released, and the address of its repository on GitHub")
    if set(form) != {"file", "label"} or not re.fullmatch(r"[a-z0-9-]+\.yml", str(form.get("file"))) \
            or not re.fullmatch(r"[a-z0-9-]+", str(form.get("label"))):
        found.append("form names the file of the request form, as live-session.yml, and its label")
    for way in WAYS:
        facts = ways[way]
        if not isinstance(facts, dict) or set(facts) != {"open", "since", "first_answer"} or not isinstance(facts["open"], bool) \
                or not isinstance(facts["since"], str) or not isinstance(facts["first_answer"], dict):
            found.append(f"the {way} way states open (true or false), since (a date or empty) and first_answer")
            continue
        measured = facts["first_answer"]
        whole = set(measured) == set(MEASURES) and all(is_count(measured[name]) for name in MEASURES[:3]) \
            and is_day(measured["date"]) and measured["longest_seconds"] >= measured["median_seconds"]
        if measured and not whole:
            found.append(f"the {way} way's measured time to the first answer is all of {', '.join(MEASURES)}, or empty")
        if facts["open"]:
            if not is_day(facts["since"]):
                found.append(f"the {way} way is open without the date it opened")
            if not whole:
                found.append(f"the {way} way is open without a measured time to the first answer")
            elif is_day(facts["since"]) and measured["date"] > facts["since"]:
                found.append(f"the {way} way opened before its time to the first answer was measured")
            if is_day(facts["since"]) and is_day(desk["as_of"]) and facts["since"] > desk["as_of"]:
                found.append(f"the {way} way opened after the day the facts were last checked: set as_of")
        elif facts["since"]:
            found.append(f"the {way} way is closed and yet carries an opening date")
    if found:
        return found
    if ways["live"]["open"]:
        if not ways["github"]["open"]:
            found.append("the live way is open while the github way is closed: a connection code is asked for on GitHub")
        elif ways["live"]["since"] < ways["github"]["since"]:
            found.append("the live way opened before the github way")
        if not coworker["released"]:
            found.append(f"the live way is open while AI coworker {coworker['min_version']} is not released")
        path = forms / form["file"]
        if made_up:
            pass
        elif not path.is_file():
            found.append(f"the live way is open without the request form {form['file']} in .github/ISSUE_TEMPLATE")
        elif form["label"] not in form_labels(path.read_text(encoding="utf-8")):
            found.append(f"the request form {form['file']} does not carry the label {form['label']}")
    return found


# The layout check lays the page out with a way open before any way is. Only what cannot be known yet
# is made up, the dates and the measured times; every other fact stays the one in tools/desk/desk.toml.
MADE_UP = {"github": ("2026-12-01", {"median_seconds": 32, "longest_seconds": 58, "trials": 12, "date": "2026-11-28"}),
           "live": ("2026-12-15", {"median_seconds": 47, "longest_seconds": 95, "trials": 10, "date": "2026-12-12"})}


def as_if_open(desk: dict, ways: list[str]) -> dict:
    """The facts of the desk with these ways open, for the layout check. A way that is really open keeps its own facts."""
    if not ways or set(ways) - set(WAYS) or ("live" in ways and "github" not in ways):
        raise ValueError("--open takes github or github,live")
    made = {way: dict(desk["ways"][way]) for way in WAYS}
    for way in ways:
        if not made[way]["open"]:
            made[way] = {"open": True, "since": MADE_UP[way][0], "first_answer": dict(MADE_UP[way][1])}
    last = max([desk["as_of"]] + [made[way]["since"] for way in ways])
    return desk | {"ways": made, "as_of": last, "coworker": desk["coworker"] | ({"released": True} if "live" in ways else {})}


def open_ways(desk: dict) -> list[str]:
    return [way for way in WAYS if desk["ways"][way]["open"]]


def shown_parts(desk: dict) -> list[str]:
    """Which parts about the desk are shown in this state. The section itself is there in every state."""
    ways = open_ways(desk)
    parts = ["always"] + [way if way in ways else f"{way}_closed" for way in WAYS]
    if "live" not in ways:
        parts.append("released" if desk["coworker"]["released"] else "unreleased")
    parts.append("any" if ways else "none")
    return parts + (["waiting"] if len(ways) < len(WAYS) else [])


def shown_keys(desk: dict) -> list[str]:
    """The sentences about the desk that are shown in this state, each once."""
    return list(dict.fromkeys(key for part in shown_parts(desk) for key in DESK_TEXT[part]))


def desk_facts(lang: str, desk: dict) -> dict:
    """Every fact of the desk a sentence can name, as text in this language."""
    facts = {name: str(desk["limits"][name]) for name in LIMITS if name != "code_uses"}
    facts |= {"name": desk["name"], "min_version": desk["coworker"]["min_version"], "label": desk["form"]["label"],
              "as_of": spell(lang, "d", desk["as_of"])}
    for way in WAYS:
        since, measured = desk["ways"][way]["since"], desk["ways"][way]["first_answer"]
        facts[f"{way}.since"] = spell(lang, "d", since) if since else ""
        facts[f"{way}.measured"] = spell(lang, "d", measured["date"]) if measured else ""
        for short in ("median", "longest"):
            facts[f"{way}.{short}"] = str(measured[f"{short}_seconds"]) if measured else ""
        facts[f"{way}.trials"] = str(measured["trials"]) if measured else ""
    return facts


def desk_status(desk: dict | None = None) -> dict:
    """The desk's part of the status file, the same in every state: per way whether it is open and
    since when, the limits, the lowest version of AI coworker and whether that version is released."""
    desk = desk if desk is not None else load_desk()
    ways = {way: {"open": desk["ways"][way]["open"], "since": desk["ways"][way]["since"] or None} for way in WAYS}
    return ways | {"limits": {name: desk["limits"][name] for name in LIMITS}, "coworker_min_version": desk["coworker"]["min_version"],
                   "coworker_min_version_released": desk["coworker"]["released"]}


def desk_links(desk: dict) -> dict:
    """The addresses the section links to: where an issue is written, the request form, AI coworker."""
    return {"github": f"{REPO_URL}/issues/new", "form": f"{REPO_URL}/issues/new?template={desk['form']['file']}",
            "coworker": desk["coworker"]["repository"]}


def desk_answer(lang: str, strings: dict, desk: dict) -> str:
    """The answer to the question about talking in real time. With no way open it begins with "Not yet"."""
    glue = "" if lang == "ja" else " "   # Japanese puts no space between sentences
    return glue.join(expand(lang, strings[key], desk) for key in ANSWERS[tuple(open_ways(desk))])


def desk_rule_state(desk: dict, way: str) -> str:
    """How the working rules state whether a way is open."""
    facts = desk["ways"][way]
    return f"open since {spell('en', 'd', facts['since'])}" if facts["open"] else "not open yet"


def desk_guide(desk: dict) -> str:
    """The guide's part about talking in real time, in the page's own sentences: what the desk is, with no
    way open what holds today, each way (open with its steps and addresses, or closed and described as what
    is coming), the limits, and with a way open what an outside agent should know before it writes."""
    parts, keys = shown_parts(desk), shown_keys(desk)
    strings, links = load_strings("en"), desk_links(desk)
    say = lambda key: plain("en", strings[key], desk)
    rows = ["", "## Talking with the project in real time", "", f"{say('desk_p1')} {say('desk_p2')}"]
    if "none" in parts:
        rows += ["", say("desk_p3"), "", say("desk_today"), "", f"- {say('desk_gh_link')}: {links['github']}"]
    for way, short in (("github", "gh"), ("live", "live")):
        if way in parts:
            rows += ["", f"### {say(f'desk_{short}_n')}: {say(f'desk_{short}_h')}", "", say(f"desk_{short}_state"), ""]
            steps = [key for key in keys if re.fullmatch(rf"desk_{short}_s\d", key)]
        else:
            need = [say(key) for key in ("desk_live_need", "desk_live_need_wait") if way == "live" and key in keys]
            rows += ["", f"### {say(f'desk_{short}_n')}: {say(f'desk_{short}_h_closed')}", "",
                     " ".join([say("desk_closed")] + need + [say("desk_soon")]), ""]
            steps = [key for key in keys if re.fullmatch(rf"desk_{short}_c\d", key)]
        rows += [f"{number}. {say(key)}" for number, key in enumerate(steps, 1)] + [""]
        if way == "github" and way in parts:
            rows.append(f"- {say('desk_gh_link')}: {links['github']}")
        if way == "live":
            if way in parts:
                rows += [f"- {say('desk_live_form')}: {links['form']}", f"- Label of the request form: {desk['form']['label']}"]
            rows += [f"- {say('desk_live_cw')}: {links['coworker']}", f"- Lowest version of AI coworker: {desk['coworker']['min_version']}"]
        if rows[-1] == "":
            rows.pop()
    rows += ["", f"### {say('desk_limits_h')}", ""] + ([say("desk_limits_p"), ""] if "waiting" in parts else [])
    rows += [f"- {say(key)}" for key in keys if re.fullmatch(r"desk_limit\d", key)]
    if "any" in parts:
        rows += ["", f"### {say('desk_know_h')}", ""] + [f"- {say(key)}" for key in keys if re.fullmatch(r"desk_know\d", key)]
    return "\n".join(rows) + "\n"


def headline(lang: str, parts: list[str]) -> str:
    """One span per word (per phrase in Japanese, which has no spaces) so the headline can rise
    in piece by piece without ever splitting a character. Parts marked * are emphasized."""
    glue = "" if lang == "ja" else " "
    if len(parts) < 2 or parts[-1].startswith("*") != parts[-2].startswith("*"):
        raise ValueError(f"{lang}: the last two parts of the headline must share their emphasis")
    out, step, inside = [], 0, False
    for index, part in enumerate(parts):
        strong = part.startswith("*")
        if strong != inside:
            out.append("<em>" if strong else "</em>")
            inside, step = strong, step + 1
        if index == len(parts) - 2:
            out.append('<span class="pair">')  # the last two never part, so no word ends up alone
        out.append(f'<span class="w" style="--i:{step}">{part.lstrip("*")}</span>')
        step += 1
    out.append("</span>")
    if inside:
        out.append("</em>")
    html = glue.join(out)
    for tag in ("<em>", '<span class="pair">'):
        html = html.replace(f"{tag}{glue}", tag) if glue else html
    return html.replace(f"{glue}</span></em>", "</span></em>").replace(f"{glue}</span>", "</span>").replace(f"{glue}</em>", "</em>") if glue else html


# --- the status: every value of the section about now ------------------------------------------


def charter_status(text: str, today: str) -> dict:
    """What the charter says about now: its version, the step that today falls in, that step's gate."""
    version, issued = re.search(r"^- 판: v(\d+\.\d+) \((\d{4}-\d\d-\d\d)\)$", text, re.M).groups()
    schedule = text.split("\n## 일정\n", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| (?:[^|]*~ )?(\d{4})년 (\d+)월 (\d+)일 \|", schedule, re.M)
    ends = [f"{y}-{int(m):02d}-{int(d):02d}" for y, m, d in rows]
    if len(ends) != 5 or ends != sorted(ends):
        raise ValueError("the charter's schedule does not have its five steps in order")
    index = next((i for i, end in enumerate(ends, 1) if today <= end), len(ends))
    step = {"index": index, "of": len(ends), "gate_plan": ends[index - 1], "gate_today": None, "gate_today_chance": None}
    # The charter gives a date under today's conditions for the first gate only, with its own condition:
    # the values measured in the trial run entered.
    found = re.search(r"방향 고정 관문은 시범에서 잰 값을 넣으면 지금 조건에서 가운데 (\d{4}-\d\d-\d\d), \d+% \d{4}-\d\d-\d\d이고"
                      r"\((\d{4}-\d\d-\d\d)까지 닿을 확률 (\d+)%\)", schedule)
    if found and index == 1 and found.group(2) == ends[0]:
        step["gate_today"], step["gate_today_chance"] = found.group(1), int(found.group(3))
    first = re.search(r"벤치마크 과제 (\d+)개\(웹", schedule)
    if not first:
        raise ValueError("the charter does not say how many benchmark tasks come first")
    return {"charter": {"version": version, "date": issued}, "step": step, "tasks_first": int(first.group(1))}


def tasks_ready(tasks: Path = TASKS) -> int:
    return sum(1 for path in tasks.iterdir() if (path / "task.toml").is_file()) if tasks.is_dir() else 0


def clean(title: str) -> str:
    """A title as one line of plain text. It is still escaped wherever it is put into HTML."""
    title = " ".join("".join(ch if ch.isprintable() else " " for ch in title).split())
    return title if len(title) <= 140 else title[:139].rstrip() + "…"


def changes_from_log(log: str) -> list[dict]:
    """The changes merged to main, newest first, from `git log --first-parent`: the date, the title of
    the pull request and its address. Only merged pull requests count; no author is kept."""
    found = []
    for record in log.split("\x1e"):
        fields = record.strip("\n").split("\x1f")
        if len(fields) != 3:
            continue
        when, subject, body = fields
        merged = re.match(r"Merge pull request #(\d+) from \S+$", subject)
        squashed = re.match(r"(.+) \(#(\d+)\)$", subject)
        if merged:
            number, title = merged.group(1), next((line for line in body.splitlines() if line.strip()), "")
        elif squashed:
            number, title = squashed.group(2), squashed.group(1)
        else:
            continue
        title = clean(title)
        if title:
            day = dt.datetime.fromisoformat(when).astimezone(dt.timezone.utc).date().isoformat()
            found.append({"date": day, "title": title, "url": f"{REPO_URL}/pull/{number}"})
    return found[:CHANGES_SHOWN]


def merged_changes(root: Path = ROOT) -> list[dict]:
    log = subprocess.run(["git", "-C", str(root), "log", "--first-parent", "-n", "60", "--format=%cI%x1f%s%x1f%b%x1e"],
                         capture_output=True, text=True, check=True).stdout
    return changes_from_log(log)


def open_issues() -> int:
    """How many issues are open to outside contributors. A count only: no text of an issue is read."""
    url = f"https://api.github.com/search/issues?q=repo:{REPO}+is:issue+is:open+label:{WELCOME}&per_page=1"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "dodam-site-build"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return int(json.load(response)["total_count"])


def inputs_digest(status: dict) -> str:
    """One value for everything the published pages show: the status without its build time, and every
    file the pages are built from. The scheduled build deploys only when it differs from the published one."""
    shown = {key: value for key, value in status.items() if key not in {"built", "digest"}}
    sha = hashlib.sha256(json.dumps(shown, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    sources = [HERE / "template.html", HERE / "guide.txt", Path(__file__), DESK] + sorted((HERE / "strings").glob("*.json"))
    sources += sorted(p for p in OUT.rglob("*") if p.is_file() and p.name != "index.html")
    for path in sources:
        sha.update(path.name.encode("utf-8") + b"\0" + path.read_bytes())
    return sha.hexdigest()


def status(now: dt.datetime | None = None, issues: int | None = None, charter: str | None = None,
           changes: list[dict] | None = None, ready: int | None = None, desk: dict | None = None) -> dict:
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).replace(microsecond=0)
    text = charter if charter is not None else CHARTER.read_text(encoding="utf-8")
    said = charter_status(text, now.date().isoformat())
    result = {
        "version": STATUS_VERSION,
        "built": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "charter": said["charter"],
        "step": said["step"],
        "tasks": {"ready": tasks_ready() if ready is None else ready, "first": said["tasks_first"]},
        "open_issues": open_issues() if issues is None else int(issues),
        "changes": merged_changes() if changes is None else changes,
        "desk": desk_status(desk),
    }
    result["digest"] = inputs_digest(result)
    return result


def value(status: dict, name: str):
    found = status
    for part in name.split("."):
        found = found.get(part) if isinstance(found, dict) else None
    return found


def moment(lang: str, stamp: str) -> str:
    """A build time as the page shows it: the day in the language's form, the time in UTC."""
    return f"{spell(lang, 'd', stamp[:10])} {stamp[11:16]} UTC"


def live(lang: str, text: str, status: dict | None, strings: dict) -> str:
    """Put the status into a sentence. Each value is an element of its own, named after the field of
    the status file it shows, so that an open page can set it again. Without a status it is empty."""
    def one(match: re.Match) -> str:
        name = match.group(1)
        if name not in LIVE_NAMES:
            raise ValueError(f"{lang}: no value of the status is called {name}")
        if name == "step.name":
            names = [strings[f"t{i}_h"] for i in range(1, 6)]
            index = value(status, "step.index") if status else None
            shown = html.escape(names[index - 1]) if index else ""
            return f'<span data-live-pick="step.index" data-options="{html.escape("|".join(names))}">{shown}</span>'
        found = value(status, name) if status else None
        if name == "built":
            shown = moment(lang, found) if found else ""
            return f'<time data-live-time="built" datetime="{html.escape(found or "")}">{shown}</time>'
        if name in LIVE_DATES:
            shown = spell(lang, "d", found) if found else ""
            return f'<time data-live-date="{name}" datetime="{html.escape(found or "")}">{shown}</time>'
        return f'<span data-live="{name}">{"" if found is None else html.escape(str(found))}</span>'

    return LIVE.sub(one, text)


def changes_list(lang: str, status: dict | None) -> str:
    rows = []
    for change in (status or {}).get("changes", []):
        day, title, url = change["date"], html.escape(change["title"]), html.escape(change["url"], quote=True)
        rows.append(f'<li><time datetime="{html.escape(day)}">{spell(lang, "d", day)}</time> '
                    f'<a class="link" href="{url}" lang="en">{title}</a></li>')
    return "".join(rows)


# --- what search engines, answer engines and AI agents read -------------------------------------


def plain(lang: str, text: str, desk: dict | None = None) -> str:
    """A string as the text a reader sees: dates spelled, markup removed."""
    return html.unescape(re.sub(r"<[^>]+>", "", expand(lang, text, desk)))


def questions(lang: str, strings: dict, desk: dict) -> list[tuple[str, str]]:
    """The questions and answers as text. The last one, about talking in real time, is answered from the facts of the desk."""
    asked = [(plain(lang, strings[f"q{i}"]), plain(lang, strings[f"a{i}"])) for i in range(1, QUESTIONS + 1)]
    return asked + [(plain(lang, strings["q7"], desk), plain(lang, desk_answer(lang, strings, desk)))]


def structured(lang: str, strings: dict, desk: dict | None = None) -> dict:
    """Structured data, made from the strings the visible text is made from."""
    desk = desk if desk is not None else load_desk()
    site, page = SITE_URL + "#site", page_url(lang) + "#page"
    return {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "@id": site, "name": "Dodam", "url": SITE_URL, "inLanguage": LANGS},
            {"@type": "WebPage", "@id": page, "url": page_url(lang), "name": plain(lang, strings["title"]),
             "description": plain(lang, strings["description"]), "inLanguage": lang, "isPartOf": {"@id": site},
             "primaryImageOfPage": f"{SITE_URL}card-{lang}.png", "about": {"@id": SITE_URL + "#project"}},
            {"@type": "SoftwareSourceCode", "@id": SITE_URL + "#project", "name": "Dodam",
             "description": plain(lang, strings["lede"]), "url": SITE_URL, "codeRepository": REPO_URL,
             "license": [f"{REPO_URL}/blob/main/LICENSE-MIT", f"{REPO_URL}/blob/main/LICENSE-APACHE"],
             "creativeWorkStatus": plain(lang, strings["status"])},
            {"@type": "FAQPage", "@id": page_url(lang) + "#faq", "inLanguage": lang, "isPartOf": {"@id": page},
             "mainEntity": [{"@type": "Question", "name": question, "acceptedAnswer": {"@type": "Answer", "text": answer}}
                            for question, answer in questions(lang, strings, desk)]},
        ],
    }


def card_source(lang: str, strings: dict | None = None) -> str:
    """One value for everything a sharing image is drawn from: the headline, the status line, the colors
    of the page and the drawing itself. card.py records it in cards.json when it draws the image."""
    strings = strings if strings is not None else load_strings(lang)
    css = (OUT / "style.css").read_text(encoding="utf-8").split("@media", 1)[0]
    colors = sorted(re.findall(r"--(?:bg|surface|ink|muted|line|accent|glow|sans):\s*[^;]+;", css))
    drawing = (HERE / "card.py").read_text(encoding="utf-8")
    parts = [lang, json.dumps(strings["h1"], ensure_ascii=False), strings["status"], "\n".join(colors), drawing, str(CARD)]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def sitemap() -> str:
    rows = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">']
    for lang in LANGS:
        rows.append(f"  <url>\n    <loc>{page_url(lang)}</loc>")
        rows += [f'    <xhtml:link rel="alternate" hreflang="{other}" href="{page_url(other)}"/>' for other in LANGS]
        rows.append(f'    <xhtml:link rel="alternate" hreflang="x-default" href="{SITE_URL}"/>\n  </url>')
    return "\n".join(rows + ["</urlset>"]) + "\n"


def working_rule(words: str) -> str:
    """One rule of the working rules (AGENTS.md), in its own words, for the guide."""
    found = [line[2:].replace("`", "") for line in (ROOT / "AGENTS.md").read_text(encoding="utf-8").splitlines()
             if line.startswith("- ") and words in line]
    if len(found) != 1:
        raise ValueError(f"the working rules do not have one rule that says: {words}")
    return found[0]


def guide(status: dict, desk: dict | None = None) -> str:
    """The guide for AI agents (llms.txt), in English: the page's own strings, the status, two rules
    in the working rules' own words, and the page's sentences about the desk in whatever state it is."""
    desk = desk if desk is not None else load_desk()
    strings = load_strings("en")
    step = status["step"]
    values = {key: plain("en", text, desk) for key, text in strings.items() if isinstance(text, str)}
    values |= {
        "site": SITE_URL, "repo": REPO_URL, "welcome": WELCOME,
        "rule_start": working_rule("before sending a large change"), "rule_share": working_rule("no right to share"),
        "pages": ", ".join(f"{NAMES[lang]} {page_url(lang)}" for lang in LANGS),
        "built": moment("en", status["built"]),
        "charter_version": status["charter"]["version"], "charter_date": spell("en", "d", status["charter"]["date"]),
        "step_name": strings[f"t{step['index']}_h"], "step_index": str(step["index"]), "step_of": str(step["of"]),
        "gate_plan": spell("en", "d", step["gate_plan"]),
        "gate_today": "" if not step["gate_today"] else
            f" {plain('en', strings['cap_today'])} with the values measured in the trial run entered, the charter's median is "
            f"{spell('en', 'd', step['gate_today'])}, and the chance of reaching the gate by the plan's date is {step['gate_today_chance']}%.",
        "tasks_ready": str(status["tasks"]["ready"]), "tasks_first": str(status["tasks"]["first"]),
        "open_issues": str(status["open_issues"]), "status_version": str(status["version"]),
        "changes": "\n".join(f"- {spell('en', 'd', c['date'])}: {c['title']} ({c['url']})" for c in status["changes"]) or "- none yet",
        "rules": "\n".join(f"- {values[f'rule{i}_h']}. {values[f'rule{i}_p']}" for i in range(1, 8)),
        "questions": "\n\n".join(f"### {question}\n\n{answer}" for question, answer in questions("en", strings, desk)),
        "desk": desk_guide(desk),
    }
    return KEY.sub(lambda m: values[m.group(1)], (HERE / "guide.txt").read_text(encoding="utf-8"))


def specials(lang: str) -> dict:
    root = "" if lang == "en" else "../"
    alternates = [f'<link rel="alternate" hreflang="{l}" href="{page_url(l)}">' for l in LANGS]
    alternates.append(f'<link rel="alternate" hreflang="x-default" href="{SITE_URL}">')

    def links(indent: str, wrap: str, cls: str) -> str:
        rows = []
        for l in LANGS:
            current = ' aria-current="page"' if l == lang else ""
            a = f'<a{cls} href="{href(lang, l)}" lang="{l}" hreflang="{l}"{current}>{NAMES[l]}</a>'
            rows.append(f"{indent}{wrap.format(a)}")
        return "\n".join(rows)

    return {
        "lang": lang,
        "root": root,
        "home": "./",
        "canonical": page_url(lang),
        "alternates": "\n  ".join(alternates),
        "this_language": NAMES[lang],
        "switcher": links("            ", "<li>{}</li>", ""),
        "foot_languages": links("        ", "{}", ' class="link"'),
        "repo": REPO_URL,
        "site": SITE_URL,
        "locale": LOCALES[lang],
        "card": f"{SITE_URL}card-{lang}.png",
        "card_width": str(CARD[0]),
        "card_height": str(CARD[1]),
    }


def measurement(analytics: str | None) -> str:
    ident = ANALYTICS_ID if analytics is None else analytics
    if ident and not re.fullmatch(r"G-[A-Z0-9]{4,20}", ident):
        raise ValueError("a measurement ID of Google Analytics is G- followed by letters and digits")
    return ident


def policy(ident: str) -> str:
    """The content security policy: the page's own address, and with a measurement ID what Google's
    measurement needs. Styles may be inline because the headline's words carry their order in a style
    attribute; scripts may not."""
    needs = GOOGLE_NEEDS if ident else {}
    rules = {"default-src": [], "script-src": needs.get("script-src", []), "style-src": ["'unsafe-inline'"],
             "img-src": ["data:"] + needs.get("img-src", []), "connect-src": needs.get("connect-src", []),
             "object-src": None, "base-uri": [], "form-action": None}
    return "; ".join(f"{name} 'none'" if extra is None else " ".join([name, "'self'"] + extra) for name, extra in rules.items())


def render(lang: str, strings: dict | None = None, status: dict | None = None, analytics: str | None = None,
           desk: dict | None = None) -> str:
    strings = strings if strings is not None else load_strings(lang)
    desk = desk if desk is not None else load_desk()
    ident = measurement(analytics)
    values = specials(lang)
    values["analytics_id"] = ident
    values["csp"] = policy(ident)
    parts = shown_parts(desk)
    for key, text in strings.items():
        values[key] = headline(lang, text) if isinstance(text, list) else live(lang, expand(lang, text, desk), status, strings)
    values["a7"] = desk_answer(lang, strings, desk)
    values["desk_form"] = html.escape(desk_links(desk)["form"], quote=True)
    values["coworker_repo"] = html.escape(desk_links(desk)["coworker"], quote=True)
    values["live_hidden"] = "" if status else " hidden"
    values["gate_today_hidden"] = "" if status and status["step"]["gate_today"] else " hidden"
    values["changes"] = changes_list(lang, status)
    values["structured"] = json.dumps(structured(lang, strings, desk), ensure_ascii=False, indent=1).replace("<", "\\u003c")
    text = MEASURED.sub(r"\1" if ident else "", template())
    for name, block in OPEN_ONLY.items():
        text = block.sub(r"\1" if name in parts else "", text)
    return KEY.sub(lambda m: values[m.group(1)], text)


def pages(status: dict | None = None, analytics: str | None = None, desk: dict | None = None) -> dict[str, str]:
    built = {page_file(lang): render(lang, status=status, analytics=analytics, desk=desk) for lang in LANGS}
    if status is None:
        built["sitemap.xml"] = sitemap()
    return built


def publish(out: Path, status: dict, analytics: str | None = None, desk: dict | None = None) -> None:
    """What is deployed: the files of site/, the pages with the section about now filled in, the status
    file and the guide for AI agents. The measurement script is among them only with a measurement ID.
    The desk's part of the status file is always what the facts of the desk say, whatever status is given."""
    ident = measurement(analytics)
    desk = desk if desk is not None else load_desk()
    status = status | {"desk": desk_status(desk)}
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(OUT, out, ignore=shutil.ignore_patterns("__pycache__"))
    for name, text in pages(status, ident, desk).items():
        (out / name).write_text(text, encoding="utf-8")
    if not ident:
        (out / "analytics.js").unlink()
    (out / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "llms.txt").write_text(guide(status, desk), encoding="utf-8")


def published_digest() -> str | None:
    """The digest of what is published now. None only when no status file is published yet (not found).
    Any other failure to read it is an error: the run fails and deploys nothing, so that a lasting fault
    is seen instead of deploying again every hour."""
    request = urllib.request.Request(SITE_URL + "status.json", headers={"User-Agent": "dodam-site-build"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            found = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    digest = found.get("digest") if isinstance(found, dict) else None
    if not isinstance(digest, str) or not digest:
        raise ValueError("the published status file has no digest")
    return digest


def shape(value) -> dict:
    """What must be the same in every language: dates, numbers, markup and emphasis."""
    if isinstance(value, list):
        return {"kind": "list", "emphasis": any(p.startswith("*") for p in value), "digits": sorted(re.findall(r"\d+", "".join(value)))}
    rest = FACT.sub(" ", PLACE.sub(" ", value))
    return {
        "kind": "text",
        "live": sorted(LIVE.findall(value)),
        "placeholders": sorted(m.group(0) for m in PLACE.finditer(value)) + sorted(FACT.findall(value)),
        "digits": sorted(re.findall(r"\d+(?:[.,]\d+)*", rest)),
        "tags": sorted(re.findall(r"</?([a-z]+)", rest)),
    }


def problems(strings: dict[str, dict] | None = None) -> list[str]:
    strings = strings if strings is not None else {lang: load_strings(lang) for lang in LANGS}
    found = []
    used = set(KEY.findall(template())) - set(specials("en")) - COMPUTED | ANSWER_KEYS
    about_desk = {key for keys in DESK_TEXT.values() for key in keys} | ANSWER_KEYS
    found += [f"the template lacks the sentence about the desk {key}" for key in sorted(about_desk - used)]
    found += [f"the template has a sentence about the desk that no state shows: {key}"
              for key in sorted(key for key in used - about_desk if key.startswith(("desk_", "a7", "q7")))]
    for lang in LANGS:
        have = set(strings[lang])
        found += [f"{lang}: missing string {key}" for key in sorted(used - have)]
        found += [f"{lang}: extra string {key}" for key in sorted(have - used)]
        for key, value in strings[lang].items():
            text = "".join(value) if isinstance(value, list) else value
            if not text.strip():
                found.append(f"{lang}: empty string {key}")
            if re.search(r"https?:|href=|<a[\s>]", text):
                found.append(f"{lang}: {key} carries a link; links belong in the template")
            if re.search(COUNT_WORDS[lang], text, re.I):
                found.append(f"{lang}: {key} writes a count as a word; write it in digits")
            if re.search(r"[０-９]", text):
                found.append(f"{lang}: {key} uses full-width digits")
            if set(LIVE.findall(text)) - LIVE_NAMES:
                found.append(f"{lang}: {key} names a value the status does not have")
            if LIVE.search(text) and re.search(r"\d", PLACE.sub("", text)):
                found.append(f"{lang}: {key} mixes a figure typed by hand with values of the status")
            if set(FACT.findall(text)) - FACT_NAMES:
                found.append(f"{lang}: {key} names a fact the desk does not have")
            if key in about_desk and re.search(r"\d", FACT.sub("", text)):
                found.append(f"{lang}: {key} types a figure about the desk by hand; name the fact, as {{desk:code_minutes}}")
            if key not in about_desk and FACT.search(text):
                found.append(f"{lang}: {key} names a fact of the desk outside the sentences about the desk")
            if set(re.findall(r"</?([a-z]+)", text)) - ALLOWED_TAGS:
                found.append(f"{lang}: {key} uses markup that belongs in the template")
            if lang != "en" and key in strings["en"] and shape(value) != shape(strings["en"][key]):
                found.append(f"{lang}: {key} differs from English in numbers, dates or markup")
    return found


def option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def main(argv: list[str]) -> int:
    desk, as_if = load_desk(), option(argv, "--open")
    issues = problems() + desk_problems(desk)
    if as_if and not issues:
        desk = as_if_open(desk, as_if.split(","))
        issues = desk_problems(desk, made_up=True)
    for issue in issues:
        print(issue, file=sys.stderr)
    if issues:
        return 1
    if "--live" in argv:
        given, count = option(argv, "--status"), option(argv, "--issues")
        if given:
            now = json.loads(Path(given).read_text(encoding="utf-8"))
        else:
            now = status(issues=None if count is None else int(count), desk=desk)
        publish(Path(option(argv, "--live")), now, option(argv, "--analytics"), desk)
        if "--compare" in argv:
            print("changed=" + ("false" if published_digest() == now["digest"] else "true"))
        return 0
    stale = 0
    for name, text in pages(desk=desk).items():
        path = OUT / name
        if "--check" in argv:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                print(f"site/{name} is stale", file=sys.stderr)
                stale = 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return stale


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
