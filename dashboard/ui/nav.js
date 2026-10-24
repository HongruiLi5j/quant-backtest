/* ==========================================================
 * 文件: ui/nav.js（基于 mockups/shared/topnav.js，只保留 Workspace 一种顶栏）
 * 用途: 新界面顶栏。真实站只有站主一个人，所以没有 Workspace / Public 切换、没有身份。
 *   - 每项两个地址：href = 新页面（相对 w/ 目录），classic = 旧界面里的同一页
 *   - 新页面还没做好的项，点了跳旧页面：靠下面 BUILT 这份静态清单判断
 *     新页面做好后，把它的 id 加进 BUILT（共用文件，各终端把需求写进 AQ-ui-status/requests.md，由 U5 统一改）
 *   - 开发预览（地址栏带 ?mock=1）时所有项都当作已做好，并把 ?mock=1 带到下一页，方便点着看
 *   - 右上角「旧版界面」链到 /；「后台」直接链到 /admin（另一套页面，管理令牌登录）
 *   - 所有文字 textContent，走 i18n（词条在 i18n/parts/U0.*.json）
 * ========================================================== */

import { el, svg } from './shared/dom.js';
import { t } from './shared/i18n.js';

/** 新页面已经做好的页面 id；不在这里的项点了会去旧页面 */
export const BUILT = new Set([]);

/** 导航顺序即快捷键：前 9 项对应 1–9 */
export const NAV = [
  { id: 'today', key: 'ui.nav.today', href: 'today.html', classic: '/#/today' },
  { id: 'signals', key: 'ui.nav.signals', href: 'signals.html', classic: '/#/trade' },
  { id: 'market', key: 'ui.nav.market', href: 'market.html', classic: '/#/market' },
  { id: 'watch', key: 'ui.nav.watch', href: 'watch.html', classic: '/#/watch' },
  { id: 'backtest', key: 'ui.nav.backtest', href: 'backtest.html', classic: '/#/lab' },
  { id: 'holdings', key: 'ui.nav.holdings', href: 'holdings.html', classic: '/#/holdings' },
  { id: 'monitor', key: 'ui.nav.monitor', href: 'monitor.html', classic: '/#/monitor' },
  { id: 'usmonitor', key: 'ui.nav.usmonitor', href: 'monitor.html?market=us', classic: '/#/usmonitor' },
  { id: 'research', key: 'ui.nav.research', href: 'research.html', classic: '/#/cmp' },
  { id: 'reports', key: 'ui.nav.reports', href: 'reports.html', classic: '/#/today' },
  { id: 'system', key: 'ui.nav.system', href: 'system.html', classic: '/#/today' },
  { id: 'settings', key: 'ui.nav.settings', href: 'settings.html', classic: '/#/today' },
  { id: 'admin', key: 'ui.nav.admin', href: '/admin', classic: '/admin', external: true },
];

const mockOn = () => new URLSearchParams(window.location.search).get('mock') === '1';

/** 这一项实际要去的地址：已做好 → 新页面；没做好 → 旧页面；后台永远 /admin */
export function navTarget(item) {
  if (item.external) return item.href;
  if (mockOn()) return item.href.includes('?') ? `${item.href}&mock=1` : `${item.href}?mock=1`;
  const page = item.id === 'usmonitor' ? 'monitor' : item.id;
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
    el('a', { class: 'btn btn--sm btn--ghost topnav__classic', href: '/', title: t('ui.nav.classic_tip') }, t('ui.nav.classic')));

  host.replaceChildren(el('div', { class: 'topnav__inner' }, brand, nav, tools));
  // 注意：不要设 host.dataset.mode = 'workspace'——那套规则是给 10 项居中导航写的，13 项会压到搜索框
}
