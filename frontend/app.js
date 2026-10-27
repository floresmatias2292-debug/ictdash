/* FVG-ICT charts — TradingView-style frontend */
const TFS = ['5m', '15m', '1h'];
const TF_SEC = { '5m': 300, '15m': 900, '1h': 3600 };
const LAYERS = { oracle: 'CoinGecko consensus line', fvg: 'Fair value gaps', mitigated: 'Mitigated FVG / OB', ob: 'Order blocks', structure: 'BOS / CHoCH', sweeps: 'Liquidity sweeps', pd: 'Premium / Discount', or: 'Opening range 9:30', volume: 'Volume' };
const CLEAN = { fvg: 4, ob: 3, structure: 3, sweeps: 2, ors: { '5m': 2, '15m': 3, '1h': 3 } };

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
const S = {
  sym: store.get('sym', 'NVDA'), layout: store.get('layout', 'focus'), focus: store.get('focus', '5m'),
  source: store.get('source', 'perp'), detail: store.get('detail', 'clean'), theme: store.get('theme', 'light'),
  show: { ...Object.fromEntries(Object.keys(LAYERS).map((k) => [k, k !== 'mitigated'])), ...store.get('layers', {}) },
  tz: store.get('tz', 'local'), data: null, live: {}, sessionOpen: false, panes: {}, seen: null, alertsOn: false, panel: store.get('panel', true),
};
if (!TFS.includes(S.focus)) S.focus = '5m';

const $ = (s, r = document) => r.querySelector(s);
const fmt = (x, d = 2) => (x == null || isNaN(x) ? '—' : Number(x).toFixed(d));
const sgn = (x, d = 2) => (x == null || isNaN(x) ? '—' : `${x >= 0 ? '+' : ''}${Number(x).toFixed(d)}`);
const vol = (v) => (v == null ? '—' : v >= 1e9 ? (v / 1e9).toFixed(2) + 'B' : v >= 1e6 ? (v / 1e6).toFixed(2) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'K' : String(Math.round(v)));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

// ── time zone: local legal time by default (browser zone), New York optional ──
const LOCAL_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
const tzName = () => (S.tz === 'ny' ? 'America/New_York' : LOCAL_TZ);
const _offCache = new Map();
function tzOffset(ts, tz = tzName()) {
  // seconds to add to a UTC epoch so lightweight-charts (which draws UTC) shows wall-clock time in `tz`
  const key = `${tz}|${Math.floor(ts / 3600)}`;
  if (_offCache.has(key)) return _offCache.get(key);
  const d = new Date(ts * 1000), p = (z) => new Date(d.toLocaleString('en-US', { timeZone: z }));
  const off = Math.round((p(tz) - p('UTC')) / 1000);
  _offCache.set(key, off);
  return off;
}
const toChart = (ts) => ts + tzOffset(ts);
const fmtTime = (ts) => new Date(ts * 1000).toLocaleString('es-AR', { timeZone: tzName(), day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });
const tzLabel = () => {
  const off = tzOffset(Date.now() / 1000) / 3600;
  return `${S.tz === 'ny' ? 'New York' : LOCAL_TZ.split('/').pop().replace(/_/g, ' ')} (UTC${off >= 0 ? '+' : '−'}${Math.abs(off)})`;
};
function shiftPayload(d) {
  // the server sends UTC epochs; convert once to chart time in the selected zone
  const sh = (b) => ({ ...b, time: toChart(b.time) });
  for (const tf of Object.keys(d.charts || {})) {
    const c = d.charts[tf]; if (!c) continue;
    c.candles = c.candles.map(sh); c.forming = c.forming.map(sh);
  }
  for (const tf of Object.keys(d.omni || {})) d.omni[tf].candles = d.omni[tf].candles.map(sh);
  d.oracle_hist = (d.oracle_hist || []).map(([t, i, v]) => [toChart(t), i, v]);
  return d;
}

// ── colors (TradingView palette) ───────────────────────────────────────────
const C = () => ({
  bull: '#089981', bear: '#f23645', bullFill: 'rgba(8,153,129,0.14)', bearFill: 'rgba(242,54,69,0.13)',
  bullLine: 'rgba(8,153,129,0.75)', bearLine: 'rgba(242,54,69,0.75)',
  obBull: 'rgba(41,98,255,0.12)', obBullB: 'rgba(41,98,255,0.75)', obBear: 'rgba(255,152,0,0.13)', obBearB: 'rgba(255,152,0,0.85)',
  mit: 'rgba(120,123,134,0.08)', mitB: 'rgba(120,123,134,0.5)', sweep: '#f7a600', choch: S.theme === 'dark' ? '#d1d4dc' : '#131722',
  eq: 'rgba(120,123,134,0.8)', or: 'rgba(8,153,129,0.07)', orB: 'rgba(8,153,129,0.85)', accent: '#2962ff',
  halo: S.theme === 'dark' ? 'rgba(19,23,34,0.9)' : 'rgba(255,255,255,0.92)',
});

