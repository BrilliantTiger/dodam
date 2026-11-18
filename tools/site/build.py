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
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "site"
CHARTER = ROOT / "docs" / "charter.ko.md"
TASKS = ROOT / "bench" / "tasks"
SITE_URL = "https://brillianttigercorp.com/dodam/"
REPO_URL = "https://github.com/BrilliantTiger/dodam"
# The measurement ID of Google Analytics: this one setting, empty until the owner has created the
# property. While it is empty the built pages hold no analytics code and name no other host.
ANALYTICS_ID = ""
MEASURED = re.compile(r"<!--measured-->\n(.*?)<!--/measured-->\n", re.S)   # parts of the template that exist only with an ID
# What Google's measurement needs, from Google's guide to content security policies for the Google tag.
GOOGLE_NEEDS = {
    "script-src": ["https://www.googletagmanager.com"],
    "img-src": ["https://www.googletagmanager.com", "https://*.google-analytics.com"],
    "connect-src": ["https://www.googletagmanager.com", "https://*.google-analytics.com", "https://*.google.com"],
}
REPO = REPO_URL.split("github.com/", 1)[1]
WELCOME = "ai-welcome"   # the label of issues open to outside contributors
STATUS_VERSION = 1
CHANGES_SHOWN = 5
QUESTIONS = 6
CARD = (1200, 630)       # the image shown when the page is shared
LANGS = ["en", "ko", "ja", "es"]
NAMES = {"en": "English", "ko": "한국어", "ja": "日本語", "es": "Español"}
MONTHS = {
    "en": "January February March April May June July August September October November December".split(),
    "es": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(),
}

KEY = re.compile(r"\{\{([a-z0-9_]+)\}\}")
PLACE = re.compile(r"\{(?:(d|m|n):([0-9-]+)|(repo))\}")
LIVE = re.compile(r"\[([a-z_]+(?:\.[a-z_]+)?)\]")   # a value of the status, as [tasks.ready]
LIVE_DATES = {"charter.date", "step.gate_plan", "step.gate_today"}
LIVE_NUMBERS = {"charter.version", "step.gate_today_chance", "tasks.ready", "tasks.first", "open_issues"}
COMPUTED = {"changes", "gate_today_hidden", "live_hidden", "structured", "csp", "analytics_id"}   # filled in by render()
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


def expand(lang: str, text: str) -> str:
    return PLACE.sub(lambda m: REPO_URL if m.group(3) else spell(lang, m.group(1), m.group(2)), text)


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
    sources = [HERE / "template.html", HERE / "guide.txt", HERE / "analytics.js", Path(__file__)] + sorted((HERE / "strings").glob("*.json"))
    sources += sorted(p for p in OUT.rglob("*") if p.is_file() and p.name != "index.html")
    for path in sources:
        sha.update(path.name.encode("utf-8") + b"\0" + path.read_bytes())
    return sha.hexdigest()


def status(now: dt.datetime | None = None, issues: int | None = None, charter: str | None = None,
           changes: list[dict] | None = None, ready: int | None = None) -> dict:
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


def plain(lang: str, text: str) -> str:
    """A string as the text a reader sees: dates spelled, markup removed."""
    return html.unescape(re.sub(r"<[^>]+>", "", expand(lang, text)))


def structured(lang: str, strings: dict) -> dict:
    """Structured data, made from the strings the visible text is made from."""
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
             "mainEntity": [{"@type": "Question", "name": plain(lang, strings[f"q{i}"]),
                             "acceptedAnswer": {"@type": "Answer", "text": plain(lang, strings[f"a{i}"])}}
                            for i in range(1, QUESTIONS + 1)]},
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


