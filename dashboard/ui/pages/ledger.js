/* ==========================================================
 * 文件: ui/pages/ledger.js —— 前向账本展示页（T040）
 * 接口: GET /api/ledger/summary（后台设置 ledger_page_on 默认关闭；关着时 404，本页显示「功能未开启」）
 * 只展示「先登记、后核对」的统计与局限，不显示股票代码、不显示买卖清单。文字一律 textContent。
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';

const pct = (x, d = 1) => (x === null || x === undefined || !Number.isFinite(x) ? '—' : `${(x * 100).toFixed(d)}%`);
const int = (x) => (x === null || x === undefined || !Number.isFinite(x) ? '—' : String(Math.round(x)));

function rowsTable(rows) {
  const body = rows.map((r) => el('tr', {},
    el('td', {}, t(`led.act.${r.action}`)), el('td', { class: 'num' }, `${int(r.h)}`),
    el('td', { class: 'num' }, int(r.n)), el('td', { class: 'num' }, int(r.days)),
    el('td', { class: 'num' }, r.hit_lo === null ? pct(r.hit_excess) : `${pct(r.hit_excess)} (${pct(r.hit_lo, 0)}–${pct(r.hit_hi, 0)})`),
    el('td', { class: 'num' }, pct(r.mean_excess, 2)), el('td', { class: 'num' }, pct(r.net_excess, 2)),
    el('td', { class: r.enough ? '' : 'muted' }, r.enough ? (r.verdict || '—') : t('led.insufficient'))));
  return el('div', { class: 'tablewrap' }, el('table', { class: 'table' },
    el('thead', {}, el('tr', {}, ...['led.col.action', 'led.col.h', 'led.col.n', 'led.col.days', 'led.col.hit', 'led.col.mean', 'led.col.net', 'led.col.verdict'].map((k) => el('th', {}, t(k))))),
    el('tbody', {}, ...body)));
}

function csvOf(rows) {
  const head = ['action', 'hold_days', 'n', 'signal_days', 'hit_excess', 'hit_lo', 'hit_hi', 'mean_excess', 'net_excess', 'enough', 'verdict'];
  const cell = (x) => (x === null || x === undefined ? '' : String(x));
  return [head.join(','), ...rows.map((r) => [r.action, r.h, r.n, r.days, r.hit_excess, r.hit_lo, r.hit_hi, r.mean_excess, r.net_excess, r.enough, r.verdict].map(cell).join(','))].join('\n') + '\n';
}

export async function render(root) {
  let d;
  try {
    d = await getJSON('/api/ledger/summary');
  } catch (e) {
    root.replaceChildren(el('div', { class: 'card' }, el('p', { class: 'muted' }, e.status === 404 ? t('led.off') : t('ui.state.error'))));
    return;
  }
  if (d.status !== 'ok') {
    root.replaceChildren(el('div', { class: 'card' }, el('p', { class: 'muted' }, t('led.na'))));
    return;
  }
  const warn = d.any_enough ? null : el('div', { class: 'card u1-note is-warn', role: 'alert' },
    t('led.small', { days: int(d.evaluated_days), min: int(d.min_sample) }));
  const refresh = el('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, t('led.btn.refresh'));
  refresh.addEventListener('click', () => render(root));
  const csv = el('button', { class: 'btn btn--sm btn--ghost', type: 'button', disabled: !d.rows.length }, t('led.btn.csv'));
  csv.addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([csvOf(d.rows)], { type: 'text/csv;charset=utf-8' }));
    const a = el('a', { href: url, download: 'forward_ledger_summary.csv' });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  root.replaceChildren(
    el('div', { class: 'card' }, el('p', {}, t('led.intro')), el('p', { class: 'muted' }, t('led.nosignal')), el('div', { class: 'row' }, refresh, csv)),
    warn,
    el('div', { class: 'card' },
      el('p', {}, t('led.counts', { s: int(d.signal_days), e: int(d.evaluated_days), date: d.today || '—' })),
      d.promotion.any_passed ? el('p', {}, t('led.promo.yes')) : el('p', { class: 'muted' }, t('led.promo.no', { w: int(d.promotion.complete_weeks), m: int(d.promotion.min_weeks) })),
      d.rows.length ? rowsTable(d.rows) : el('p', { class: 'muted' }, t('led.norows'))),
    el('div', { class: 'card' }, el('h3', {}, t('led.caveats')),
      el('ul', {}, ...d.caveats.map((c) => el('li', {}, c)), el('li', {}, t('led.caveat.indep')))));
}