// ── overlay items from the Pine port output ────────────────────────────────
function nearest(zones, price, k) {
  const d = (z) => (price >= z.bot && price <= z.top ? 0 : Math.min(Math.abs(price - z.top), Math.abs(price - z.bot)));
  return zones.slice().sort((a, b) => d(a) - d(b)).slice(0, k);
}
function buildOverlay(c, tf) {
  const col = C(), items = [], sh = S.show, clean = S.detail === 'clean';
  const n = c.candles.length, last = n ? c.candles[n - 1].close : 0;
  if (sh.pd && c.pd) {
    if (!clean) {
      items.push({ kind: 'box', l: c.pd.x0, r: n + 15, top: c.pd.hi, bot: c.pd.eq, fill: 'rgba(242,54,69,0.04)' });
      items.push({ kind: 'box', l: c.pd.x0, r: n + 15, top: c.pd.eq, bot: c.pd.lo, fill: 'rgba(8,153,129,0.04)' });
    }
    items.push({ kind: 'seg', l: c.pd.x0, r: n + 15, price: c.pd.hi, color: 'rgba(242,54,69,0.45)', dash: [2, 3], text: 'Premium', textColor: col.bear, align: 'left', textPos: 'below' });
    items.push({ kind: 'seg', l: c.pd.x0, r: n + 15, price: c.pd.eq, color: col.eq, dash: [4, 3], text: 'EQ', textColor: col.eq, align: 'left' });
    items.push({ kind: 'seg', l: c.pd.x0, r: n + 15, price: c.pd.lo, color: 'rgba(8,153,129,0.45)', dash: [2, 3], text: 'Discount', textColor: col.bull, align: 'left' });
  }
  if (sh.or && c.ors) for (const o of (clean ? c.ors.slice(-CLEAN.ors[tf]) : c.ors))
    items.push({ kind: 'box', l: o.start, r: o.end, top: o.high, bot: o.low, fill: col.or, stroke: col.orB, width: 1.5, text: 'OR NY', textColor: col.orB });
  const zones = (arr, k) => {
    const a = arr.filter((z) => z.state === 'active' || (sh.mitigated && !clean));
    return clean ? nearest(a.filter((z) => z.state === 'active'), last, k) : a;
  };
  if (sh.ob) for (const o of zones(c.obs, CLEAN.ob)) {
    const act = o.state === 'active';
    items.push({ kind: 'box', l: o.left, r: act ? null : o.end, top: o.top, bot: o.bot, fill: act ? (o.bull ? col.obBull : col.obBear) : col.mit,
      stroke: act ? (o.bull ? col.obBullB : col.obBearB) : col.mitB, dash: act ? null : [2, 2], text: act ? `${o.bull ? 'Bull' : 'Bear'} OB` : null, textColor: o.bull ? col.obBullB : col.obBearB });
  }
  if (sh.fvg) for (const z of zones(c.fvgs, CLEAN.fvg)) {
    const act = z.state === 'active';
    items.push({ kind: 'box', l: z.left, r: act ? null : z.end, top: z.top, bot: z.bot, fill: act ? (z.bull ? col.bullFill : col.bearFill) : col.mit,
      stroke: act ? (z.bull ? col.bullLine : col.bearLine) : col.mitB, width: z.comps.strong && act ? 2 : 1, dash: act ? null : [2, 2],
      text: act ? `${z.bull ? '▲' : '▼'} FVG ${'★'.repeat(z.score) || '·'}` : null, textColor: z.bull ? col.bull : col.bear });
  }
  if (sh.sweeps) for (const s of (clean ? c.sweeps.slice(-CLEAN.sweeps) : c.sweeps))
    items.push({ kind: 'seg', l: s.from, r: s.i, price: s.price, color: col.sweep, dash: [4, 3], width: 1.5, text: `${s.kind} sweep`, textPos: s.kind === 'BSL' ? 'above' : 'below', align: 'right' });
  if (sh.structure) for (const s of (clean ? c.structure.slice(-CLEAN.structure) : c.structure)) {
    const cl = s.kind === 'CHoCH' ? col.choch : s.dir === 1 ? col.bull : col.bear;
    items.push({ kind: 'seg', l: s.from, r: s.i, price: s.price, color: cl, width: s.kind === 'CHoCH' ? 1.5 : 1, text: `${s.kind} ${s.dir === 1 ? '▲' : '▼'}`, textPos: s.dir === 1 ? 'above' : 'below' });
  }
  return items;
}
function buildMarkers(c) {
  if (S.detail === 'clean') return [];
  const col = C(), m = [];
  for (const z of c.fvgs) {
    const born = z.left + 2;
    if (!(z.comps.strong || z.score >= 3) || born >= c.candles.length) continue;
    m.push({ time: c.candles[born].time, position: z.bull ? 'belowBar' : 'aboveBar', color: z.bull ? col.bull : col.bear,
      shape: z.bull ? 'arrowUp' : 'arrowDown', text: `${z.size_pct.toFixed(3)}% ${'★'.repeat(z.score)}` });
  }
  return m.sort((a, b) => a.time - b.time);
}

// ── panes ──────────────────────────────────────────────────────────────────
function chartOptions() {
  return {
    autoSize: true,
    layout: { background: { color: css('--pane') }, textColor: css('--text'), fontSize: 11, fontFamily: '-apple-system, BlinkMacSystemFont, Trebuchet MS, Roboto, sans-serif' },
    grid: { vertLines: { color: css('--grid') }, horzLines: { color: css('--grid') } },
    rightPriceScale: { borderColor: css('--line'), scaleMargins: { top: 0.14, bottom: 0.22 } },
    timeScale: { borderColor: css('--line'), timeVisible: true, secondsVisible: false, rightOffset: 6, barSpacing: 7 },
    crosshair: { mode: 0, vertLine: { color: css('--muted'), labelBackgroundColor: css('--chrome') }, horzLine: { color: css('--muted'), labelBackgroundColor: css('--chrome') } },
  };
}
function makePane(tf) {
  const el = document.createElement('div');
  el.className = 'pane'; el.dataset.tf = tf;
  el.innerHTML = `<div class="chart"></div><div class="legend"></div>
    <div class="pane-tools"><button class="mk" title="Make this the large chart">◎</button><button class="mx" title="Maximize (Esc to restore)">⤢</button></div>
    <div class="pbadge"></div>`;
  $('#grid').appendChild(el);
  const chart = LightweightCharts.createChart($('.chart', el), chartOptions());
  const candles = chart.addCandlestickSeries({ upColor: '#089981', downColor: '#f23645', borderVisible: false, wickUpColor: '#089981', wickDownColor: '#f23645', priceLineVisible: true, priceLineWidth: 1 });
  const volume = chart.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false });
  chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  const overlay = new Overlay();
  candles.attachPrimitive(overlay);
  const mark = candles.createPriceLine({ price: 0, color: '#2962ff', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: 'Omni' });
  const oracle = chart.addLineSeries({ color: '#9c27b0', lineWidth: 2, lineStyle: 1, priceLineVisible: false, lastValueVisible: true, title: 'CG', crosshairMarkerVisible: false });
  el.insertAdjacentHTML('beforeend', '<div class="tvbox hidden"></div>');
  const p = { tf, el, chart, candles, volume, overlay, mark, oracle, fitted: false, bars: [], tvKey: null };
  chart.subscribeCrosshairMove((param) => legend(p, param));
  $('.mx', el).onclick = () => maximize(tf);
  $('.mk', el).onclick = () => setFocus(tf);
  S.panes[tf] = p;
}
function applyTheme() {
  document.documentElement.dataset.theme = S.theme;
  window.OVERLAY_HALO = C().halo;
  for (const p of Object.values(S.panes)) { p.chart.applyOptions(chartOptions()); p.tvKey = null; }
  if (S.data) renderAll();
}

