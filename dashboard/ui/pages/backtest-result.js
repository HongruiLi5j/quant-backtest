/* ==========================================================
 * 文件: ui/pages/backtest-result.js
 * 用途: 回测结果区（对应旧页面 renderResult / renderReport）：
 *   8 张指标卡（和买入持有对比）→ 详细指标分组 → 净值图（对数坐标）→ 回撤图 → 持仓文字
 *   → 最近收益表 → 最近交易表。分析区（样本内外、月度、蒙特卡洛……）在 backtest-analysis.js
 *   r = GET /api/backtest 的返回，见 src/registry.py: run()
 * ========================================================== */
import { el, fmt } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { tDefault } from '../shared/t-fallback.js';
import { attachTooltip, drawdownChart, equityChart, syncTimeScale } from '../shared/chart-helpers.js';
import { card, isNum, legend, pct, table, tb, toneClass } from './u2-lib.js';

/** [统计字段, 词条, 小数位, 单位, 持有基准的显示方式] —— 与旧页面 8 张卡一致 */
const TILES = [
  ['CAGR%', 'metric.cagr', 1, '%'], ['ExcessSharpe', 'u2.bt.excess_sharpe', 2, ''],
  ['MaxDD%', 'metric.maxdd', 1, '%'], ['Vol%', 'metric.vol', 1, '%'],
  ['Calmar', 'metric.calmar', 2, ''], ['Exposure%', 'metric.exposure', 0, '%', '100%'],
  ['Trades', 'metric.trades', 0, '', '—'], ['Win%', 'metric.win', 0, '%', '—'],
];

function tiles(r) {
  const s = r.stats; const b = r.bench_stats;
  return el('section', { class: 'ws-grid ws-grid--kpi u2-tiles', 'aria-label': t('backtest.kpi.title') },
    ...TILES.map(([key, lab, d, unit, fixed]) => el('div', { class: 'metric' },
      el('span', { class: 'metric__label' }, t(lab)),
      el('span', { class: 'metric__value num' }, isNum(s[key]) ? fmt(s[key], d, unit) : '—'),
      el('span', { class: 'metric__foot num' }, t('u2.bt.vs_hold', { v: fixed ?? (isNum(b[key]) ? fmt(b[key], d, unit) : '—') })))));
}

/** 后端 report 的单项格式：f = pct / int / days / x / num */
function reportValue(it) {
  const v = it.v;
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  if (it.f === 'pct') return `${v.toFixed(2)}%`;
  if (it.f === 'int') return String(Math.round(v));
  if (it.f === 'days') return t('u2.bt.days_n', { n: Math.round(v) });
  if (it.f === 'x') return `${v.toFixed(2)}x`;
  return Math.abs(v) < 0.1 && v !== 0 ? v.toFixed(4) : v.toFixed(2);
}

function report(groups) {
  if (!groups?.length) return null;
  return card(t('u2.bt.report'), null,
    ...groups.map((g) => el('div', { class: 'u2-rg' },
      el('p', { class: 'eyebrow' }, tb(g.group)),
      el('dl', { class: 'u2-rgrid' }, ...g.items.map((it) => el('div', { class: 'u2-ri' },
        el('dt', { 'data-tip': it.tip ? tb(it.tip) : null, tabindex: it.tip ? '0' : null }, tb(it.k)),
        el('dd', { class: 'num' }, reportValue(it))))))));
}

