/* ==========================================================
 * 文件: ui/shared/page-help.js
 * 用途: D67（R3-content-1）每页顶部一个默认折叠的「怎么读这一页」。
 *   回答四件事：这页回答什么问题、先看哪个数、哪个数别当真、下一步去哪。
 *   条目在字典 c67.<页面>.1…（中英文同一提交，顺序取到第一个缺的为止）；相关指标直接复用 D10 术语表的 ⓘ（gl.*），不另写定义。
 *   由 page-boot.js 在页头（.ws-head）下面插一次，不放进页面自己的渲染区，所以按 R 刷新不会把它冲掉；换语言时重建。
 *   展开 / 收起只记在本浏览器（偏好，不是数据）。
 * ========================================================== */
import { el, store } from './dom.js';
import { t } from './i18n.js';
import { withInfo } from './glossary.js';

/** 每页挂哪些术语（gl.* 里已有的） */
const TERMS = {
  today: ['since_launch', 'cagr', 'sharpe_cash', 'maxdd', 'ma300'],
  signals: ['y2020', 'sharpe_cash', 'win'],
  market: ['y2020', 'sharpe_cash'],
  backtest: ['cagr', 'sharpe_cash', 'sharpe_rf0', 'maxdd', 'win'],
  research: ['since_launch', 'y2020'],
  monitor: [],
};
const MAX_ITEMS = 8;

function items(page) {
  const out = [];
  for (let i = 1; i <= MAX_ITEMS; i++) {
    const k = `c67.${page}.${i}`;
    const v = t(k);
    if (v === k) break;
    out.push(v);
  }
  return out;
}

/** 在页头下面放（或换语言后重建）「怎么读这一页」；不在名单里的页面什么也不做 */
export function mountHelp(page) {
  if (!(page in TERMS)) return;
  const list = items(page);
  const head = document.querySelector('#main .ws-head');
  document.querySelector('[data-page-help]')?.remove();
  if (!head || !list.length) return;
  const key = `qb-help-${page}`;
  const terms = TERMS[page].filter((x) => t(`gl.${x}.name`) !== `gl.${x}.name`);
  const box = el('details', { class: 'card ws-help', 'data-page-help': '', style: { margin: '0 0 16px', padding: '12px 16px' } },
    el('summary', { style: { cursor: 'pointer', 'font-weight': '600' } }, t('c67.title')),
    el('ol', { style: { margin: '8px 0 0', 'padding-left': '20px', 'line-height': '1.7' } }, ...list.map((x) => el('li', {}, x))),
    terms.length ? el('p', { class: 'muted', style: { margin: '8px 0 0' } }, t('c67.terms'), ' ',
      ...terms.flatMap((x, i) => [i ? ' · ' : '', ...withInfo(t(`gl.${x}.name`), x)])) : null);
  if (store.get(key) === '1') box.open = true;
  box.addEventListener('toggle', () => store.set(key, box.open ? '1' : '0'));
  head.after(box);
}