// ── layout ─────────────────────────────────────────────────────────────────
let preMax = null;
function applyLayout() {
  const g = $('#grid');
  g.className = `grid layout-${S.layout}`;
  const order = [S.focus, ...TFS.filter((t) => t !== S.focus)];
  for (const tf of order) { const p = S.panes[tf]; p.el.classList.toggle('big', tf === S.focus); g.appendChild(p.el); }
  document.querySelectorAll('#layouts button').forEach((b) => b.classList.toggle('on', b.dataset.l === S.layout));
  $('#focusTf').innerHTML = TFS.map((t) => `<button data-t="${t}" class="${t === S.focus ? 'on' : ''}">${t}</button>`).join('');
  for (const p of Object.values(S.panes)) $('.mx', p.el).textContent = S.layout === 'single' && preMax ? '⤡' : '⤢';
}
function setLayout(l) { S.layout = l; preMax = null; store.set('layout', l); applyLayout(); }
function setFocus(tf) { S.focus = tf; store.set('focus', tf); applyLayout(); renderPanel(); }
function maximize(tf) {
  if (S.layout === 'single' && preMax) { S.layout = preMax.layout; S.focus = preMax.focus; preMax = null; }
  else { preMax = { layout: S.layout, focus: S.focus }; S.layout = 'single'; S.focus = tf; }
  applyLayout(); renderPanel();
}

// ── chart render ───────────────────────────────────────────────────────────
function seriesFor(tf) {
  if (S.source === 'omni') {
    const o = S.data.omni[tf] || { candles: [] };
    return { bars: o.candles.map((b) => ({ ...b })), overlayData: o.native ? { ...o.native, candles: o.candles.slice(0, -1) } : null, omni: true };
  }
  const c = S.data.charts[tf];
  return { bars: c ? [...c.candles, ...c.forming].map((b) => ({ ...b })) : [], overlayData: c, omni: false };
}
function renderChart(tf) {
  const p = S.panes[tf];
  const tv = S.source === 'tv';
  $('.tvbox', p.el).classList.toggle('hidden', !tv);
  for (const sel of ['.chart', '.legend', '.pbadge']) $(sel, p.el).style.visibility = tv ? 'hidden' : '';
  if (tv) { tradingViewWidget(p); return; }
  renderOracleLine(p);
  const { bars, overlayData, omni } = seriesFor(tf);
  p.bars = bars;
  const range = p.fitted ? p.chart.timeScale().getVisibleLogicalRange() : null;
  p.candles.setData(bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
  p.volume.setData(S.show.volume && !omni ? bars.map((b) => ({ time: b.time, value: b.volume || 0, color: b.close >= b.open ? 'rgba(8,153,129,0.45)' : 'rgba(242,54,69,0.45)' })) : []);
  p.overlay.setItems(overlayData ? buildOverlay(overlayData, tf) : omni ? yahooZonesOnOmni(tf) : []);
  p.candles.setMarkers(overlayData && !omni ? buildMarkers(overlayData) : []);
  if (range && p.fitted) p.chart.timeScale().setVisibleLogicalRange(range);
  else if (bars.length) { const n = bars.length; p.chart.timeScale().setVisibleLogicalRange(n < 120 ? { from: -2, to: Math.max(n + 4, 60) } : { from: n - 150, to: n + 6 }); p.fitted = true; }
  let msg = null;
  if (omni && bars.length < 3) msg = `Building ${S.data.market.omni} ${tf} candles from the live mark. The public Omni API has no history; the chart fills in while the server runs.`;
  else if (!omni && !bars.length) {
    const m = S.data.meta || {};
    msg = m.loading ? `First start on this computer: downloading ${S.data.market.source} history. The chart appears by itself in a few minutes.`
      : `No ${S.data.market.source} candles for ${S.sym} ${tf}.` + ((m.history_error?.[S.sym]) ? ` Error: ${m.history_error[S.sym]}. Check the internet connection / that ${S.data.market.source} is reachable from this network.` : ' Waiting for data…');
  }
  const empty = $('.empty', p.el);
  if (msg) { if (empty) empty.textContent = msg; else p.el.insertAdjacentHTML('beforeend', `<div class="empty">${msg}</div>`); }
  else empty?.remove();
  legend(p, null);
  badge(p);
}
// ── parallel oracle: CoinGecko consensus (median index across venues) ───────
function renderOracleLine(p) {
  if (S.source === 'omni' || !S.show.oracle) { p.oracle.setData([]); return; }
  const sec = TF_SEC[p.tf], pts = new Map();
  for (const [t, idx] of (S.data.oracle_hist || [])) pts.set(t - (t % sec), idx);   // last value per bar
  const first = p.bars.length ? p.bars[0].time : 0;
  p.oracle.setData([...pts.entries()].filter(([t]) => t >= first).sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value })));
}
function renderOracle() {
  const o = S.data?.oracle || {}, c = o.consensus, v = o.venue, last = lastPrice();
  const bp = (a, b) => (a && b ? (a / b - 1) * 1e4 : null);
  const cls = (x) => (x == null ? '' : Math.abs(x) >= 25 ? 'warn' : '');
  const fbp = (x) => (x == null ? '—' : `${x >= 0 ? '+' : ''}${x.toFixed(1)} bp`);
  if (!c && !v) { $('#oracleBlock').innerHTML = '<div class="muted">Waiting for the first CoinGecko / venue-index read…</div>'; return; }
  const age = c ? Math.round((Date.now() / 1000 - c.ts) / 60) : null;
  $('#oracleBlock').innerHTML = `<table>
    <tr><th></th><th>Price</th><th>vs consensus</th></tr>
    <tr><td>${S.data.market.source} last</td><td>${fmt(last)}</td><td class="${cls(bp(last, c?.index_med))}">${fbp(bp(last, c?.index_med))}</td></tr>
    <tr><td>${S.data.market.source} mark</td><td>${fmt(v?.mark)}</td><td class="${cls(bp(v?.mark, c?.index_med))}">${fbp(bp(v?.mark, c?.index_med))}</td></tr>
    <tr><td>${S.data.market.source} index (oracle)</td><td>${fmt(v?.index)}</td><td class="${cls(bp(v?.index, c?.index_med))}">${fbp(bp(v?.index, c?.index_med))}</td></tr>
    <tr><td><b style="color:#9c27b0">CoinGecko consensus</b></td><td><b>${fmt(c?.index_med)}</b></td><td class="muted">${c ? `${c.n} venues` : '—'}</td></tr>
    <tr><td>Venue index range</td><td colspan="2">${c ? `${fmt(c.lo)} – ${fmt(c.hi)} (${fbp(bp(c.hi, c.lo))} wide)` : '—'}</td></tr></table>
    ${c ? `<details style="margin-top:6px"><summary class="muted" style="cursor:pointer;font-size:11px">Venues (top by volume)</summary><table>${c.venues.map((x) => `<tr><td>${x.market.replace(/ \(Futures\)| Futures/, '')}</td><td>${fmt(x.index)}</td><td class="muted">${fbp(bp(x.index, c.index_med))}</td></tr>`).join('')}</table></details>` : ''}
    <div class="note">Consensus = median of the index (oracle) prices of every perp venue CoinGecko lists for this market (stale, thin and > 2% off-median venues dropped), read every 2 min${age != null ? ` · last read ${age} min ago` : ''}. Purple line on the charts. Amber = more than 25 bp away.</div>`;
}

