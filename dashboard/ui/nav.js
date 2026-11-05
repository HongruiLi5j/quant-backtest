/* ==========================================================
 * 文件: ui/nav.js（基于 mockups/shared/topnav.js，只保留 Workspace 一种顶栏）
 * 用途: 新界面顶栏。真实站只有站主一个人，所以没有 Workspace / Public 切换、没有身份。
 *   - 每项两个地址：href = 新页面（相对 w/ 目录），classic = 旧界面里的同一页
 *   - 新页面还没做好的项，点了跳旧页面：靠下面 BUILT 这份静态清单判断
 *     新页面做好后，把它的 id 加进 BUILT（共用文件，各终端把需求写进 AQ-ui-status/requests.md，由 U5 统一改）
 *   - 开发预览（地址栏带 ?mock=1）时所有项都当作已做好，并把 ?mock=1 带到下一页，方便点着看
 *   - 右上角「旧版界面」链到 /；「后台」指向新页面 admin.html（w/admin.html，未登录时页面里给 /admin 的登录链接）；旧 /admin 保留作退路
 *   - 所有文字 textContent，走 i18n（词条在 i18n/parts/U0.*.json）
 * ========================================================== */

import { el, svg } from './shared/dom.js';
import { t } from './shared/i18n.js';

/** 新页面已经做好的页面 id；不在这里的项点了会去旧页面 */
export const BUILT = new Set(['today', 'signals', 'market', 'watch', 'backtest', 'holdings', 'monitor', 'research', 'reports', 'system', 'settings', 'admin']);

/** 导航顺序即快捷键：前 9 项对应 1–9 */
export const NAV = [
  { id: 'today', key: 'ui.nav.today', href: 'today.html', classic: '/#/today' },
  { id: 'signals', key: 'ui.nav.signals', href: 'signals.html', classic: '/#/trade' },
  { id: 'market', key: 'ui.nav.market', href: 'market.html', classic: '/#/market' },
  { id: 'watch', key: 'ui.nav.watch', href: 'watch.html', classic: '/#/watch' },
  { id: 'backtest', key: 'ui.nav.backtest', href: 'backtest.html', classic: '/#/lab' },
  { id: 'holdings', key: 'ui.nav.holdings', href: 'holdings.html', classic: '/#/holdings' },
  { id: 'monitor', key: 'ui.nav.monitor', href: 'monitor.html', classic: '/#/monitor' },
  // D45：「美股盯盘」并进「盯盘」——同一个页面，页内已有 A股/美股 切换；顶栏少一项（手机上更是如此）。旧链接 monitor.html?market=us 照常能用
  { id: 'research', key: 'ui.nav.research', href: 'research.html', classic: '/#/cmp' },
  { id: 'settings', key: 'ui.nav.settings', href: 'settings.html', classic: '/#/today' },
  { id: 'reports', key: 'ui.nav.reports', href: 'reports.html', classic: '/#/today' },   // N22 报告页（只读）；旧界面没有对应页面
  { id: 'system', key: 'ui.nav.system', href: 'system.html', classic: '/admin' },   // N21 系统页（只读）
  { id: 'admin', key: 'ui.nav.admin', href: 'admin.html', classic: '/admin' },   // 新后台页；/admin 旧页面保留作退路（设置页里有链接）
];

const mockOn = () => new URLSearchParams(window.location.search).get('mock') === '1';

/** 这一项实际要去的地址：已做好 → 新页面；没做好 → 旧页面；后台永远 /admin */
export function navTarget(item) {
  if (item.external) return item.href;
  if (mockOn()) return item.href.includes('?') ? `${item.href}&mock=1` : `${item.href}?mock=1`;
  if (item.id === 'admin') return '/admin?enter=1&to=%2Fui%2Fw%2Fadmin.html';   // 本机本人点击自动登录后台，再回到新后台页
  const page = item.id;
  return BUILT.has(page) ? item.href : item.classic;
}

function icon(name, iconsHref) {
  const s = svg('svg', { class: 'btn__icon', 'aria-hidden': 'true' });
  s.append(svg('use', { href: `${iconsHref}#i-${name}` }));
  return s;
}

