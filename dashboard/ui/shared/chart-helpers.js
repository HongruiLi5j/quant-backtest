/* ==========================================================
 * 文件: shared/chart-helpers.js
 * 版本: 0.1.0 · 更新: 2026-10-23 · 作者: Claude（为李泓锐）
 * 用途: TradingView lightweight-charts 5.2.1 的封装（CDN 全局变量 LightweightCharts）。
 *   - 颜色全部从 CSS tokens 读取，切换深浅色 / 涨跌色时自动重绘（监听 themechange）
 *   - 权益曲线 / 回撤水下图 / K 线 + 买卖点，三种图共用一套配置
 *   - 玻璃拟态悬浮提示：subscribeCrosshairMove 拿数据，只改 transform 与 textContent
 *   - CDN 没加载成功时，在容器里显示一行提示而不是报错
 * ========================================================== */

import { el, cssVar, fmt } from './dom.js';
import { t } from './i18n.js';

const LWC = () => window.LightweightCharts;
const registry = new Set();     // 已创建的图表，换主题时统一重绘

/** 给颜色加透明度：支持 #rrggbb 与 rgba() */
export function withAlpha(color, alpha) {
  const c = color.trim();
  if (c.startsWith('#') && c.length === 7) {
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  const m = c.match(/rgba?\(([^)]+)\)/);
  if (m) {
    const [r, g, b] = m[1].split(',').map((s) => s.trim());
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  return c;
}

/** 从 CSS 变量读当前配色 */
export function chartColors() {
  return {
    line: cssVar('--chart-1', '#9f7bff'),
    line2: cssVar('--chart-2', '#5b8cff'),
    bench: cssVar('--chart-bench', 'rgba(255,255,255,0.45)'),
    dd: cssVar('--chart-dd', '#ff8a8a'),
    grid: cssVar('--chart-grid', 'rgba(255,255,255,0.05)'),
    text: cssVar('--chart-text', 'rgba(255,255,255,0.6)'),
    rise: cssVar('--fill-rise', '#f87171'),
    fall: cssVar('--fill-fall', '#22c55e'),
    riseText: cssVar('--color-rise', '#ff8a8a'),
    fallText: cssVar('--color-fall', '#4ade80'),
    crosshair: cssVar('--accent-glow', '#9f7bff'),
    font: cssVar('--font-mono', 'monospace'),
  };
}

function baseOptions(c) {
  return {
    autoSize: true,
    layout: {
      background: { type: 'solid', color: 'transparent' },
      textColor: c.text,
      fontFamily: c.font,
      fontSize: 11,
      attributionLogo: true,          // lightweight-charts 许可要求保留署名
    },
    grid: { vertLines: { color: 'transparent' }, horzLines: { color: c.grid } },
    rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.08 } },
    timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
    crosshair: {
      mode: 0,
      vertLine: { color: withAlpha(c.crosshair, 0.6), width: 1, style: 3, labelBackgroundColor: c.crosshair },
      horzLine: { color: withAlpha(c.crosshair, 0.4), width: 1, style: 3, labelBackgroundColor: c.crosshair },
    },
    handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true },
    localization: { priceFormatter: (v) => fmt(v, 1) },
  };
}

/** CDN 没加载时的兜底提示 */
function unavailable(container) {
  container.replaceChildren(el('p', { class: 'state muted', style: { display: 'flex' } }, t('chart.unavailable')));
  return null;
}

/** 创建一张基础图并登记，返回 { chart, recolor } */
function makeChart(container, recolor) {
  const lib = LWC();
  if (!lib) return unavailable(container);
  const chart = lib.createChart(container, baseOptions(chartColors()));
  const entry = { chart, recolor: () => { const c = chartColors(); chart.applyOptions(baseOptions(c)); recolor(c); } };
  registry.add(entry);
  return entry;
}

/** 权益曲线：策略（紫→透明渐变面积）vs 基准（虚线） */
export function equityChart(container, data) {
  const lib = LWC();
  let strat;
  let bench;
  const entry = makeChart(container, (c) => {
    strat.applyOptions({ lineColor: c.line, topColor: withAlpha(c.line, 0.38), bottomColor: withAlpha(c.line2, 0.02) });
    bench.applyOptions({ color: c.bench });
  });
  if (!entry) return null;
  const c = chartColors();
  strat = entry.chart.addSeries(lib.AreaSeries, {
    lineColor: c.line, lineWidth: 2, topColor: withAlpha(c.line, 0.38), bottomColor: withAlpha(c.line2, 0.02),
    priceFormat: { type: 'custom', formatter: (v) => fmt(v, 1, '%') }, lastValueVisible: true, priceLineVisible: false,
  });
  bench = entry.chart.addSeries(lib.LineSeries, {
    color: c.bench, lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false,
    priceFormat: { type: 'custom', formatter: (v) => fmt(v, 1, '%') },
  });
  strat.setData(data.dates.map((d, i) => ({ time: d, value: (data.equity[i] - 1) * 100 })));
  bench.setData(data.dates.map((d, i) => ({ time: d, value: (data.bench[i] - 1) * 100 })));
  entry.chart.timeScale().fitContent();
  return { chart: entry.chart, strat, bench };
}

