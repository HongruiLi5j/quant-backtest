/* ==========================================================
 * 文件: shared/url-state.js（A08）
 * 用途: 把表格的排序 / 筛选状态同步到网址 location.search，刷新或把链接发给别人时能还原。
 *   - 参数名 = 页面前缀 + "_" + 短键，例如全市场页 mk_s=sharpe&mk_o=a（mk=market；s=排序列；o=方向）。
 *     前缀把各页的参数隔开，也不会碰 ?mock=1 / ?state= / ?debug= / ?market= 这些已有参数。
 *   - 用 history.replaceState 改地址：只替换当前这条历史，不新增，所以「后退」按钮不会被每次排序塞满。
 *   - 只读写本页前缀的参数，其他参数和 #hash 原样保留；值为空字符串 / null / undefined 的键不写进网址。
 *   - 网址里的值都是不可信输入：这里只负责读出字符串，每个页面必须自己对照白名单校验后才能用。
 * ========================================================== */

const SEP = '_';

/** 读出某前缀下的所有参数，返回 { 短键: 值 }；没有就是空对象 */
export function pickState(prefix, search = window.location.search) {
  const out = {};
  for (const [k, v] of new URLSearchParams(search)) {
    if (k.startsWith(prefix + SEP)) out[k.slice(prefix.length + 1)] = v;
  }
  return out;
}

/** 纯函数：在 search 字符串里把某前缀的参数整体换成 values，返回新的 search（含 ?，没有参数时是空串） */
export function mergeSearch(search, prefix, values) {
  const q = new URLSearchParams(search);
  for (const k of [...q.keys()]) if (k.startsWith(prefix + SEP)) q.delete(k);
  for (const [k, v] of Object.entries(values)) {
    if (v === null || v === undefined || v === '' || v === false) continue;
    q.set(prefix + SEP + k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : '';
}

/** 网址里现在有没有这个前缀的参数 */
export function hasState(prefix, search = window.location.search) {
  return Object.keys(pickState(prefix, search)).length > 0;
}

/** 把状态写进网址（replaceState）；失败（沙箱 / Safari 的调用频率限制）就静默放弃，不影响页面功能 */
export function writeState(prefix, values) {
  try {
    const { pathname, search, hash } = window.location;
    const next = mergeSearch(search, prefix, values);
    if (next === search) return;
    window.history.replaceState(window.history.state, '', `${pathname}${next}${hash}`);
  } catch { /* 地址栏同步只是便利功能 */ }
}

/** 等于默认值就返回空串（不写进网址），让网址只记「和默认不一样的地方」 */
export const nonDefault = (value, dflt) => (value === dflt ? '' : value);

/** 排序方向：asc → "a"，desc → "d"（网址里短一点）；反向解析见 dirFromUrl */
export const dirToUrl = (asc) => (asc ? 'a' : 'd');
export const dirFromUrl = (v) => (v === 'a' ? 'asc' : v === 'd' ? 'desc' : null);

/** 日期串 YYYY-MM-DD 且是真实存在的日期（2026-02-31 会被 Date 滚动成 3 月，所以要回算对比） */
export function validDate(s) {
  if (typeof s !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const d = new Date(`${s}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;
}
