(() => {
  const P = PLAN;
  const DAY = 86400000;
  const NS = 'http://www.w3.org/2000/svg';
  const toT = s => { const [y, m, d] = s.split('-').map(Number); return Date.UTC(y, m - 1, d); };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const el = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
  const man = x => x >= 10000 ? '약 ' + String(Math.round(x / 1000) / 10).replace(/\.0$/, '') + '만 달러' : '약 ' + (Math.round(x / 100) * 100).toLocaleString('ko-KR') + '달러';

  // ---- schedule chart
  const T0 = toT(P.start), T1 = toT(P.end) + DAY;
  const avail = document.getElementById('g-scroll').clientWidth;
  const PX = avail >= 860 ? Math.max((avail - 2) / ((T1 - T0) / DAY), 0.6) : 1.1;
  const W = Math.round((T1 - T0) / DAY * PX);
  const X = s => (toT(s) - T0) / DAY * PX;
  const XE = s => (toT(s) + DAY - T0) / DAY * PX;
  const TOP = 50, ROW = 13, BAR = 9, PAD = 6;
  const byGroup = {};
  P.items.forEach(it => (byGroup[it.group] = byGroup[it.group] || []).push(it));
  let y = TOP;
  const lanes = P.groups.map(g => {
    const its = (byGroup[g.id] || []).filter(i => !i.milestone).sort((a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0));
    const rows = [], place = [];
    its.forEach(it => {
      const x0 = X(it.start), x1 = Math.max(XE(it.end), x0 + 3);
      let r = rows.findIndex(end => end + 3 <= x0);
      if (r < 0) { r = rows.length; rows.push(x1); } else rows[r] = x1;
      place.push({ it, r, x0, x1 });
    });
    const ms = (byGroup[g.id] || []).filter(i => i.milestone);
    const h = PAD * 2 + (Math.max(rows.length, 1) + (ms.length ? 1 : 0)) * ROW;
    const lane = { g, y, h, place, ms, msRow: rows.length };
    y += h;
    return lane;
  });
  const H = y + 6;
  const svg = document.getElementById('g-svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', W);
  svg.setAttribute('height', H);
  P.periods.forEach((p, k) => {
    const x0 = Math.max(X(p.start), 0), x1 = XE(p.end);
    el('rect', { x: x0, y: 0, width: x1 - x0, height: H, fill: k % 2 ? 'var(--band)' : 'var(--surface)' }, svg);
    el('text', { x: x0 + 4, y: 13, class: 'g-period' }, svg).textContent = p.name;
  });
  for (let yy = 2026; yy <= 2029; yy++) for (let m = 1; m <= 12; m++) {
    const s = `${yy}-${String(m).padStart(2, '0')}-01`;
    const t = toT(s);
    if (t < T0 || t >= T1) continue;
    const x = X(s);
    el('line', { x1: x, x2: x, y1: 18, y2: m === 1 ? H : 30, class: m === 1 ? 'g-year' : 'g-tick' }, svg);
    if (m === 1 || m === 7) el('text', { x: x + 3, y: 29, class: 'g-axis' }, svg).textContent = m === 1 ? `${yy}년` : '7월';
  }
  el('text', { x: 3, y: 29, class: 'g-axis' }, svg).textContent = '2026년 10월';
  lanes.forEach(L => el('line', { x1: 0, x2: W, y1: L.y + L.h, y2: L.y + L.h, class: 'g-sep' }, svg));
  P.gates.forEach(gt => {
    const x = X(gt.date) + PX / 2;
    el('line', { x1: x, x2: x, y1: 34, y2: H, class: 'g-gate' }, svg);
    const end = gt.id === 'g_10';
    el('text', { x: end ? x - 3 : x + 3, y: 44, class: 'g-gate-t', 'text-anchor': end ? 'end' : 'start' }, svg).textContent = gt.label;
  });
  const xt = X(P.today);
  el('line', { x1: xt, x2: xt, y1: 34, y2: H, class: 'g-today' }, svg);
  const nodes = [];
  lanes.forEach(L => {
    L.place.forEach(({ it, r, x0, x1 }) => {
      const yb = L.y + PAD + r * ROW + (ROW - BAR) / 2;
      const cls = 'g-bar' + (it.critical ? ' crit' : '') + (it.ongoing ? ' ongo' : '');
      const rc = el('rect', { x: x0.toFixed(1), y: yb, width: Math.max(x1 - x0 - 1, 2).toFixed(1), height: BAR, rx: 2, class: cls, tabindex: 0, role: 'button', 'aria-label': `${it.name}, ${it.start}부터 ${it.end}까지` }, svg);
      rc.__it = it;
      nodes.push(rc);
    });
    L.ms.forEach(it => {
      const cx = X(it.start) + PX / 2, cy = L.y + PAD + L.msRow * ROW + ROW / 2, s = 5.5;
      const d = el('path', { d: `M${cx} ${cy - s}L${cx + s} ${cy}L${cx} ${cy + s}L${cx - s} ${cy}Z`, class: 'g-ms', tabindex: 0, role: 'button', 'aria-label': `이정표 ${it.name}, ${it.start}` }, svg);
      d.__it = it;
      nodes.push(d);
    });
  });
  const lab = document.getElementById('g-labels');
  lab.style.height = H + 'px';
  const head = document.createElement('div');
  head.className = 'g-lab g-lab-head';
  head.style.top = '0px'; head.style.height = TOP + 'px';
  head.textContent = '갈래';
  lab.appendChild(head);
  lanes.forEach(L => {
    const d = document.createElement('div');
    d.className = 'g-lab';
    d.style.top = L.y + 'px'; d.style.height = L.h + 'px';
    d.textContent = L.g.name;
    lab.appendChild(d);
  });
  const det = document.getElementById('g-detail');
  const eff = it => [it.human_h ? `사람 ${it.human_h}h` : '', it.ai_days ? `AI ${it.ai_days}작업일` : '', it.cost_usd ? man(it.cost_usd) : ''].filter(Boolean).join(' · ') || '—';
  let sel = null;
  function show(node) {
    if (sel) sel.classList.remove('sel');
    sel = node; node.classList.add('sel');
    const it = node.__it;
    const g = P.groups.find(x => x.id === it.group).name;
    const range = it.start === it.end ? it.start : `${it.start} ~ ${it.end}`;
    const tags = [it.milestone ? ['ms', '이정표'] : null, it.critical ? ['crit', '주경로'] : null, it.ongoing ? ['on', '상시'] : null].filter(Boolean).map(([c, t]) => `<span class="tag ${c}">${t}</span>`).join('');
    det.innerHTML = `<p class="d-top"><span class="it-date">${range}</span><strong>${esc(it.name)}</strong>${tags}</p><p class="d-g">${esc(g)}</p>`
      + `<dl class="it-kv"><div><dt>산출물</dt><dd>${esc(it.out)}</dd></div><div><dt>끝났다는 표시</dt><dd>${esc(it.done)}</dd></div>`
      + `<div><dt>앞선 일</dt><dd>${esc((it.after_names || []).join(', ') || '—')}</dd></div><div><dt>맡는 쪽 · 공수</dt><dd>${esc(it.who)} · ${esc(eff(it))}</dd></div></dl>`;
  }
  nodes.forEach(n => {
    n.addEventListener('click', () => show(n));
    n.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); show(n); } });
  });
  const first = nodes.find(n => n.__it.id === 'b_baseline');
  if (first) show(first);

  // ---- monthly charts
  const tip = document.getElementById('tip');
  const showTip = (e, text) => {
    if (text) tip.textContent = text;
    tip.hidden = false;
    const x = Math.min(e.clientX + 12, window.innerWidth - tip.offsetWidth - 8);
    tip.style.left = Math.max(8, x) + 'px';
    tip.style.top = Math.max(8, e.clientY - 34) + 'px';
  };
  const hideTip = () => { tip.hidden = true; };
  function bars(id, series, unit, ref) {
    const s = document.getElementById(id);
    const keys = Object.keys(series).sort();
    const Wc = 640, Hc = 210, L = 30, R = 8, T = 12, Bm = 24;
    const max = Math.max(...keys.map(k => series[k]), ...[].concat(ref || [0]));
    const step = max > 20 ? 10 : max > 8 ? 2 : 1;
    const top = Math.ceil(max / step) * step;
    const ih = Hc - T - Bm, bw = (Wc - L - R) / keys.length;
    s.setAttribute('viewBox', `0 0 ${Wc} ${Hc}`);
    for (let v = 0; v <= top; v += step * (top / step > 6 ? 2 : 1)) {
      const yy = T + ih * (1 - v / top);
      el('line', { x1: L, x2: Wc - R, y1: yy, y2: yy, class: 'c-grid' }, s);
      el('text', { x: L - 6, y: yy + 4, class: 'c-ax', 'text-anchor': 'end' }, s).textContent = v;
    }
    keys.forEach((k, n) => {
      const v = series[k], h = Math.max(ih * v / top, 0.8), x = L + n * bw + 1;
      const b = el('rect', { x, y: T + ih - h, width: Math.max(bw - 2, 1), height: h, rx: 2, class: 'c-bar' }, s);
      const hit = el('rect', { x: L + n * bw, y: T, width: bw, height: ih, class: 'c-hit' }, s);
      const label = `${k.slice(0, 4)}년 ${Number(k.slice(5))}월 · ${Math.round(v * 10) / 10}${unit}`;
      hit.addEventListener('pointerenter', e => { b.classList.add('hl'); showTip(e, label); });
      hit.addEventListener('pointermove', e => showTip(e));
      hit.addEventListener('pointerleave', () => { b.classList.remove('hl'); hideTip(); });
      if ((k.endsWith('-01') || n === 0) && x + 30 < Wc - R) el('text', { x: x + 1, y: Hc - 7, class: 'c-ax' }, s).textContent = k.slice(0, 4);
    });
    [].concat(ref || []).forEach(rv => {
      const yy = T + ih * (1 - rv / top);
      el('line', { x1: L, x2: Wc - R, y1: yy, y2: yy, class: 'c-ref' }, s);
      el('text', { x: Wc - R - 2, y: yy - 5, class: 'c-ax', 'text-anchor': 'end' }, s).textContent = `${rv}${unit}`;
    });
  }
  bars('c-hours', P.hours, '시간');
  bars('c-agents', P.demand, '작업일', [15, 10]);

  // ---- work breakdown filters
  const chips = [...document.querySelectorAll('.chip[data-period]')];
  const critBtn = document.getElementById('only-crit');
  let period = 'all', crit = false;
  function apply() {
    let shown = 0;
    document.querySelectorAll('.it').forEach(li => {
      let ok = true;
      if (period !== 'all') { const [ps, pe] = period.split('|'); ok = li.dataset.s <= pe && li.dataset.e >= ps; }
      if (crit && li.dataset.crit !== '1') ok = false;
      li.hidden = !ok;
      if (ok) shown++;
    });
    document.querySelectorAll('.wg').forEach(sec => { sec.hidden = !sec.querySelector('.it:not([hidden])'); });
    document.getElementById('wbs-empty').hidden = shown > 0;
  }
  chips.forEach(c => c.addEventListener('click', () => {
    chips.forEach(x => { x.classList.toggle('on', x === c); x.setAttribute('aria-pressed', String(x === c)); });
    period = c.dataset.period;
    apply();
  }));
  critBtn.addEventListener('click', () => { crit = !crit; critBtn.classList.toggle('on', crit); critBtn.setAttribute('aria-pressed', String(crit)); apply(); });
})();