// ── TradingView reference view (official free embed widget; no data API) ────
function tradingViewWidget(p) {
  const sym = S.data.tradingview, interval = { '5m': '5', '15m': '15', '1h': '60' }[p.tf];
  const key = `${sym}|${interval}|${S.theme}|${tzName()}`;
  if (p.tvKey === key) return;
  p.tvKey = key;
  const box = $('.tvbox', p.el);
  box.innerHTML = '<div class="tradingview-widget-container" style="height:100%;width:100%"><div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div></div>';
  const sc = document.createElement('script');
  sc.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
  sc.async = true;
  sc.textContent = JSON.stringify({ autosize: true, symbol: sym, interval, timezone: tzName(), theme: S.theme, style: '1', locale: 'en',
    hide_side_toolbar: true, allow_symbol_change: false, save_image: false, calendar: false, support_host: 'https://www.tradingview.com' });
  $('.tradingview-widget-container', box).appendChild(sc);
}

function yahooZonesOnOmni(tf) {
  const y = S.data.charts[tf], o = S.data.omni[tf], col = C();
  if (!y || !o || !o.candles.length) return [];
  const px = o.candles[o.candles.length - 1].close, items = [];
  if (S.show.ob) for (const z of nearest(y.obs.filter((z) => z.state === 'active'), px, 2))
    items.push({ kind: 'box', l: -1e6, r: null, top: z.top, bot: z.bot, fill: 'rgba(120,123,134,0.05)', stroke: z.bull ? col.obBullB : col.obBearB, dash: [4, 3], text: `${S.data.market.source} ${z.bull ? 'bull' : 'bear'} OB`, textColor: z.bull ? col.obBullB : col.obBearB });
  if (S.show.fvg) for (const z of nearest(y.fvgs.filter((z) => z.state === 'active'), px, 2))
    items.push({ kind: 'box', l: -1e6, r: null, top: z.top, bot: z.bot, fill: 'rgba(120,123,134,0.05)', stroke: z.bull ? col.bullLine : col.bearLine, dash: [4, 3], text: `${S.data.market.source} ${z.bull ? 'bull' : 'bear'} FVG`, textColor: z.bull ? col.bull : col.bear });
  return items;
}
function legend(p, param) {
  if (!S.data) return;
  let b = null;
  if (param && param.time != null) b = p.bars.find((x) => x.time === param.time);
  if (!b) b = p.bars[p.bars.length - 1];
  const c = S.data.charts[p.tf], t = c?.table || {};
  const omni = S.source === 'omni';
  const chg = b ? (b.close - b.open) / b.open * 100 : null;
  const cls = b && b.close >= b.open ? 'up' : 'dn';
  const rv = c?.rvol || {};
  const r = rv.forming_rvol ?? rv.rvol;
  const trend = t.trend === 1 ? '<span class="up">▲ Bullish</span>' : t.trend === -1 ? '<span class="dn">▼ Bearish</span>' : '—';
  $('.legend', p.el).innerHTML = `
    <div class="l1"><span class="sym">${S.sym}</span> <span class="src">· ${p.tf} · ${omni ? S.data.market.omni + ' (built)' : S.data.market.source + ' ' + S.data.market.ticker}</span>
      ${b ? `<span class="k">O</span><span class="v ${cls}">${fmt(b.open)}</span><span class="k">H</span><span class="v ${cls}">${fmt(b.high)}</span><span class="k">L</span><span class="v ${cls}">${fmt(b.low)}</span><span class="k">C</span><span class="v ${cls}">${fmt(b.close)}</span> <span class="v ${cls}">${sgn(chg)}%</span>` : ''}</div>
    ${!omni && S.show.volume ? `<div><span class="k" style="margin-left:0">Vol</span> <span class="v">${vol(b?.volume)}</span><span class="k">RVOL</span><span class="rv"><i style="width:${Math.min(100, (r || 0) / 3 * 100)}%"></i></span><span class="v ${r >= 1.5 ? 'up' : ''}">${r == null ? '—' : r.toFixed(2) + '×'}</span>${b && b.buy_pct != null ? `<span class="k">Buy</span><span class="v ${b.buy_pct >= 55 ? 'up' : b.buy_pct <= 45 ? 'dn' : ''}">${b.buy_pct.toFixed(0)}%</span>` : ''}</div>` : ''}
    <div class="ind">FVG-ICT ${trend} · ${t.pd ? `${t.pd.zone} ${Math.round(t.pd.pos)}%` : '—'} · FVG ${t.fvg_bull ?? 0}/${t.fvg_bear ?? 0} · OB ${t.ob_bull ?? 0}/${t.ob_bear ?? 0}</div>`;
}
function badge(p) {
  const pr = S.data.charts[p.tf]?.prob;
  $('.pbadge', p.el).innerHTML = pr ? `<span class="L">L ${Math.round(pr.p_up * 100)}%</span><span class="S">S ${Math.round((1 - pr.p_up) * 100)}%</span>` : '';
}
function renderAll() { for (const tf of TFS) renderChart(tf); renderPanel(); }

