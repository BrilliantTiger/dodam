import random, json, sys
sys.argv = [sys.argv[0], '1200']
import montecarlo2 as MC
import montecarlo as M
random.seed(11)
LIGHT = dict(M.MEASURE, b_ref27=85, b_full28=75, b_final=140)  # measurement sized for subscriptions
DEFER = ("p_reg", "k_llvm")                                      # registry and LLVM release builds after 1.0
ONE, DOUBLE, TWO_COMPANIES = list(M.SCEN)
rows = {}
for label, scen, kw in [
    ("계정 하나, 측정 원안", ONE, {}),
    ("계정 하나, 측정을 구독에 맞게 줄임", ONE, dict(measure=LIGHT)),
    ("계정 하나, 측정 줄임 + 저장소 · LLVM 1.0 뒤로", ONE, dict(measure=LIGHT, defer=DEFER)),
    ("용량 두 배, 측정을 구독에 맞게 줄임", DOUBLE, dict(measure=LIGHT)),
    ("용량 두 배, 측정 줄임 + 저장소 · LLVM 1.0 뒤로", DOUBLE, dict(measure=LIGHT, defer=DEFER)),
    ("두 회사, 측정을 구독에 맞게 줄임", TWO_COMPANIES, dict(measure=LIGHT)),
]:
    r = MC.summary(scen, True, sims=1200, **kw)
    rows[label] = r
    print(f"{label}: 0.1 P50 {r['rel_01'][1]} | 1.0 P10 {r['rel_10'][0]} P50 {r['rel_10'][1]} P80 {r['rel_10'][2]} P90 {r['rel_10'][3]} | P(<=2029-12-28) {r['p_latest']:.1%} P(<=2030-12-31) {r['p_2030']:.1%}")
json.dump(rows, open('scenarios.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
