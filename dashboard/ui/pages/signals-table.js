/* 信号页·表格与手机卡片（U1）。对应旧界面 renderTrade / tradeRows / tradeCard。
 * 17 列全部保留；手机（≤600px）改成卡片。所有文本走 el()，不拼 HTML。 */
import { el, fmt, toneClass } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { tDefault } from '../shared/t-fallback.js';
import { actBadge, numNA, isNum, tr, stockHref } from './U1-common.js';

/** [排序键, 词条, 是否数字列]；reason / flags 不能排序 */
export const COLUMNS = [['code', 'u1.sig.col.code'], ['close', 'u1.sig.col.close', 1], ['d1', 'u1.sig.col.d1', 1], ['avg_turnover_20d', 'u1.sig.col.turnover', 1],
  ['liquidity', 'u1.sig.col.liquidity'], ['lot_cost', 'u1.sig.col.lot', 1], ['action', 'u1.sig.col.action'], ['now', 'u1.sig.col.now', 1], ['next', 'u1.sig.col.next', 1],
  ['reason', 'u1.sig.col.reason'], ['total', 'u1.sig.col.total', 1], ['b_total', 'u1.sig.col.b_total', 1], ['beat_bh', 'u1.sig.col.beat'], ['maxdd', 'u1.sig.col.maxdd', 1],
  ['trades', 'u1.sig.col.trades', 1], ['sharpe', 'u1.sig.col.sharpe', 1], ['flags', 'u1.sig.col.flags']];
export const UNSORTABLE = ['reason', 'flags'];

/** 取值：先看 performance（样本外字段），再看行本身 */
export const rowValue = (row, key) => row.performance?.[key] ?? row[key] ?? null;

/** 排序：缺值排最后，同值按代码；个人模式“加减仓”标签里先放减仓 */
export function sortRows(rows, view, reduceFirst = false) {
  return [...rows].sort((a, b) => {
    if (reduceFirst && a.action !== b.action) return a.action === '减仓' ? -1 : 1;
    const x = rowValue(a, view.sort); const y = rowValue(b, view.sort);
    if (x === null || y === null) return x === y ? a.code.localeCompare(b.code) : x === null ? 1 : -1;
    const delta = typeof x === 'string' ? x.localeCompare(String(y), 'zh-CN') : Number(x) - Number(y);
    return delta * view.dir || a.code.localeCompare(b.code);
  });
}

const position = (v) => (isNum(v) ? `${(v * 100).toFixed(1)}%` : t('u1.na'));
/** 没有信号的相关股票显示“无信号”；有异常标记但仓位缺失仍显示“数据不足” */
export const targetText = (r) => ((!r.action && !isNum(r.now) && !isNum(r.next) && !(r.flags?.length)) ? t('u1.sig.no_signal') : position(r.next));
const flagsText = (r) => (r.flags?.length ? r.flags.join(tDefault('a02.sep.semi')) : t('u1.sig.no_flags'));
const beatText = (p) => (typeof p.beat_bh === 'boolean' ? t(p.beat_bh ? 'u1.yes' : 'u1.no') : t('u1.na'));

function nameCell(r, note) {
  return el('td', {},
    el('a', { href: stockHref(r.code), class: 'u1-name' }, r.name), ' ', el('span', { class: 'mono muted' }, r.code),
    el('span', { class: 'u1-sub' }, t('u1.sig.data_date', { d: r.last_date })),
    note?.node ?? null);
}

function tableRow(r, exception, note) {
  const p = r.performance ?? {};
  const reason = r.reason ?? '';
  return el('tr', { class: note?.cls ?? null },
    nameCell(r, note),
    el('td', { class: 'num' }, numNA(r.close), el('span', { class: 'u1-sub' }, r.price_note || t('u1.na'))),
    el('td', { class: `num ${exception ? '' : toneClass(r.d1)}` }, exception ? '—' : (isNum(r.d1) ? fmt(r.d1, 2, '%', true) : t('u1.na'))),
    el('td', { class: 'num' }, isNum(r.avg_turnover_20d) ? fmt(r.avg_turnover_20d, 0) : t('u1.na')),
    el('td', {}, tr('u1.liq', r.liquidity) || t('u1.na')),
    el('td', { class: 'num', title: t('u1.sig.lot_tip') }, numNA(r.lot_cost)),
    el('td', {}, r.action ? actBadge(r.action) : t('u1.sig.no_action')),
    el('td', { class: 'num' }, position(r.now)),
    el('td', { class: 'num' }, el('b', {}, targetText(r))),
    el('td', { class: 'u1-reason-cell' }, el('details', {}, el('summary', {}, reason.length > 28 ? `${reason.slice(0, 28)}…` : reason || t('u1.na')), el('p', {}, reason))),
    el('td', { class: 'num' }, numNA(p.total, '%')),
    el('td', { class: 'num' }, numNA(p.b_total, '%')),
    el('td', {}, beatText(p)),
    el('td', { class: 'num' }, numNA(p.maxdd, '%')),
    el('td', { class: 'num' }, numNA(p.trades, '', 0), p.small_sample ? el('span', { class: 'u1-sub is-warn' }, t('u1.sig.small_sample')) : null),
    el('td', { class: 'num' }, numNA(p.sharpe)),
    el('td', { class: exception ? 'is-warn' : '' }, flagsText(r)));
}

