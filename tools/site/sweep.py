#!/usr/bin/env python3
"""Lays the introduction page out in a real browser at many sizes and fails on layout faults.

    python3 tools/site/sweep.py                 the full check (what the branch check runs)
    python3 tools/site/sweep.py --quick         every fourth width, for a fast local look
    python3 tools/site/sweep.py --shots DIR     also save screenshots of the named sizes
    python3 tools/site/sweep.py --desk WAYS     the site was built with these ways to the desk open
                                                (github or github,live): the section about talking in
                                                real time must be there with exactly those ways; without
                                                this option it must not be there at all

For each of the four languages it checks, at every width from 320 to 1100 CSS px in steps of
8 px, once very tall (9:21) and once nearly square:

  - the page does not scroll sideways (scrollWidth equals clientWidth);
  - no visible element and no text reaches outside the viewport;
  - no text is cut off by a box that clips its contents;
  - every link and button is at least 44 by 44 CSS px and none overlap;
  - pieces that sit side by side (header, hero, diagram labels, buttons) do not overlap.

Everything is checked twice: in the system font and in a deliberately wide font, because other
systems fall back to wider fonts than the one this was written on.

At named sizes (ordinary phones, the cover and inner screens of foldable phones, tablets, desktop,
portrait and landscape) it also checks the hero headline, the header and the diagram. Then it
doubles the text size, folds and unfolds the page without reloading it, and, where the browser
can emulate a hinge, checks that nothing sits on it.

Decoration that is clipped on purpose is left out: the drifting background, the marquee of
language names (the same names are in the text), the inside of the diagram, and things kept
off screen for screen readers.

The sizes of the foldable phones are estimates, not maker-published browser values. The makers
publish panel pixels only: Galaxy Z Fold8 cover 1248 x 1972 and main 1848 x 2448, Galaxy Z Fold8
Ultra cover 1080 x 2520 and main 2504 x 2256 (Samsung newsroom, 2026), iPhone Duo outer
1398 x 2034 and inner 1878 x 2670 (Apple specifications page, 2026). The CSS sizes below divide
those by the pixel ratio that device-size sites assume (3, and 3.5 for the Ultra). That is why the
check sweeps every width instead of trusting these few.

Needs the playwright package and Chrome or Chromium. Nothing here runs on the published page.
"""

import argparse
import re
import functools
import http.server
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

SITE = Path(__file__).resolve().parents[2] / "site"
LANGS = {"en": "", "ko": "ko/", "ja": "ja/", "es": "es/"}

# name, width, height in CSS px. Foldable values are estimates (see above).
NAMED = [
    ("small phone", 320, 568),
    ("phone 360", 360, 800),
    ("phone 390", 390, 844),
    ("phone 430", 430, 932),
    ("phone 390 landscape", 844, 390),
    ("phone 430 landscape", 932, 430),
    ("phone 360 landscape", 800, 360),
    ("short landscape", 740, 340),
    ("Galaxy Z Fold8 cover (estimate)", 416, 657),
    ("Galaxy Z Fold8 cover landscape (estimate)", 657, 416),
    ("Galaxy Z Fold8 inner portrait (estimate)", 616, 816),
    ("Galaxy Z Fold8 inner landscape (estimate)", 816, 616),
    ("Galaxy Z Fold8 Ultra cover (estimate)", 360, 840),
    ("Galaxy Z Fold8 Ultra cover at ratio 3.5 (estimate)", 309, 720),
    ("Galaxy Z Fold8 Ultra inner portrait (estimate)", 645, 715),
    ("Galaxy Z Fold8 Ultra inner landscape (estimate)", 715, 645),
    ("iPhone Duo outer (estimate)", 466, 678),
    ("iPhone Duo outer landscape (estimate)", 678, 466),
    ("iPhone Duo inner portrait (estimate)", 626, 890),
    ("iPhone Duo inner landscape (estimate)", 890, 626),
    ("older fold cover", 344, 882),
    ("tablet portrait", 820, 1180),
    ("tablet landscape", 1180, 820),
    ("desktop", 1440, 900),
    ("wide desktop", 1920, 1080),
]
FOLD = [((416, 657), (816, 616)), ((466, 678), (890, 626))]  # cover, then unfolded
BIG_TEXT = [(320, 700), (360, 800), (390, 844), (416, 657), (466, 678), (616, 816), (816, 616), (1024, 768)]