/** 净值 + 回撤两张图。净值用对数坐标（和旧页面一致），两张图时间轴联动 */
function chartsCard(r, label) {
  const eqBox = el('div', { class: 'chart chart--lg', role: 'img', 'aria-label': t('backtest.equity.aria') });
  const ddBox = el('div', { class: 'chart chart--sm', role: 'img', 'aria-label': t('backtest.dd.title') });
  const node = card(t('u2.bt.equity_log'), null,
    legend([{ label, color: 'var(--chart-1)' }, { label: t('u2.bt.hold'), color: 'var(--chart-bench)' }]),
    eqBox,
    el('p', { class: 'eyebrow', style: { margin: 'var(--space-4) 0 var(--space-2)' } }, t('backtest.dd.title')),
    ddBox);
  // 图要等节点进入页面、有了宽度再建
  queueMicrotask(() => requestAnimationFrame(() => {
    const eq = equityChart(eqBox, { dates: r.dates, equity: r.equity, bench: r.bench });
    const dd = drawdownChart(ddBox, { dates: r.dates, drawdown: r.drawdown });
    if (eq) {
      const fmtV = (v) => fmt(v, 2);
      eq.chart.priceScale('right').applyOptions({ mode: 1 });
      eq.strat.applyOptions({ priceFormat: { type: 'custom', formatter: fmtV } });
      eq.bench.applyOptions({ priceFormat: { type: 'custom', formatter: fmtV } });
      eq.strat.setData(r.dates.map((d, i) => ({ time: d, value: r.equity[i] })));
      eq.bench.setData(r.dates.map((d, i) => ({ time: d, value: r.bench[i] })));
      eq.chart.timeScale().fitContent();
      attachTooltip(eq.chart, eqBox, (p) => {
        const s = p.seriesData.get(eq.strat); const b = p.seriesData.get(eq.bench);
        return s ? [{ label: label, value: fmt(s.value, 3) }, { label: t('u2.bt.hold'), value: fmt(b?.value, 3) }] : null;
      });
    }
    if (dd) attachTooltip(dd.chart, ddBox, (p) => { const d = p.seriesData.get(dd.series); return d ? [{ label: t('backtest.dd.title'), value: fmt(d.value, 2, '%'), tone: 'is-fall' }] : null; });
    syncTimeScale(eq?.chart, dd?.chart);
  }));
  return node;
}

/** 当前持仓 / 明日目标，如「沪深300ETF 60%、…」；空仓显示「空仓」 */
function positionText(r, names) {
  const ps = (p) => (Object.keys(p).length ? Object.entries(p).map(([k, v]) => `${names[k] || k} ${(v * 100).toFixed(0)}%`).join(tDefault('a02.sep.list')) : t('u2.bt.flat'));
  return el('p', { class: 'muted u2-pos' },
    t('u2.bt.pos_range', { start: r.start, end: r.end, cost: fmt(r.cost_bp, 0) }), ' ',
    t('u2.bt.pos_now'), el('b', {}, ps(r.position_now)), t('u2.bt.pos_next'), el('b', {}, ps(r.position_next)), tDefault('a02.punct.end'));
}

const RECENT = [['1D', 'u2.bt.r.1d'], ['1W', 'u2.bt.r.1w'], ['1M', 'u2.bt.r.1m'], ['3M', 'u2.bt.r.3m'], ['1Y', 'u2.bt.r.1y'], ['YTD', 'u2.bt.r.ytd']];

function recentCard(r) {
  return card(t('u2.bt.recent'), null,
    table([], RECENT.map(([k, lab]) => [t(lab),{ v: pct(r.recent[k], 2), num: true, cls: toneClass(r.recent[k]) }])));
}

function tradesCard(r) {
  const body = r.trades.length
    ? table([{ text: t('backtest.trades.entry') }, { text: t('backtest.trades.exit') }, { text: t('backtest.trades.days'), num: true }, { text: t('backtest.trades.ret'), num: true }],
      r.trades.map((x) => [{ v: x.entry, num: true }, x.open ? el('span', { class: 'badge badge--accent' }, t('u2.bt.holding')) : { v: x.exit, num: true },
        { v: String(x.days), num: true }, { v: pct(x.ret, 2), num: true, cls: toneClass(x.ret) }]), { scroll: true })
    : el('p', { class: 'muted' }, t('u2.bt.no_trades'));
  return card(t('u2.bt.recent_trades'), t('backtest.trades.count', { n: r.trades.length }), body);
}

/** 结果区主入口：返回一个 DocumentFragment（不含分析区） */
export function resultNodes(r, label, names) {
  const frag = document.createDocumentFragment();
  frag.append(tiles(r));
  const rep = report(r.report);
  if (rep) frag.append(el('div', { class: 'u2-gap' }, rep));
  frag.append(el('div', { class: 'u2-gap' }, chartsCard(r, label)));
  frag.append(positionText(r, names));
  return frag;
}

export const recentAndTrades = (r) => el('div', { class: 'ws-grid ws-grid--2 u2-gap' }, recentCard(r), tradesCard(r));