function headCell(col, view, onSort) {
  const [key, label, numeric] = col;
  if (UNSORTABLE.includes(key)) return el('th', { scope: 'col', class: numeric ? 'num' : null }, t(label));
  const active = view.sort === key;
  const btn = el('button', { class: 'table__sort', type: 'button', 'aria-sort': active ? 'true' : null }, t(label), el('span', { 'aria-hidden': 'true' }, active ? (view.dir === 1 ? ' ↑' : ' ↓') : ''));
  btn.addEventListener('click', () => onSort(key));
  return el('th', { scope: 'col', class: numeric ? 'num' : null, 'aria-sort': active ? (view.dir === 1 ? 'ascending' : 'descending') : null }, btn);
}

function card(r, exception, note) {
  const p = r.performance ?? {};
  const dt = (k, v) => el('div', {}, el('dt', {}, t(k)), el('dd', {}, v));
  return el('article', { class: `card u1-card ${exception ? 'is-exception' : ''} ${note?.cls ?? ''}` },
    el('div', { class: 'u1-card__head' },
      el('a', { href: stockHref(r.code) }, el('strong', {}, r.name), el('span', { class: 'u1-sub' }, `${r.code} · ${t('u1.sig.data_date', { d: r.last_date })}`)),
      r.action ? actBadge(r.action) : t('u1.sig.no_action')),
    exception ? el('p', { class: 'is-warn' }, flagsText(r)) : null, note?.node ?? null,
    el('dl', { class: 'u1-card__grid' },
      dt('u1.sig.card.move', `${position(r.now)} → ${targetText(r)}`),
      dt('u1.sig.col.d1', exception ? '—' : (isNum(r.d1) ? fmt(r.d1, 2, '%', true) : t('u1.na'))),
      dt('u1.sig.col.liquidity', tr('u1.liq', r.liquidity) || t('u1.na')),
      dt('u1.sig.col.lot', numNA(r.lot_cost))),
    el('details', {}, el('summary', {}, t('u1.sig.card.more')),
      el('p', { class: 'u1-reason' }, r.reason),
      el('dl', { class: 'u1-card__grid' },
        dt('u1.sig.col.close', `${numNA(r.close)} ${r.price_note || ''}`),
        dt('u1.sig.col.turnover', isNum(r.avg_turnover_20d) ? fmt(r.avg_turnover_20d, 0) : t('u1.na')),
        dt('u1.sig.col.total', numNA(p.total, '%')), dt('u1.sig.col.b_total', numNA(p.b_total, '%')),
        dt('u1.sig.col.beat', beatText(p)), dt('u1.sig.col.maxdd', numNA(p.maxdd, '%')),
        dt('u1.sig.col.trades', numNA(p.trades, '', 0)), dt('u1.sig.col.sharpe', numNA(p.sharpe)),
        dt('u1.sig.col.flags', flagsText(r)))));
}

/** 生成列表：桌面表格 / 手机卡片。opts: { exception, mobile, noteFor, onSort } */
export function buildList(rows, view, opts) {
  const { exception = false, mobile = false, noteFor = () => null, onSort = () => {} } = opts;
  if (mobile) return el('div', { class: 'u1-cards' }, rows.length ? rows.map((r) => card(r, exception, noteFor(r))) : el('p', { class: 'muted' }, t('u1.sig.no_match')));
  return el('div', { class: 'table-wrap' }, el('table', { class: 'table u1-sig-table' },
    el('thead', {}, el('tr', {}, ...COLUMNS.map((c) => headCell(c, view, onSort)))),
    el('tbody', {}, ...(rows.length ? rows.map((r) => tableRow(r, exception, noteFor(r))) : [el('tr', {}, el('td', { colspan: COLUMNS.length, class: 'muted u1-empty' }, t('u1.sig.no_match')))]))));
}

/** CSV 列：[取值, 词条]；表头跟当前语言走 */
export const CSV_COLUMNS = [[(r) => r.code, 'u1.sig.col.code'], [(r) => r.name, 'u1.col.name'], [(r) => r.last_date, 'u1.sig.data_date_h'], [(r) => r.action, 'u1.sig.col.action'],
  [(r) => r.now, 'u1.sig.col.now'], [(r) => r.next, 'u1.sig.col.next'], [(r) => r.close, 'u1.sig.col.close'], [(r) => r.d1, 'u1.sig.col.d1'],
  [(r) => r.avg_turnover_20d, 'u1.sig.col.turnover'], [(r) => r.liquidity, 'u1.sig.col.liquidity'], [(r) => r.lot_cost, 'u1.sig.col.lot'], [(r) => r.reason, 'u1.sig.col.reason'],
  [(r) => r.performance?.total, 'u1.sig.col.total'], [(r) => r.performance?.b_total, 'u1.sig.col.b_total'], [(r) => r.performance?.maxdd, 'u1.sig.col.maxdd'],
  [(r) => r.performance?.trades, 'u1.sig.col.trades'], [(r) => r.performance?.sharpe, 'u1.sig.col.sharpe'], [(r) => (r.flags ?? []).join(tDefault('a02.sep.semi')), 'u1.sig.col.flags']];

/** 下载 CSV（带 BOM 让 Excel 认中文）。导出范围 = 当前标签筛选后的行，不含持仓联动信息 */
export function downloadCSV(name, rows) {
  const q = (v) => { const s = v === null || v === undefined ? '' : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const text = `﻿${[CSV_COLUMNS.map(([, k]) => q(t(k))).join(',')].concat(rows.map((r) => CSV_COLUMNS.map(([get]) => q(get(r) ?? t('u1.na'))).join(','))).join('\n')}`;
  const a = el('a', { href: URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' })), download: name });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