CHECK = """
(opts) => {
  const out = [];
  const root = document.documentElement, vw = root.clientWidth, vh = root.clientHeight;
  const skip = '.blobs, .marquee, svg, .sr, .skip, .progress, details:not([open]) > ul';
  const name = el => el.tagName.toLowerCase() + (el.className && el.className.baseVal === undefined && el.className ? '.' + String(el.className).split(' ')[0] : '');
  const shown = el => { const s = getComputedStyle(el); return s.display !== 'none' && s.visibility !== 'hidden' && el.getClientRects().length > 0; };
  const hit = (a, b, gap) => a.left < b.right - gap && b.left < a.right - gap && a.top < b.bottom - gap && b.top < a.bottom - gap;

  if (root.scrollWidth !== vw) out.push(`page scrolls sideways (${root.scrollWidth} > ${vw})`);

  for (const el of document.body.querySelectorAll('*')) {
    if (el.closest(skip) || !shown(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && (r.right > vw + 0.5 || r.left < -0.5)) out.push(`${name(el)} reaches outside the viewport`);
  }

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const el = node.parentElement;
    if (!node.nodeValue.trim() || el.closest(skip) || !shown(el)) continue;
    range.selectNodeContents(node);
    const boxes = [];
    for (let up = el; up && up !== root; up = up.parentElement) {
      const s = getComputedStyle(up);
      if (s.overflowX !== 'visible' || s.overflowY !== 'visible') boxes.push(up.getBoundingClientRect());
    }
    for (const r of range.getClientRects()) {
      if (r.width === 0) continue;
      if (r.right > vw + 0.5 || r.left < -0.5) { out.push(`text in ${name(el)} reaches outside the viewport`); break; }
      if (boxes.some(b => r.left < b.left - 1 || r.right > b.right + 1 || r.top < b.top - 1 || r.bottom > b.bottom + 1)) { out.push(`text in ${name(el)} is clipped`); break; }
      if (opts.hinge && hit(r, opts.hinge, 0)) { out.push(`text in ${name(el)} sits on the hinge`); break; }
    }
  }

  const targets = [...document.querySelectorAll('a[href], button, summary')].filter(el => !el.closest(skip) && shown(el));
  const rects = targets.map(el => el.getBoundingClientRect());
  targets.forEach((el, i) => {
    const r = rects[i];
    if (r.width < 43.5 || r.height < 43.5) out.push(`${name(el)} "${el.textContent.trim().slice(0, 18)}" is a ${Math.round(r.width)} by ${Math.round(r.height)} touch target`);
    if (opts.hinge && hit(r, opts.hinge, 0)) out.push(`${name(el)} sits on the hinge`);
    for (let j = i + 1; j < targets.length; j++) {
      if (!el.contains(targets[j]) && !targets[j].contains(el) && hit(r, rects[j], 0.5)) out.push(`${name(el)} and ${name(targets[j])} overlap`);
    }
  });

  for (const group of ['.top .wrap > *', '.nav > *', '.hero .wrap > *', '.cta > *', '.dam-labels > *', '.foot > *', '.more > *']) {
    const items = [...document.querySelectorAll(group)].filter(shown).map(el => [el, el.getBoundingClientRect()]);
    items.forEach(([a, ra], i) => items.slice(i + 1).forEach(([b, rb]) => {
      if (a.parentElement === b.parentElement && hit(ra, rb, 0.5)) out.push(`${name(a)} and ${name(b)} overlap`);
    }));
  }

  if (opts.named) {
    const words = [...document.querySelectorAll('h1 .w')];
    const lines = {};
    words.forEach(w => { const t = Math.round(w.getBoundingClientRect().top); lines[t] = (lines[t] || 0) + 1; });
    const tops = Object.keys(lines).map(Number).sort((a, b) => a - b);
    if (tops.length > 1 && lines[tops[tops.length - 1]] < 2) out.push('the headline ends with a single word on its own line');
    const head = document.querySelector('.top');
    const stuck = ['sticky', 'fixed'].includes(getComputedStyle(head).position);
    const headH = head.getBoundingClientRect().height;
    if (stuck && headH > Math.max(vh * 0.16, 1)) out.push(`the fixed header takes ${Math.round(headH)} of ${vh} px`);
    const h1 = document.querySelector('h1').getBoundingClientRect();
    // While the question about measurement is open it stands above the page, in the flow, and covers
    // nothing. The headline must then fit one screen counted from below the question.
    const asking = document.querySelector('#consent');
    const pushed = asking && !asking.hidden ? asking.getBoundingClientRect().height : 0;
    if (pushed > vh * 0.45) out.push(`the question about measurement takes ${Math.round(pushed)} of ${vh} px`);
    if (h1.bottom - pushed > vh + 0.5) out.push(`the headline does not fit the first screen (${Math.round(h1.bottom - pushed)} > ${vh})`);
    const svg = document.querySelector('.dam svg').getBoundingClientRect();
    const room = vh - (stuck ? headH : 0);
    if (svg.height > room + 0.5) out.push(`the diagram is taller than the screen (${Math.round(svg.height)} > ${Math.round(room)})`);
    if (svg.width < 200) out.push(`the diagram is only ${Math.round(svg.width)} px wide`);
  }
  return [...new Set(out)].slice(0, 6);
}
"""

