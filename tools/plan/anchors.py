"""Milestone dates (P50, P80) for the planning scenario: one account per company (about 15 AI working days a week), measurement sized for subscriptions."""
import random, json, sys, datetime as dt
from collections import defaultdict
sys.argv = [sys.argv[0], '2000']
import montecarlo2 as MC
import montecarlo as M
LIGHT = dict(M.MEASURE, b_ref27=85, b_full28=75, b_final=140)
random.seed(2029)
ms = [i['id'] for i in MC.ITEMS if i.get('milestone')]
res = defaultdict(list)
S = 2000
for _ in range(S):
    r = MC.run(list(M.SCEN)[0], True, measure=LIGHT)
    for m in ms:
        res[m].append(r[m] if r[m] is not None else MC.HORIZON)
out = {}
for m in ms:
    v = sorted(res[m])
    out[m] = {q: str(MC.T0 + dt.timedelta(days=int(v[int(p * S)]))) for q, p in (("p50", 0.5), ("p80", 0.8), ("p10", 0.1))}
    print(m, out[m])
json.dump(out, open('anchors.json', 'w'), indent=1)
