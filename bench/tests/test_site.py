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
SITE_URL = "https://brillianttigercorp.com/dodam/"
REPO_URL = "https://github.com/BrilliantTiger/dodam"
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


def test_only_two_readmes_exist():
    assert sorted(p.name for p in ROOT.glob("README*")) == ["README.ko.md", "README.md"]
