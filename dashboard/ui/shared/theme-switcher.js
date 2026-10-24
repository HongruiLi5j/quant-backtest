/* ==========================================================
 * 文件: shared/theme-switcher.js
 * 版本: 0.1.0 · 更新: 2026-10-23 · 作者: Claude（为李泓锐）
 * 用途: 深浅色切换 + 涨跌色（A 股红涨 / 美股绿涨）切换。
 *   - 主题写在 <html data-theme>，未手动选择时跟随系统 prefers-color-scheme
 *   - 涨跌色写在 <html data-market>，页面也可以用 data-market 默认值指定
 *   - 切换时派发 'themechange'，图表监听后重读 CSS 变量重绘
 * ========================================================== */

import { store } from './dom.js';

const root = document.documentElement;

/** 当前实际生效的主题（包含跟随系统的情况） */
export function currentTheme() {
  if (root.dataset.theme) return root.dataset.theme;
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function notify() {
  window.dispatchEvent(new CustomEvent('themechange', { detail: { theme: currentTheme(), market: root.dataset.market } }));
  for (const btn of document.querySelectorAll('[data-action="toggle-theme"]')) {
    btn.setAttribute('aria-pressed', String(currentTheme() === 'light'));
    // 图标显示「点了会切到哪种」：深色时显示太阳，浅色时显示月亮
    const use = btn.querySelector('use');
    if (use) use.setAttribute('href', use.getAttribute('href').replace(/#i-(sun|moon)$/, currentTheme() === 'light' ? '#i-moon' : '#i-sun'));
  }
  for (const btn of document.querySelectorAll('[data-action="toggle-market"]')) {
    btn.setAttribute('aria-pressed', String(root.dataset.market === 'us'));
    const label = btn.querySelector('[data-market-label]');
    if (label) label.textContent = root.dataset.market === 'us' ? 'US' : 'A股';
  }
}

export function setTheme(theme) {
  root.dataset.theme = theme;
  store.set('qb-theme', theme);
  notify();
}

export function setMarket(market) {
  root.dataset.market = market;
  store.set('qb-market', market);
  notify();
}

/** 初始化：恢复上次选择；没有就按页面默认 / 系统 */
export function initTheme() {
  const theme = store.get('qb-theme');
  if (theme === 'light' || theme === 'dark') root.dataset.theme = theme;
  const market = store.get('qb-market');
  if (market === 'cn' || market === 'us') root.dataset.market = market;
  else if (!root.dataset.market) root.dataset.market = 'cn';
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
    if (!store.get('qb-theme')) notify();
  });
  notify();
}

document.addEventListener('click', (event) => {
  if (event.target.closest('[data-action="toggle-theme"]')) {
    setTheme(currentTheme() === 'light' ? 'dark' : 'light');
  } else if (event.target.closest('[data-action="toggle-market"]')) {
    setMarket(root.dataset.market === 'us' ? 'cn' : 'us');
  }
});