def guide(status: dict) -> str:
    """The guide for AI agents (llms.txt), in English: the page's own strings and the status."""
    strings = load_strings("en")
    step = status["step"]
    values = {key: plain("en", text) for key, text in strings.items() if isinstance(text, str)}
    values |= {
        "site": SITE_URL, "repo": REPO_URL, "welcome": WELCOME,
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
        "questions": "\n\n".join(f"### {values[f'q{i}']}\n\n{values[f'a{i}']}" for i in range(1, QUESTIONS + 1)),
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


def render(lang: str, strings: dict | None = None, status: dict | None = None, analytics: str | None = None) -> str:
    strings = strings if strings is not None else load_strings(lang)
    ident = measurement(analytics)
    values = specials(lang)
    values["analytics_id"] = ident
    values["csp"] = policy(ident)
    for key, text in strings.items():
        values[key] = headline(lang, text) if isinstance(text, list) else live(lang, expand(lang, text), status, strings)
    values["live_hidden"] = "" if status else " hidden"
    values["gate_today_hidden"] = "" if status and status["step"]["gate_today"] else " hidden"
    values["changes"] = changes_list(lang, status)
    values["structured"] = json.dumps(structured(lang, strings), ensure_ascii=False, indent=1).replace("<", "\\u003c")
    return KEY.sub(lambda m: values[m.group(1)], MEASURED.sub(r"\1" if ident else "", template()))


def pages(status: dict | None = None, analytics: str | None = None) -> dict[str, str]:
    built = {page_file(lang): render(lang, status=status, analytics=analytics) for lang in LANGS}
    if status is None:
        built["sitemap.xml"] = sitemap()
    return built


def publish(out: Path, status: dict, analytics: str | None = None) -> None:
    """What is deployed: the files of site/, the pages with the section about now filled in, the status
    file and the guide for AI agents. The measurement script is among them only with a measurement ID."""
    ident = measurement(analytics)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(OUT, out, ignore=shutil.ignore_patterns("__pycache__"))
    for name, text in pages(status, ident).items():
        (out / name).write_text(text, encoding="utf-8")
    if ident:
        shutil.copyfile(HERE / "analytics.js", out / "analytics.js")
    (out / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out / "llms.txt").write_text(guide(status), encoding="utf-8")


def published_digest() -> str | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(SITE_URL + "status.json", headers={"User-Agent": "dodam-site-build"}), timeout=30) as response:
            return json.load(response).get("digest")
    except Exception:
        return None   # nothing is published yet, or it cannot be read: deploy


def shape(value) -> dict:
    """What must be the same in every language: dates, numbers, markup and emphasis."""
    if isinstance(value, list):
        return {"kind": "list", "emphasis": any(p.startswith("*") for p in value), "digits": sorted(re.findall(r"\d+", "".join(value)))}
    rest = PLACE.sub(" ", value)
    return {
        "kind": "text",
        "live": sorted(LIVE.findall(value)),
        "placeholders": sorted(m.group(0) for m in PLACE.finditer(value)),
        "digits": sorted(re.findall(r"\d+(?:[.,]\d+)*", rest)),
        "tags": sorted(re.findall(r"</?([a-z]+)", rest)),
    }


def problems(strings: dict[str, dict] | None = None) -> list[str]:
    strings = strings if strings is not None else {lang: load_strings(lang) for lang in LANGS}
    found = []
    used = set(KEY.findall(template())) - set(specials("en")) - COMPUTED
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
            if set(re.findall(r"</?([a-z]+)", text)) - ALLOWED_TAGS:
                found.append(f"{lang}: {key} uses markup that belongs in the template")
            if lang != "en" and key in strings["en"] and shape(value) != shape(strings["en"][key]):
                found.append(f"{lang}: {key} differs from English in numbers, dates or markup")
    return found


def option(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def main(argv: list[str]) -> int:
    issues = problems()
    for issue in issues:
        print(issue, file=sys.stderr)
    if issues:
        return 1
    if "--live" in argv:
        given, count = option(argv, "--status"), option(argv, "--issues")
        if given:
            now = json.loads(Path(given).read_text(encoding="utf-8"))
        else:
            now = status(issues=None if count is None else int(count))
        publish(Path(option(argv, "--live")), now, option(argv, "--analytics"))
        if "--compare" in argv:
            print("changed=" + ("false" if published_digest() == now["digest"] else "true"))
        return 0
    stale = 0
    for name, text in pages().items():
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
