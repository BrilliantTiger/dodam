"""Checks on the public introduction page in site/ and the workflow that publishes it.

The page is served in four languages built from one template (tools/site). These tests fail
when a language lacks a string, has an extra one, or shows a different number, date or link.
They live with the benchmark tests because that is the test run the branch check executes.
"""

import copy
import importlib.util
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
LANGS = ["en", "ko", "ja", "es"]
NAMES = {"en": "English", "ko": "한국어", "ja": "日本語", "es": "Español"}
SECTIONS = ["what", "why", "guarantees", "how", "now", "join", "rules", "license"]
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
        self.text = ""
        self._inside: list[str] = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
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
    alternates = {a.get("hreflang"): a.get("href") for a in links if a.get("rel") == "alternate"}
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
    loaded += [a.get("href") for a in p.all("link") if a.get("rel") not in {"canonical", "alternate"}]
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
    }


NAMES_NOT_FIGURES = r"(?<![\d.])(?:1\.0|0\.1|2\.0|5\.5)(?![\d.])"   # releases, the license, a model's name
FROM_RESEARCH = {"stat1", "stat2", "stat3", "src1", "src2", "src3", "why_note"}   # sources kept as they are


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
        if path.suffix == ".js":
            assert not re.search(r"https?://|fetch\(|XMLHttpRequest|sendBeacon|WebSocket|import\(", text), path.name


def test_script_is_one_small_file_of_our_own():
    assert [p.name for p in site_files() if p.suffix == ".js"] == ["motion.js"]


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
    assert sorted(built) == sorted(str(page_path(lang).relative_to(SITE)) for lang in LANGS)
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
        key = text_key(s, "{m:2038-11}")
        s["es"][key] = s["es"][key].replace("{m:2038-11}", "{m:2037-11}")

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
    assert re.search(r"paths:\s*\n\s*-\s*['\"]?site/\*\*", head)
    assert re.search(r"branches:\s*(?:\[\s*main\s*\]|\n\s*-\s*main)", head)
    blocks = re.split(r"\n  (?=[\w-]+:\n)", jobs)
    writers = [b for b in blocks if "pages: write" in b or "id-token: write" in b]
    assert len(writers) == 1
    assert "pages: write" in writers[0] and "id-token: write" in writers[0]
    assert "deploy-pages@" in writers[0]


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
