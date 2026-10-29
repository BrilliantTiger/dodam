import asyncio, sys, json, pathlib
from playwright.async_api import async_playwright
HTML = pathlib.Path(sys.argv[1]).resolve(); OUT = pathlib.Path(sys.argv[2]); OUT.mkdir(exist_ok=True)
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(); errs = []
        for (w, h, scheme, name) in [(1280, 900, 'light', 'desk_light'), (1280, 900, 'dark', 'desk_dark'), (390, 844, 'light', 'phone_light'), (390, 844, 'dark', 'phone_dark')]:
            ctx = await b.new_context(viewport={'width': w, 'height': h}, color_scheme=scheme, device_scale_factor=1)
            pg = await ctx.new_page()
            pg.on('console', lambda m, n=name: errs.append(f'{n} console.{m.type}: {m.text}') if m.type in ('error', 'warning') else None)
            pg.on('pageerror', lambda e, n=name: errs.append(f'{n} pageerror: {e}'))
            await pg.goto(HTML.as_uri()); await pg.evaluate('document.fonts.ready'); await pg.wait_for_timeout(400)
            sw = await pg.evaluate('document.documentElement.scrollWidth')
            if sw > w: errs.append(f'{name}: horizontal overflow {sw} > {w}')
            info = await pg.evaluate('''() => ({bars: document.querySelectorAll('#g-svg .g-bar').length, ms: document.querySelectorAll('#g-svg .g-ms').length,
               items: document.querySelectorAll('.it').length, detail: document.getElementById('g-detail').innerText.slice(0,80),
               svgW: document.getElementById('g-svg').getAttribute('width'), hb: document.querySelectorAll('#c-hours .c-bar').length})''')
            await pg.screenshot(path=str(OUT / f'{name}_top.png'))
            await pg.screenshot(path=str(OUT / f'{name}_full.png'), full_page=True)
            # interaction: click a chip and a bar
            await pg.evaluate("document.querySelectorAll('.chip[data-period]')[2].click()"); vis = await pg.evaluate("[...document.querySelectorAll('.it')].filter(x=>!x.hidden).length")
            await pg.click('#only-crit'); vis2 = await pg.evaluate("[...document.querySelectorAll('.it')].filter(x=>!x.hidden).length")
            print(name, json.dumps(info, ensure_ascii=False), 'filtered', vis, 'crit', vis2)
            await ctx.close()
        await b.close(); print('ERRORS', errs)
asyncio.run(main())
