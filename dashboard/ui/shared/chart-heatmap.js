/* ==========================================================
 * 文件: shared/chart-heatmap.js
 * 版本: 0.1.0 · 更新: 2026-10-23 · 作者: Claude（为李泓锐）
 * 用途: 月度收益热力图（年份 × 12 个月 + 全年），纯 CSS Grid，不依赖图表库。
 *       颜色强度 --k = |收益| / 最大绝对值，正负分别走涨/跌色（随 A 股 / 美股切换）。
 *       每个格子是可聚焦的 role="gridcell"，带 aria-label，读屏能逐格读出。
 * ========================================================== */

import { el, fmt } from './dom.js';
import { t } from './i18n.js';

/** monthly: { "2024": [m1..m12 或 null] }, yearly: { "2024": 50.1 } */
export function renderHeatmap(container, monthly, yearly) {
  const years = Object.keys(monthly).sort().reverse();
  const all = years.flatMap((y) => monthly[y]).filter((v) => v !== null);
  const maxAbs = Math.max(...all.map(Math.abs), 1);
  const months = Array.from({ length: 12 }, (_, i) => t(`month.${i + 1}`));

  const grid = el('div', { class: 'heatmap', role: 'grid', 'aria-label': t('backtest.heatmap.aria') });
  // axe（aria-required-parent / children）：role=grid 里的格子必须放在 role=row 里。每行包一层 .heatmap__row（display: contents），
  // CSS 网格布局完全不变，读屏能按行读；方向键导航按格子顺序，也不受影响
  const head = el('div', { class: 'heatmap__row', role: 'row' });
  head.append(el('div', { class: 'heatmap__head', role: 'columnheader' }, ''));
  months.forEach((m) => head.append(el('div', { class: 'heatmap__head', role: 'columnheader' }, m)));
  head.append(el('div', { class: 'heatmap__head', role: 'columnheader' }, t('backtest.heatmap.year')));
  grid.append(head);

  for (const y of years) {
    const row = el('div', { class: 'heatmap__row', role: 'row' });
    grid.append(row);
    row.append(el('div', { class: 'heatmap__year', role: 'rowheader' }, y));
    monthly[y].forEach((v, i) => {
      if (v === null) {
        row.append(el('div', { class: 'heatmap__cell', role: 'gridcell', tabindex: '-1', 'aria-label': `${y} ${months[i]} —` }, ''));
        return;
      }
      const k = Math.min(1, Math.abs(v) / maxAbs).toFixed(2);
      row.append(el('div', {
        class: `heatmap__cell ${v >= 0 ? 'heatmap__cell--pos' : 'heatmap__cell--neg'}`,
        role: 'gridcell', tabindex: '-1', style: { '--k': k },
        'aria-label': `${y} ${months[i]} ${fmt(v, 2, '%', true)}`,
        'data-tip': `${y}-${String(i + 1).padStart(2, '0')}  ${fmt(v, 2, '%', true)}`,
      }, fmt(v, 1)));
    });
    const total = yearly[y];
    row.append(el('div', {
      class: `heatmap__cell heatmap__cell--total ${total >= 0 ? 'heatmap__cell--pos' : 'heatmap__cell--neg'}`,
      role: 'gridcell', tabindex: '-1', style: { '--k': Math.min(1, Math.abs(total) / (maxAbs * 4)).toFixed(2) },
      'aria-label': `${y} ${t('backtest.heatmap.year')} ${fmt(total, 2, '%', true)}`,
    }, fmt(total, 1)));
  }
  container.replaceChildren(grid);

  // 键盘：方向键在格子间移动（roving tabindex）
  const cells = [...grid.querySelectorAll('[role="gridcell"][tabindex]')];
  if (cells[0]) cells[0].tabIndex = 0;
  grid.addEventListener('keydown', (e) => {
    const i = cells.indexOf(document.activeElement);
    if (i < 0) return;
    const step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 13, ArrowUp: -13 }[e.key];
    if (!step) return;
    e.preventDefault();
    const next = cells[Math.max(0, Math.min(cells.length - 1, i + step))];
    cells[i].tabIndex = -1;
    next.tabIndex = 0;
    next.focus();
  });
}