/** 回撤水下图：以 0 为基线，向下填充 */
export function drawdownChart(container, data) {
  const lib = LWC();
  let series;
  const entry = makeChart(container, (c) => {
    series.applyOptions({ bottomLineColor: c.dd, bottomFillColor1: withAlpha(c.dd, 0.05), bottomFillColor2: withAlpha(c.dd, 0.4) });
  });
  if (!entry) return null;
  const c = chartColors();
  series = entry.chart.addSeries(lib.BaselineSeries, {
    baseValue: { type: 'price', price: 0 },
    topLineColor: 'transparent', topFillColor1: 'transparent', topFillColor2: 'transparent',
    bottomLineColor: c.dd, bottomFillColor1: withAlpha(c.dd, 0.05), bottomFillColor2: withAlpha(c.dd, 0.4),
    lineWidth: 1, priceLineVisible: false,
    priceFormat: { type: 'custom', formatter: (v) => fmt(v, 1, '%') },
  });
  series.setData(data.dates.map((d, i) => ({ time: d, value: data.drawdown[i] })));
  entry.chart.applyOptions({ rightPriceScale: { scaleMargins: { top: 0.05, bottom: 0.02 } } });
  entry.chart.timeScale().fitContent();
  return { chart: entry.chart, series };
}

/** K 线 + 历史买卖信号点 */
export function candleChart(container, kline, markers = []) {
  const lib = LWC();
  let series;
  let markerApi;
  const toMarkers = (c) => markers.map((m) => (m.side === 'buy'
    ? { time: m.time, position: 'belowBar', shape: 'arrowUp', color: c.riseText, text: 'B' }
    : { time: m.time, position: 'aboveBar', shape: 'arrowDown', color: c.fallText, text: `S ${fmt(m.ret, 1, '%', true)}` }));
  const entry = makeChart(container, (c) => {
    series.applyOptions({ upColor: c.rise, downColor: c.fall, wickUpColor: c.rise, wickDownColor: c.fall });
    markerApi.setMarkers(toMarkers(c));
  });
  if (!entry) return null;
  const c = chartColors();
  series = entry.chart.addSeries(lib.CandlestickSeries, {
    upColor: c.rise, downColor: c.fall, wickUpColor: c.rise, wickDownColor: c.fall, borderVisible: false,
    priceFormat: { type: 'price', precision: 3, minMove: 0.001 },
  });
  series.setData(kline);
  markerApi = lib.createSeriesMarkers(series, toMarkers(c));
  entry.chart.timeScale().fitContent();
  return { chart: entry.chart, series };
}

/**
 * 玻璃拟态悬浮提示。render(param) 返回 [{label, value, tone}] 行；
 * 位置只写 CSS 变量 --tx / --ty（transform），不触发布局。
 */
export function attachTooltip(chart, container, render) {
  const tip = el('div', { class: 'chart-tip', role: 'presentation', 'aria-hidden': 'true' });
  container.append(tip);
  chart.subscribeCrosshairMove((param) => {
    if (!param.point || !param.time || param.point.x < 0 || param.point.y < 0) {
      tip.classList.remove('is-visible');
      return;
    }
    const rows = render(param);
    if (!rows) return;
    tip.replaceChildren(
      el('div', { class: 'chart-tip__date' }, String(param.time)),
      ...rows.map((r) => el('div', { class: 'chart-tip__row' }, el('span', { class: 'muted' }, r.label), el('span', { class: r.tone ?? '' }, r.value))),
    );
    const w = tip.offsetWidth || 180;
    const x = Math.min(container.clientWidth - w - 8, Math.max(8, param.point.x + 16));
    const y = Math.max(8, Math.min(param.point.y - 24, container.clientHeight - 96));
    tip.style.setProperty('--tx', `${x}px`);
    tip.style.setProperty('--ty', `${y}px`);
    tip.classList.add('is-visible');
  });
  return tip;
}

/** 两张图时间轴联动（拖动/缩放上面的，下面跟着动） */
export function syncTimeScale(a, b) {
  if (!a || !b) return;
  let lock = false;
  const link = (from, to) => from.timeScale().subscribeVisibleLogicalRangeChange((range) => {
    if (lock || !range) return;
    lock = true;
    to.timeScale().setVisibleLogicalRange(range);
    lock = false;
  });
  link(a, b);
  link(b, a);
}

/** 主题或涨跌色改变时，所有图表重读 tokens */
window.addEventListener('themechange', () => {
  requestAnimationFrame(() => registry.forEach((entry) => entry.recolor()));
});
