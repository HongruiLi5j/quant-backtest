/* ==========================================================
 * 文件: ui/pages/u4-util.js
 * 用途: U4 四个页面（market / watch / research / settings）共用的小工具。
 *   - 数据市场（A 股 / 美股）的读写：存在本机浏览器 qb-data-market，和「涨跌色」(qb-market) 互不影响
 *   - 信号徽章、市值格式、CSV 下载、错误提示、策略下拉
 *   所有文字都用 textContent / el()，不拼 HTML 字符串。
 * ========================================================== */

import { el, store, fmt, toneClass } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { tDefault } from '../shared/t-fallback.js';

export const MARKET_KEY = 'qb-data-market';
export { fmt, toneClass };

/** 当前数据市场：本机保存的值（必须在 meta.markets 里）→ 服务端默认 → cn */
export function dataMarket(meta) {
  const keys = Object.keys(meta?.markets ?? {});
  const saved = store.get(MARKET_KEY, '');
  if (keys.includes(saved)) return saved;
  return keys.includes(meta?.default_market) ? meta.default_market : (keys[0] ?? 'cn');
}

export function setDataMarket(m) { store.set(MARKET_KEY, m); }

/** 网址里带了市场（A08：mk_m / wl_m）且在 meta.markets 里就用网址的，否则退回本机保存的；不改写本机保存的值 */
export function pickMarket(meta, fromUrl) {
  return fromUrl && Object.hasOwn(meta?.markets ?? {}, fromUrl) ? fromUrl : dataMarket(meta);
}

/** A 股 / 美股切换按钮；点了先存再回调 */
export function marketSwitch(meta, current, onChange) {
  return el('div', { class: 'segmented', role: 'group', 'aria-label': t('u4.market.switch') },
    ...Object.entries(meta.markets).map(([k, v]) => {
      const b = el('button', { class: 'segmented__btn', type: 'button', 'aria-pressed': String(k === current) }, v.name);
      b.addEventListener('click', () => { setDataMarket(k); onChange(k); });
      return b;
    }));
}

/** 百分数（带正负号），空值显示 — */
export const pct = (v, d = 1) => fmt(v, d, '%', true);

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
export { isNum };

/** 市值：T / B / M */
export function capFmt(v) {
  if (!isNum(v)) return '—';
  if (v >= 1e12) return `${(v / 1e12).toFixed(2)}T`;
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  return `${(v / 1e6).toFixed(0)}M`;
}

const ACTIONS = { 买入: ['badge--rise', 'buy'], 加仓: ['badge--rise', 'add'], 换仓: ['badge--rise', 'swap'], 卖出: ['badge--fall', 'sell'], 减仓: ['badge--fall', 'trim'], 持有: ['', 'hold'], 空仓: ['', 'flat'] };

/** 信号徽章；服务端空字符串表示已退市 */
export function pill(action) {
  if (!action) return el('span', { class: 'badge' }, t('u4.act.delisted'));
  const [cls, key] = ACTIONS[action] ?? ['', null];
  return el('span', { class: `badge ${cls}`.trim() }, key ? t(`u4.act.${key}`) : action);
}

/** 策略下拉：meta.strategies 里 kind 为 single 的（不含 buy_hold） */
export function strategySelect(meta, value, { raw = false, onChange } = {}) {
  const sel = el('select', { class: 'ws-select', 'aria-label': t('u4.f.strategy') });
  for (const [k, v] of Object.entries(meta.strategies ?? {})) {
    if (v.kind === 'single' && k !== 'buy_hold') sel.append(el('option', { value: k }, v.label));
  }
  if (raw) sel.append(el('option', { value: 'raw' }, t('u4.f.raw')));
  sel.value = [...sel.options].some((o) => o.value === value) ? value : 'blend2';
  if (onChange) sel.addEventListener('change', () => onChange(sel.value));
  return sel;
}

/** 带标签的控件：<label>名称 + 控件</label> */
export function field(labelKey, control) {
  return el('label', { class: 'u4-field' }, el('span', { class: 'muted' }, t(labelKey)), control);
}

/** 排序表头按钮 */
export function sortTh(label, key, state, numeric = true) {
  const on = state.sort === key;
  return el('th', { scope: 'col', class: numeric ? 'num' : null, 'aria-sort': on ? (state.dir === 'asc' ? 'ascending' : 'descending') : null },
    el('button', { class: 'table__sort', type: 'button', dataset: { k: key } }, label, el('span', { 'aria-hidden': 'true' }, on ? (state.dir === 'asc' ? ' ↑' : ' ↓') : '')));
}

/** 把接口错误显示在一个容器里（登录过期给回首页链接），不白屏 */
export function showError(box, err) {
  box.hidden = false;
  box.replaceChildren(err?.auth
    ? el('span', {}, t('ui.auth.expired'), ' ', el('a', { href: '/' }, t('ui.auth.home')))
    : el('span', {}, `${t('ui.state.error')}${err?.message ? `${tDefault('a02.sep.colon')}${err.message}` : ''}`));
}

/** 下载 CSV（带 BOM，Excel 打开不乱码）。header: [[字段或函数, 列名], ...] */
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

/** 简单防抖 */
export function debounce(fn, ms) {
  let h = 0;
  return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}
