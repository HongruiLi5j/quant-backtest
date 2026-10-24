/* ==========================================================
 * 文件: ui/pages/u3-common.js
 * 用途: U3 三个页面（持仓 / 盯盘 / 美股盯盘）共用的小工具：数字格式、表格、消息条、错误码 → 文案、POST 包装
 * 规则: 文本一律 textContent（el() 内部就是 createTextNode），不拼 HTML
 * ========================================================== */

import { el, fmt, toneClass } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { postJSON } from '../shared/api.js';

/** 字典里没有这个 key 时 t() 会原样返回 key */
export const hasKey = (key) => t(key) !== key;

const isNum = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
export { isNum };

/** 千分位数字；后端的金额是字符串，这里转 Number 只用于显示 */
export const num = (v, digits = 2, signed = false) => (isNum(v) ? fmt(Number(v), digits, '', signed) : '—');
/** 小数比例 → 百分数：0.1785 → 17.85% */
export const ratio = (v, digits = 2, signed = false) => (isNum(v) ? fmt(Number(v) * 100, digits, '%', signed) : '—');
/** 已经是百分数的值（盯盘接口的 pct）：1.23 → 1.23% */
export const pctText = (v, digits = 2) => (isNum(v) ? fmt(Number(v), digits, '%') : '—');
/** 涨跌 class；空值不上色 */
export const tone = (v) => (isNum(v) ? toneClass(Number(v)) : '');

/** 带 class 的纯文本 span */
export const span = (text, cls = null) => el('span', { class: cls }, text ?? '—');

/** 表格：headKeys 是表头词条，rows 是 <tr> 数组；numCols 里的列号右对齐；没有行时显示空态文字 */
export function table(headKeys, rows, emptyKey, { numCols = [], id = null } = {}) {
  if (!rows.length) return el('p', { class: 'ws-note u3-empty', role: 'status' }, t(emptyKey));
  const right = new Set(numCols);
  return el('div', { class: 'table-wrap', tabindex: 0 },
    el('table', { class: 'table', id },
      el('thead', {}, el('tr', {}, ...headKeys.map((k, i) => el('th', { scope: 'col', class: right.has(i) ? 'num' : null }, t(k))))),
      el('tbody', {}, ...rows)));
}

/** 单元格；numeric=true 右对齐 */
export const td = (child, numeric = false, cls = null) =>
  el('td', { class: [numeric ? 'num' : '', cls || ''].join(' ').trim() || null }, child);

/** 页面顶部的消息条：show(key, 'error'|'ok') / clear() */
export function messageBox() {
  const box = el('div', { class: 'u3-msg-box', 'aria-live': 'polite' });
  return {
    node: box,
    show(key, kind = 'error', vars = {}) {
      box.replaceChildren(el('p', { class: `u3-msg u3-msg--${kind}`, role: kind === 'error' ? 'alert' : 'status' }, t(key, vars)));
    },
    clear() { box.replaceChildren(); },
  };
}

/** ApiError → 词条 key。登录过期给统一提示，其余按 <前缀>.err.<错误码> 找，找不到用 <前缀>.err.generic */
export function errKey(err, prefixes) {
  if (err?.auth) return 'ui.auth.expired';
  const code = err?.body?.error;
  const net = err?.status === 0 ? 'NETWORK' : err?.status === 404 && !code ? 'UNAVAILABLE' : null;
  for (const p of prefixes) {
    for (const c of [code, net]) {
      if (c && hasKey(`${p}.err.${c}`)) return `${p}.err.${c}`;
    }
  }
  return `${prefixes[prefixes.length - 1]}.err.generic`;
}

/** POST 包装：成功返回 { ok: true, data }，失败返回 { ok: false, err }；业务上 ok !== true 也算失败 */
export async function post(path, body) {
  try {
    const data = await postJSON(path, body);
    if (data && data.ok === false) return { ok: false, err: { body: data } };
    return { ok: true, data };
  } catch (err) {
    return { ok: false, err };
  }
}
