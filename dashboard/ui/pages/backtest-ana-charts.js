/* ==========================================================
 * 文件: ui/pages/backtest-ana-charts.js
 * 用途: 回测分析区里的图：滚动 Sharpe、仓位占用、多基准对比（lightweight-charts），
 *       以及每笔收益 / 持仓天数直方图（纯 SVG，用 svg() 一个个建节点，不拼字符串）
 * ========================================================== */
import { el, fmt, svg } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { card, legend, lineChart, palette, paletteVars } from './u2-lib.js';

/** 直方图：h = { edges, counts }；kind = 'pct'（负区间标跌色）| 'days' */
export function histogram(box, h, kind) {
  const counts = h?.counts ?? []; const edges = h?.edges ?? [];
  if (!counts.length) { box.replaceChildren(el('p', { class: 'muted' }, t('u2.ana.few'))); return; }
  const W = Math.max(280, box.clientWidth || 420); const H = 150; const m = { l: 8, r: 8, t: 8, b: 22 };
  const mx = Math.max(...counts, 1); const bw = (W - m.l - m.r) / counts.length;
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', class: 'u2-hist__svg', 'aria-label': t(kind === 'pct' ? 'u2.ana.hist_ret' : 'u2.ana.hist_days') });   // axe：role=img 的 svg 要有名字
  counts.forEach((n, i) => {
    const hh = (n / mx) * (H - m.t - m.b);
    const neg = kind === 'pct' && (edges[i] + edges[i + 1]) / 2 < 0;
    const rect = svg('rect', { x: m.l + i * bw + 1, y: H - m.b - hh, width: Math.max(1, bw - 2), height: hh, rx: 2, class: neg ? 'u2-bar u2-bar--neg' : 'u2-bar' });
    const tip = svg('title');
    tip.textContent = t('u2.ana.bin_tip', { a: edges[i], b: edges[i + 1], n });
    rect.append(tip);
    root.append(rect);
  });
  [0, Math.floor(counts.length / 2), counts.length].forEach((i) => {
    const label = svg('text', { x: m.l + i * bw, y: H - 6, 'text-anchor': i === 0 ? 'start' : i === counts.length ? 'end' : 'middle', class: 'u2-axis' });
    label.textContent = kind === 'pct' ? `${edges[i]}%` : t('u2.ana.days_tick', { n: edges[i] });
    root.append(label);
  });
  box.replaceChildren(root);
}

/** 滚动 Sharpe + 仓位占用（两列）+ 多基准（整行） */
export function chartSection(a, r) {
  const rsBox = el('div', { class: 'chart u2-chart' });
  const exBox = el('div', { class: 'chart u2-chart' });
  const parts = [el('div', { class: 'ws-grid ws-grid--2' },
    card(t('u2.ana.rolling'), t('u2.ana.rolling_sub'),
      legend([{ label: t('chart.strategy'), color: 'var(--chart-1)' }, { label: t('u2.bt.hold'), color: 'var(--chart-bench)' }]), rsBox),
    a.exposure ? card(t('u2.ana.exposure'), t('u2.ana.exposure_sub'), exBox) : null)];
  const bmBox = el('div', { class: 'chart chart--lg' });
  if (a.benchmarks) {
    const names = Object.keys(a.benchmarks);
    parts.push(el('div', { class: 'u2-gap' }, card(t('u2.ana.bench'), t('u2.ana.bench_sub'),
      legend([{ label: t('chart.strategy'), color: 'var(--chart-1)' }, { label: t('u2.bt.hold'), color: 'var(--chart-bench)' },
        ...names.map((n, i) => ({ label: n, color: paletteVars[(i + 1) % paletteVars.length] }))]), bmBox)));
  }
  queueMicrotask(() => requestAnimationFrame(() => {
    lineChart(rsBox, r.dates, [
      { values: a.bench_rolling_sharpe, color: (c) => c.bench, width: 1 },
      { values: a.rolling_sharpe, color: (c) => c.line, width: 2 }], { zero: true });
    if (a.exposure) lineChart(exBox, r.dates, [{ values: a.exposure, color: (c) => c.line2, width: 1, area: true }], { yfmt: (v) => fmt(v * 100, 0, '%') });
    if (a.benchmarks) {
      const extra = Object.values(a.benchmarks).map((values, i) => ({ values, color: palette[(i + 1) % palette.length], width: 1 }));
      lineChart(bmBox, r.dates, [{ values: r.bench, color: (c) => c.bench, width: 1 }, { values: r.equity, color: (c) => c.line, width: 2 }, ...extra], { log: true });
    }
  }));
  return el('div', {}, ...parts);
}
