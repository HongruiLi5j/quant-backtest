/* ==========================================================
 * 文件: ui/pages/u2-lib.js
 * 用途: U2（回测 / 个股）两页共用的小工具：表格、CSV 导出、折线图、图例、动作徽章
 *   - 折线图用本地 lightweight-charts（页面 html 里用 script 标签引入 ../vendor/...，全局 LightweightCharts）
 *   - 颜色全部取 CSS tokens（chartColors），切换主题时自动重绘（themechange）
 *   - 所有文字走 textContent / el()，不拼 HTML
 * ========================================================== */
import { el, fmt, toneClass, cssVar } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { chartColors, withAlpha } from '../shared/chart-helpers.js';

export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
/** 带正负号的百分数文本，空值显示 — */
export const pct = (v, d = 1) => fmt(v, d, '%', true);
export { toneClass };

/** 表格：heads = [{ text, num }]，rows = [[单元格(字符串/Node/{v, cls})]]；cls 可加 is-rise 等 */
export function table(heads, rows, opts = {}) {
  const cell = (c, tag = 'td') => {
    const spec = c !== null && typeof c === 'object' && !(c instanceof Node) && 'v' in c ? c : { v: c };
    return el(tag, { class: [spec.num ? 'num' : '', spec.cls ?? ''].join(' ').trim() || null, colspan: spec.span ?? null }, spec.v);
  };
  const head = heads.length ? el('thead', {}, el('tr', {}, ...heads.map((h) => el('th', { scope: 'col', class: h.num ? 'num' : null }, h.text)))) : null;
  const body = el('tbody', {}, ...rows.map((r) => el('tr', { class: r.cls ?? null, dataset: r.dataset ?? {} }, ...(r.cells ?? r).map((c) => cell(c)))));
  return el('div', { class: `table-wrap ${opts.scroll ? 'u2-scroll' : ''}`.trim() }, el('table', { class: 'table' }, head, body));
}

/** 带标题的卡片 */
export function card(title, sub, ...children) {
  return el('section', { class: 'card' },
    el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, title), sub ? el('span', { class: 'card__sub' }, sub) : null),
    ...children);
}

