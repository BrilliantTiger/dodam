#!/usr/bin/env python3
"""Makes the image shown when the page is shared, one per language: site/card-<lang>.png.

    python3 tools/site/card.py

It draws the page's own design (its colors from site/style.css, its name mark, its headline and its
status line from the strings) at 1200 by 630 px in the Chrome on this machine. The images are
committed. Run it again when the headline, the status line or the colors change; a test fails when a
card is missing or has another size. Needs the pinned browser driver in requirements.txt.
"""

import html
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402

PAGE = """<!doctype html><html lang="{lang}"><meta charset="utf-8"><style>
html,body{{margin:0}}
body{{width:{width}px;height:{height}px;box-sizing:border-box;padding:72px 80px;display:flex;flex-direction:column;
justify-content:space-between;background:{bg};color:{ink};font-family:{sans};overflow:hidden;position:relative}}
body::after{{content:"";position:absolute;right:-180px;top:-220px;width:640px;height:640px;border-radius:50%;
background:radial-gradient(circle,{glow},transparent 68%)}}
.brand{{display:flex;align-items:center;gap:16px;font-size:40px;font-weight:700;letter-spacing:-0.02em}}
.brand i{{width:14px;height:40px;border-radius:6px;background:{accent}}}
h1{{margin:0;max-width:980px;font-size:{size}px;line-height:1.08;font-weight:720;letter-spacing:-0.035em;word-break:keep-all}}
h1 em{{font-style:normal;color:{accent}}}
p{{margin:0;display:inline-flex;align-self:flex-start;padding:12px 24px;border:2px solid {line};border-radius:99px;
font-size:28px;font-weight:600;color:{muted};background:{surface}}}
</style><div class="brand"><i></i>Dodam</div><h1>{headline}</h1><p>{status}</p></html>"""


def tokens() -> dict:
    css = (build.OUT / "style.css").read_text(encoding="utf-8").split("@media", 1)[0]
    return {name: re.search(rf"--{name}:\s*([^;]+);", css).group(1) for name in ("bg", "surface", "ink", "muted", "line", "accent", "glow", "sans")}


def headline(lang: str, parts: list[str]) -> str:
    glue = "" if lang == "ja" else " "
    plain = glue.join(html.escape(p) for p in parts if not p.startswith("*"))
    strong = glue.join(html.escape(p.lstrip("*")) for p in parts if p.startswith("*"))
    return f"{plain}{glue}<em>{strong}</em>"


def main() -> int:
    width, height = build.CARD
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        for lang in build.LANGS:
            strings = build.load_strings(lang)
            page.set_content(PAGE.format(lang=lang, width=width, height=height, size=76 if lang in ("en", "es") else 80,
                                         headline=headline(lang, strings["h1"]), status=html.escape(strings["status"]), **tokens()))
            if page.evaluate("document.body.scrollHeight > innerHeight || document.querySelector('h1').scrollWidth > 1040"):
                raise SystemExit(f"{lang}: the headline does not fit the card")
            page.screenshot(path=str(build.OUT / f"card-{lang}.png"))
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
