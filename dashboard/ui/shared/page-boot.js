/* ==========================================================
 * 文件: ui/shared/page-boot.js（来自 mockups/quant-ai-dark/w/workspace.js）
 * 用途: 每个新页面的统一入口。页面 html 只写骨架（见 w/_template.html）：
 *   <body class="ws" data-page="<id>"> + 加载本文件；页面自己的逻辑写在 ui/pages/<id>.js：
 *     export async function render(root) { ... }
 *   本文件负责：主题、语言、顶栏、命令面板、快捷键，然后调用 render(root)，并管理四种状态：
 *     loading（骨架屏）→ ready（内容）/ error（出错）/ empty（render 返回 'empty'）
 *   - root 是 main 里的 [data-ws-body]，render 往里放内容，一律 textContent / el()，不拼 HTML 字符串
 *   - render 里 getJSON 抛的 ApiError 如果 err.auth 为真，错误卡片显示「登录已过期，请从首页重新进入」
 *   - 语言切换后重新 render；R 键刷新当前页；地址栏 ?state=loading|error|empty 可强制看某种状态
 *   - Workspace 规则：没有星云、磁吸、倾斜、光束；快捷键 1–9 / R / /（搜索）/ ?
 * ========================================================== */

import { el } from './dom.js';
import { initI18n, t } from './i18n.js';
import { initTheme } from './theme-switcher.js';
import { initCursor } from './custom-cursor.js';
import { initTickers } from './number-ticker.js';
import { initPalette, openPalette } from './command-palette.js';
import { toast } from './toast.js';
import { renderTopnav, NAV, navTarget } from '../nav.js';

const page = document.body.dataset.page || '';
const params = new URLSearchParams(window.location.search);
let mod = null;
let renderSeq = 0;

/** 顶栏高亮：monitor 页带 ?market=us 时算「美股盯盘」 */
const currentNav = () => (page === 'monitor' && params.get('market') === 'us' ? 'usmonitor' : page);

function drawNav() {
  renderTopnav(document.querySelector('[data-topnav]'), {
    current: currentNav(),
    iconsHref: '../shared/icons.svg',
    brandHref: navTarget(NAV[0]),
  });
}

const main = () => document.querySelector('main[data-state]');

function setState(state) {
  main()?.setAttribute('data-state', state);
}

/** 错误卡片：登录过期给回首页的链接，其他错误显示统一提示 */
function showError(err) {
  const auth = err?.auth === true;
  const box = main();
  const title = box?.querySelector('[data-bind="error-title"]');
  const link = box?.querySelector('[data-bind="login-link"]');
  if (title) title.textContent = auth ? t('ui.auth.expired') : t('ui.state.error');
  if (link) link.hidden = !auth;
  const retry = box?.querySelector('[data-action="retry"]');
  if (retry) retry.hidden = auth;
  setState('error');
}

async function renderPage() {
  const seq = ++renderSeq;
  const forced = params.get('state');
  setState('loading');
  if (forced === 'loading') return;
  if (forced === 'error') { showError(null); return; }
  if (forced === 'empty') { setState('empty'); return; }
  const body = document.querySelector('[data-ws-body]') ?? main();
  try {
    if (!mod) mod = await import(`../pages/${page}.js`);
    const result = await mod.render(body);
    if (seq !== renderSeq) return;   // 期间又触发了一次渲染，丢掉这次
    if (result === 'empty') { setState('empty'); return; }
    initTickers();
    setState('ready');
  } catch (err) {
    if (seq !== renderSeq) return;
    console.warn('页面渲染失败', page, err);
    showError(err);
  }
}

/** 快捷键表：原生 <dialog> */
function keysDialog() {
  let dlg = document.querySelector('.ws-keys');
  if (!dlg) {
    const rows = [
      ['1–9', t('ws.keys.pages')], ['/', t('ws.keys.search')], ['R', t('ws.keys.refresh')], ['?', t('ws.keys.help')], ['Esc', t('ws.keys.close')],
    ];
    dlg = el('dialog', { class: 'dialog ws-keys', 'aria-label': t('ws.keys.title') },
      el('p', { class: 'card__title' }, t('ws.keys.title')),
      el('dl', {}, ...rows.flatMap(([k, v]) => [el('dt', {}, el('span', { class: 'kbd' }, k)), el('dd', {}, v)])));
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
    document.body.append(dlg);
  }
  dlg.showModal();
}

function onKey(e) {
  const tag = e.target.tagName;
  if (e.metaKey || e.ctrlKey || e.altKey || tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target.isContentEditable) return;
  if (document.querySelector('dialog[open]')) return;
  if (/^[1-9]$/.test(e.key)) {
    const link = document.querySelector(`.topnav [data-hotkey="${e.key}"]`);
    if (link) { e.preventDefault(); link.click(); }
  } else if (e.key === '/') {
    e.preventDefault();
    openPalette();
  } else if (e.key === 'r' || e.key === 'R') {
    e.preventDefault();
    renderPage();
    toast({ title: t('ws.refreshed') });
  } else if (e.key === '?') {
    e.preventDefault();
    keysDialog();
  }
}

async function boot() {
  initTheme();
  try {
    await initI18n();
  } catch (err) {
    console.warn('字典加载失败，按页面里写的中文显示', err);
  }
  drawNav();
  initPalette({
    pages: NAV.map((n) => ({ key: n.key, href: navTarget(n), group: 'palette.group.pages' })),
    stockHref: (code) => `/#/stock/${encodeURIComponent(code)}`,   // 个股详情先用旧界面，新界面没有这一页
  });
  initCursor();
  document.addEventListener('keydown', onKey);
  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-action="retry"]')) renderPage();
  });
  window.addEventListener('langchange', () => { drawNav(); renderPage(); });
  await renderPage();
}

boot();