/** 浏览器端导出 CSV（带 BOM，Excel 打开不乱码）。header = [[字段或函数, 列名]] */
export function downloadCSV(name, header, rows) {
  const q = (v) => { const s = v === null || v === undefined ? '' : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const lines = [header.map((h) => q(h[1])).join(',')].concat(rows.map((r) => header.map((h) => q(typeof h[0] === 'function' ? h[0](r) : r[h[0]])).join(',')));
  const url = URL.createObjectURL(new Blob([`﻿${lines.join('\n')}`], { type: 'text/csv;charset=utf-8' }));
  const a = el('a', { href: url, download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 图例：items = [{ label, color(CSS 颜色或 var()) }] */
export function legend(items) {
  return el('div', { class: 'chart-legend' }, ...items.map((i) => el('span', {}, el('span', { class: 'chart-legend__swatch', style: { '--c': i.color } }), i.label)));
}

const charts = new Set();
/** 图表公共配置（和 shared/chart-helpers.js 的 baseOptions 同风格） */
export function baseOptions(c, height) {
  return {
    autoSize: true,
    ...(height ? { height } : {}),
    layout: { background: { type: 'solid', color: 'transparent' }, textColor: c.text, fontFamily: c.font, fontSize: 11, attributionLogo: true },
    grid: { vertLines: { color: 'transparent' }, horzLines: { color: c.grid } },
    rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.1, bottom: 0.08 } },
    timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
    crosshair: {
      mode: 0,
      vertLine: { color: withAlpha(c.crosshair, 0.6), width: 1, style: 3, labelBackgroundColor: c.crosshair },
      horzLine: { color: withAlpha(c.crosshair, 0.4), width: 1, style: 3, labelBackgroundColor: c.crosshair },
    },
    handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true },
  };
}

/** 登记一张图：换主题时按 recolor(c) 重新取色 */
export function track(chart, recolor) {
  const entry = { chart, recolor: () => { const c = chartColors(); chart.applyOptions(baseOptions(c)); recolor?.(c); } };
  charts.add(entry);
  return entry;
}
/** 换主题时重绘；容器已经不在页面里的图（重新跑回测后留下的旧图）顺手销毁 */
window.addEventListener('themechange', () => requestAnimationFrame(() => charts.forEach((e) => {
  if (!e.chart.chartElement().isConnected) { e.chart.remove(); charts.delete(e); return; }
  e.recolor();
})));

/** 图表库没加载时的提示 */
export function chartMissing(box) {
  box.replaceChildren(el('p', { class: 'state muted', style: { display: 'flex' } }, t('u2.chart.missing')));
  return null;
}

/**
 * 通用折线图（时间轴 = 日期字符串）。
 * series: [{ name, values, color: (c)=>颜色, width, dash, area }]；opts: { log, yfmt, zero }
 * 返回 { chart, lines }；values 里的 null 会被跳过
 */
export function lineChart(box, dates, series, opts = {}) {
  const lib = window.LightweightCharts;
  if (!lib) return chartMissing(box);
  const c0 = chartColors();
  const chart = lib.createChart(box, baseOptions(c0));
  const yfmt = opts.yfmt ?? ((v) => fmt(v, 2));
  if (opts.log) chart.priceScale('right').applyOptions({ mode: 1 });
  const lines = series.map((s) => {
    const kind = s.area ? lib.AreaSeries : lib.LineSeries;
    const color = s.color(c0);
    const api = chart.addSeries(kind, {
      ...(s.area ? { lineColor: color, topColor: withAlpha(color, 0.3), bottomColor: withAlpha(color, 0.02) } : { color }),
      lineWidth: s.width ?? 2, lineStyle: s.dash ? 2 : 0, priceLineVisible: false, lastValueVisible: false,
      priceFormat: { type: 'custom', formatter: yfmt },
    });
    api.setData(dates.map((d, i) => ({ time: d, value: s.values[i] })).filter((p) => isNum(p.value)));
    return api;
  });
  if (opts.zero && lines[0]) lines[0].createPriceLine({ price: 0, color: c0.grid, lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
  chart.timeScale().fitContent();
  track(chart, (c) => lines.forEach((api, i) => {
    const color = series[i].color(c);
    api.applyOptions(series[i].area ? { lineColor: color, topColor: withAlpha(color, 0.3), bottomColor: withAlpha(color, 0.02) } : { color });
  }));
  return { chart, lines };
}

/** 一组备用折线色（取 tokens，不写死颜色） */
export const palette = [(c) => c.line, (c) => c.line2, (c) => c.riseText, (c) => c.fallText, (c) => cssVar('--accent-glow', c.line)];
/** 图例用的 CSS 变量名，和 palette 一一对应 */
export const paletteVars = ['var(--chart-1)', 'var(--chart-2)', 'var(--color-rise)', 'var(--color-fall)', 'var(--accent-glow)'];

/** 后端返回的固定中文词（指标名、分组、说明、假设标题、板块、时区）→ 当前语言；词条里没有就原样显示 */
export function tb(text) {
  if (typeof text !== 'string') return text;
  const key = `u2.be.${text}`;
  const v = t(key);
  return v === key ? text : v;
}

/** 动作（买入/卖出/加仓/减仓/持有/空仓）→ 词条 key 与徽章样式。后端给的是中文原词 */
const ACTIONS = { 买入: ['buy', 'badge--rise'], 加仓: ['add', 'badge--rise'], 卖出: ['sell', 'badge--fall'], 减仓: ['trim', 'badge--fall'], 持有: ['hold', 'badge--accent'], 空仓: ['flat', ''] };
export function actionBadge(action) {
  const [k, cls] = ACTIONS[action] ?? [null, ''];
  return el('span', { class: `badge ${cls}`.trim() }, k ? t(`u2.action.${k}`) : (action ?? '—'));
}