// ── side panel ─────────────────────────────────────────────────────────────
function skillTag(sk) {
  if (!sk || sk.auc == null) return '<span class="skill none">untested</span>';
  const edge = sk.auc >= 0.53 && sk.logloss < sk.logloss_base;
  return `<span class="skill ${edge ? 'ok' : 'none'}" title="Out-of-sample AUC ${sk.auc.toFixed(3)} (0.5 = coin flip). Log-loss ${sk.logloss.toFixed(4)} vs base rate ${sk.logloss_base.toFixed(4)}.">${edge ? 'edge' : 'no edge'} · AUC ${sk.auc.toFixed(2)}</span>`;
}
function renderPanel() {
  if (!S.data) return;
  const d = S.data, q = S.live[S.sym];
  const yl = d.charts['5m']?.candles; const yc = yl ? yl[yl.length - 1].close : null;
  const last = lastPrice();
  $('#quoteBlock').innerHTML = `<div class="qsym">${S.sym} <span class="qsub">${d.market.source} · ${d.market.ticker}</span></div>
    <div class="qpx">${fmt(last)}</div>
    <div class="qsub"><b id="clock"></b> · ${tzLabel()}</div>
    <div class="qsub">Perp price, 24/7 · Omni ${d.market.omni} ${q ? fmt(q.mark) : '—'}${q && last ? ` (${sgn((q.mark / last - 1) * 100)}%)` : ''} · NY cash ${S.sessionOpen ? 'open' : 'closed'}</div>`;
  $('#probBlock').innerHTML = TFS.map((tf) => {
    const pr = d.charts[tf]?.prob;
    if (!pr) return '';
    const L = pr.p_up * 100;
    return `<div class="prow"><span class="tf">${tf}</span><div class="pbar"><div class="L" style="width:${L}%">L ${L.toFixed(1)}%</div><div class="S" style="width:${100 - L}%">S ${(100 - L).toFixed(1)}%</div></div>
      <div class="pmeta">${skillTag(pr.skill)} · base ${(pr.base_up * 100).toFixed(1)}% up · n=${pr.n_train.toLocaleString()}</div></div>`;
  }).join('') + `<div class="note">+${d.model.k_atr}×ATR before −${d.model.k_atr}×ATR from the next bar, within ${Object.entries(d.model.horizon).map(([t, h]) => `${h} bars on ${t}`).join(', ')}. Logistic model on the Pine state (structure, sweeps, CHoCH, FVG/OB proximity, P/D, OR, momentum, RVOL, HTF trend), refit every closed bar.</div>`;
  const sig = TFS.map((tf) => [tf, d.charts[tf]?.signal]).filter(([, s]) => s && s.side);
  const thr = d.settings.signal_prob;
  $('#signalBlock').innerHTML = sig.length ? sig.map(([tf, s]) => `
      <div class="sig ${s.side === 1 ? 'bull' : 'bear'}">${s.side === 1 ? 'LONG' : 'SHORT'} ${S.sym} · ${tf} · ${Math.max(s.p_long, s.p_short).toFixed(0)}%</div>
      <div class="kv"><span>Entry (Omni mark)</span><span>${fmt(s.entry)}</span><span>Stop (1×ATR)</span><span class="bear">${fmt(s.stop)}</span>
      <span>Target (1×ATR)</span><span class="bull">${fmt(s.target)}</span><span>Size</span><span>${fmt(s.notional)} USDC · x${s.lev} · margin ${fmt(s.margin)}</span>
      <span>Liquidation</span><span class="warn">${fmt(s.liq)}</span><span>Risk</span><span>${fmt(s.risk_usdc)} USDC (${d.settings.risk_pct}%) · costs ${fmt(s.cost_r)}R</span></div>`).join('<hr>')
    : `<div class="muted">No signal: no timeframe at ≥ ${thr}% long or short.</div>
       <div class="note">Out of sample, none of the timeframes shows a directional edge (AUC ≈ 0.50), so the model keeps probabilities near 50%. A signal appears only if the probability crosses ${thr}%.</div>`;
  $('#volBlock').innerHTML = TFS.map((tf) => {
    const rv = d.charts[tf]?.rvol || {}; const r = rv.forming_rvol ?? rv.rvol;
    return `<div class="vrow"><span class="tf">${tf}</span><div class="vmeter" title="Relative volume: last bar vs 20-bar average (pace-adjusted while forming). Tick = 1×."><i style="width:${Math.min(100, (r || 0) / 3 * 100)}%"></i><b></b></div><span class="${r >= 1.5 ? 'bull' : r < 0.7 ? 'muted' : ''}">${r == null ? '—' : r.toFixed(2) + '×'}</span></div>`;
  }).join('') + (d.charts['5m']?.rvol?.buy_pct != null ? TFS.map((tf) => {
    const rv = d.charts[tf]?.rvol || {}; const bp = rv.buy_pct_12;
    return bp == null ? '' : `<div class="vrow"><span class="tf">${tf}</span><div class="buybar" title="Taker-buy share of volume over the last 12 bars (Binance)"><i style="width:${bp}%"></i></div><span class="${bp >= 55 ? 'bull' : bp <= 45 ? 'bear' : ''}">${bp.toFixed(0)}% buy</span></div>`;
  }).join('') : '') + `<div class="note">RVOL: latest bar vs its 20-bar average (full bar = 3×, tick = 1×).${d.charts['5m']?.rvol?.buy_pct != null ? ' Buy: aggressive (taker) buy share of volume, last 12 bars.' : ''}</div>`;
  const rows = [['ATR | min FVG', (t) => `${fmt(t.atr)} | ${t.min_fvg_pct?.toFixed(3)}%`], ['Structure', (t) => t.trend === 1 ? '<span class="bull">▲ Bull</span>' : t.trend === -1 ? '<span class="bear">▼ Bear</span>' : '—'],
    ['P/D zone', (t) => t.pd ? `<span class="${t.pd.pos > 55 ? 'bear' : t.pd.pos < 45 ? 'bull' : ''}">${t.pd.zone.slice(0, 4)} ${Math.round(t.pd.pos)}%</span>` : '—'],
    ['FVG ▲/▼', (t) => `${t.fvg_bull} / ${t.fvg_bear}`], ['OB ▲/▼', (t) => `${t.ob_bull} / ${t.ob_bear}`],
    ['Last sweep', (t) => t.last_sweep ? `<span class="warn">${t.last_sweep} ${t.last_sweep_ago}</span>` : '—'], ['Last CHoCH', (t) => t.last_shift ? `${t.last_shift === 1 ? '▲' : '▼'} ${t.last_shift_ago}` : '—']];
  $('#tableBlock').innerHTML = `<table><tr><th></th>${TFS.map((t) => `<th>${t}</th>`).join('')}</tr>${rows.map(([k, f]) => `<tr><td>${k}</td>${TFS.map((tf) => `<td>${d.charts[tf] ? f(d.charts[tf].table) : '—'}</td>`).join('')}</tr>`).join('')}</table><div class="note">Sweep / CHoCH counts are bars ago on that timeframe.</div>`;
  const sk = d.charts[S.focus]?.prob?.skill;
  $('#validBlock').innerHTML = sk ? `<div class="note" style="margin-top:6px">${S.focus} · ${d.group} · walk-forward, ${sk.n_oos.toLocaleString()} out-of-sample bars</div>
    <div class="kv"><span>AUC (0.5 = coin flip)</span><span>${sk.auc.toFixed(3)}</span><span>Log-loss vs base rate</span><span>${sk.logloss.toFixed(4)} vs ${sk.logloss_base.toFixed(4)}</span>
    <span>Signals ≥ 60% / ≤ 40%</span><span>${sk.signals.n} · hit ${sk.signals.hit == null ? '—' : sk.signals.hit.toFixed(0) + '%'}${sk.signals.net_r == null ? '' : ' · ' + sgn(sk.signals.net_r) + 'R'}</span></div>
    <table style="margin-top:6px"><tr><th>Predicted</th><th>n</th><th>Avg pred</th><th>Actual up</th></tr>${sk.calibration.map((b) => `<tr><td>${b.bin}</td><td>${b.n}</td><td>${(b.pred * 100).toFixed(0)}%</td><td>${(b.actual * 100).toFixed(0)}%</td></tr>`).join('')}</table>
    <div class="note">Large chart's timeframe. Regenerate with research/prob_validate.py.</div>` : '<div class="muted">No validation report.</div>';
  renderOracle();
  renderSocial();
  renderLoris();
  renderAlerts(d.alerts);
}
// ── Loris Tools funding ────────────────────────────────────────────────────
function lorisSpark(hist) {
  const lines = Object.entries(hist || {}).filter(([, pts]) => pts.length > 1);
  if (!lines.length) return '';
  const all = lines.flatMap(([, p]) => p), t0 = Math.min(...all.map((p) => p[0])), t1 = Math.max(...all.map((p) => p[0]));
  let lo = Math.min(0, ...all.map((p) => p[1])), hi = Math.max(0, ...all.map((p) => p[1])); if (hi === lo) hi = lo + 1;
  const W = 300, H = 60, x = (t) => (t - t0) / Math.max(1, t1 - t0) * W, y = (v) => H - (v - lo) / (hi - lo) * H;
  const color = { median: 'var(--muted)', variational: 'var(--accent)', omni: 'var(--accent)', binance: '#f0b90b' };
  return `<svg class="lspark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}" class="zero"/>${lines.map(([ex, p]) => `<polyline fill="none" stroke="${color[ex] || '#9c27b0'}" stroke-width="1.5" vector-effect="non-scaling-stroke" points="${p.map(([t, v]) => `${x(t).toFixed(1)},${y(v).toFixed(1)}`).join(' ')}"><title>${ex}</title></polyline>`).join('')}</svg>
    <div class="note">Last ${Math.round((t1 - t0) / 3600)} h, bps/8 h: ${lines.map(([ex]) => `<span style="color:${color[ex] || '#9c27b0'}">■</span> ${ex}`).join(' ')}</div>`;
}
function renderLoris() {
  const d = S.data, L = d?.loris, box = $('#lorisBlock'), err = d?.meta?.loris_error;
  if (!box) return;
  const head = err ? `<div class="warn">${esc(err)}</div>` : '';
  if (!L || !L.venues.length) {
    box.innerHTML = head + (d.loris_key ? '<div class="muted">Waiting for the first Loris read…</div>'
      : `<div class="muted">No API key.</div><div class="note">The Loris free plan only returns BTC and ETH. For ${S.sym}, get a paid key at loris.tools/pricing and add a line <code>LORIS_API_KEY=your_key</code> to the <b>.env</b> file in the project folder, then restart.</div>`);
    return;
  }
  const bp = (x) => `${x >= 0 ? '+' : ''}${x.toFixed(2)}`;
  const rows = L.venues.slice().reverse().map((v) => `<tr class="${v.label ? 'home' : ''}"><td>${esc(v.exchange)}${v.label && v.label.toLowerCase() !== v.exchange.toLowerCase() ? ` <span class="chip">${esc(v.label)}</span>` : ''}</td><td class="${v.bps_8h > 0 ? 'bear' : v.bps_8h < 0 ? 'bull' : ''}">${bp(v.bps_8h)}</td><td>${sgn(v.apr, 1)}%</td><td class="muted">${v.interval_h ? v.interval_h + 'h' : '—'}</td></tr>`).join('');
  const o = L.omni;
  box.innerHTML = `${head}<div class="kv">
      <span>Omni (Variational)</span><span>${o ? `${bp(o.bps_8h)} bps/8h · ${sgn(o.apr, 1)}% APR` : 'not listed'}</span>
      <span>Median, ${L.venues.length} venues</span><span>${bp(L.median_bps)} bps/8h</span>
      <span>Cheapest to be long</span><span class="bull">${esc(L.best_long)}</span>
      <span>Best to be short</span><span class="bear">${esc(L.best_short)}</span>
      <span>Spread (short − long)</span><span>${L.spread_bps.toFixed(2)} bps/8h · ${L.spread_apr.toFixed(1)}% APR</span></div>
    ${lorisSpark(d.loris_hist)}
    <details><summary class="note">All venues</summary><table><tr><th>Venue</th><th>bps/8h</th><th>APR</th><th>Every</th></tr>${rows}</table></details>
    <div class="note">Positive = longs pay shorts. Updated ${fmtTime(L.ts)} from loris.tools. Spread is before fees and price moves between venues.</div>`;
}
// ── X (Twitter) ────────────────────────────────────────────────────────────
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const ago = (ts) => { const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60)); return m < 60 ? `${m}m` : m < 1440 ? `${Math.floor(m / 60)}h` : `${Math.floor(m / 1440)}d`; };
const sentTag = (x) => x == null ? '<span class="muted">—</span>' : `<span class="${x > 0.1 ? 'bull' : x < -0.1 ? 'bear' : 'muted'}">${x > 0.1 ? 'Bullish' : x < -0.1 ? 'Bearish' : 'Neutral'} ${sgn(x)}</span>`;
function renderSocial() {
  const s = S.data?.social, box = $('#socialBlock');
  $('#socialWrap').style.display = s ? '' : 'none';
  if (!s) return;
  const err = S.data.meta?.social_error;
  if (!s.provider) {
    box.innerHTML = `<div class="muted">Not connected.</div><div class="note">Create a file <b>.env</b> in the project folder with one line:<br><code>TWITTERAPI_IO_KEY=your_key</code> (twitterapi.io, ≈ $0.15 / 1,000 tweets) or<br><code>X_BEARER_TOKEN=your_token</code> (official X API, ≈ $5 / 1,000 tweets),<br>then restart the dashboard. Search: <code>${esc(s.query)}</code></div>`;
    return;
  }
  const h1 = s.h1, h24 = s.h24, max = Math.max(1, ...s.hourly);
  const spark = s.hourly.map((v, i) => `<i style="height:${Math.max(4, v / max * 100)}%" title="${v} tweets, ${48 - i}h ago"></i>`).join('');
  const tw = (t) => `<a class="tweet" href="${esc(t.url)}" target="_blank" rel="noopener"><div class="th"><b>@${esc(t.user)}</b>${t.verified ? ' <span class="vb">✓</span>' : ''} <span class="muted">${vol(t.followers)} · ${ago(t.ts)}</span>${t.perp ? ' <span class="chip">perp</span>' : ''}<span class="ts ${t.sent > 0 ? 'bull' : t.sent < 0 ? 'bear' : 'muted'}">${t.sent > 0 ? '▲' : t.sent < 0 ? '▼' : '·'}</span></div><div class="tt">${esc(t.text)}</div><div class="muted tm">♥ ${vol(t.likes)} · ⟲ ${vol(t.rts)} · 💬 ${vol(t.replies)}${t.views ? ` · 👁 ${vol(t.views)}` : ''}</div></a>`;
  box.innerHTML = `${err ? `<div class="warn">Last X read failed: ${esc(err)}</div>` : ''}
    <div class="kv"><span>Buzz (1 h vs 24 h pace)</span><span class="${s.buzz >= 3 ? 'warn' : ''}">${s.buzz == null ? '—' : s.buzz.toFixed(1) + '×'} · ${h1.n} tweets/h</span>
    <span>Sentiment 1 h</span><span>${sentTag(h1.sent)}</span><span>Sentiment 24 h</span><span>${sentTag(h24.sent)}</span>
    <span>24 h ▲ / ▼ / perp</span><span><span class="bull">${h24.bull}</span> / <span class="bear">${h24.bear}</span> / ${h24.perp} of ${h24.n}</span></div>
    <div class="spark" title="Tweets per hour, last 48 h">${spark}</div>
    ${s.top.length ? `<details><summary class="note">Top tweets 24 h</summary>${s.top.map(tw).join('')}</details>` : ''}
    <div class="tweets">${s.latest.length ? s.latest.map(tw).join('') : '<div class="muted">No tweets stored yet — first read within a few minutes.</div>'}</div>
    <div class="note">${s.provider === 'x' ? 'Official X API' : 'twitterapi.io'} · every ${Math.round(s.poll_sec / 60)} min · today ${s.usage.tweets.toLocaleString()} / ${s.usage.cap.toLocaleString()} tweets ≈ $${s.usage.usd.toFixed(2)}</div>`;
}
function lastPrice() {
  const c = S.data?.charts['5m'];
  if (!c) return null;
  const all = [...c.candles, ...c.forming];
  return all.length ? all[all.length - 1].close : null;
}
function renderAlerts(alerts) {
  const first = S.seen == null; if (first) S.seen = new Set();
  for (const a of alerts) if (!S.seen.has(a.id)) { S.seen.add(a.id); if (!first) notify(a); }
  $('#alertBlock').innerHTML = alerts.length ? alerts.slice().reverse().map((a) => `<div class="alert ${a.type}"><span class="t">${fmtTime(a.ts)}</span><b>${a.symbol}</b><span class="muted">${a.tf}</span><span class="${a.dir === 1 ? 'bull' : 'bear'}">${a.text}</span></div>`).join('') : '<div class="muted">No alerts on the latest bars.</div>';
}
let audio = null;
function notify(a) {
  if (!S.alertsOn) return;
  if (audio) { const o = audio.createOscillator(), g = audio.createGain(); o.frequency.value = a.type === 'signal' ? 880 : 620; o.connect(g); g.connect(audio.destination); g.gain.setValueAtTime(0.15, audio.currentTime); g.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.3); o.start(); o.stop(audio.currentTime + 0.3); }
  if ('Notification' in window && Notification.permission === 'granted') new Notification(`${a.symbol} ${a.tf} — ${a.type}`, { body: a.text, tag: a.id });
}

