/* 信号页·表格与手机卡片（U1）。对应旧界面 renderTrade / tradeRows / tradeCard。
 * 17 列全部保留；手机（≤600px）改成卡片。所有文本走 el()，不拼 HTML。 */
import { el, fmt, fmtCN, toneClass, safeFileName } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { info } from '../shared/glossary.js';   // D111：列名后面的 ⓘ
import { tDefault } from '../shared/t-fallback.js';
import { actBadge, numNA, isNum, tr, stockHref } from './U1-common.js';

/** [排序键, 词条, 是否数字列]；reason / flags 不能排序 */
export const COLUMNS = [['code', 'u1.sig.col.code'], ['close', 'u1.sig.col.close', 1], ['d1', 'u1.sig.col.d1', 1], ['avg_turnover_20d', 'u1.sig.col.turnover', 1],
  ['liquidity', 'u1.sig.col.liquidity'], ['lot_cost', 'u1.sig.col.lot', 1], ['action', 'u1.sig.col.action'], ['now', 'u1.sig.col.now', 1], ['next', 'u1.sig.col.next', 1],
  ['reason', 'u1.sig.col.reason'], ['total', 'u1.sig.col.total', 1], ['b_total', 'u1.sig.col.b_total', 1], ['beat_bh', 'u1.sig.col.beat'], ['maxdd', 'u1.sig.col.maxdd', 1],
  ['trades', 'u1.sig.col.trades', 1], ['sharpe', 'u1.sig.col.sharpe', 1], ['flags', 'u1.sig.col.flags']];
/** D111：列 / 卡片字段 → 术语（词条在 i18n 的 gl.*）。2020 年起的数字都是同一段回测，所以挂 y2020 或对应指标 */
const COL_TERMS = { liquidity: 'liquidity', lot_cost: 'lot', total: 'y2020', b_total: 'bench', beat_bh: 'beat_hold', maxdd: 'maxdd', trades: 'trades', sharpe: 'sharpe_cash' };
const LABEL_TERMS = { 'u1.sig.col.liquidity': 'liquidity', 'u1.sig.col.lot': 'lot', 'u1.sig.col.total': 'y2020', 'u1.sig.col.b_total': 'bench', 'u1.sig.col.beat': 'beat_hold',
  'u1.sig.col.maxdd': 'maxdd', 'u1.sig.col.trades': 'trades', 'u1.sig.col.sharpe': 'sharpe_cash' };
export const UNSORTABLE = ['reason', 'flags'];
/** D102：R1 检验里的"卖出类"（和 overnight/research/quant_s1_replay.py 的 SELLS 一致） */
export const SELL_ACTIONS = ['卖出', '减仓'];
/** D84：默认隐藏的列（表格原来 17 列、2357px 宽）。「列」菜单可以打开，选择记在本机；导出 CSV 始终是全部列 */
export const DEFAULT_HIDDEN = ['liquidity', 'lot_cost', 'b_total', 'beat_bh', 'trades', 'sharpe'];

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

// 仓位：0 显示 0%，不写 0.0%（D02）
const position = (v) => (isNum(v) ? (v === 0 ? '0%' : `${(v * 100).toFixed(1)}%`) : t('u1.na'));
/** 没有信号的相关股票显示“无信号”；有异常标记但仓位缺失仍显示“数据不足” */
export const targetText = (r) => ((!r.action && !isNum(r.now) && !isNum(r.next) && !(r.flags?.length)) ? t('u1.sig.no_signal') : position(r.next));
/** R8：后端提示整句的英文（精确句走字典 u1.flag.<原句>，带数字的几句按格式拆开；认不出的原样显示） */
function flagText(f) {
  const hit = tr('u1.flag', f);
  if (hit !== f) return hit;
  let m = /^数据日期异常：(.+)，应为 (.+)$/.exec(f);
  if (m) return t('u1.flag.date', { a: m[1], b: m[2] });
  m = /^今日收盘涨停：历史上约 (\d+%) 次日开盘仍涨停买不进（账本 (\d+) 条）$/.exec(f);
  if (m) return t('u1.flag.close_limit', { p: m[1], n: m[2] });
  m = /^数据异常：(.+)$/s.exec(f);
  return m ? t('u1.flag.err', { m: m[1] }) : f;
}
const flagsText = (r) => (r.flags?.length ? r.flags.map(flagText).join(tDefault('a02.sep.semi')) : t('u1.sig.no_flags'));
const beatText = (p) => (typeof p.beat_bh === 'boolean' ? t(p.beat_bh ? 'u1.yes' : 'u1.no') : t('u1.na'));