STATE = """
() => {
  const svg = document.querySelector('.dam svg').getBoundingClientRect();
  const fig = document.querySelector('.dam');
  const pad = parseFloat(getComputedStyle(fig).paddingLeft) + parseFloat(getComputedStyle(fig).paddingRight) + 2;
  const mark = document.querySelector('#now .timeline').getBoundingClientRect();
  return {
    sideways: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    svgFits: svg.width > 150 && svg.width <= fig.getBoundingClientRect().width - pad + 2 && Math.abs(svg.width / svg.height - 640 / 316) < 0.6,
    readingInView: mark.bottom > 0 && mark.top < innerHeight,
    drawn: parseFloat(getComputedStyle(document.querySelector('.timeline')).getPropertyValue('--p')),
    pieces: [...document.querySelectorAll('.dam .pc')].map(e => Math.round(new DOMMatrix(getComputedStyle(e).transform).m41)),
    track: Math.round(new DOMMatrix(getComputedStyle(document.querySelector('.track')).transform).m41 * 10),
    counts: [...document.querySelectorAll('[data-count]')].map(e => e.textContent + '/' + e.dataset.count),
  };
}
"""


# The section about now: what the script sets after reading the status file must be what the build
# wrote into the page, and a title from outside must stay text.
LIVE = """async () => {
  const text = (node) => node.textContent.replace(/\\s+/g, ' ').trim();
  const box = document.querySelector('#live');
  if (!box) return ['the page has no section about now'];
  const served = new DOMParser().parseFromString(await (await fetch(location.href)).text(), 'text/html').querySelector('#live');
  const found = [];
  if (box.hidden) found.push('the section about now is hidden although a status is published');
  if (text(box) !== text(served)) found.push('the script shows other values than the build wrote: ' + text(box).slice(0, 200));
  if (box.querySelectorAll('.changes li').length !== served.querySelectorAll('.changes li').length) found.push('the script lists other changes than the build');
  if (box.querySelector('.changes img, .changes script, .changes li li')) found.push('a title was put into the page as markup');
  if (window.__marked) found.push('markup in a title ran as script');
  return found;
}"""


# The section about talking with the project's AI in real time: which ways it shows, and that the steps
# of each way are numbered.
DESK = """() => {
  const section = document.querySelector('#desk');
  const marks = [...document.querySelectorAll('#desk .steps')].map(list =>
    [...list.children].map(item => getComputedStyle(item, '::before').content.replace(/"/g, '')).join(','));
  return {there: !!section, ways: document.querySelectorAll('#desk .card').length, marks,
          links: [...document.querySelectorAll('#desk a[href]')].map(a => a.href),
          question: [...document.querySelectorAll('#faq h3')].length};
}"""