// ── live ───────────────────────────────────────────────────────────────────
function tickLive() {
  // Omni mark: price line on every chart, and the live candle when the chart source is Omni.
  const q = S.live[S.sym];
  if (!q || !S.data) return;
  for (const tf of TFS) {
    const p = S.panes[tf];
    p.mark.applyOptions({ price: q.mark });
    if (S.source !== 'omni') continue;
    const bars = p.bars, last = bars[bars.length - 1];
    const nowU = Math.floor(Date.now() / 1000), t = toChart(nowU - (nowU % TF_SEC[tf]));
    if (last && last.time === t) Object.assign(last, { high: Math.max(last.high, q.mark), low: Math.min(last.low, q.mark), close: q.mark });
    else if (!last || t > last.time) bars.push({ time: t, open: q.mark, high: q.mark, low: q.mark, close: q.mark });
    else continue;
    const b = bars[bars.length - 1];
    p.candles.update({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close });
    legend(p, null);
  }
}
function applyCandles(updates) {
  // Live futures candles (Binance / Hyperliquid), pushed every 5 s around the clock.
  const u = updates?.[S.sym];
  if (!u || !S.data) return;
  for (const tf of TFS) {
    const rows = (u[tf] || []).map((r) => ({ ...r, time: toChart(r.time) })); if (!rows.length) continue;
    const c = S.data.charts[tf];
    for (const r of rows) {                         // keep the payload in sync (used by the panel)
      if (!c) break;
      const all = c.forming.length ? c.forming : c.candles;
      const lastC = c.forming.length ? c.forming[c.forming.length - 1] : c.candles[c.candles.length - 1];
      if (lastC && lastC.time === r.time) Object.assign(lastC, r);
      else if (!lastC || r.time > lastC.time) { if (c.forming.length) c.candles.push(...c.forming.splice(0)); c.forming.push({ ...r }); }
    }
    if (S.source === 'omni') continue;
    const p = S.panes[tf];
    for (const r of rows) {
      const last = p.bars[p.bars.length - 1];
      if (last && last.time === r.time) Object.assign(last, r);
      else if (!last || r.time > last.time) p.bars.push({ ...r });
      else continue;
      p.candles.update({ time: r.time, open: r.open, high: r.high, low: r.low, close: r.close });
      if (S.show.volume) p.volume.update({ time: r.time, value: r.volume || 0, color: r.close >= r.open ? 'rgba(8,153,129,0.45)' : 'rgba(242,54,69,0.45)' });
    }
    legend(p, null);
  }
  const el = $('#quoteBlock .qpx'); const lp = lastPrice(); if (el && lp != null) el.textContent = fmt(lp);
}
function setStatus(ok, txt) { const el = $('#status'); el.className = `status ${ok ? 'ok' : 'err'}`; $('.txt', el).textContent = txt; }