/** D02：整张表大多数行相同的值（数据日期、价格口径）只在表头说明里写一次，行里只标出不一样的 */
function commonOf(rows, key) {
  const n = new Map();
  for (const r of rows) n.set(r[key], (n.get(r[key]) ?? 0) + 1);
  return [...n.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
}

function nameCell(r, note, common) {
  return el('td', {},
    el('a', { href: stockHref(r.code), class: 'u1-name' }, r.name), ' ', el('span', { class: 'mono muted' }, r.code),
    r.last_date !== common.date ? el('span', { class: 'u1-sub is-warn' }, t('u1.sig.data_date', { d: r.last_date })) : null,
    note?.node ?? null);
}

// D84：keep(列号) 决定这一格要不要；隐藏的列在生成时就不放进行里（不用事后删格子，也不依赖 table.rows 这类浏览器专有接口）
function tableRow(r, exception, note, common, keep = () => true) {
  const p = r.performance ?? {};
  const reason = r.reason ?? '';
  return el('tr', { class: note?.cls ?? null }, ...[
    nameCell(r, note, common),
    el('td', { class: 'num' }, numNA(r.close), (r.price_note || '') !== (common.priceNote || '') ? el('span', { class: 'u1-sub' }, tr('u1.pn', r.price_note) || t('u1.na')) : null),
    el('td', { class: `num ${exception ? '' : toneClass(r.d1)}` }, exception ? '—' : (isNum(r.d1) ? fmt(r.d1, 2, '%', true) : t('u1.na'))),
    el('td', { class: 'num', title: isNum(r.avg_turnover_20d) ? fmt(r.avg_turnover_20d, 0) : null }, isNum(r.avg_turnover_20d) ? fmtCN(r.avg_turnover_20d) : t('u1.na')),   // D02：16.1亿，完整数字在 title
    el('td', {}, tr('u1.liq', r.liquidity) || t('u1.na')),
    el('td', { class: 'num', title: t('u1.sig.lot_tip') }, isNum(r.lot_cost) ? fmt(r.lot_cost, 0) : t('u1.na')),   // 元，不要 .00
    el('td', {}, r.action ? actBadge(r.action) : t('u1.sig.no_action')),
    el('td', { class: 'num' }, position(r.now)),
    el('td', { class: 'num' }, el('b', {}, targetText(r))),
    // D12（C14）：第一行是后端给的一句话触发原因（r.trigger，只写今天真正变化的子策略和条件），展开看完整推导；
    // 旧清单没有 trigger 字段时照旧截取 reason 前 28 字
    el('td', { class: 'u1-reason-cell' }, el('details', {}, el('summary', {}, r.trigger || (reason.length > 28 ? `${reason.slice(0, 28)}…` : reason || t('u1.na'))), el('p', {}, reason))),
    el('td', { class: 'num' }, numNA(p.total, '%')),
    el('td', { class: 'num' }, numNA(p.b_total, '%')),
    el('td', {}, beatText(p)),
    el('td', { class: 'num' }, numNA(p.maxdd, '%')),
    el('td', { class: 'num' }, numNA(p.trades, '', 0), p.small_sample ? el('span', { class: 'u1-sub is-warn' }, t('u1.sig.small_sample')) : null),
    el('td', { class: 'num' }, numNA(p.sharpe)),
    // D90-3：限宽 + 省略号，完整文字在 title
    el('td', { class: `u1-flags-cell ${exception ? 'is-warn' : ''}`.trim(), title: flagsText(r) }, flagsText(r))].filter((_, i) => keep(i)));
}

function headCell(col, view, onSort) {
  const [key, label, numeric] = col;
  const ic = COL_TERMS[key] ? info(COL_TERMS[key]) : null;   // ⓘ 放在排序按钮外面，点它不触发排序
  if (UNSORTABLE.includes(key)) return el('th', { scope: 'col', class: numeric ? 'num' : null }, t(label), ic);
  const active = view.sort === key;
  // axe：aria-sort 只能放在列头 th 上（下面的 th 已经有），按钮上不再写
  const btn = el('button', { class: 'table__sort', type: 'button' }, t(label), el('span', { 'aria-hidden': 'true' }, active ? (view.dir === 1 ? ' ↑' : ' ↓') : ''));
  btn.addEventListener('click', () => onSort(key));
  return el('th', { scope: 'col', class: numeric ? 'num' : null, 'aria-sort': active ? (view.dir === 1 ? 'ascending' : 'descending') : null }, btn, ic);
}

function card(r, exception, note) {
  const p = r.performance ?? {};
  const dt = (k, v) => el('div', {}, el('dt', {}, t(k), LABEL_TERMS[k] ? info(LABEL_TERMS[k]) : null), el('dd', {}, v));
  return el('article', { class: `card u1-card ${exception ? 'is-exception' : ''} ${note?.cls ?? ''}` },
    el('div', { class: 'u1-card__head' },
      el('a', { href: stockHref(r.code) }, el('strong', {}, r.name), el('span', { class: 'u1-sub' }, `${r.code} · ${t('u1.sig.data_date', { d: r.last_date })}`)),
      r.action ? actBadge(r.action) : t('u1.sig.no_action')),
    exception ? el('p', { class: 'is-warn' }, flagsText(r)) : null, note?.node ?? null,
    el('dl', { class: 'u1-card__grid' },
      dt('u1.sig.card.move', `${position(r.now)} → ${targetText(r)}`),
      dt('u1.sig.col.d1', exception ? '—' : (isNum(r.d1) ? fmt(r.d1, 2, '%', true) : t('u1.na'))),
      dt('u1.sig.col.liquidity', tr('u1.liq', r.liquidity) || t('u1.na')),
      dt('u1.sig.col.lot', isNum(r.lot_cost) ? fmt(r.lot_cost, 0) : t('u1.na'))),
    r.trigger ? el('p', { class: 'u1-reason' }, r.trigger) : null,   // D12：手机卡片也先显示触发原因
    el('details', {}, el('summary', {}, t('u1.sig.card.more')),
      el('p', { class: 'u1-reason' }, r.reason),
      el('dl', { class: 'u1-card__grid' },
        dt('u1.sig.col.close', `${numNA(r.close)} ${tr('u1.pn', r.price_note)}`),
        dt('u1.sig.col.turnover', isNum(r.avg_turnover_20d) ? fmtCN(r.avg_turnover_20d) : t('u1.na')),
        dt('u1.sig.col.total', numNA(p.total, '%')), dt('u1.sig.col.b_total', numNA(p.b_total, '%')),
        dt('u1.sig.col.beat', beatText(p)), dt('u1.sig.col.maxdd', numNA(p.maxdd, '%')),
        dt('u1.sig.col.trades', numNA(p.trades, '', 0)), dt('u1.sig.col.sharpe', numNA(p.sharpe)),
        dt('u1.sig.col.flags', flagsText(r)))));
}

/** 生成列表：桌面表格 / 手机卡片。opts: { exception, mobile, noteFor, onSort, hidden（隐藏的列 key 集合，D84） } */
export function buildList(rows, view, opts) {
  const { exception = false, mobile = false, noteFor = () => null, onSort = () => {}, hidden = new Set() } = opts;
  // D102（R1）：有卖出 / 减仓行时，表头说明里写一次"可在次日收盘前卖出"，必须带"近年在变小、不是策略优势"（不逐行重复）
  const sellNote = rows.some((r) => SELL_ACTIONS.includes(r.action)) ? t('c102.sell_timing') : null;
  if (mobile) {
    // R7-ux-2：手机上先只画前 8 张卡片，其余点"显示更多"每次再加 10 张，页面长度从 ~7,300px 降到约 1/3；排序/筛选后重绘会回到前 8 张
    const FIRST = 8, STEP = 10;
    const box = el('div', { class: 'u1-cards' }, sellNote ? el('p', { class: 'muted u1-caption' }, sellNote) : null);
    if (!rows.length) { box.append(el('p', { class: 'muted' }, t('u1.sig.no_match'))); return box; }
    let shownN = 0;
    const more = el('button', { class: 'btn btn--sm u1-more', type: 'button' }, t('u1.sig.more_cards', { n: rows.length }));   // 创建时就带文字（a11y 静态检查要求按钮有子节点），draw() 里再更新剩余数
    const draw = (n) => {
      const next = Math.min(rows.length, shownN + n);
      more.before(...rows.slice(shownN, next).map((r) => card(r, exception, noteFor(r))));
      shownN = next;
      more.hidden = shownN >= rows.length;
      more.textContent = t('u1.sig.more_cards', { n: rows.length - shownN });
    };
    more.addEventListener('click', () => draw(STEP));
    box.append(more);
    draw(FIRST);
    return box;
  }
  const common = { date: commonOf(rows, 'last_date'), priceNote: commonOf(rows, 'price_note') };
  const caption = rows.length && common.date ? el('caption', { class: 'u1-caption' }, [t('u1.sig.data_date', { d: common.date }), tr('u1.pn', common.priceNote), sellNote].filter(Boolean).join(' · ')) : null;
  // D84：隐藏的列（股票列总显示）；表头和每一行按同一个 keep 过滤，列号对齐
  const keep = (i) => COLUMNS[i][0] === 'code' || !hidden.has(COLUMNS[i][0]);
  const shown = COLUMNS.filter((_, i) => keep(i)).length;
  // D01：限高滚动框，表头吸顶、首列吸左
  const table = el('table', { class: 'table table--pin-first u1-sig-table' },
    caption,
    el('thead', {}, el('tr', {}, ...COLUMNS.filter((_, i) => keep(i)).map((c) => headCell(c, view, onSort)))),
    el('tbody', {}, ...(rows.length ? rows.map((r) => tableRow(r, exception, noteFor(r), common, keep)) : [el('tr', {}, el('td', { colspan: shown, class: 'muted u1-empty' }, t('u1.sig.no_match')))])));
  return el('div', { class: 'table-wrap table-wrap--tall' }, table);
}

/** D84：「列」菜单。勾选 = 显示；改动立即回调 onChange(新的隐藏集合)。股票列总是显示 */
export function columnMenu(hidden, onChange) {
  const label = t('ux.sig.cols') === 'ux.sig.cols' ? 'Columns' : t('ux.sig.cols');
  const boxes = COLUMNS.filter(([k]) => k !== 'code').map(([k, key]) => {
    const cb = el('input', { type: 'checkbox', checked: !hidden.has(k), dataset: { col: k } });
    return el('label', { class: 'u1-colmenu__item' }, cb, ' ', t(key));
  });
  const menu = el('details', { class: 'u1-colmenu' }, el('summary', { class: 'btn btn--sm' }, label), el('div', { class: 'u1-colmenu__list' }, ...boxes));
  menu.addEventListener('change', () => {
    const next = new Set([...menu.querySelectorAll('input[data-col]')].filter((cb) => !cb.checked).map((cb) => cb.dataset.col));
    onChange(next);
  });
  return menu;
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
  const a = el('a', { href: URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' })), download: safeFileName(name) });   // D48
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
