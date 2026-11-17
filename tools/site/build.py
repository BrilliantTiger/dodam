#!/usr/bin/env python3
"""Builds the introduction page in four languages from one template.

    python3 tools/site/build.py          write site/index.html and site/<lang>/index.html
    python3 tools/site/build.py --check  fail if the strings disagree or the pages are stale

The structure, every link and every element live once, in template.html. Each language has one
strings file. Dates and large numbers are written in the strings as {d:2027-02-24}, {m:2038-11}
and {n:2000} and are spelled here in each language's usual form, so a date cannot differ between
languages. problems() reports any string that a language lacks or adds, and any string whose
numbers, dates or markup differ from the English one. Standard library only.
"""

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "site"
SITE_URL = "https://brillianttigercorp.com/dodam/"
REPO_URL = "https://github.com/BrilliantTiger/dodam"
LANGS = ["en", "ko", "ja", "es"]
NAMES = {"en": "English", "ko": "한국어", "ja": "日本語", "es": "Español"}
MONTHS = {
    "en": "January February March April May June July August September October November December".split(),
    "es": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(),
}

KEY = re.compile(r"\{\{([a-z0-9_]+)\}\}")
PLACE = re.compile(r"\{(?:(d|m|n):([0-9-]+)|(repo))\}")
ALLOWED_TAGS = {"strong", "code", "span"}


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
    out, step, inside = [], 0, False
    for part in parts:
        strong = part.startswith("*")
        if strong != inside:
            out.append("<em>" if strong else "</em>")
            inside, step = strong, step + 1
        out.append(f'<span class="w" style="--i:{step}">{part.lstrip("*")}</span>')
        step += 1
    if inside:
        out.append("</em>")
    html = glue.join(out)
    return html.replace(f"<em>{glue}", "<em>").replace(f"{glue}</em>", "</em>") if glue else html


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
    }


def render(lang: str, strings: dict | None = None) -> str:
    strings = strings if strings is not None else load_strings(lang)
    values = specials(lang)
    for key, value in strings.items():
        values[key] = headline(lang, value) if isinstance(value, list) else expand(lang, value)
    return KEY.sub(lambda m: values[m.group(1)], template())


def pages() -> dict[str, str]:
    return {page_file(lang): render(lang) for lang in LANGS}


def shape(value) -> dict:
    """What must be the same in every language: dates, numbers, markup and emphasis."""
    if isinstance(value, list):
        return {"kind": "list", "emphasis": any(p.startswith("*") for p in value), "digits": sorted(re.findall(r"\d+", "".join(value)))}
    rest = PLACE.sub(" ", value)
    return {
        "kind": "text",
        "placeholders": sorted(m.group(0) for m in PLACE.finditer(value)),
        "digits": sorted(re.findall(r"\d+(?:[.,]\d+)*", rest)),
        "tags": sorted(re.findall(r"</?([a-z]+)", rest)),
    }


def problems(strings: dict[str, dict] | None = None) -> list[str]:
    strings = strings if strings is not None else {lang: load_strings(lang) for lang in LANGS}
    found = []
    used = set(KEY.findall(template())) - set(specials("en"))
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
            if re.search(r"[０-９]", text):
                found.append(f"{lang}: {key} uses full-width digits")
            if set(re.findall(r"</?([a-z]+)", text)) - ALLOWED_TAGS:
                found.append(f"{lang}: {key} uses markup that belongs in the template")
            if lang != "en" and key in strings["en"] and shape(value) != shape(strings["en"][key]):
                found.append(f"{lang}: {key} differs from English in numbers, dates or markup")
    return found


def main(argv: list[str]) -> int:
    issues = problems()
    for issue in issues:
        print(issue, file=sys.stderr)
    if issues:
        return 1
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