async function load() {
  const r = await fetch(`/api/state?symbol=${S.sym}`);
  const d = await r.json();
  if (d.error) { S.sym = 'NVDA'; store.set('sym', 'NVDA'); return load(); }
  S.data = shiftPayload(d); S.live = d.live || {}; S.sessionOpen = d.session_open;
  $('#tzBtn').textContent = `🕒 ${tzLabel()}`;
  if (!$('#symbols').children.length) buildSymbols(d.groups);
  renderAll(); tickLive();
  setStatus(true, `calc v${d.meta.version} · ${new Date(d.meta.last_calc * 1000).toLocaleTimeString()}`);
}
function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.type === 'live') {
      S.live = m.live; S.sessionOpen = m.session_open; tickLive();
    } else if (m.type === 'oracle') {
      if (!S.data) return;
      const o = m.oracle[S.sym]; if (!o) return;
      const prevTs = S.data.oracle?.consensus?.ts;
      S.data.oracle = o;
      if (o.consensus && o.consensus.ts !== prevTs) {
        const t0 = toChart(o.consensus.ts);
        (S.data.oracle_hist = S.data.oracle_hist || []).push([t0, o.consensus.index_med, o.venue?.index ?? null]);
        for (const tf of TFS) renderOracleLine(S.panes[tf]);
      }
      renderOracle();
    } else if (m.type === 'loris') {
      if (!S.data) return;
      S.data.loris = m.loris[S.sym] || null; S.data.meta = m.meta; renderLoris();
    } else if (m.type === 'social') {
      if (!S.data || !m.social[S.sym]) return;
      S.data.social = m.social[S.sym]; S.data.meta = m.meta; renderSocial();
    } else if (m.type === 'candles') {
      applyCandles(m.candles);
      setStatus(!m.meta.candle_error, m.meta.candle_error ? `${S.data?.market.source || 'Feed'} error` : `live 24/7 · ${fmtTime(m.meta.last_candles)}`);
    } else load();
  };
  ws.onclose = () => { setStatus(false, 'reconnecting…'); setTimeout(connect, 3000); };
}

