"""Checks on the public introduction page in site/ and the workflow that publishes it.

The page is served in four languages built from one template (tools/site). These tests fail
when a language lacks a string, has an extra one, or shows a different number, date or link.
They live with the benchmark tests because that is the test run the branch check executes.
"""

import copy
import datetime as dt
import json
import struct
import xml.etree.ElementTree as ET
import importlib.util
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
LANGS = ["en", "ko", "ja", "es"]
NAMES = {"en": "English", "ko": "한국어", "ja": "日本語", "es": "Español"}
SECTIONS = ["what", "why", "guarantees", "how", "now", "join", "rules", "faq", "license"]
PAGE_WEIGHT_LIMIT = 150 * 1024


def page_path(lang: str) -> Path:
    return SITE / "index.html" if lang == "en" else SITE / lang / "index.html"


def page_url(lang: str) -> str:
    return SITE_URL if lang == "en" else f"{SITE_URL}{lang}/"


def builder():
    spec = importlib.util.spec_from_file_location("site_build", ROOT / "tools" / "site" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The address is set once, in the build script. Everything else reads it from there.
SITE_URL = builder().SITE_URL
REPO_URL = builder().REPO_URL


class Page(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.title = ""
        self.inline_script = ""
        self.data_blocks: list[str] = []
        self._data = False
        self.text = ""
        self._inside: list[str] = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":   # a block of structured data is data: the browser never runs it
            self._data = dict(attrs).get("type") == "application/ld+json"
            if self._data:
                self.data_blocks.append("")
        if tag not in {"meta", "link", "br", "img", "rect", "circle", "path", "use"}:
            self._inside.append(tag)

    def handle_endtag(self, tag):
        if tag in self._inside:
            while self._inside.pop() != tag:
                pass

    def handle_data(self, data):
        here = self._inside[-1] if self._inside else ""
        if here == "title":
            self.title += data
        elif here == "script" and self._data:
            self.data_blocks[-1] += data
        elif here == "script":
            self.inline_script += data
        elif here != "style":
            self.text += data + " "  # elements are separate words

    def all(self, tag: str) -> list[dict[str, str | None]]:
        return [a for t, a in self.tags if t == tag]

    def hrefs(self) -> list[str]:
        return [a["href"] for a in self.all("a") if a.get("href")]


def page(lang: str) -> Page:
    return Page(page_path(lang).read_text(encoding="utf-8"))


def is_local(url: str) -> bool:
    return not re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", url.strip(), re.I)


def site_files() -> list[Path]:
    return [p for p in SITE.rglob("*") if p.is_file()]


# --- each page on its own -------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_page_declares_language_title_and_description(lang):
    p = page(lang)
    assert p.all("html")[0].get("lang") == lang
    assert "Dodam" in p.title
    metas = {m.get("name"): m.get("content") for m in p.all("meta")}
    assert len(metas.get("description") or "") >= 50
    assert "width=device-width" in (metas.get("viewport") or "")


def test_titles_and_descriptions_differ_between_languages():
    pages = [page(lang) for lang in LANGS]
    assert len({p.title for p in pages}) == len(LANGS)
    descriptions = [next(m["content"] for m in p.all("meta") if m.get("name") == "description") for p in pages]
    assert len(set(descriptions)) == len(LANGS)


@pytest.mark.parametrize("lang", LANGS)
def test_page_names_itself_and_the_other_languages_for_search_engines(lang):
    links = page(lang).all("link")
    assert [a.get("href") for a in links if a.get("rel") == "canonical"] == [page_url(lang)]
    alternates = {a.get("hreflang"): a.get("href") for a in links if a.get("rel") == "alternate" and a.get("hreflang")}
    assert alternates == {**{other: page_url(other) for other in LANGS}, "x-default": SITE_URL}


@pytest.mark.parametrize("lang", LANGS)
def test_language_switcher_is_plain_links_in_each_language_s_own_name(lang):
    p = page(lang)
    here = page_path(lang).parent
    for other in LANGS:
        targets = [
            a for a in p.all("a")
            if a.get("hreflang") == other and is_local(a.get("href") or "http:")
            and (here / a["href"]).resolve() == page_path(other).parent
        ]
        assert targets, f"{lang} page has no link to the {other} page"
        assert NAMES[other] in p.text
    assert "navigator.language" not in (SITE / "motion.js").read_text(encoding="utf-8")
    assert not [m for m in p.all("meta") if (m.get("http-equiv") or "").lower() == "refresh"]


@pytest.mark.parametrize("lang", LANGS)
def test_page_has_every_section_and_one_heading(lang):
    p = page(lang)
    ids = {a.get("id") for _, a in p.tags}
    assert [s for s in SECTIONS if s not in ids] == []
    assert len(p.all("h1")) == 1
    assert len(p.all("main")) == 1


@pytest.mark.parametrize("lang", LANGS)
def test_page_links_to_the_places_people_join_from(lang):
    hrefs = page(lang).hrefs()
    assert REPO_URL in hrefs
    assert f"{REPO_URL}/discussions" in hrefs
    assert f"{REPO_URL}/security/policy" in hrefs
    assert f"{REPO_URL}/blob/main/AGENTS.md" in hrefs
    assert any(h.startswith(f"{REPO_URL}/issues") and "ai-welcome" in h for h in hrefs)


@pytest.mark.parametrize("lang", LANGS)
def test_page_loads_nothing_from_another_host_and_stays_light(lang):
    p = page(lang)
    loaded = [a.get("src") for t, a in p.tags if a.get("src")]
    loaded += [a.get("href") for a in p.all("link") if a.get("rel") not in {"canonical", "alternate", "sitemap"}]
    assert [u for u in loaded if u and not is_local(u)] == []
    assert p.inline_script.strip() == ""
    weight = page_path(lang).stat().st_size
    for url in loaded:
        target = (page_path(lang).parent / url.split("?")[0].split("#")[0]).resolve()
        assert target.is_file(), url
        weight += target.stat().st_size
    assert weight < PAGE_WEIGHT_LIMIT


@pytest.mark.parametrize("lang", LANGS)
def test_page_keeps_the_project_name_in_latin_letters(lang):
    p = page(lang)
    assert "Dodam" in p.text
    assert p.text.count("도담") <= 3, "도담 appears only where the page explains the name"
    assert not re.search(r"[０-９]", p.text), "use ASCII digits"


@pytest.mark.parametrize("lang", LANGS)
def test_numbers_about_insecure_code_name_their_source(lang):
    text = page(lang).text
    assert text.count("Veracode") >= 3
    assert "2025" in text and "2026" in text


@pytest.mark.parametrize("lang", LANGS)
def test_page_names_the_seven_existing_languages(lang):
    text = page(lang).text
    for name in ("Python", "TypeScript", "Java", "C#", "Go", "Rust", "C++"):
        assert name in text


# --- goals read as goals, and facts are the charter's ---------------------------------------

DESIGN_PHASE = {"en": "design phase", "ko": "설계 단계", "ja": "設計段階", "es": "fase de diseño"}
NO_COMPILER = {"en": "no compiler", "ko": "컴파일러는 아직 없", "ja": "コンパイラはまだありません", "es": "no hay compilador"}


@pytest.mark.parametrize("lang", LANGS)
def test_share_texts_say_it_is_a_design_with_no_compiler_yet(lang):
    p = page(lang)
    metas = {m.get("name") or m.get("property"): m.get("content") or "" for m in p.all("meta")}
    for text in (p.title, metas["og:title"]):
        assert DESIGN_PHASE[lang] in text.lower(), text
    for text in (metas["description"], metas["og:description"]):
        assert DESIGN_PHASE[lang] in text.lower(), text
        assert NO_COMPILER[lang] in text.lower(), text


def charter() -> str:
    return (ROOT / "docs" / "charter.ko.md").read_text(encoding="utf-8")


def charter_guarantees() -> list[str]:
    after = charter().split("아래 열 가지는", 1)[1]
    rows = []
    for line in after.splitlines()[1:]:
        if line.startswith("| **"):
            rows.append(line)
        elif rows:
            break
    return rows


@pytest.mark.parametrize("lang", LANGS)
def test_guarantee_cards_are_the_ten_in_the_charter(lang):
    assert len(charter_guarantees()) == 10
    html = page_path(lang).read_text(encoding="utf-8")
    section = html.split('id="guarantees"', 1)[1].split('id="how"', 1)[0]
    assert section.count('class="card"') == 10
    assert re.search(r"(?<!\d)10(?!\d)", Page("<main>" + section.split("<ul", 1)[0]).text)


# --- the figures the page takes from the charter -----------------------------------------------
#
# The charter gives two sets of dates: the plan's dates at the plan's capacity, and the dates under
# today's conditions. charter_figures() reads every date, count and percentage the page uses out of
# the charter's own sentences; page_figures() says which string of the page carries which of them.
# A figure changed by hand in the charter, or in the strings of one language, is then a fault.


def iso(year: str, month: str, day: str = "") -> str:
    return f"{year}-{int(month):02d}" + (f"-{int(day):02d}" if day else "")


def charter_figures(text: str) -> dict:
    def one(pattern: str, where: str) -> tuple:
        found = re.findall(pattern, where, re.M)
        assert len(found) == 1, f"the charter does not say this once: {pattern} ({len(found)})"
        return found[0] if isinstance(found[0], tuple) else (found[0],)

    schedule = text.split("\n## 일정\n", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| (?:[^|]*~ )?(\d{4})년 (\d+)월 (\d+)일 \| ([^|]+) \|", schedule, re.M)
    assert len(rows) == 5 and rows[-1][3] == "1.0", rows
    span = r"(\d{4}-\d\d-\d\d)~(\d{4}-\d\d-\d\d)"
    months = r"(\d{4})년 (\d+)월~(\d{4})년 (\d+)월"
    low50, high50, pct, low80, high80 = one(rf"1\.0의 가운데 날짜는 {span}\(\d+주 폭\), (\d+)% 날짜는 {span}\(", schedule)
    seeds, runs, y1, m1, y2, m2 = one(rf"씨앗 (\d+)개로 ([\d,]+)번씩 돌리면, 끝까지 지금 조건으로 갈 때 1\.0의 가운데 날짜는 씨앗에 따라 {months}이고", schedule)
    pct_stop, ys, ms = one(r"두 경우 모두 (\d+)% 날짜는 시뮬레이션이 멈추는 (\d{4})년 (\d+)월보다 늦다", schedule)
    assert pct == pct_stop
    version, issued = one(r"^- 판: v(\d+\.\d+) \((\d{4}-\d\d-\d\d)\)$", text)
    roles = text.split("\n| 역할 | 하는 일 |\n", 1)[1].split("\n\n", 1)[0].splitlines()[1:]
    return {
        "milestones": [iso(y, m, d) for y, m, d, _ in rows],
        "plan_runs": one(r"터지게 해서 ([\d,]+)번 돌렸다", schedule)[0].replace(",", ""),
        "plan_days": one(r"위 표와 문단의 날짜는 계획의 기준 용량\(AI 회사마다 계정 하나, 주 약 (\d+)작업일\)", schedule)[0],
        "companies": str(len(set(re.findall(r"Claude Max 20x|ChatGPT Pro|Google AI Ultra", one(r"^\| AI 모델 사용 \|.*$", text)[0])))),
        "median": [low50, high50],
        "pct": pct,
        "by_pct": [low80, high80],
        "today_days": one(r"이 조건을 주 약 (\d+)작업일로", schedule)[0],
        "today_seeds": seeds,
        "today_runs": runs.replace(",", ""),
        "today_median": [iso(y1, m1), iso(y2, m2)],
        "stop": iso(ys, ms),
        "version": version,
        "issued": issued,
        "guarantees": str(len(charter_guarantees())),
        "roles": str(len(roles)),
        "languages": one(r"기존 언어 (\d+)개로 잰 비교 기준", schedule)[0],
        "tasks_first": one(r"벤치마크 과제 (\d+)개\(웹", schedule)[0],
        "tasks_public": one(r"편집기 지원\(LSP\), 벤치마크 과제 (\d+)개", schedule)[0],
        "service_months": one(r"실서비스 하나를 (\d+)개월 넘게", schedule)[0],
    }


def page_figures(c: dict) -> dict:
    """For each string of the page that carries a figure: its placeholders and its plain numbers.
    Names of releases and of the license are names, not figures."""
    d = lambda value: "{d:%s}" % value
    m = lambda value: "{m:%s}" % value
    n = lambda value: "{n:%s}" % value
    plan = {f"t{i}_w": ([d(day)], []) for i, day in enumerate(c["milestones"], 1)}
    return plan | {
        "t5_today": ([m(c["today_median"][0]), m(c["today_median"][1])], []),
        "now_cap": ([], [c["companies"]]),
        "now_note": ([n(c["plan_runs"]), d(c["issued"]), d(c["median"][0]), d(c["median"][1]), d(c["by_pct"][0]), d(c["by_pct"][1]),
                      n(c["today_runs"]), m(c["today_median"][0]), m(c["today_median"][1]), m(c["stop"])],
                     [c["companies"], c["plan_days"], c["version"], c["pct"], c["pct"], c["today_days"], c["today_seeds"]]),
        "g_p": ([], [c["guarantees"]]),
        "how_p": ([], [c["roles"]]),
        "now_p": ([], [c["languages"]]),
        "langs_label": ([], [c["languages"]]),
        "t1_p": ([], [c["tasks_first"], c["languages"]]),
        "t3_p": ([], [c["tasks_public"]]),
        "t4_p": ([], [c["service_months"]]),
        "a3": ([], [c["languages"]]),
        "a5": ([d(c["milestones"][4]), m(c["today_median"][0]), m(c["today_median"][1]), d(c["milestones"][2])], [c["companies"]]),
    }


NAMES_NOT_FIGURES = r"(?<![\d.])(?:1\.0|0\.1|2\.0|5\.5)(?![\d.])"   # releases, the license, a model's name
FROM_RESEARCH = {"stat1", "stat2", "stat3", "src1", "src2", "src3", "why_note", "a6"}   # sources kept as they are


def figure_faults(charter_text: str, strings: dict) -> list[str]:
    """Every way the page's figures differ from the charter's, in any of the four languages."""
    build = builder()
    try:
        want = page_figures(charter_figures(charter_text))
    except AssertionError as error:
        return [str(error)]
    faults = list(build.problems(strings))
    en = strings["en"]
    for key, value in en.items():
        if key in FROM_RESEARCH or isinstance(value, list):
            continue
        places = sorted(match.group(0) for match in build.PLACE.finditer(value) if match.group(1))
        plain = sorted(re.findall(r"\d+(?:\.\d+)*", re.sub(NAMES_NOT_FIGURES, " ", build.PLACE.sub(" ", value))))
        wanted_places, wanted_plain = want.get(key, ([], []))
        if places != sorted(wanted_places):
            faults.append(f"{key}: dates and large numbers {places}, the charter gives {sorted(wanted_places)}")
        if plain != sorted(wanted_plain):
            faults.append(f"{key}: numbers {plain}, the charter gives {sorted(wanted_plain)}")
    faults += [f"{key}: the page has no such string" for key in want if key not in en]
    return faults


def all_strings() -> dict:
    build = builder()
    return {lang: build.load_strings(lang) for lang in build.LANGS}


def test_every_date_count_and_percentage_from_the_charter_is_the_charter_s():
    assert figure_faults(charter(), all_strings()) == []


def now_section(lang: str) -> str:
    return page_path(lang).read_text(encoding="utf-8").split('id="now"', 1)[1].split('id="join"', 1)[0]


@pytest.mark.parametrize("lang", LANGS)
def test_built_page_spells_the_charter_s_dates_in_its_own_language(lang):
    build, c = builder(), charter_figures(charter())
    text = Page("<main>" + now_section(lang)).text
    for day in c["milestones"] + c["median"] + c["by_pct"] + [c["issued"]]:
        assert build.spell(lang, "d", day) in text, day
    for month in c["today_median"] + [c["stop"]]:
        assert build.spell(lang, "m", month) in text, month
    for runs in (c["plan_runs"], c["today_runs"]):
        assert build.spell(lang, "n", runs) in text, runs
    assert re.search(rf"(?<![\d.]){re.escape(c['version'])}(?![\d.])", text), "the page does not name the charter's version"
    assert re.search(rf"(?<!\d){c['pct']}\s?%", text)


# One figure of the charter changed by hand, each as the charter writes it.
CHARTER_VARIANTS = [
    ("~ 2028년 5월 10일 | 최소 언어", "~ 2028년 6월 10일 | 최소 언어"),
    ("~ 2032년 2월 25일 | 1.0", "~ 2032년 2월 26일 | 1.0"),
    ("터지게 해서 2,000번 돌렸다", "터지게 해서 3,000번 돌렸다"),
    ("계획의 기준 용량(AI 회사마다 계정 하나, 주 약 15작업일)에서의 계획 날짜다", "계획의 기준 용량(AI 회사마다 계정 하나, 주 약 16작업일)에서의 계획 날짜다"),
    ("1.0의 가운데 날짜는 2032-02-11~", "1.0의 가운데 날짜는 2032-02-12~"),
    ("~2032-03-24(6주 폭)", "~2032-03-31(6주 폭)"),
    ("80% 날짜는 2034-01-18~2034-04-19(13주 폭)", "80% 날짜는 2034-01-18~2034-05-19(13주 폭)"),
    ("80% 날짜는 2034-01-18~2034-04-19(13주 폭)", "70% 날짜는 2034-01-18~2034-04-19(13주 폭)"),
    ("이 조건을 주 약 4작업일로", "이 조건을 주 약 5작업일로"),
    ("씨앗 5개로 1,200번씩 돌리면, 끝까지", "씨앗 6개로 1,200번씩 돌리면, 끝까지"),
    ("씨앗 5개로 1,200번씩 돌리면, 끝까지", "씨앗 5개로 1,300번씩 돌리면, 끝까지"),
    ("씨앗에 따라 2040년 5월~2041년 1월이고", "씨앗에 따라 2040년 4월~2041년 1월이고"),
    ("씨앗에 따라 2040년 5월~2041년 1월이고", "씨앗에 따라 2040년 5월~2041년 2월이고"),
    ("시뮬레이션이 멈추는 2042년 10월보다 늦다", "시뮬레이션이 멈추는 2042년 11월보다 늦다"),
    ("기존 언어 7개로 잰 비교 기준", "기존 언어 8개로 잰 비교 기준"),
    ("벤치마크 과제 30개(웹", "벤치마크 과제 40개(웹"),
    ("편집기 지원(LSP), 벤치마크 과제 100개", "편집기 지원(LSP), 벤치마크 과제 200개"),
    ("실서비스 하나를 3개월 넘게", "실서비스 하나를 6개월 넘게"),
    ("| 정리하는 AI |", "| 정리하는 AI | 한 줄 더 |\n| 지켜보는 AI |"),
]


def test_the_page_names_the_version_of_the_charter_it_takes_its_figures_from():
    text = charter()
    version, issued = re.search(r"^- 판: v(\d+\.\d+) \((\d{4}-\d\d-\d\d)\)$", text, re.M).groups()
    later = text.replace(f"- 판: v{version} ({issued})", "- 판: v9.9 (2031-01-01)", 1)
    assert figure_faults(later, all_strings()) != []
    assert f"version {version}," in all_strings()["en"]["now_note"]


def test_the_english_charter_carries_the_same_version_and_date_as_the_korean_one():
    version, issued = re.search(r"^- 판: v(\d+\.\d+) \((\d{4}-\d\d-\d\d)\)$", charter(), re.M).groups()
    english = (ROOT / "docs" / "charter.en.md").read_text(encoding="utf-8")
    assert re.findall(r"^- Version: .*$", english, re.M) == [f"- Version: v{version} ({issued})"]
    for text, marker in ((charter(), f"| v{version} | {issued} |"), (english, f"| v{version} | {issued} |")):
        assert text.rstrip().splitlines()[-1].startswith(marker), "the last row of the history is not this version"


def test_the_tools_of_the_page_use_no_withdrawn_date_even_as_an_example():
    build = builder()
    for path in sorted((ROOT / "tools" / "site").rglob("*")):
        if path.suffix not in {".py", ".html", ".json", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8")
        for day in WITHDRAWN_DATES:
            assert day not in text and day[:7] not in text, f"{path.name} still writes {day[:7]}"
            assert build.spell("en", "m", day[:7]) not in text, path.name


@pytest.mark.parametrize("old,new", CHARTER_VARIANTS)
def test_a_figure_changed_by_hand_in_the_charter_is_caught(old, new):
    text = charter()
    assert text.count(old) == 1, old
    assert figure_faults(text.replace(old, new), all_strings()) != []


# One figure changed by hand in the strings of one language.
STRING_VARIANTS = [
    ("t5_today", "{m:2040-05}", "{m:2039-05}"),
    ("t5_today", "{m:2041-01}", "{m:2041-02}"),
    ("t5_w", "{d:2032-02-25}", "{d:2031-02-25}"),
    ("t1_w", "{d:2027-02-24}", "{d:2027-02-25}"),
    ("now_note", "{m:2042-10}", "{m:2042-09}"),
    ("now_note", "{d:2034-04-19}", "{d:2034-04-18}"),
    ("now_note", "{n:1200}", "{n:2000}"),
    ("now_note", "80", "90"),
    ("now_note", "15", "30"),
    ("now_cap", "3", "2"),
]


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("key,old,new", STRING_VARIANTS)
def test_a_figure_changed_by_hand_in_one_language_is_caught(lang, key, old, new):
    strings = all_strings()
    assert old in strings[lang][key], f"{lang} {key} does not carry {old}"
    strings[lang][key] = strings[lang][key].replace(old, new, 1)
    assert figure_faults(charter(), strings) != []


# What charter versions 0.8 and 0.9 withdrew: the single dates for Claude alone, for a shared account and
# for two companies, and the statement that each month with Claude alone costs about two weeks.
WITHDRAWN_DATES = ["2038-11-10", "2040-07-11", "2033-08-31"]
TWO_WEEKS = {"en": r"\b(?:two|2) weeks\b", "ko": r"(?:두|2) ?주", "ja": r"(?:2|二)週間", "es": r"\b(?:dos|2) semanas\b"}


@pytest.mark.parametrize("lang", LANGS)
def test_nothing_the_charter_withdrew_is_on_the_page(lang):
    build = builder()
    text = page(lang).text
    for day in WITHDRAWN_DATES:
        assert build.spell(lang, "d", day) not in text, day
        month = build.spell(lang, "m", day[:7])
        today_range = [build.spell(lang, "m", value) for value in charter_figures(charter())["today_median"]]
        assert month not in text or month in today_range, month
    assert not re.search(TWO_WEEKS[lang], text, re.I)
    assert not re.search(r"(?<![\d.])0\.7(?![\d.])", text), "the page still names charter version 0.7"


# The timeline is read on its own: each date there names the capacity it assumes, and the date for 1.0
# under today's conditions stands beside the plan's, as the same kind of element.
@pytest.mark.parametrize("lang", LANGS)
def test_each_date_of_the_timeline_names_its_capacity_and_1_0_has_today_s_beside_the_plan_s(lang):
    build, c = builder(), charter_figures(charter())
    strings = build.load_strings(lang)
    assert "cap_plan" in strings and "cap_today" in strings, "the capacities have no name on the page"
    plan, today = strings["cap_plan"], strings["cap_today"]
    timeline = now_section(lang).split('<ol class="timeline">', 1)[1].split("</ol>", 1)[0]
    items = re.findall(r"<li>(.*?)</li>", timeline, re.S)
    assert len(items) == 5
    dates = [re.findall(r'<span class="when"><span class="cap">(.*?)</span>(.*?)(?=<span class="(?:when|tag)"|<h3>)', item, re.S) for item in items]
    for index, found in enumerate(dates):
        assert item_has_no_bare_date(items[index]), "a date of the timeline does not name its capacity"
        assert [cap for cap, _ in found] == ([plan, today] if index == 4 else [plan]), (lang, index)
        assert build.spell(lang, "d", c["milestones"][index]) in found[0][1]
    for month in c["today_median"]:
        assert build.spell(lang, "m", month) in dates[4][1][1]
    assert plan != today and plan.strip() and today.strip()


def item_has_no_bare_date(item: str) -> bool:
    return item.count('<span class="when">') == item.count('<span class="when"><span class="cap">')



# --- the site as a whole --------------------------------------------------------------------


def test_styles_and_script_reach_no_other_host():
    for path in site_files():
        if path.suffix not in {".css", ".js", ".svg"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"@import|url\(\s*['\"]?(?:https?:)?//", text), path.name
        if path.suffix == ".js" and path.name != "analytics.js":   # the measurement script has a test of its own
            assert not re.search(r"https?://|XMLHttpRequest|sendBeacon|WebSocket|import\(", text), path.name


def test_script_is_one_small_file_of_our_own():
    assert sorted(p.name for p in site_files() if p.suffix == ".js") == ["analytics.js", "motion.js"]


def test_site_respects_reduced_motion_color_schemes_and_each_script():
    css = (SITE / "style.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in css
    assert "prefers-color-scheme: dark" in css
    assert "prefers-reduced-motion" in (SITE / "motion.js").read_text(encoding="utf-8")
    assert re.search(r":lang\(ko\)[^{]*\{[^}]*word-break:\s*keep-all", css)
    assert re.search(r":lang\(ja\)[^{]*\{[^}]*line-break:\s*strict", css)
    assert "Hiragino Sans" in css and "Apple SD Gothic Neo" in css


def test_site_has_no_contact_details_or_internal_codes():
    for path in site_files():
        if path.suffix not in {".html", ".css", ".js", ".svg"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text) == [], path
        assert re.findall(r"\b(?:DOD-\d+|[AG]\d{1,2}\b(?=[^\w%]))", text) == [], path
        assert "linear.app" not in text, path


# --- the four languages do not drift --------------------------------------------------------


def test_committed_pages_are_exactly_what_the_template_builds():
    built = builder().pages()
    assert sorted(built) == sorted([str(page_path(lang).relative_to(SITE)) for lang in LANGS] + ["sitemap.xml"])
    for name, text in built.items():
        assert (SITE / name).read_text(encoding="utf-8") == text, f"site/{name} is stale: run tools/site/build.py"


def test_strings_agree_across_languages():
    assert builder().problems() == []


def page_facts(lang: str) -> dict:
    p = page(lang)
    return {
        "ids": [a["id"] for _, a in p.tags if a.get("id")],
        "outline": [t for t, _ in p.tags if t in {"section", "h1", "h2", "h3", "li", "figure", "pre", "footer"}],
        "links": sorted(h for h in p.hrefs() if not is_local(h)),
        "counted": [a["data-count"] for _, a in p.tags if a.get("data-count")],
        "years": sorted(re.findall(r"(?<!\d)20[2-4]\d(?!\d)", p.text)),
        "percents": sorted(re.findall(r"\d+(?=\s?%)", p.text)),
    }


@pytest.mark.parametrize("lang", [lang for lang in LANGS if lang != "en"])
def test_built_page_has_the_same_sections_numbers_dates_and_links_as_english(lang):
    assert page_facts(lang) == page_facts("en")


def edited(change):
    build = builder()
    strings = copy.deepcopy({lang: build.load_strings(lang) for lang in build.LANGS})
    change(strings)
    return build.problems(strings)


def text_key(strings: dict, needle: str) -> str:
    return next(k for k, v in strings["en"].items() if isinstance(v, str) and needle in v)


def test_check_catches_a_missing_string():
    assert edited(lambda s: s["ja"].pop("lede"))


def test_check_catches_an_extra_string():
    assert edited(lambda s: s["es"].update(extra="sobra"))


def test_check_catches_a_different_number():
    def change(s):
        key = text_key(s, "70%")
        s["ko"][key] = s["ko"][key].replace("70", "80")

    assert edited(change)


SEVEN = {"en": ("7 existing languages", "seven existing languages"), "ko": ("기존 언어 7개", "기존 언어 일곱 개"),
         "ja": ("既存の7つの言語", "既存の七つの言語"), "es": ("7 lenguajes existentes", "siete lenguajes existentes")}


def test_check_catches_a_different_count():
    def change(s):
        key = text_key(s, SEVEN["en"][0])
        assert SEVEN["ja"][0] in s["ja"][key]
        s["ja"][key] = s["ja"][key].replace("7", "8", 1)

    assert edited(change)


def test_check_rejects_a_count_written_as_a_word_even_in_every_language():
    def change(s):
        key = text_key(s, SEVEN["en"][0])
        for lang, (digits, words) in SEVEN.items():
            assert digits in s[lang][key]
            s[lang][key] = s[lang][key].replace(digits, words)

    assert edited(change)


def test_check_catches_a_different_date():
    def change(s):
        key = text_key(s, "{m:2042-10}")
        s["es"][key] = s["es"][key].replace("{m:2042-10}", "{m:2041-10}")

    assert edited(change)


def test_check_catches_a_link_written_into_one_language():
    def change(s):
        s["ja"]["lede"] += ' <a href="https://example.com/">x</a>'

    assert edited(change)


# --- publishing and pointers ----------------------------------------------------------------


def test_workflow_pins_actions_and_keeps_permissions_narrow():
    text = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses
    assert [u for u in uses if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", u)] == []
    head, _, jobs = text.partition("\njobs:")
    assert "write" not in head
    blocks = re.split(r"\n  (?=[\w-]+:\n)", jobs)
    writers = [b for b in blocks if "pages: write" in b or "id-token: write" in b]
    assert len(writers) == 1
    assert "pages: write" in writers[0] and "id-token: write" in writers[0]
    assert "deploy-pages@" in writers[0]
    assert sorted(set(re.findall(r"^\s+([\w-]+): (?:read|write)$", text, re.M))) == ["contents", "id-token", "issues", "pages"]
    assert not re.search(r"secrets\.", text), "the workflow uses a stored secret"


def workflow_triggers(name: str) -> str:
    text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    return re.sub(r"(?m)^\s*#.*\n", "", text).split("\non:\n", 1)[1].split("\npermissions:", 1)[0]


def test_every_merge_to_main_and_a_fixed_interval_build_the_page_and_nothing_else_does():
    triggers = workflow_triggers("pages.yml")
    assert re.search(r"push:\s*\n\s+branches:\s*\[\s*main\s*\]", triggers)
    assert "paths" not in triggers, "a merge that does not touch site/ would not build the page"
    assert re.search(r"schedule:\s*\n\s+- cron: \"\d+ \* \* \* \*\"", triggers), "the page is not built again every hour"
    assert not re.search(r"pull_request|workflow_dispatch|workflow_run|issue", triggers), "something else can deploy the page"
    text = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    jobs = re.split(r"\n  (?=[\w-]+:\n)", text.partition("\njobs:")[2])[1:]
    assert len(jobs) == 2
    for job in jobs:
        assert "if: github.ref == 'refs/heads/main'" in job, "a job could run for something other than main"
    assert "build.py --live" in jobs[0] and "--compare" in jobs[0]
    assert "github.event_name == 'push' || needs.build.outputs.changed == 'true'" in jobs[1], \
        "the scheduled build deploys although nothing it shows has changed"
    assert "fetch-depth: 0" in jobs[0], "the merged changes cannot be read from a shallow checkout"


def test_site_check_workflow_pins_actions_and_only_reads():
    text = (ROOT / ".github" / "workflows" / "site.yml").read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*(\S+)", text)
    assert uses
    assert [u for u in uses if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", u)] == []
    assert "write" not in text
    assert "tools/site/sweep.py" in text and "--require-hashes" in text


# --- phones and foldable phones (the browser sweep in tools/site/sweep.py checks the layout) ---


@pytest.mark.parametrize("lang", LANGS)
def test_page_lets_the_layout_reach_under_cut_outs_safely(lang):
    metas = {m.get("name"): m.get("content") for m in page(lang).all("meta")}
    assert "viewport-fit=cover" in metas["viewport"]
    assert "user-scalable" not in metas["viewport"] and "maximum-scale" not in metas["viewport"]


def test_styles_are_fluid_and_touch_friendly():
    css = (SITE / "style.css").read_text(encoding="utf-8")
    for needed in ("env(safe-area-inset-left)", "env(safe-area-inset-right)", "env(safe-area-inset-top)",
                   "env(safe-area-inset-bottom)", "svh", "dvh", "horizontal-viewport-segments: 2",
                   "vertical-viewport-segments: 2", "env(viewport-segment-", "(hover: hover)"):
        assert needed in css, needed
    outside_hover = re.sub(r"@media \(hover: hover\)[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", "", css)
    assert ":hover" not in outside_hover, "hover effects belong inside @media (hover: hover)"
    assert not re.search(r"\bheight:\s*\d+px", re.sub(r"(?s)\.progress\s*\{.*?\}", "", css)), "no fixed pixel heights"
    assert not re.search(r"(?<![\w-])100vh\b(?![^;]*;\s*min-height:[^;]*svh)", css) or "100svh" in css


def test_script_lays_out_again_on_resize_and_rests_in_a_hidden_tab():
    js = (SITE / "motion.js").read_text(encoding="utf-8")
    assert "ResizeObserver" in js
    assert "visibilitychange" in js


@pytest.mark.parametrize("name", ["README.md", "README.ko.md"])
def test_readme_links_to_the_page_near_the_top_and_names_the_four_languages(name):
    head = "\n".join((ROOT / name).read_text(encoding="utf-8").splitlines()[:8])
    assert SITE_URL in head
    for language in NAMES.values():
        assert language in head
    # Every link in the README that is not to GitHub is one of the four page addresses.
    links = [u for u in re.findall(r"https?://[^\s)>\]]+", head) if "github.com" not in u]
    assert links
    assert [u for u in links if u not in {page_url(lang) for lang in LANGS}] == []


def test_site_check_runs_on_every_push_and_pull_request():
    text = (ROOT / ".github" / "workflows" / "site.yml").read_text(encoding="utf-8")
    head = text.partition("\njobs:")[0]
    assert "paths" not in head and "branches" not in head
    assert re.search(r"\n  push:", head) and re.search(r"\n  pull_request:", head)


def test_only_two_readmes_exist():
    assert sorted(p.name for p in ROOT.glob("README*")) == ["README.ko.md", "README.md"]


# --- the page follows the project by itself --------------------------------------------------
#
# The section about now holds no value of its own. The build computes a status from the charter, the
# tasks, the history of main and GitHub's count of open issues, writes it into the pages, into the
# status file an open page reads again, and into the guide for AI agents.

NOW = dt.datetime(2026, 12, 1, 8, 30, tzinfo=dt.timezone.utc)
SAMPLE = ROOT / "tools" / "site" / "sample_status.json"
MARKUP = '<img src=x onerror=alert(1)> & "quoted" </a><script>alert(2)</script>'
STATUS_FIELDS = {"version", "built", "charter", "step", "tasks", "open_issues", "changes", "digest"}


def need(name: str):
    build = builder()
    assert hasattr(build, name), f"the build script has no {name}"
    return getattr(build, name)


def rendered(lang: str, status: dict | None = None) -> str:
    """The page of one language, built with a status."""
    render = need("render")
    assert "status" in render.__code__.co_varnames[:render.__code__.co_argcount], "the pages cannot be built with a status"
    return render(lang, status=status)


def made_status(text: str | None = None, now: dt.datetime = NOW, issues: int = 2, ready: int = 1, changes=()) -> dict:
    return need("status")(now=now, issues=issues, charter=charter() if text is None else text, changes=list(changes), ready=ready)


def sample() -> dict:
    assert SAMPLE.is_file()
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def published(tmp_path, status: dict | None = None) -> Path:
    need("publish")(tmp_path / "out", status or sample())
    return tmp_path / "out"


def live_section(html_text: str) -> str:
    assert 'id="live"' in html_text, "the page has no section about now"
    return html_text.split('id="live"', 1)[1].split('<div class="marquee', 1)[0]


def test_the_status_is_what_the_charter_says_about_now():
    c, status = charter_figures(charter()), made_status()
    assert set(status) == STATUS_FIELDS and status["version"] == 1
    assert status["built"] == "2026-12-01T08:30:00Z"
    assert status["charter"] == {"version": c["version"], "date": c["issued"]}
    gate = re.search(r"방향 고정 관문은 시범에서 잰 값을 넣으면 지금 조건에서 가운데 (\d{4}-\d\d-\d\d), .*?닿을 확률 (\d+)%", charter())
    assert status["step"] == {"index": 1, "of": 5, "gate_plan": c["milestones"][0], "gate_today": gate.group(1),
                              "gate_today_chance": int(gate.group(2))}
    assert status["tasks"] == {"ready": 1, "first": int(c["tasks_first"])}
    assert status["open_issues"] == 2 and status["changes"] == []


STATUS_VARIANTS = [
    ("- 판: v0.10 (2026-11-18)", "- 판: v0.11 (2026-12-24)", lambda s: (s["charter"]["version"], s["charter"]["date"]), ("0.11", "2026-12-24")),
    ("~ 2027년 2월 24일 | 방향 고정", "~ 2027년 3월 31일 | 방향 고정", lambda s: (s["step"]["gate_plan"], s["step"]["gate_today"]), ("2027-03-31", None)),
    ("지금 조건에서 가운데 2027-06-23,", "지금 조건에서 가운데 2027-08-04,", lambda s: s["step"]["gate_today"], "2027-08-04"),
    ("(2027-02-24까지 닿을 확률 4%)", "(2027-02-24까지 닿을 확률 9%)", lambda s: s["step"]["gate_today_chance"], 9),
    ("벤치마크 과제 30개(웹", "벤치마크 과제 40개(웹", lambda s: s["tasks"]["first"], 40),
]


@pytest.mark.parametrize("old,new,read,want", STATUS_VARIANTS)
def test_a_value_changed_in_the_charter_changes_the_status(old, new, read, want):
    text = charter()
    assert text.count(old) == 1, old
    assert read(made_status(text.replace(old, new))) == want
    assert read(made_status()) != want


def test_the_step_is_the_one_the_day_of_the_build_falls_in():
    c = charter_figures(charter())
    later = made_status(now=dt.datetime(2027, 3, 1, tzinfo=dt.timezone.utc))
    assert later["step"] == {"index": 2, "of": 5, "gate_plan": c["milestones"][1], "gate_today": None, "gate_today_chance": None}
    last_day = made_status(now=dt.datetime.fromisoformat(c["milestones"][0] + "T23:59:00+00:00"))
    assert last_day["step"]["index"] == 1
    assert made_status(now=dt.datetime(2040, 1, 1, tzinfo=dt.timezone.utc))["step"]["index"] == 5


def test_benchmark_tasks_ready_are_counted_in_the_repository(tmp_path):
    count = need("tasks_ready")
    for name in ("a", "b", "notes"):
        (tmp_path / name).mkdir()
    (tmp_path / "a" / "task.toml").write_text("")
    (tmp_path / "b" / "task.toml").write_text("")
    (tmp_path / "README.md").write_text("")
    assert count(tmp_path) == 2
    real = sum(1 for path in (ROOT / "bench" / "tasks").iterdir() if (path / "task.toml").is_file())
    assert count() == real and real >= 1


LOG = "\x1e".join([
    "2026-11-18T03:33:45+09:00\x1fMerge pull request #12 from someone/branch-name\x1fRecord the pilot measurement\n\nlonger text",
    "2026-11-17T10:00:00+00:00\x1fBench: a squashed change (#11)\x1fbody",
    "2026-11-17T09:00:00+00:00\x1fA commit pushed without a pull request\x1f",
    "2026-11-16T09:00:00+00:00\x1fMerge pull request #9 from outside/fork\x1f" + MARKUP + "\n",
    "2026-11-15T09:00:00+00:00\x1fMerge pull request #8 from a/b\x1f  spaced \t and\x07 controlled  ",
    "2026-11-14T09:00:00+00:00\x1fMerge pull request #7 from a/b\x1fseven",
    "2026-11-13T09:00:00+00:00\x1fMerge pull request #6 from a/b\x1fsix",
    "2026-11-12T09:00:00+00:00\x1fMerge pull request #5 from a/b\x1ffive",
]) + "\x1e"


def test_changes_are_the_pull_requests_merged_to_main_without_their_authors():
    changes = need("changes_from_log")(LOG)
    assert len(changes) == 5, "more or fewer than the latest 5 changes"
    assert [set(change) for change in changes] == [{"date", "title", "url"}] * 5
    assert changes[0] == {"date": "2026-11-17", "title": "Record the pilot measurement", "url": f"{REPO_URL}/pull/12"}   # the day in UTC
    assert changes[1] == {"date": "2026-11-17", "title": "Bench: a squashed change", "url": f"{REPO_URL}/pull/11"}
    assert changes[2]["title"] == MARKUP and changes[2]["url"] == f"{REPO_URL}/pull/9"
    assert changes[3]["title"] == "spaced and controlled"
    text = json.dumps(changes)
    assert "someone" not in text and "outside/fork" not in text and "without a pull request" not in text


LIVE_KEYS = ["live_h", "live_p", "live_charter", "live_step", "live_gate_plan", "live_gate_today", "live_tasks", "live_issues",
             "live_built", "changes_h", "changes_note"]


@pytest.mark.parametrize("lang", LANGS)
def test_no_figure_in_the_section_about_now_is_typed_by_hand(lang):
    build = builder()
    strings = build.load_strings(lang)
    template = (ROOT / "tools" / "site" / "template.html").read_text(encoding="utf-8")
    block = live_section(template)
    assert sorted(re.findall(r"\{\{([a-z0-9_]+)\}\}", block)) == sorted(LIVE_KEYS + ["root", "repo", "live_hidden", "gate_today_hidden", "changes"])
    assert not re.search(r"\d", re.sub(r"<h[34]>|</h[34]>", "", block)), "a figure is typed into the template"
    for key in LIVE_KEYS:
        assert key in strings, f"{lang}: the sentence {key} is missing"
        assert not re.search(r"\d", strings[key]), f"{lang} {key}: a figure is typed by hand"

    def change(s):
        s[lang]["live_tasks"] = s[lang]["live_tasks"].replace("[tasks.first]", "30")

    assert edited(change), "a figure typed in place of a value of the status is not caught"


@pytest.mark.parametrize("lang", LANGS)
def test_the_section_about_now_shows_the_status_in_each_language(lang):
    build, status = builder(), sample()
    html_text = rendered(lang, status=status)
    block = live_section(html_text)
    assert " hidden" not in block.split(">", 1)[0]
    text = Page("<main><div " + block).text
    for day in (status["charter"]["date"], status["step"]["gate_plan"], status["step"]["gate_today"]):
        assert build.spell(lang, "d", day) in text, day
    assert build.spell(lang, "d", status["built"][:10]) + " 09:07 UTC" in text
    names = {name: value for name, value in re.findall(r'<span data-live="([a-z_.]+)">([^<]*)</span>', block)}
    assert names == {"charter.version": "0.10", "step.gate_today_chance": "4", "tasks.ready": "12", "tasks.first": "30", "open_issues": "3"}
    assert build.load_strings(lang)["t1_h"] in text
    for change in status["changes"]:
        assert build.spell(lang, "d", change["date"]) in text and change["url"] in block
    without = live_section(rendered(lang))
    assert without.split(">", 1)[0].endswith(" hidden"), "a page built without a status shows an empty section"
    assert re.findall(r'<span data-live="[a-z_.]+">([^<]*)</span>', without) == [""] * 5 and "<li><time" not in without
    later = dict(status, step=dict(status["step"], index=2, gate_today=None, gate_today_chance=None))
    assert '<li data-live-show="step.gate_today" hidden>' in rendered(lang, status=later)
    assert build.load_strings(lang)["t2_h"] in Page("<main><div " + live_section(rendered(lang, status=later))).text


@pytest.mark.parametrize("lang", LANGS)
def test_a_title_that_carries_markup_reaches_the_page_as_text(lang):
    status = sample()
    assert status["changes"][0]["title"].endswith(MARKUP)
    html_text = rendered(lang, status=status)
    block = live_section(html_text)
    assert "<img" not in block and "<script" not in block and "onerror=alert(1)>" not in block
    assert "&lt;img src=x onerror=alert(1)&gt; &amp; &quot;quoted&quot; &lt;/a&gt;&lt;script&gt;" in block
    shown = Page("<main><div " + block)
    assert MARKUP in shown.text and "img" not in [tag for tag, _ in shown.tags]
    bad = dict(status, changes=[{"date": "2027-01-04", "title": "x", "url": 'https://example.com/"><b>'}])
    assert '"><b>' not in live_section(rendered(lang, status=bad))


@pytest.mark.parametrize("lang", [lang for lang in LANGS if lang != "en"])
def test_other_languages_say_that_titles_are_shown_in_english(lang):
    word = {"ko": "영어", "ja": "英語", "es": "inglés"}[lang]
    assert word in builder().load_strings(lang).get("changes_note", "")
    links = re.findall(r"<a [^>]*>", live_section(rendered(lang, status=sample())).split('class="changes"', 1)[1])
    assert links and all('lang="en"' in link for link in links)


def test_an_open_page_reads_the_status_file_from_its_own_address_and_sets_text_only():
    script = (SITE / "motion.js").read_text(encoding="utf-8")
    assert len(re.findall(r"\bfetch\(", script)) == 1, "the script must read one thing: the status file"
    assert re.search(r"\.fetch\(\s*box\.getAttribute\(\"data-status\"\)", script)
    assert "setInterval(load" in script, "an open page does not read the status again"
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function", script), "the script sets markup"
    assert "textContent" in script and 'credentials: "omit"' in script
    for lang in LANGS:
        address = re.search(r'id="live" data-status="([^"]+)"', page_path(lang).read_text(encoding="utf-8"))
        assert address and is_local(address.group(1)) and address.group(1).endswith("status.json")
        assert (page_path(lang).parent / address.group(1)).resolve() == (SITE / "status.json").resolve()


def test_the_published_build_writes_the_pages_the_status_file_and_the_guide(tmp_path):
    out, status = published(tmp_path), sample()
    assert json.loads((out / "status.json").read_text(encoding="utf-8")) == status
    assert (out / "status.json").stat().st_size < 4096, "the status file is not small"
    for lang in LANGS:
        name = "index.html" if lang == "en" else f"{lang}/index.html"
        assert (out / name).read_text(encoding="utf-8") == rendered(lang, status=status)
    for name in ("style.css", "motion.js", "favicon.svg", "sitemap.xml", "llms.txt") + tuple(f"card-{lang}.png" for lang in LANGS):
        assert (out / name).is_file(), name
    assert not list(out.rglob("__pycache__"))


def test_the_scheduled_build_deploys_only_when_what_the_page_shows_has_changed():
    digest = need("inputs_digest")
    first = made_status()
    assert first["digest"] == made_status(now=NOW + dt.timedelta(hours=5))["digest"], "the time of the build alone changes the digest"
    assert first["digest"] != made_status(issues=3)["digest"]
    assert first["digest"] != made_status(ready=2)["digest"]
    assert first["digest"] != made_status(changes=[{"date": "2026-11-30", "title": "t", "url": f"{REPO_URL}/pull/1"}])["digest"]
    assert first["digest"] != made_status(now=dt.datetime(2027, 3, 1, tzinfo=dt.timezone.utc))["digest"]
    assert digest(first) == first["digest"] and re.fullmatch(r"[0-9a-f]{64}", first["digest"])


def test_working_rules_say_the_page_changes_with_what_it_says():
    rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    line = next((l for l in rules.splitlines() if "introduction page" in l), "")
    assert "same pull request" in line and "four languages" in line


# --- search engines -----------------------------------------------------------------------------


def test_sitemap_lists_the_four_pages_with_their_language_alternates():
    path = SITE / "sitemap.xml"
    assert path.is_file(), "there is no sitemap"
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9", "x": "http://www.w3.org/1999/xhtml"}
    urls = ET.fromstring(path.read_text(encoding="utf-8")).findall("s:url", ns)
    assert [u.find("s:loc", ns).text for u in urls] == [page_url(lang) for lang in LANGS]
    for u in urls:
        found = {a.get("hreflang"): a.get("href") for a in u.findall("x:link", ns)}
        assert found == {lang: page_url(lang) for lang in LANGS} | {"x-default": SITE_URL}


def png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG image"
    return struct.unpack(">II", head[16:24])


@pytest.mark.parametrize("lang", LANGS)
def test_shared_page_shows_a_title_a_description_and_an_image_of_the_usual_size(lang):
    metas = {m.get("name") or m.get("property"): m.get("content") or "" for m in page(lang).all("meta")}
    assert metas.get("og:image") == f"{SITE_URL}card-{lang}.png"
    image = SITE / f"card-{lang}.png"
    assert image.is_file(), "the image shown when the page is shared is missing"
    assert png_size(image) == (1200, 630) and image.stat().st_size < 300 * 1024
    assert (metas.get("og:image:width"), metas.get("og:image:height")) == ("1200", "630")
    assert metas.get("twitter:card") == "summary_large_image" and metas.get("og:image:alt") == metas["og:title"]
    assert metas["og:title"] and metas["og:description"] and metas["og:url"] == page_url(lang)


def visible_questions(lang: str) -> list[tuple[str, str]]:
    html_text = page_path(lang).read_text(encoding="utf-8")
    assert 'id="faq"' in html_text, "the page has no questions and answers"
    section = html_text.split('id="faq"', 1)[1].split("</section>", 1)[0]
    pairs = re.findall(r"<div><h3>(.*?)</h3><p>(.*?)</p></div>", section, re.S)
    return [(Page("<main>" + q).text.strip(), Page("<main>" + a).text.strip()) for q, a in pairs]


def structured(lang: str) -> dict:
    blocks = page(lang).data_blocks
    assert len(blocks) == 1, "the page has no structured data"
    assert "</" not in blocks[0]
    return {node["@type"]: node for node in json.loads(blocks[0])["@graph"]}


@pytest.mark.parametrize("lang", LANGS)
def test_structured_data_says_what_the_visible_page_says(lang):
    p, data = page(lang), structured(lang)
    metas = {m.get("name") or m.get("property"): m.get("content") or "" for m in p.all("meta")}
    assert data["WebPage"]["name"] == p.title and data["WebPage"]["description"] == metas["description"]
    assert data["WebPage"]["url"] == page_url(lang) and data["WebPage"]["inLanguage"] == lang
    assert data["WebPage"]["primaryImageOfPage"] == metas["og:image"]
    assert data["WebSite"]["url"] == SITE_URL and data["WebSite"]["name"] == "Dodam"
    project = data["SoftwareSourceCode"]
    assert project["name"] == "Dodam" and project["codeRepository"] == REPO_URL
    words = lambda text: " ".join(text.split())
    assert words(project["description"]) in words(p.text), "the project's description is not on the page"
    assert words(project["creativeWorkStatus"]) in words(p.text)
    asked = [(q["name"], q["acceptedAnswer"]["text"]) for q in data["FAQPage"]["mainEntity"]]
    assert [(words(q), words(a)) for q, a in asked] == [(words(q), words(a)) for q, a in visible_questions(lang)]
    assert not re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", json.dumps(data).replace("@type", "").replace("@id", "").replace("@context", "").replace("@graph", ""))


# --- answer engines -----------------------------------------------------------------------------

SENTENCE_END = r"[.!?](?=\s|$)|[。？]"


@pytest.mark.parametrize("lang", LANGS)
def test_questions_have_short_answers_that_can_be_quoted_alone(lang):
    pairs = visible_questions(lang)
    assert len(pairs) == 6
    for question, answer in pairs:
        assert "Dodam" in question and "Dodam" in answer, question
        sentences = len(re.findall(SENTENCE_END, re.sub(r"\b(?:AGENTS\.md|\d\.\d)", "x", answer)))
        assert 2 <= sentences <= 3, (question, sentences)
    strings = builder().load_strings(lang)
    ready = strings["a5"].lower()
    assert "{d:" in strings["a5"] and "{m:" in strings["a5"]
    if lang == "en":
        assert "plan’s capacity" in ready and "today’s conditions" in ready and "not a promise" in ready
        assert ready.count("plan’s capacity") == 2, "a date in the answer does not name the capacity it assumes"


@pytest.mark.parametrize("lang", LANGS)
def test_the_first_sentence_of_the_page_says_what_dodam_is(lang):
    strings = builder().load_strings(lang)
    first = re.split(SENTENCE_END, strings["lede"], maxsplit=1)[0]
    assert first.startswith("Dodam") and len(first) > 30, "the first sentence does not stand alone"
    body = page_path(lang).read_text(encoding="utf-8").split("<main>", 1)[1]
    paragraph = re.search(r"<p[^>]*>(?!<)([^<]+)", body.split("</h1>", 1)[1]).group(1)
    assert paragraph.startswith(first), "the first sentence after the headline is not the one that says what Dodam is"


# --- language models and AI agents ------------------------------------------------------------


def test_the_guide_for_agents_says_what_the_page_and_the_status_say(tmp_path):
    build, status = builder(), sample()
    out = published(tmp_path)
    guide = (out / "llms.txt").read_text(encoding="utf-8")
    en = build.load_strings("en")
    assert guide.startswith("# Dodam\n\n> ")
    assert Page("<main>" + build.expand("en", en["description"])).text.strip() in guide
    for day in (status["charter"]["date"], status["step"]["gate_plan"], status["step"]["gate_today"]):
        assert build.spell("en", "d", day) in guide, day
    for fact in (f"version {status['charter']['version']}", f"{status['tasks']['ready']} of the first {status['tasks']['first']}",
                 f"outside contributors: {status['open_issues']}.", f"{status['step']['gate_today_chance']}%", en["t1_h"],
                 "January 5, 2027 09:07 UTC", f"step {status['step']['index']} of {status['step']['of']}"):
        assert fact in guide, fact
    for change in status["changes"]:
        assert change["title"] in guide and change["url"] in guide
    for i in range(1, 8):   # the rules are stated in the guide itself, in the page's own words
        assert en[f"rule{i}_h"] in guide and en[f"rule{i}_p"] in guide, i
    for question, answer in visible_questions("en"):
        assert question in guide and answer in guide
    for address in (f"{REPO_URL}/blob/main/docs/charter.ko.md", f"{REPO_URL}/blob/main/docs/charter.en.md", f"{REPO_URL}/blob/main/AGENTS.md",
                    f"{REPO_URL}/issues?q=is%3Aopen+label%3Aai-welcome", f"{SITE_URL}status.json"):
        assert address in guide, address
    later = dict(status, tasks={"ready": 13, "first": 30}, open_issues=7)
    assert "13 of the first 30" in build.guide(later) and "outside contributors: 7." in build.guide(later)


def test_the_guide_describes_every_field_of_the_status_file():
    status, guide = sample(), need("guide")(sample())
    assert set(status) == STATUS_FIELDS
    for name in ("version", "built", "charter.version", "charter.date", "step.index", "step.of", "step.gate_plan", "step.gate_today",
                 "step.gate_today_chance", "tasks.ready", "tasks.first", "open_issues", "changes", "digest"):
        assert re.search(rf"(?m)^- (?:[\w., ]*, )?{re.escape(name)}[,:]", guide), f"the guide does not describe {name}"
    assert f"version {status['version']} of the file" in guide


@pytest.mark.parametrize("lang", LANGS)
def test_pages_point_to_the_guide_for_agents(lang):
    links = [a for a in page(lang).all("link") if a.get("rel") == "alternate" and a.get("type") == "text/plain"]
    assert len(links) == 1 and links[0].get("title")
    target = (page_path(lang).parent / links[0]["href"]).resolve()
    assert target == (SITE / "llms.txt").resolve()


def test_what_is_published_holds_no_personal_details_and_no_internal_codes(tmp_path):
    out = published(tmp_path, made_status(changes=need("changes_from_log")(LOG)))
    for path in sorted(out.rglob("*")):
        if path.suffix not in {".html", ".txt", ".json", ".xml"}:
            continue
        text = path.read_text(encoding="utf-8")
        text = re.sub(r'"@(?:type|id|context|graph)"', "", text)
        assert re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text) == [], path.name
        assert re.findall(r"\bDOD-\d+", text) == [] and "linear.app" not in text, path.name
        assert not re.search(r"/(?:Users|home)/[\w.-]+/", text), path.name
    status = json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert all(set(change) == {"date", "title", "url"} for change in status["changes"]), "the status file names an author"


# --- measurement, only with the visitor's agreement --------------------------------------------
#
# The owner decided on 2026-11-18 that the page is measured with Google Analytics. The measurement ID
# is one setting and it is empty until the owner has created the property. While it is empty the page
# has no analytics code and reaches no other host. When it is set, nothing is loaded before the visitor
# agrees. What happens in the browser (asking first, declining, the browser's refusing signals) is
# checked by tools/site/sweep.py --consent.

SAMPLE_ID = "G-SAMPLE0000"
GOOGLE_NEEDS = {   # from Google's guide to content security policies for the Google tag, without advertising
    "script-src": {"https://www.googletagmanager.com"},
    "img-src": {"https://www.googletagmanager.com", "https://*.google-analytics.com"},
    "connect-src": {"https://www.googletagmanager.com", "https://*.google-analytics.com", "https://*.google.com"},
}
CONSENT_KEYS = ["consent_label", "consent_text", "consent_yes", "consent_no", "consent_settings", "measure_p"]


def policy_of(html_text: str) -> dict:
    metas = [a for tag, a in Page(html_text).tags if tag == "meta" and (a.get("http-equiv") or "").lower() == "content-security-policy"]
    assert len(metas) == 1, "the page declares no content security policy"
    return {words[0]: set(words[1:]) for words in (part.split() for part in metas[0]["content"].split(";")) if words}


def other_hosts(policy: dict) -> dict:
    found = {name: {word for word in words if not word.startswith("'") and word != "data:"} for name, words in policy.items()}
    return {name: words for name, words in found.items() if words}


def measured(lang: str) -> str:
    render = need("render")
    assert "analytics" in render.__code__.co_varnames[:render.__code__.co_argcount], "the pages cannot be built with a measurement ID"
    return render(lang, analytics=SAMPLE_ID)


MEASUREMENT_ID = "G-DHJKG68707"   # the owner's property, created on 2026-11-18


def unmeasured(lang: str) -> str:
    render = need("render")
    assert "analytics" in render.__code__.co_varnames[:render.__code__.co_argcount], "the pages cannot be built with a measurement ID"
    return render(lang, analytics="")


def test_the_measurement_id_is_one_setting_and_the_tag_is_loaded_by_it():
    build = builder()
    assert hasattr(build, "ANALYTICS_ID"), "the build script has no setting for the measurement ID"
    assert build.ANALYTICS_ID == MEASUREMENT_ID, "the setting is not the owner's property"
    assert (ROOT / "tools" / "site" / "build.py").read_text(encoding="utf-8").count(MEASUREMENT_ID) == 1, "the ID is set in more than one place"
    for folder in ("tools/site", "site", ".github"):
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".html", ".js", ".json", ".yml", ".txt", ".css"}:
                text = path.read_text(encoding="utf-8")
                assert set(re.findall(r"\bG-[A-Z0-9]{6,}\b", text)) <= {SAMPLE_ID, MEASUREMENT_ID}, f"{path.name} holds another measurement ID"
                # A container can be given other tags later; the page promises measurement without advertising.
                assert not re.search(r"\bGTM-[A-Z0-9]+|gtm\.js|googletagmanager\.com/ns", text), f"{path.name} uses a container"
    for lang in LANGS:
        scripts = [a for a in page(lang).all("script") if a.get("src")]
        assert [a.get("data-id") for a in scripts if a["src"].endswith("analytics.js")] == [MEASUREMENT_ID], lang


@pytest.mark.parametrize("lang", LANGS)
def test_without_a_measurement_id_the_page_has_no_analytics_code_and_may_reach_no_other_host(lang):
    html_text = unmeasured(lang)
    assert not re.search(r"google|gtag|analytics|consent|cookie|dataLayer", html_text, re.I), "the page carries analytics code"
    assert [a.get("src") for a in Page(html_text).all("script") if a.get("src")] == [("" if lang == "en" else "../") + "motion.js"]
    policy = policy_of(html_text)
    assert policy["default-src"] == {"'self'"} and other_hosts(policy) == {}
    assert not re.search(r"google|gtag|dataLayer", (SITE / "motion.js").read_text(encoding="utf-8"), re.I)


def test_what_is_published_carries_the_measurement_script_only_with_an_id(tmp_path):
    publish = need("publish")
    assert "analytics" in publish.__code__.co_varnames[:publish.__code__.co_argcount], "what is published cannot be built with a measurement ID"
    publish(tmp_path / "plain", sample(), analytics="")
    assert not (tmp_path / "plain" / "analytics.js").exists(), "the measurement script is published although nothing uses it"
    publish(tmp_path / "measured", sample(), analytics=SAMPLE_ID)
    assert (tmp_path / "measured" / "analytics.js").is_file()
    assert SAMPLE_ID in (tmp_path / "measured" / "index.html").read_text(encoding="utf-8")
    publish(tmp_path / "set", sample())
    assert (tmp_path / "set" / "analytics.js").is_file() and MEASUREMENT_ID in (tmp_path / "set" / "index.html").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        publish(tmp_path / "wrong", sample(), analytics='G-X"><script>')


@pytest.mark.parametrize("lang", LANGS)
def test_with_a_measurement_id_the_policy_names_the_page_and_google_s_measurement_and_nothing_else(lang):
    html_text = measured(lang)
    p = Page(html_text)
    assert other_hosts(policy_of(html_text)) == GOOGLE_NEEDS
    assert policy_of(html_text)["default-src"] == {"'self'"}
    scripts = [a for a in p.all("script") if a.get("src")]
    assert [is_local(a["src"]) for a in scripts] == [True, True] and scripts[1]["src"].endswith("analytics.js")
    assert scripts[1].get("data-id") == SAMPLE_ID
    assert p.inline_script.strip() == "", "the page has an inline script"
    assert [name for _, attrs in p.tags for name in attrs if name.startswith("on")] == [], "the page has an inline handler"
    outside_policy = re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>', "", html_text)
    assert not re.search(r"googletagmanager|google-analytics|gtag", outside_policy), "the page names Google's hosts outside its policy"


@pytest.mark.parametrize("lang", LANGS)
def test_with_a_measurement_id_the_page_asks_in_the_visitor_s_language_and_says_what_is_measured(lang):
    strings = builder().load_strings(lang)
    for key in CONSENT_KEYS:
        assert strings.get(key, "").strip(), f"{lang}: the sentence {key} is missing"
    assert "Google Analytics" in strings["consent_text"] and "Google Analytics" in strings["measure_p"]
    html_text = measured(lang)
    bar = re.search(r'<div class="consent" id="consent"[^>]*>(.*?)</div>\s*</div>', html_text, re.S)
    assert bar and " hidden" in html_text.split('id="consent"', 1)[1].split(">", 1)[0], "the bar must start hidden: the script shows it"
    buttons = re.findall(r'<button type="button" class="([^"]*)" data-consent="(yes|no)">([^<]+)</button>', bar.group(1))
    assert [(kind, text) for _, kind, text in buttons] == [("yes", strings["consent_yes"]), ("no", strings["consent_no"])]
    assert buttons[0][0] == buttons[1][0], "agree and decline are not the same kind of button"
    shown = " ".join(Page(html_text).text.split())
    for key in ("consent_text", "measure_p", "consent_settings"):
        assert " ".join(strings[key].split()) in shown, key
    assert re.search(r'<button[^>]* data-consent-open hidden>', html_text.split("<footer", 1)[1]), "the footer has no way to change the choice"


def test_the_measurement_script_loads_nothing_by_itself_and_no_advertising_features():
    path = SITE / "analytics.js"
    assert path.is_file(), "the measurement script is not among the files of the site"
    script = path.read_text(encoding="utf-8")
    code = re.sub(r"(?m)^\s*//.*$", "", script)
    assert re.findall(r"https?://[^\"' ]+", code) == ["https://www.googletagmanager.com/gtag/js?id="]
    for signal in ("globalPrivacyControl", "doNotTrack"):
        assert signal in code, f"a browser's {signal} signal is not read"
    for off in ('ad_storage: "denied"', 'ad_user_data: "denied"', 'ad_personalization: "denied"', "allow_google_signals: false",
                "allow_ad_personalization_signals: false"):
        assert off in code, off
    assert "localStorage" in code, "the choice is not remembered on the visitor's device"
    assert not re.search(r"innerHTML|document\.write|\beval\(", code)


def test_sharing_images_were_drawn_from_the_headline_status_line_and_colors_of_now():
    """The images are drawn in a browser and committed. What each was drawn from is recorded beside them,
    so a changed headline, status line or color without a new image fails here."""
    source = need("card_source")
    record = ROOT / "tools" / "site" / "cards.json"
    assert record.is_file(), "what the sharing images were drawn from is not recorded"
    drawn = json.loads(record.read_text(encoding="utf-8"))
    for lang in LANGS:
        assert drawn.get(lang) == source(lang), f"{lang}: the sharing image is older than its headline; run tools/site/card.py"
    strings = builder().load_strings("en")
    assert source("en", dict(strings, h1=strings["h1"][:-1] + ["*slow."])) != source("en")
    assert source("en", dict(strings, status="Released")) != source("en")


# What the owner's property collects for a visitor who agrees (as set on 2026-11-18, with the collection
# of detailed location and device data switched off): the paragraph on the page names each item, the
# cookies that agreeing sets, how to change the choice, and where the choice itself is kept.
MEASURED_WORDS = {
    "en": ["only for visitors who agree", "which pages are opened", "how long a page stays active in the foreground", "end of a page",
           "links that leave the site", "browser", "operating system", "language", "kind of device", "country and region",
           "derives from the IP address without storing the address", "where the visit came from",
           "first-party cookies with a random identifier and the state of the session", "returning browser",
           "change your choice below at any time", "stops the measurement and removes those cookies", "Advertising features are off",
           "nothing is loaded from Google and no cookie is set", "remembered in the browser’s storage on your device"],
    "ko": ["동의한 방문자에 한해", "어떤 페이지를 열었는지", "얼마나 오래", "끝까지", "바깥으로 나가는 링크", "브라우저", "운영체제", "언어", "기기 종류",
           "나라와 지역", "IP 주소에서 알아내며 주소는 저장하지 않는다", "어디를 거쳐", "무작위 식별자와 세션 상태", "다시 온 브라우저",
           "아래에서 언제든 바꿀 수 있고", "측정이 멈추고 그 쿠키가 지워진다", "광고 기능은 꺼져", "쿠키도 만들지 않는다", "브라우저 저장소에 기억된다"],
    "ja": ["同意した訪問者についてのみ", "開かれたページ", "前面で表示されていた時間", "最後までスクロール", "サイトの外へ出るリンク", "ブラウザ", "OS", "言語",
           "端末の種類", "国と地域", "IPアドレスから割り出し、アドレス自体は保存しません", "どこから来たか", "ランダムな識別子とセッションの状態", "再訪したブラウザ",
           "いつでも変更でき", "計測が止まり、そのCookieは削除されます", "広告機能はオフ", "Cookieも設定しません", "ブラウザのストレージに記憶されます"],
    "es": ["solo de quienes aceptan", "qué páginas se abren", "cuánto tiempo permanece una página activa en primer plano", "hasta el final de una página",
           "enlaces que salen del sitio", "navegador", "sistema operativo", "idioma", "tipo de dispositivo", "país y región",
           "deduce de la dirección IP sin guardarla", "de dónde llegó la visita", "identificador aleatorio y el estado de la sesión",
           "navegador que vuelve", "cambiar su elección más abajo en cualquier momento", "detiene la medición y elimina esas cookies",
           "funciones publicitarias están desactivadas", "ni se guarda ninguna cookie", "almacenamiento del navegador de su dispositivo"],
}
# Switched off in the property, so the page must not claim them: city, coordinates, screen size, device model.
NOT_MEASURED = {"en": r"\bcity\b|screen|model|coordinates", "ko": r"도시|화면 크기|해상도|모델|좌표", "ja": r"都市|市区町村|画面|解像度|機種|座標",
                "es": r"ciudad|pantalla|resolución|modelo|coordenadas"}


@pytest.mark.parametrize("lang", LANGS)
def test_the_paragraph_says_what_the_property_measures(lang):
    paragraph = builder().load_strings(lang)["measure_p"]
    for words in MEASURED_WORDS[lang]:
        assert words in paragraph, f"{lang}: the paragraph does not say “{words}”"
    assert not re.search(NOT_MEASURED[lang], paragraph, re.I), f"{lang}: the paragraph names something the property does not collect"
    assert " ".join(paragraph.split()) in " ".join(page(lang).text.split()), "the published page does not carry the paragraph"
    footer = page_path(lang).read_text(encoding="utf-8").split("<footer", 1)[1]
    assert footer.index("data-consent-open") > footer.index(paragraph[:20]), "the way to change the choice is not below the paragraph"


def test_japanese_writes_browser_one_way():
    text = json.dumps(builder().load_strings("ja"), ensure_ascii=False)
    assert "ブラウザ" in text and "ブラウザー" not in text


def test_agreeing_again_resumes_measurement_and_a_second_run_of_the_script_does_nothing():
    """What the script does in a browser is checked by tools/site/sweep.py --consent; this pins its shape."""
    script = (SITE / "analytics.js").read_text(encoding="utf-8")
    measure = script.split("function measure()", 1)[1].split("\n  }\n", 1)[0]
    assert measure.index('window["ga-disable-" + id] = false') < measure.index("if (loaded) return"), \
        "agreeing again after declining leaves the measurement switched off until a reload"
    start = script.split("function start()", 1)[1]
    assert re.search(r"if \([^)]*data-measure[^)]*\) return;", start) and 'setAttribute("data-measure"' in start, \
        "the script would attach its handlers twice if it were evaluated twice"


def test_working_rules_say_what_the_public_page_may_contact():
    rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    line = next((l for l in rules.splitlines() if "no other host" in l), "")
    for words in ("Before the visitor agrees", "no cookie", "without advertising features", "owner's decision"):
        assert words in line, words


class Answer:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return self.body


def digest_when(monkeypatch, outcome):
    import urllib.error
    import urllib.request

    def fake(request, timeout=None):
        assert request.full_url == SITE_URL + "status.json"
        if isinstance(outcome, Exception):
            raise outcome
        return Answer(outcome)

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return need("published_digest")()


def test_only_a_missing_status_file_counts_as_nothing_published(monkeypatch):
    import urllib.error

    assert digest_when(monkeypatch, b'{"version": 1, "digest": "abc"}') == "abc"
    missing = urllib.error.HTTPError(SITE_URL + "status.json", 404, "Not Found", None, None)
    assert digest_when(monkeypatch, missing) is None
    for fault in (urllib.error.HTTPError(SITE_URL + "status.json", 500, "Server Error", None, None),
                  urllib.error.HTTPError(SITE_URL + "status.json", 403, "Forbidden", None, None),
                  urllib.error.URLError("no route"), TimeoutError("timed out"), b"<html>not a status</html>", b'{"version": 1}'):
        with pytest.raises(Exception):
            digest_when(monkeypatch, fault)   # the run fails and nothing is deployed, instead of deploying every hour unseen


@pytest.mark.parametrize("lang", LANGS)
def test_sharing_locale_is_a_language_with_its_territory(lang):
    metas = {m.get("property"): m.get("content") for m in page(lang).all("meta") if m.get("property")}
    assert metas.get("og:locale") == {"en": "en_US", "ko": "ko_KR", "ja": "ja_JP", "es": "es_ES"}[lang]


def test_rules_the_guide_takes_from_the_working_rules_are_the_working_rules_words():
    rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8").replace("`", "")
    wanted = [line[2:] for line in rules.splitlines()
              if line.startswith("- ") and ("before sending a large change" in line or "no right to share" in line)]
    assert len(wanted) == 2
    guide = need("guide")(sample())
    for line in wanted:
        assert line in guide, f"the guide does not say, in the working rules' words: {line}"
    template = (ROOT / "tools" / "site" / "guide.txt").read_text(encoding="utf-8")
    assert "large change" not in template and "no right to share" not in template, "the guide writes a working rule in words of its own"


def test_each_language_uses_one_word_for_an_issue():
    build = builder()
    text = lambda lang: json.dumps(build.load_strings(lang), ensure_ascii=False)
    assert "イシュー" in text("ja") and not re.search(r"Issue", text("ja")), "Japanese writes an issue in more than one way"
    assert not re.search(r"\b(?:una issue|issues? abiertas?|las issues)\b", text("es"), re.I), "Spanish gives an issue more than one gender"
