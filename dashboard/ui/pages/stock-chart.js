/* ==========================================================
 * 文件: ui/pages/stock-chart.js
 * 用途: 个股 K 线（对应旧 kChart）。lightweight-charts 5 的多窗格：
 *   0 价格（K 线 ≤300 根，更多时用收盘面积线，且默认只显示最近约 1 年）+ 策略均线 / 止损线 + 布林带 + 买卖点标记
 *   1 仓位条（越深仓位越重） 2 成交量 3 策略副图（ADX / RSI(2)） 4+ MACD / RSI / KDJ
 *   悬浮提示：开高低收 + 涨跌 + 各线数值 + 仓位。颜色取 CSS tokens，换主题时重绘。
 *   d = GET /api/stock 的返回（src/detail.py: build）
 * ========================================================== */
import { el, fmt } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { attachTooltip, chartColors, withAlpha } from '../shared/chart-helpers.js';
import { baseOptions, chartMissing, isNum, pct, toneClass, track } from './u2-lib.js';

const H = { price: 300, pos: 44, vol: 70, sub: 90, ind: 100 };   // D07：仓位窗格 36→44，上面留出标题位置
/* D07 fix（revtech）：0–100 的副图坐标刻度（20/40/60/80）和参考线标签（30/70、20/80）离得太近，互相遮挡。
 * 刻度文字只保留参考线那几个数，其他刻度不写字；参考线标签本身走同一个 formatter，所以照常显示。 */
const levelsOnly = (levels) => ({ type: 'custom', minMove: 1, formatter: (v) => (levels.includes(Math.round(v)) ? String(Math.round(v)) : '') });
const TIGHT = { scaleMargins: { top: 0.06, bottom: 0.06 } };   // 固定 0–100 时坐标不再多出 120
const CANDLE_MAX = 300;     // 超过这个根数价格窗格改画收盘面积线
const RECENT_BARS = 252;    // 默认可见窗口：最近约 1 年（A 股一年约 245~252 个交易日）

/** 数据量够大（已经改画面积线）时才有「近 1 年 / 全部」两种视图；否则一屏本来就放得下 */
export const canZoomRecent = (n) => n > CANDLE_MAX;

