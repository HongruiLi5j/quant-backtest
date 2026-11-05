// A12 探针：在 node 里用极简假 DOM + 假图表库，真的跑 pages/stock-chart.js 的 kChart（原文件，不换桩）。
// 记录：价格窗格画 K 线还是面积线、首屏可见范围（setVisibleLogicalRange / fitContent 各调了几次）、各序列喂入的点数、买卖点标记数量，
// 以 JSON 打印给 tests/test_stock_chart_a12.py 断言。不联网、不读 data/。
// 用法：node --experimental-default-type=module a12_probe.mjs <ui 目录>
const [ui] = process.argv.slice(2);
const uiUrl = new URL(`file://${ui}/`);

/* ---------- 极简假 DOM（只实现 kChart / el / attachTooltip 用到的部分） ---------- */
class FNode { constructor() { this.parent = null; this.kids = []; } }
class FText extends FNode { constructor(s) { super(); this.s = String(s); } get textContent() { return this.s; } }
class FEl extends FNode {
  constructor(tag) {
    super();
    this.tag = tag; this.attrs = {}; this.dataset = {}; this.className = ''; this.props = {};
    this.style = { setProperty: (k, v) => { this.props[k] = v; } };
    this.classList = { add() {}, remove() {} };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  append(...c) { for (const y of c) { const x = y instanceof FNode ? y : new FText(y); x.parent = this; this.kids.push(x); } }
  replaceChildren(...c) { this.kids = []; this.append(...c); }
}
globalThis.Node = FNode;
globalThis.document = { createElement: (t) => new FEl(t), createTextNode: (s) => new FText(s), addEventListener() {},
  documentElement: { dataset: {}, lang: '' }, querySelectorAll: () => [] };
const rafs = [];
globalThis.window = { location: { search: '' }, dispatchEvent() {}, addEventListener() {} };
globalThis.requestAnimationFrame = (f) => { rafs.push(f); return rafs.length; };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });   // 取不到 CSS 变量就用 chartColors 里写的默认色

/* ---------- 假图表库：只记录调用 ---------- */
let log;
const makeSeries = (kind) => ({
  kind,
  setData(arr) { log.setData.push([kind, arr.length]); },
  applyOptions() {}, createPriceLine() {},
});
const makeChart = () => ({
  addSeries: (kind) => { log.kinds.push(kind); return makeSeries(kind); },
  timeScale: () => ({
    fitContent() { log.fit += 1; },
    setVisibleLogicalRange(r) { log.range.push({ ...r }); },
  }),
  panes: () => [],
  applyOptions() {}, subscribeCrosshairMove() {}, chartElement: () => ({ isConnected: true }),
});
window.LightweightCharts = {
  CandlestickSeries: 'candle', AreaSeries: 'area', LineSeries: 'line', HistogramSeries: 'hist',
  createChart: () => makeChart(),
  createSeriesMarkers: () => ({ setMarkers(list) { log.markers.push(list.length); log.markerSides = list.map((m) => m.text); } }),
};

const mod = await import(new URL('pages/stock-chart.js', uiUrl));

/* ---------- 合成 n 根日线（不含周末的日期不重要，只要不重复）；交易含一笔「整笔在窗口之前」的、一笔持仓中的 ---------- */
function makeData(n) {
  const day = (i) => new Date(Date.UTC(2020, 0, 1) + i * 86400000).toISOString().slice(0, 10);
  const dates = Array.from({ length: n }, (_, i) => day(i));
  const arr = (f) => Array.from({ length: n }, (_, i) => f(i));
  const close = arr((i) => 10 + Math.sin(i / 9));
  const trades = [
    { entry: day(n - 5), exit: null, open: true },                                   // 持仓中：只有买点
    { entry: day(Math.floor(n / 2)), exit: day(Math.floor(n / 2) + 3), open: false },
    { entry: day(10), exit: day(14), open: false },
    { entry: '1999-01-01', exit: '1999-01-05', open: false },                        // 日期不在数据里：原逻辑就不画
  ];
  return {
    dates, open: close, high: close.map((v) => v + 0.1), low: close.map((v) => v - 0.1), close,
    volume: arr(() => 1000), position: arr((i) => (i % 7 < 3 ? 1 : 0)),
    lines: [{ name: 'MA5', values: close, dash: false }, { name: 'stop', values: close, dash: true }],
    sub: null, ind: null, trades,
  };
}
const expectedMarkers = (d) => {
  const have = new Set(d.dates);
  return d.trades.reduce((s, t) => s + (have.has(t.entry) ? 1 : 0) + (t.exit && have.has(t.exit) ? 1 : 0), 0);
};

function run(n, view, then) {
  log = { setData: [], kinds: [], fit: 0, range: [], markers: [], markerSides: [] };
  rafs.length = 0;
  const d = makeData(n);
  const box = new FEl('div');
  const h = mod.kChart(box, d, 'off', view);
  const res = { n, view, hasShowRange: typeof h?.showRange === 'function', expectedMarkers: expectedMarkers(d) };
  if (then) then(h, res);
  return { ...res, price: log.kinds[0], fit: log.fit, range: log.range, markers: log.markers.at(-1), markerCalls: log.markers.length,
    setData: log.setData.map((x) => x.join(':')).join(',') };
}

const out = { canZoom: [252, 300, 301, 756].map((n) => [n, mod.canZoomRecent(n)]) };
for (const n of [252, 300, 301, 756, 3500]) {
  out[`${n}.recent`] = run(n, 'recent');
  out[`${n}.all`] = run(n, 'all');
}
out['default_view'] = (() => { log = { setData: [], kinds: [], fit: 0, range: [], markers: [], markerSides: [] }; mod.kChart(new FEl('div'), makeData(756), 'off'); return { range: log.range, fit: log.fit }; })();
// 切换：先「近 1 年」，再 showRange('all')，再 showRange('recent')；切换本身不能再动数据和标记
out['toggle'] = run(756, 'recent', (h, res) => {
  const before = { setData: log.setData.length, markers: log.markers.length };
  h.showRange('all'); res.afterAll = { fit: log.fit, range: log.range.length };
  h.showRange('recent'); res.afterRecent = { fit: log.fit, range: log.range.length };
  res.unchanged = log.setData.length === before.setData && log.markers.length === before.markers;
});
console.log(JSON.stringify(out));