# Measurement asks first. Every request to another host is answered here with an empty script, so the
# check itself never reaches Google; it only records what the page tried to reach, and when.
BAR = """() => {
  const bar = document.querySelector('#consent');
  if (!bar) return {missing: true};
  const buttons = [...bar.querySelectorAll('button')].map(b => {
    const r = b.getBoundingClientRect(), s = getComputedStyle(b);
    return [Math.round(r.width), Math.round(r.height), s.backgroundColor, s.color, s.fontWeight, s.fontSize, s.borderTopWidth];
  });
  const open = document.querySelector('[data-consent-open]');
  return {shown: !bar.hidden && bar.getBoundingClientRect().height > 0, buttons, open: !!open && !open.hidden,
          cookie: document.cookie, lang: (bar.closest('[lang]') || document.documentElement).lang};
}"""


def consent(browser, base: str, path: str, lang: str, ident: str, report) -> None:
    def visit(setup: str | None = None):
        context = browser.new_context(reduced_motion="reduce", viewport={"width": 390, "height": 844})
        outside, blocked = [], []
        context.route(lambda url: not url.startswith(base),
                      lambda route: (outside.append(route.request.url), route.fulfill(status=200, content_type="text/javascript", body=""))[1])
        if setup:
            context.add_init_script(setup)
        page = context.new_page()
        page.on("console", lambda message: blocked.append(message.text) if "Content Security Policy" in message.text else None)
        page.goto(base + path)
        page.wait_for_timeout(400)
        return context, page, outside, blocked

    where = f"{lang} measurement"
    context, page, outside, blocked = visit()
    state = page.evaluate(BAR)
    if state.get("missing"):
        report(where, ["a measurement ID is set but the page has no bar that asks"])
        context.close()
        return
    found = []
    if not state["shown"]:
        found.append("the bar that asks is not shown to a visitor who has not chosen")
    if outside:
        found.append(f"the page reached another host before the visitor agreed: {outside[0]}")
    if state["cookie"]:
        found.append("a cookie is set before the visitor agreed")
    if len(state["buttons"]) != 2 or state["buttons"][0][1:] != state["buttons"][1][1:] or abs(state["buttons"][0][0] - state["buttons"][1][0]) > 1:
        found.append(f"agree and decline do not have equal weight: {state['buttons']}")

    page.click('#consent [data-consent="no"]')
    page.wait_for_timeout(300)
    page.reload()
    page.wait_for_timeout(400)
    state = page.evaluate(BAR)
    if state["shown"]:
        found.append("the bar asks again after the visitor declined")
    if outside or state["cookie"]:
        found.append("declining loaded something or set a cookie")
    if not state["open"]:
        found.append("the footer has no way to change the choice")
    else:
        page.click("[data-consent-open]")
        if not page.evaluate(BAR)["shown"]:
            found.append("the link in the footer does not bring the question back")
        else:
            page.click('#consent [data-consent="yes"]')
            page.wait_for_timeout(500)
            if not any(url.startswith("https://www.googletagmanager.com/gtag/js?id=" + ident) for url in outside):
                found.append("agreeing did not load the measurement")
            allowed = ("https://www.googletagmanager.com/", ".google-analytics.com/", ".google.com/")
            found += [f"after agreeing the page reached a host outside its policy: {url}" for url in outside
                      if not any(part in url.split("?")[0] for part in allowed)]
            page.reload()
            page.wait_for_timeout(500)
            if page.evaluate(BAR)["shown"] or not outside:
                found.append("the choice to agree is not remembered")
            # A change of mind in the same tab, both ways, without a reload.
            switch = f"window['ga-disable-{ident}']"
            tags = "document.querySelectorAll('script[src*=\"gtag/js\"]').length"
            page.click("[data-consent-open]")
            page.click('#consent [data-consent="no"]')
            if page.evaluate(switch) is not True:
                found.append("declining after having agreed does not switch the measurement off")
            page.click("[data-consent-open]")
            page.click('#consent [data-consent="yes"]')
            page.wait_for_timeout(300)
            if page.evaluate(switch) is not False:
                found.append("agreeing again after declining leaves the measurement switched off until a reload")
            if page.evaluate(tags) != 1:
                found.append(f"Google's tag is on the page {page.evaluate(tags)} times after agreeing again")
    found += [f"the page's own policy blocked something: {text[:160]}" for text in blocked]
    report(where, found)
    context.close()

    # The script evaluated a second time on the same page must not act twice.
    context, page, outside, blocked = visit()
    page.evaluate("""() => new Promise(done => {
      const first = document.querySelector('script[src$="analytics.js"]'), again = document.createElement('script');
      again.src = first.src + '?again'; again.setAttribute('data-id', first.getAttribute('data-id'));
      again.onload = done; again.onerror = done; document.head.appendChild(again);
    })""")
    page.click('#consent [data-consent="yes"]')
    page.wait_for_timeout(400)
    loads = page.evaluate("document.querySelectorAll('script[src*=\"gtag/js\"]').length")   # the cache hides a second request
    report(where, [] if loads == 1 else [f"with the script evaluated twice, agreeing puts Google's tag on the page {loads} times"])
    context.close()

    for name, setup in (("a global privacy control signal", "Object.defineProperty(Navigator.prototype, 'globalPrivacyControl', {get: () => true})"),
                        ("a do-not-track signal", "Object.defineProperty(Navigator.prototype, 'doNotTrack', {get: () => '1'})")):
        context, page, outside, blocked = visit(setup)
        state = page.evaluate(BAR)
        found = []
        if state["shown"]:
            found.append(f"the bar asks although the browser sends {name}")
        if outside or state["cookie"]:
            found.append(f"something was loaded or a cookie set although the browser sends {name}")
        report(where, found)
        context.close()