/** 把 values（与 dates 对齐，可含 null）变成 [{time, value}] 并丢掉空值 */
const points = (dates, values) => dates.map((time, i) => ({ time, value: values[i] })).filter((p) => isNum(p.value));
const levelLine = (series, price, c) => series.createPriceLine({ price, color: c.text, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' });

/** 策略线的颜色：虚线（止损线）用回撤红，其余按序取 tokens */
// D79：均线第 3、4 条原来用涨跌红绿，和 K 线的红涨绿跌撞色；改用中性系列色（图例 paletteVars 同序）
const lineColor = (c, L, k) => (L.dash ? c.dd : [c.line, c.line2, c.s3, c.s4][k % 4]);

/** 窗格表：返回要画的副图列表（顺序 = 窗格顺序） */
function panelList(d, ind) {
  const list = [];
  if (d.sub) list.push('sub');
  if (d.ind && ind !== 'off') list.push(...(ind === 'all' ? ['macd', 'rsi', 'kdj'] : [ind]));
  return list;
}

/** 窗格标题 */
function paneTitle(key, d) {
  return { sub: d.sub?.name, macd: 'MACD (12, 26, 9)  DIF · DEA', rsi: 'RSI (14)', kdj: 'KDJ (9, 3, 3)  K · D · J' }[key];
}

/** 悬浮提示的行 */
function tipRows(d, i, ind, panes) {
  const chg = i > 0 && isNum(d.close[i - 1]) ? (d.close[i] / d.close[i - 1] - 1) * 100 : null;
  const rows = [
    { label: `${t('chart.open')} / ${t('chart.high')}`, value: `${fmt(d.open[i])} / ${fmt(d.high[i])}` },
    { label: `${t('chart.low')} / ${t('chart.close')}`, value: `${fmt(d.low[i])} / ${fmt(d.close[i])}` },
    { label: t('u2.st.chg'), value: pct(chg, 2), tone: toneClass(chg) }];
  d.lines.forEach((L) => { if (isNum(L.values[i])) rows.push({ label: L.name, value: fmt(L.values[i]) }); });
  if (d.sub && isNum(d.sub.values[i])) rows.push({ label: d.sub.name, value: fmt(d.sub.values[i], 1) });
  if (d.ind) {
    const f2 = (v) => (isNum(v) ? fmt(v, 2) : '—');
    if (ind !== 'off') rows.push({ label: t('u2.st.boll'), value: `${f2(d.ind.boll.low[i])} / ${f2(d.ind.boll.mid[i])} / ${f2(d.ind.boll.up[i])}` });
    if (panes.includes('macd')) rows.push({ label: 'MACD', value: `${f2(d.ind.macd.dif[i])} / ${f2(d.ind.macd.dea[i])}` });
    if (panes.includes('rsi')) rows.push({ label: 'RSI(14)', value: f2(d.ind.rsi[i]) });
    if (panes.includes('kdj')) rows.push({ label: 'KDJ', value: `${f2(d.ind.kdj.k[i])} / ${f2(d.ind.kdj.d[i])} / ${f2(d.ind.kdj.j[i])}` });
  }
  rows.push({ label: t('u2.st.position'), value: fmt((d.position[i] || 0) * 100, 0, '%') });
  return rows;
}

/**
 * 在 box 里画图，返回 { chart, showRange }；box 高度由窗格总高度决定。
 * ind = 'all' | 'macd' | 'rsi' | 'kdj' | 'off'（旧页面「技术指标」下拉）
 * view = 'recent' | 'all'：数据多时首屏只显示最近 RECENT_BARS 根（数据仍全部加载，可拖动）；showRange(view) 之后随时切换
 */
export function kChart(box, d, ind, view = 'recent') {
  const lib = window.LightweightCharts;
  box.replaceChildren();
  if (!lib) return chartMissing(box);
  const dates = d.dates;
  const n = dates.length;
  if (!n) return null;
  const panels = panelList(d, ind);
  const heights = [H.price, H.pos, H.vol, ...panels.map((p) => (p === 'sub' ? H.sub : H.ind))];
  box.style.height = `${heights.reduce((a, b) => a + b, 0) + 40}px`;

  const c0 = chartColors();
  const chart = lib.createChart(box, baseOptions(c0));
  const paint = [];                         // 每项 (c) => void：创建时和换主题时都执行一遍
  const idx = new Map(dates.map((x, i) => [x, i]));
  const candles = !canZoomRecent(n);

  // —— 窗格 0：价格
  const price = candles
    ? chart.addSeries(lib.CandlestickSeries, { borderVisible: false, priceLineVisible: false, lastValueVisible: true })
    : chart.addSeries(lib.AreaSeries, { lineWidth: 2, priceLineVisible: false });
  price.setData(candles
    ? dates.map((time, i) => ({ time, open: d.open[i], high: d.high[i], low: d.low[i], close: d.close[i] })).filter((p) => isNum(p.close) && isNum(p.open))
    : points(dates, d.close));
  paint.push((c) => price.applyOptions(candles
    ? { upColor: c.rise, downColor: c.fall, wickUpColor: c.rise, wickDownColor: c.fall }
    : { lineColor: c.line, topColor: withAlpha(c.line, 0.25), bottomColor: withAlpha(c.line, 0.02) }));

  const addLine = (values, pane, opts, color) => {
    const s = chart.addSeries(lib.LineSeries, { lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, ...opts }, pane);
    s.setData(points(dates, values));
    paint.push((c) => s.applyOptions({ color: color(c) }));
    return s;
  };
  if (d.ind && ind !== 'off') {             // 布林带 (20, 2)
    addLine(d.ind.boll.up, 0, { lineStyle: 2 }, (c) => withAlpha(c.line2, 0.8));
    addLine(d.ind.boll.low, 0, { lineStyle: 2 }, (c) => withAlpha(c.line2, 0.8));
    addLine(d.ind.boll.mid, 0, {}, (c) => withAlpha(c.line, 0.8));
  }
  d.lines.forEach((L, k) => addLine(L.values, 0, { lineWidth: 2, lineStyle: L.dash ? 2 : 0 }, (c) => lineColor(c, L, k)));

  // 买卖点：成交价是下一根开盘，日期就是成交那天
  const markers = [];
  for (const tr of d.trades) {
    if (idx.has(tr.entry)) markers.push({ time: tr.entry, side: 'buy' });
    if (tr.exit && idx.has(tr.exit)) markers.push({ time: tr.exit, side: 'sell' });
  }
  markers.sort((a, b) => (a.time < b.time ? -1 : 1));
  const markerApi = lib.createSeriesMarkers(price, []);
  paint.push((c) => markerApi.setMarkers(markers.map((m) => (m.side === 'buy'
    ? { time: m.time, position: 'belowBar', shape: 'arrowUp', color: c.riseText, text: t('u2.st.buy') }
    : { time: m.time, position: 'aboveBar', shape: 'arrowDown', color: c.fallText, text: t('u2.st.sell') }))));

  // —— 窗格 1：仓位条（柱高固定为 1，颜色深浅 = 仓位）
  const fixedRange = (lo, hi) => () => ({ priceRange: { minValue: lo, maxValue: hi } });
  const pos = chart.addSeries(lib.HistogramSeries, { priceLineVisible: false, lastValueVisible: false, autoscaleInfoProvider: fixedRange(0, 1) }, 1);
  // D07：仓位条的 0/1 坐标没有信息量，还和下面成交量窗格顶上的刻度叠在一起 → 不显示刻度文字；柱子只占下 55%，上面给「仓位」标题
  pos.priceScale().applyOptions({ ticksVisible: false, scaleMargins: { top: 0.45, bottom: 0 } });
  pos.applyOptions({ priceFormat: { type: 'custom', formatter: () => '' } });
  paint.push((c) => pos.setData(dates.map((time, i) => ({ time, value: isNum(d.position[i]) && d.position[i] > 0 ? 1 : 0,
    color: withAlpha(c.line2, 0.15 + 0.75 * Math.min(1, d.position[i] || 0)) }))));

  // —— 窗格 2：成交量
  const vol = chart.addSeries(lib.HistogramSeries, { priceLineVisible: false, lastValueVisible: false, priceFormat: { type: 'volume' } }, 2);
  vol.priceScale().applyOptions({ scaleMargins: { top: 0.25, bottom: 0 } });   // D07：顶部留白，给窗格标题和上一个窗格的刻度让位
  paint.push((c) => vol.setData(dates.map((time, i) => ({ time, value: isNum(d.volume[i]) ? d.volume[i] : 0,
    color: withAlpha(d.close[i] >= d.open[i] ? c.rise : c.fall, 0.5) }))));

  // —— 窗格 3+：策略副图与 MACD / RSI / KDJ
  panels.forEach((key, k) => {
    const pane = 3 + k;
    if (key === 'sub') {
      const s = addLine(d.sub.values, pane, { lineWidth: 2, autoscaleInfoProvider: fixedRange(...d.sub.range) }, (c) => c.line2);
      d.sub.levels.forEach((lv) => levelLine(s, lv, c0));
    } else if (key === 'macd') {
      const hist = chart.addSeries(lib.HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, pane);
      paint.push((c) => hist.setData(dates.map((time, i) => ({ time, value: d.ind.macd.hist[i], color: withAlpha(d.ind.macd.hist[i] >= 0 ? c.rise : c.fall, 0.6) })).filter((p) => isNum(p.value))));
      addLine(d.ind.macd.dif, pane, {}, (c) => c.line2);
      addLine(d.ind.macd.dea, pane, {}, (c) => c.line);
    } else if (key === 'rsi') {
      const s = addLine(d.ind.rsi, pane, { lineWidth: 2, autoscaleInfoProvider: fixedRange(0, 100), priceFormat: levelsOnly([30, 70]) }, (c) => c.line2);
      s.priceScale().applyOptions(TIGHT);
      [30, 70].forEach((lv) => levelLine(s, lv, c0));
    } else {
      const s = addLine(d.ind.kdj.k, pane, { autoscaleInfoProvider: fixedRange(0, 100), priceFormat: levelsOnly([20, 80]) }, (c) => c.line2);
      s.priceScale().applyOptions(TIGHT);
      addLine(d.ind.kdj.d, pane, {}, (c) => c.line);
      addLine(d.ind.kdj.j, pane, {}, (c) => c.riseText);
      [20, 80].forEach((lv) => levelLine(s, lv, c0));
    }
  });

  paint.forEach((f) => f(c0));
  // 只改「看哪一段」（逻辑坐标 = 第几根 K 线），不改喂给各序列的数据，所以买卖点标记数量不受影响
  const showRange = (v) => {
    if (v === 'recent' && canZoomRecent(n)) chart.timeScale().setVisibleLogicalRange({ from: n - RECENT_BARS, to: n - 1 });
    else chart.timeScale().fitContent();
  };
  showRange(view);
  track(chart, (c) => paint.forEach((f) => f(c)));

  // 窗格高度（lightweight-charts 里是权重，按像素给即可）+ 窗格标题
  const panes = chart.panes();
  heights.forEach((h, i) => { if (panes[i]?.setStretchFactor) panes[i].setStretchFactor(h / 100); else panes[i]?.setHeight(h); });
  const labels = [[t('u2.st.position'), 1], [t('u2.st.volume'), 2], ...panels.map((p, k) => [paneTitle(p, d), 3 + k])];
  // 窗格标题的位置要等图表把各窗格高度排好才能算。原来只等一帧：偶尔这一帧窗格高度还是 0，
  // 「仓位 / 成交量」标题就叠到价格区上（D81 像素对比时抓到）。现在高度没排好就下一帧再试（最多 30 帧），
  // 图表框尺寸变化（窗口缩放、手机转屏）时也重新摆一次。
  const spans = labels.map(([text]) => el('span', { class: 'u2-pane-label' }, text));
  box.append(...spans);
  const place = (tries = 0) => {
    const hs = panes.map((p) => p.getHeight?.() ?? 0);
    if (hs.slice(1).some((h) => h <= 0) && tries < 30) { requestAnimationFrame(() => place(tries + 1)); return; }
    let top = 0;
    const tops = hs.map((h) => { const y = top; top += h + 1; return y; });
    labels.forEach(([, i], k) => { spans[k].style.top = `${(tops[i] ?? 0) + 4}px`; });
  };
  requestAnimationFrame(() => place());
  if (window.ResizeObserver) new ResizeObserver(() => requestAnimationFrame(() => place())).observe(box);

  attachTooltip(chart, box, (param) => {
    const i = idx.get(String(param.time));
    return i === undefined ? null : tipRows(d, i, ind, panels);
  });
  return { chart, showRange };
}
