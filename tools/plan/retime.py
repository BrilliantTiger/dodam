"""Move every date of the work breakdown onto the forecast: piecewise-linear between milestone medians."""
import json, datetime as dt
from edit import *
A = json.load(open('anchors.json'))
D = dt.date.fromisoformat
orig = sorted([(D(x['start']), x['id']) for x in W.ITEMS if x.get('milestone')])
pts = [(D('2026-11-02'), D('2026-11-02'))] + [(o, D(A[i]['p50'])) for o, i in orig]
def mp(s):
    d = D(s)
    if d <= pts[0][0]:
        return s
    for (a0, b0), (a1, b1) in zip(pts, pts[1:]):
        if d <= a1:
            return (b0 + dt.timedelta(days=round((d - a0).days * (b1 - b0).days / (a1 - a0).days))).isoformat()
    return (pts[-1][1] + (d - pts[-1][0])).isoformat()
for x in W.ITEMS:
    s, e = x['start'], x['end']
    ns, ne = mp(s), mp(e)
    if x.get('milestone'):
        ns = ne = A[x['id']]['p50']
    x['start'], x['end'] = ns, ne
# keep a day between a predecessor's end and its successor's start
by = {x['id']: x for x in W.ITEMS}
changed = True
while changed:
    changed = False
    for x in W.ITEMS:
        for p in x.get('after', []):
            pe = D(by[p]['end'])
            need = pe if x.get('milestone') else pe + dt.timedelta(days=1)
            if D(x['start']) < need:
                shift = (need - D(x['start'])).days
                x['start'] = need.isoformat()
                if D(x['end']) < need:
                    x['end'] = need.isoformat()
                changed = True
save()
txt = open('wbs_data.py', encoding='utf-8').read()
txt = txt.replace('RELEASE_DATE = "2029-09-28"', f'RELEASE_DATE = "{A["rel_10"]["p50"]}"')
per = [("방향 고정", "2026-10-29", A['g_dir']['p50']), ("최소 언어", None, A['g_min']['p50']), ("AI 개선 루프와 첫 공개", None, A['rel_01']['p50']),
       ("실제로 쓰기", None, A['rel_03']['p50']), ("1.0", None, A['rel_10']['p50'])]
lines, prev = [], None
for name, s, e in per:
    s = s or (D(prev) + dt.timedelta(days=1)).isoformat()
    lines.append(f'    {{"name": "{name}", "start": "{s}", "end": "{e}"}},')
    prev = e
import re
txt = re.sub(r'PERIODS = \[\n.*?\n\]', 'PERIODS = [\n' + '\n'.join(lines) + '\n]', txt, flags=re.S)
open('wbs_data.py', 'w', encoding='utf-8').write(txt)
print(txt.split('GROUPS')[0][-700:])
