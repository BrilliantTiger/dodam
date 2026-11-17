"""Checks on the public introduction page in site/ and the workflow that publishes it.

They live with the benchmark tests because that is the test run the branch check executes.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"
SITE_URL = "https://brillianttigercorp.com/dodam/"
REPO_URL = "https://github.com/BrilliantTiger/dodam"
PAGES = {"en": SITE / "index.html", "ko": SITE / "ko" / "index.html"}
SECTIONS = ["what", "why", "guarantees", "how", "now", "join", "rules", "license"]
SIZE_LIMIT = 150 * 1024


class Page(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.title = ""
        self.inline_script = ""
        self._inside: list[str] = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._inside.append(tag)

    def handle_endtag(self, tag):
        if tag in self._inside:
            while self._inside.pop() != tag:
                pass

    def handle_data(self, data):
        if self._inside and self._inside[-1] == "title":
            self.title += data
        if self._inside and self._inside[-1] == "script":
            self.inline_script += data

    def all(self, tag: str) -> list[dict[str, str | None]]:
        return [a for t, a in self.tags if t == tag]

    def hrefs(self) -> list[str]:
        return [a["href"] for a in self.all("a") if a.get("href")]


def page(lang: str) -> Page:
    return Page(PAGES[lang].read_text(encoding="utf-8"))


def is_local(url: str) -> bool:
    return not re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", url.strip(), re.I)


def site_files() -> list[Path]:
    return [p for p in SITE.rglob("*") if p.is_file()]


def site_text() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in site_files()
        if p.suffix in {".html", ".css", ".js", ".svg", ".txt"}
    )


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_page_declares_language_title_and_description(lang):
    p = page(lang)
    assert p.all("html")[0].get("lang") == lang
    assert "Dodam" in p.title or "도담" in p.title
    metas = {m.get("name"): m.get("content") for m in p.all("meta")}
    assert len(metas.get("description") or "") >= 50
    assert "width=device-width" in (metas.get("viewport") or "")


def test_pages_link_to_each_other():
    assert "ko/" in page("en").hrefs()
    assert "../" in page("ko").hrefs()


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_page_has_every_section_and_one_heading(lang):
    p = page(lang)
    ids = {a.get("id") for _, a in p.tags}
    assert [s for s in SECTIONS if s not in ids] == []
    assert len(p.all("h1")) == 1
    assert len(p.all("main")) == 1


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_page_links_to_the_places_people_join_from(lang):
    hrefs = page(lang).hrefs()
    assert REPO_URL in hrefs
    assert f"{REPO_URL}/discussions" in hrefs
    assert f"{REPO_URL}/security/policy" in hrefs
    assert f"{REPO_URL}/blob/main/AGENTS.md" in hrefs
    assert any(h.startswith(f"{REPO_URL}/issues") and "ai-welcome" in h for h in hrefs)


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_page_loads_nothing_from_another_host(lang):
    p = page(lang)
    loaded = [a.get("src") for t, a in p.tags if a.get("src")]
    loaded += [a.get("href") for a in p.all("link") if a.get("rel") != "canonical" and a.get("rel") != "alternate"]
    assert [u for u in loaded if u and not is_local(u)] == []
    assert p.inline_script.strip() == ""
    for url in loaded:
        target = (PAGES[lang].parent / url.split("?")[0].split("#")[0]).resolve()
        assert target.is_file(), url


def test_styles_and_script_reach_no_other_host():
    for path in site_files():
        if path.suffix not in {".css", ".js", ".svg"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"@import|url\(\s*['\"]?(?:https?:)?//", text), path.name
        if path.suffix == ".js":
            assert not re.search(r"https?://|fetch\(|XMLHttpRequest|sendBeacon|WebSocket|import\(", text), path.name


def test_script_is_one_small_file_of_our_own():
    scripts = [p for p in site_files() if p.suffix == ".js"]
    assert [p.name for p in scripts] == ["motion.js"]


def test_site_respects_reduced_motion_and_color_schemes():
    css = (SITE / "style.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in css
    assert "prefers-color-scheme: dark" in css
    assert "prefers-reduced-motion" in (SITE / "motion.js").read_text(encoding="utf-8")


def test_site_is_small():
    assert sum(p.stat().st_size for p in site_files()) < SIZE_LIMIT


def test_site_has_no_contact_details_or_internal_codes():
    text = site_text()
    assert re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text) == []
    assert re.findall(r"\b(?:DOD-\d+|[AG]\d{1,2}\b(?=[^\w%]))", text) == []
    assert "linear.app" not in text


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_numbers_about_insecure_code_name_their_source(lang):
    text = PAGES[lang].read_text(encoding="utf-8")
    assert text.count("Veracode") >= 3
    for date in ("2025", "2026"):
        assert date in text


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


@pytest.mark.parametrize("name", ["README.md", "README.ko.md"])
def test_readme_links_to_the_page_near_the_top(name):
    head = "\n".join((ROOT / name).read_text(encoding="utf-8").splitlines()[:8])
    assert SITE_URL in head