// ── toolbar wiring ─────────────────────────────────────────────────────────
function buildSymbols(groups) {
  $('#symbols').innerHTML = Object.values(groups).map((g) => `<span class="glabel">${g.label}</span>` + g.symbols.map((s) => `<button data-s="${s}" class="${s === S.sym ? 'on' : ''}">${s}</button>`).join('')).join('');
  $('#symbols').onclick = (e) => {
    const s = e.target.dataset.s; if (!s || s === S.sym) return;
    S.sym = s; store.set('sym', s);
    document.querySelectorAll('#symbols button').forEach((b) => b.classList.toggle('on', b.dataset.s === s));
    for (const p of Object.values(S.panes)) { p.fitted = false; p.tvKey = null; }
    load();
  };
}
function segment(id, key, attr, onChange) {
  const box = $(id);
  const sync = () => box.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset[attr] === S[key]));
  box.onclick = (e) => { const v = e.target.dataset[attr]; if (!v) return; S[key] = v; store.set(key, v); sync(); onChange(); };
  sync();
}
function buildLayers() {
  $('#layers').innerHTML = Object.entries(LAYERS).map(([k, v]) => `<label><input type="checkbox" data-k="${k}" ${S.show[k] ? 'checked' : ''}> ${v}</label>`).join('');
  $('#layers').onchange = (e) => { S.show[e.target.dataset.k] = e.target.checked; store.set('layers', S.show); renderAll(); };
  $('#layersBtn').onclick = (e) => { const pop = $('#layers'); pop.classList.toggle('hidden'); const r = e.target.getBoundingClientRect(); pop.style.left = `${r.left}px`; };
  document.addEventListener('click', (e) => { if (!e.target.closest('#layers, #layersBtn')) $('#layers').classList.add('hidden'); });
}
const FIELDS = [['capital', 'Capital (USDC)'], ['risk_pct', 'Risk per trade (% to stop)'], ['margin_pct', 'Max margin (% of capital)'], ['signal_prob', 'Signal threshold (%)'],
  ['fee_pct', 'Perp fee per side (%)'], ['swap_fee_pct', 'Swap fee per side (%)'], ['slippage_pct', 'Slippage per side (%)'], ['mmr_pct', 'Maintenance margin (%)'],
  ['liq_buffer', 'Stop / liquidation buffer (0–1)'], ['swap_max_lev', 'Swap max leverage']];
function wire() {
  segment('#source', 'source', 's', () => { for (const p of Object.values(S.panes)) p.fitted = false; renderAll(); });
  segment('#detail', 'detail', 'd', renderAll);
  $('#layouts').onclick = (e) => { const l = e.target.dataset.l; if (l) setLayout(l); };
  $('#focusTf').onclick = (e) => { const t = e.target.dataset.t; if (t) setFocus(t); };
  $('#tzBtn').onclick = () => {
    S.tz = S.tz === 'ny' ? 'local' : 'ny'; store.set('tz', S.tz); _offCache.clear();
    for (const p of Object.values(S.panes)) { p.fitted = false; p.tvKey = null; }
    load();
  };
  $('#themeBtn').onclick = () => { S.theme = S.theme === 'dark' ? 'light' : 'dark'; store.set('theme', S.theme); applyTheme(); };
  $('#panelBtn').onclick = () => { S.panel = !S.panel; store.set('panel', S.panel); $('#panel').classList.toggle('hidden', !S.panel); };
  $('#panel').classList.toggle('hidden', !S.panel);
  $('#alertsBtn').onclick = async () => {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    if ('Notification' in window && Notification.permission === 'default') await Notification.requestPermission();
    S.alertsOn = !S.alertsOn; $('#alertsBtn').classList.toggle('on', S.alertsOn);
  };
  $('#settingsBtn').onclick = () => {
    const st = S.data.settings;
    $('#settingsGrid').innerHTML = FIELDS.map(([k, l]) => `<label>${l}<input type="number" step="any" min="0" name="${k}" value="${st[k]}"></label>`).join('');
    $('#settings').showModal();
  };
  $('#saveSettings').onclick = async (e) => {
    e.preventDefault();
    const body = {};
    for (const [k] of FIELDS) { const v = parseFloat($(`#settingsGrid [name="${k}"]`).value); if (!isNaN(v)) body[k] = v; }
    await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    $('#settings').close(); load();
  };
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && S.layout === 'single' && preMax) maximize(S.focus); });
}

setInterval(() => { const el = document.getElementById('clock'); if (el) el.textContent = new Date().toLocaleTimeString('es-AR', { timeZone: tzName(), hour12: false }); }, 1000);
document.documentElement.dataset.theme = S.theme;
window.OVERLAY_HALO = C().halo;
for (const tf of TFS) makePane(tf);
wire(); buildLayers(); applyLayout();
if (typeof LightweightCharts === 'undefined') setStatus(false, 'chart library did not load (static/vendor/lightweight-charts.js)');
else load().then(connect).catch((e) => setStatus(false, `load failed: ${e}`));
