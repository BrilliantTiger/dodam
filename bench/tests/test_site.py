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


def test_dates_and_numbers_are_the_charter_s():
    build, text = builder(), charter()
    research = (ROOT / "docs" / "research" / "2026-10-28-design-research.ko.md").read_text(encoding="utf-8")
    en = build.load_strings("en")
    found = set()
    for value in en.values():
        found.update(re.findall(r"\{(d|m|n):([0-9-]+)\}", "".join(value) if isinstance(value, list) else value))
    needed = {("d", "2027-02-24"), ("d", "2028-05-10"), ("d", "2029-04-04"), ("d", "2030-10-30"), ("d", "2032-02-25"),
              ("d", "2034-01-18"), ("m", "2038-11"), ("d", "2026-11-17"), ("n", "2000"), ("n", "1200")}
    assert needed - found == set()
    for kind, value in sorted(found):
        if kind == "n":
            assert f"{int(value):,}번" in text or (value == "2000" and "2천 번" in text), value
        elif kind == "m" and f"Veracode {value}" in research:
            continue
        elif value == "2026-10-28":
            continue  # the date of the research notes read above
        else:
            assert build.spell("ko", kind, value) in text, value
    assert "판: v0.7 (2026-11-17)" in text and "0.7" in en["now_note"]
    for fact, key in (("벤치마크 과제 30개", "t1_p"), ("기존 언어 7개", "t1_p"), ("벤치마크 과제 100개", "t3_p"), ("3개월 넘게", "t4_p")):
        assert fact in text
        assert re.search(r"\d+", fact).group(0) in re.findall(r"\d+", en[key]), key
    for line, key in (("45%가 불안전했고, Java는 70% 넘게", "stat1"), ("95%를 넘었는데 보안은 약 55%", "stat2"), ("평균 56%, 최고는 GPT-5.5의 68%", "stat3")):
        assert line in research
        assert set(re.findall(r"\d+(?:\.\d+)?", line)) - {"45", "56"} <= set(re.findall(r"\d+(?:\.\d+)?", en[key])), key


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
