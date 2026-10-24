/* 今日、信号两页共用的小工具（U1 负责）：数字格式、动作徽章、状态卡、本机判断 */
import { el, fmt } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { isMock } from '../shared/api.js';

export const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** 数字格式化；缺值显示 —（和旧界面一致） */
export const num = (v, digits = 2, unit = '', signed = false) => (isNum(v) ? fmt(v, digits, unit, signed) : '—');

/** 小数（0.62）→ 百分比文字；缺值显示“数据不足” */
export const ratio = (v, digits = 0) => (isNum(v) ? `${(v * 100).toFixed(digits)}%` : t('u1.na'));

/** 带“数据不足”兜底的数字（旧交易页的 tradeNumber） */
export const numNA = (v, unit = '', digits = 2) => (isNum(v) ? `${v.toFixed(digits)}${unit}` : t('u1.na'));

/** 服务端给的中文枚举值（买入/卖出/正常…）→ 当前语言；没有词条就原样显示 */
export function tr(prefix, value) {
  if (value === null || value === undefined || value === '') return '';
  const key = `${prefix}.${value}`;
  const out = t(key);
  return out === key ? String(value) : out;
}

/** 动作徽章：买入/加仓红（涨色），卖出/减仓绿（跌色），其余中性 */
export function actBadge(action) {
  if (!action) return el('span', { class: 'badge' }, t('u1.act.none'));
  const tone = ['买入', '加仓', '换仓'].includes(action) ? 'badge--rise' : ['卖出', '减仓'].includes(action) ? 'badge--fall' : '';
  return el('span', { class: `badge ${tone}` }, tr('u1.act', action));
}

/** 同目录下另一个页面的地址；预览模式把 ?mock=1 带过去 */
export const pageHref = (name) => (isMock() ? `${name}?mock=1` : name);

/** 个股详情新界面没有，先去旧界面 */
export const stockHref = (code) => `/#/stock/${encodeURIComponent(code)}`;

/** 只有本机访问才读的接口（我的相关、模拟账户）用这个判断 */
export const isLocalHost = () => ['127.0.0.1', 'localhost', '[::1]', '::1'].includes(window.location.hostname);

/** 一张提示卡（没数据 / 区块暂不可用） */
export function noteCard(text, tone = '') {
  return el('div', { class: `card u1-note ${tone}`, role: tone === 'is-warn' ? 'alert' : null }, text);
}

/** 非致命错误：401/403 提示重新登录，其余给通用提示 */
export function softError(err) {
  if (err?.auth) {
    return el('div', { class: 'card u1-note is-warn', role: 'alert' }, t('ui.auth.expired'), ' ', el('a', { href: '/' }, t('ui.auth.home')));
  }
  return noteCard(`${t('u1.section_unavailable')}${err?.message ? `：${err.message}` : ''}`, 'is-warn');
}

/** 卡片标题行 */
export const cardHead = (title, sub = null) => el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, title), sub ? el('span', { class: 'card__sub' }, sub) : null);
