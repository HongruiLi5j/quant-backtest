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
  root.replaceChildren(
    el('div', { class: 'card' }, el('p', {}, t('led.intro')), el('p', { class: 'muted' }, t('led.nosignal'))),
    warn,
    el('div', { class: 'card' },
      el('p', {}, t('led.counts', { s: int(d.signal_days), e: int(d.evaluated_days), date: d.today || '—' })),
      d.promotion.any_passed ? el('p', {}, t('led.promo.yes')) : el('p', { class: 'muted' }, t('led.promo.no', { w: int(d.promotion.complete_weeks), m: int(d.promotion.min_weeks) })),
      d.rows.length ? rowsTable(d.rows) : el('p', { class: 'muted' }, t('led.norows'))),
    el('div', { class: 'card' }, el('h3', {}, t('led.caveats')),
      el('ul', {}, ...d.caveats.map((c) => el('li', {}, c)), el('li', {}, t('led.caveat.indep')))));
}