function iconBtn(action, name, labelKey, iconsHref) {
  return el('button', { class: 'btn btn--ghost btn--icon topnav__icon', type: 'button', dataset: { action }, 'aria-label': t(labelKey) }, icon(name, iconsHref));
}

/**
 * 渲染顶栏
 * @param {HTMLElement} host  <header class="topnav">
 * @param {{ current?: string, iconsHref?: string, brandHref?: string }} opt
 */
export function renderTopnav(host, opt = {}) {
  if (!host) return;
  const iconsHref = opt.iconsHref ?? '../shared/icons.svg';

  const brand = el('a', { class: 'brand topnav__brand', href: opt.brandHref ?? 'today.html' },
    el('span', { class: 'brand__mark', 'aria-hidden': 'true' }), el('span', {}, 'Quant Backtest'));

  const nav = el('nav', { class: 'topnav__nav topnav__nav--flat', 'aria-label': t('common.nav_main') });
  NAV.forEach((item, i) => {
    const hotkey = i < 9 ? String(i + 1) : null;
    nav.append(el('a', {
      class: 'topnav__link',
      href: navTarget(item),
      'aria-current': item.id === opt.current ? 'page' : null,
      dataset: { hotkey, nav: item.id },
      title: hotkey ? `${t(item.key)} · ${hotkey}` : t(item.key),
    }, t(item.key)));
  });

  const tools = el('div', { class: 'topnav__tools' },
    el('button', { class: 'search-trigger topnav__search', type: 'button', dataset: { action: 'open-palette' }, 'aria-label': t('common.search_aria') },
      icon('search', iconsHref),
      el('span', { class: 'search-trigger__text' }, t('ws.search')),
      el('span', { class: 'kbd', 'aria-hidden': 'true' }, '⌘K')),
    iconBtn('toggle-lang', 'lang', 'common.toggle_lang', iconsHref),
    iconBtn('toggle-theme', 'moon', 'common.toggle_theme', iconsHref),
    el('a', { class: 'btn btn--sm btn--ghost topnav__classic', href: '/?classic=1', title: t('ui.nav.classic_tip') }, t('ui.nav.classic')));

  host.replaceChildren(el('div', { class: 'topnav__inner' }, brand, nav, tools));
  overflowCue(nav);
  // 注意：不要设 host.dataset.mode = 'workspace'——那套规则是给 10 项居中导航写的，13 项会压到搜索框
}

/** D35：窄屏（手机）上导航条放不下 13 项、要横向滑动，但滚动条是隐藏的，看不出后面还有。
 *  这里：当前页那一项滚到中间；哪一侧还有内容就在那一侧加渐隐（data-fade="l" / "r" / "l r"，样式在 topnav.css）。
 *  只改导航条自己的 scrollLeft，不调用 scrollIntoView，不会带着整页上下滚。 */
function overflowCue(nav) {
  const edge = () => {
    const more = nav.scrollWidth - nav.clientWidth;
    const fade = [];
    if (more > 1 && nav.scrollLeft > 1) fade.push('l');
    if (more > 1 && nav.scrollLeft < more - 1) fade.push('r');
    if (fade.length) nav.dataset.fade = fade.join(' '); else delete nav.dataset.fade;
  };
  requestAnimationFrame(() => {
    const cur = nav.querySelector('[aria-current="page"]');
    if (cur && nav.scrollWidth > nav.clientWidth) {
      const offset = cur.getBoundingClientRect().left - nav.getBoundingClientRect().left + nav.scrollLeft;
      nav.scrollLeft = offset - (nav.clientWidth - cur.offsetWidth) / 2;
    }
    edge();
  });
  nav.addEventListener('scroll', edge, { passive: true });
  // 切语言会重画顶栏：窗口 resize 只挂一次，永远对最新那条导航生效，不越挂越多
  onResize = edge;
  if (!resizeHooked) { window.addEventListener('resize', () => onResize(), { passive: true }); resizeHooked = true; }
}
let onResize = () => {};
let resizeHooked = false;