def serve() -> str:
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/"


def launch(p):
    for options in ({"channel": "chrome"}, {}):
        try:
            return p.chromium.launch(**options)
        except Exception as error:  # the next option may still work
            last = error
    raise last


def hinge(page, width: int, height: int, gap: int = 24) -> dict | None:
    """Asks the browser to pretend the screen has a vertical hinge in the middle."""
    session = page.context.new_cdp_session(page)
    feature = {"orientation": "vertical", "offset": (width - gap) // 2, "maskLength": gap}
    metrics = {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": True, "displayFeature": feature}
    try:
        session.send("Emulation.setDeviceMetricsOverride", metrics)
    except Exception:
        return None
    page.wait_for_timeout(200)
    if not page.evaluate("matchMedia('(horizontal-viewport-segments: 2)').matches"):
        return None
    return {"left": feature["offset"], "right": feature["offset"] + gap, "top": 0, "bottom": height}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--shots")
    parser.add_argument("--site", help="the built site to check (default: the committed site/)")
    parser.add_argument("--consent", action="store_true", help="the site was built with a measurement ID: check that it asks first")
    parser.add_argument("--desk", default="", help="the ways to the desk the site was built with: github or github,live")
    args = parser.parse_args()
    global SITE
    SITE = Path(args.site).resolve() if args.site else SITE
    base, faults, notes = serve(), [], []

    def report(where: str, found: list[str]) -> None:
        faults.extend(f"{where}: {f}" for f in found)

    with sync_playwright() as p:
        browser = launch(p)
        for lang, path in LANGS.items():
            still = browser.new_context(reduced_motion="reduce", viewport={"width": 390, "height": 844})
            if args.consent:
                ident = re.search(r'analytics\.js" data-id="(G-[A-Z0-9]+)"', (SITE / path / "index.html").read_text(encoding="utf-8"))
                if ident:
                    consent(browser, base, path, lang, ident.group(1), report)
                else:
                    report(f"{lang} measurement", ["the built page names no measurement ID"])
            asked: list[str] = []
            still.on("request", lambda request: asked.append(request.url))
            page = still.new_page()
            page.goto(base + path)
            if (SITE / "status.json").is_file():
                page.wait_for_timeout(600)  # the script reads the status file once the page is there
                report(f"{lang} section about now", page.evaluate(LIVE))
                if not any(url.endswith("/status.json") for url in asked):
                    report(f"{lang} section about now", ["the script did not read the status file"])
            ways, desk = [way for way in args.desk.split(",") if way], page.evaluate(DESK)
            if desk["there"] != bool(ways) or desk["ways"] != len(ways):
                report(f"{lang} talking in real time", [f"the page shows {desk['ways']} ways to the desk, and it was built with {len(ways)} open"])
            if desk["question"] != 6 + bool(ways):
                report(f"{lang} talking in real time", [f"the page asks {desk['question']} questions"])
            for marks in desk["marks"]:   # each step shows the list's counter, not the dash of an unordered list
                if set(marks.split(",")) != {"counter(step)"}:
                    report(f"{lang} talking in real time", [f"the steps of a way are not numbered ({marks})"])
            if "live" not in ways and any("coworker" in link for link in desk["links"]):
                report(f"{lang} talking in real time", ["the page links to AI coworker although the live way is closed"])
            elsewhere = sorted({url for url in asked if not url.startswith(base)})
            if elsewhere:
                report(f"{lang}", [f"the page asked another host for {url}" for url in elsewhere])

            for width in range(320, 1101, 32 if args.quick else 8):
                for height in (round(width * 21 / 9), round(width / 1.12)):
                    page.set_viewport_size({"width": width, "height": height})
                    report(f"{lang} {width}x{height}", page.evaluate(CHECK, {}))

            for name, width, height in NAMED:
                page.set_viewport_size({"width": width, "height": height})
                page.evaluate("scrollTo(0, 0)")
                report(f"{lang} {name} {width}x{height}", page.evaluate(CHECK, {"named": True}))
                if args.shots:
                    page.wait_for_timeout(250)  # let the browser repaint at the new size first
                    shot = Path(args.shots) / f"{lang}-{width}x{height}.png"
                    shot.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(shot))

            for width, height in BIG_TEXT:
                page.set_viewport_size({"width": width, "height": height})
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                report(f"{lang} {width}x{height} with text at 200%", page.evaluate(CHECK, {}))
                page.evaluate("document.documentElement.style.fontSize = ''")

            # Other systems fall back to wider fonts than this one. Lay everything out again with a wide one.
            page.add_style_tag(content='*:not(pre):not(code) { font-family: Verdana, "DejaVu Sans", sans-serif !important; }')
            for width in range(320, 1101, 64 if args.quick else 24):
                for height in (round(width * 21 / 9), round(width / 1.12)):
                    page.set_viewport_size({"width": width, "height": height})
                    report(f"{lang} {width}x{height} in a wide font", page.evaluate(CHECK, {}))
            for name, width, height in NAMED:
                page.set_viewport_size({"width": width, "height": height})
                page.evaluate("scrollTo(0, 0)")
                report(f"{lang} {name} {width}x{height} in a wide font", page.evaluate(CHECK, {"named": True}))
            for width, height in BIG_TEXT:
                page.set_viewport_size({"width": width, "height": height})
                page.evaluate("document.documentElement.style.fontSize = '200%'")
                report(f"{lang} {width}x{height} in a wide font with text at 200%", page.evaluate(CHECK, {}))
                page.evaluate("document.documentElement.style.fontSize = ''")

            page.evaluate("document.querySelector('.langs').open = true")
            for width, height in ((320, 568), (390, 844), (816, 616)):
                page.set_viewport_size({"width": width, "height": height})
                report(f"{lang} {width}x{height} with the language menu open", page.evaluate(CHECK, {}))
            page.evaluate("document.querySelector('.langs').open = false")

            for width, height in ((816, 616), (890, 626), (715, 645)):
                page.set_viewport_size({"width": width, "height": height})
                area = hinge(page, width, height)
                if area is None:
                    notes.append("two viewport segments could not be emulated in this browser; not checked")
                    break
                report(f"{lang} {width}x{height} with a hinge", page.evaluate(CHECK, {"hinge": area}))
                if args.shots:
                    page.wait_for_timeout(250)
                    page.screenshot(path=str(Path(args.shots) / f"{lang}-{width}x{height}-hinge.png"))
            still.close()

            # Fold and unfold with motion on, without reloading.
            moving = browser.new_context(viewport={"width": FOLD[0][0][0], "height": FOLD[0][0][1]})
            page = moving.new_page()
            for cover, inner in FOLD:
                page.set_viewport_size({"width": cover[0], "height": cover[1]})
                page.goto(base + path)
                # Before anything is scrolled into view, the figures in the page are the real ones, on
                # screen and on paper: a count-up may start from zero only when it really runs.
                shown = page.evaluate("[...document.querySelectorAll('[data-count]')].map(e => e.textContent + '/' + e.dataset.count)")
                page.emulate_media(media="print")
                printed = page.evaluate("[...document.querySelectorAll('[data-count]')].map(e => e.textContent + '/' + e.dataset.count)")
                page.emulate_media(media="screen")
                for what, values in (("before it is seen", shown), ("when printed", printed)):
                    if any(v.split("/")[0] != v.split("/")[1] for v in values):
                        report(f"{lang} {cover[0]}x{cover[1]}", [f"a figure does not show its real value {what} ({values})"])
                page.evaluate("document.querySelector('.stats').scrollIntoView({block: 'center', behavior: 'instant'})")
                page.wait_for_timeout(350)
                during = page.evaluate("[...document.querySelectorAll('[data-count]')].map(e => e.textContent + '/' + e.dataset.count)")
                if all(v.split("/")[0] == v.split("/")[1] for v in during):
                    report(f"{lang} {cover[0]}x{cover[1]}", ["the figures did not count up when they came into view"])
                page.wait_for_timeout(1900)  # the numbers finish counting while they are on screen
                # Read the timeline: its top at the top of the screen, so part of its line is drawn.
                page.evaluate("document.querySelector('#now .timeline').scrollIntoView({block: 'start', behavior: 'instant'})")
                page.wait_for_timeout(600)
                for step, (width, height) in enumerate((inner, cover, inner)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(500)
                    one = page.evaluate(STATE)
                    page.wait_for_timeout(400)
                    two = page.evaluate(STATE)
                    where = f"{lang} folded {cover[0]}x{cover[1]} to {width}x{height} (step {step + 1})"
                    found = []
                    if two["sideways"]:
                        found.append("page scrolls sideways after the resize")
                    if not two["svgFits"]:
                        found.append("the diagram kept its old size")
                    if not two["readingInView"]:
                        found.append("the section being read left the screen")
                    if not 0 < two["drawn"] <= 1:
                        found.append(f"the timeline line was not redrawn ({two['drawn']})")
                    if any(c.split("/")[0] != c.split("/")[1] for c in two["counts"]):
                        found.append(f"a count did not finish ({two['counts']})")
                    report(where, found)
                    if args.shots:
                        page.screenshot(path=str(Path(args.shots) / f"{lang}-fold-{cover[0]}-step{step + 1}-{width}x{height}.png"))
                page.evaluate("document.querySelector('.dam').scrollIntoView({block: 'center', behavior: 'instant'})")
                page.wait_for_timeout(600)
                one = page.evaluate(STATE)
                page.wait_for_timeout(500)
                two = page.evaluate(STATE)
                if one["pieces"] == two["pieces"] or not two["svgFits"]:
                    report(f"{lang} unfolded {inner[0]}x{inner[1]}", ["the diagram is not running at its new size"])
                page.evaluate("document.querySelector('#now .marquee').scrollIntoView({block: 'center', behavior: 'instant'})")
                page.wait_for_timeout(600)
                one = page.evaluate(STATE)
                page.wait_for_timeout(400)
                two = page.evaluate(STATE)
                if one["track"] == two["track"]:
                    report(f"{lang} unfolded {inner[0]}x{inner[1]}", ["the marquee is not running at its new size"])
            moving.close()
        browser.close()

    for note in sorted(set(notes)):
        print(f"NOT CHECKED: {note}")
    kinds: dict[str, list[str]] = {}
    for fault in faults:
        where, _, what = fault.partition(": ")
        kinds.setdefault(what, []).append(where)
    for what, places in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
        print(f"{len(places):5d} x {what}   first at: {places[0]}; last at: {places[-1]}")
    print(f"{len(faults)} layout faults" if faults else "layout holds at every size checked")
    return 1 if faults else 0


if __name__ == "__main__":
    sys.exit(main())
