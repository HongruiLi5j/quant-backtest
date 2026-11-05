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
import { restoreFocusOnClose } from './a11y.js';
import { renderTopnav, NAV, navTarget } from '../nav.js';
import { stateLines, resolveLines, fillStateCard } from './state-card.js';
import { stockPage } from './stock-link.js';
import { versioned } from './asset-v.js';
import { prefetchJSON } from './api.js';
import { mountHelp } from './page-help.js';   // D67：每页「怎么读这一页」

const page = document.body.dataset.page || '';
const params = new URLSearchParams(window.location.search);
let mod = null;
let renderSeq = 0;

/** 顶栏高亮：就是当前页面 id（D45 起美股盯盘并进「盯盘」，monitor 页不管 A 股还是美股都高亮「盯盘」） */
const currentNav = () => page;

function drawNav() {
  renderTopnav(document.querySelector('[data-topnav]'), {
    current: currentNav(),
    iconsHref: versioned(new URL('../shared/icons.svg', import.meta.url)),   // D39：与 HTML 里的图标同一个带哈希地址，只下载一次、可长期缓存
    brandHref: navTarget(NAV[0]),
  });
}

const main = () => document.querySelector('main[data-state]');

/** D49（revtech A11-01）：加载中对读屏软件是静默的——骨架块在 aria-hidden 的 .when-loading 里，main 上也没有 aria-busy。
 *  现在：main 加 aria-busy；另在 main 外面放一个常驻的 role=status 读屏专用文字，加载时写「加载中…」，结束清空。
 *  status 区放在 main 外，是因为页面渲染会整块替换 main 里的内容，常驻节点读屏才能稳定播报。 */
let srStatus = null;
function announce(text) {
  if (!srStatus) {
    srStatus = el('p', { class: 'sr-only', role: 'status', 'aria-live': 'polite' });
    document.body.append(srStatus);
  }
  srStatus.textContent = text;
}

function setState(state) {
  const box = main();
  box?.setAttribute('data-state', state);
  if (state === 'loading') box?.setAttribute('aria-busy', 'true'); else box?.removeAttribute('aria-busy');
  announce(state === 'loading' ? t('cmp.demo.loading') : '');
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
  fillStateCard(box?.querySelector('.when-error'), resolveLines(stateLines('error', err)));   // 标题下的说明和原因（A03）
  setState('error');
}

/** 空状态卡片：标题下补一句说明，告诉用户这不是出错（A03） */
function showEmpty() {
  fillStateCard(main()?.querySelector('.when-empty'), resolveLines(stateLines('empty')));
  setState('empty');
}

async function renderPage() {
  const seq = ++renderSeq;
  const forced = params.get('state');
  setState('loading');
  if (forced === 'loading') return;
  if (forced === 'error') { showError(null); return; }
  if (forced === 'empty') { showEmpty(); return; }
  const body = document.querySelector('[data-ws-body]') ?? main();
  if (params.get('debug') === '1') {   // 页脚「接口字段核对」：每次重绘前清空，接口返回时 api.js 会逐个登记
    try { (await import('./debug-fields.js')).resetDebug(); } catch (err) { console.warn('debug-fields 加载失败', err); }
  }
  try {
    if (!mod) mod = await import(`../pages/${page}.js`);
    const result = await mod.render(body);
    if (seq !== renderSeq) return;   // 期间又触发了一次渲染，丢掉这次
    if (result === 'empty') { showEmpty(); return; }
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
      ['⌘K', t('ux.keys.palette')],   // D83：面板里能做的事（换主题 / 语言 / 涨跌色 / 本页操作）
    ];
    dlg = el('dialog', { class: 'dialog ws-keys', 'aria-label': t('ws.keys.title') },
      el('p', { class: 'card__title' }, t('ws.keys.title')),
      el('dl', {}, ...rows.flatMap(([k, v]) => [el('dt', {}, el('span', { class: 'kbd' }, k)), el('dd', {}, v)])));
    dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
    dlg.tabIndex = -1;   // 里面没有可聚焦的东西：让对话框自己能拿到焦点，读屏才会读标题（A11）
    dlg.rememberOpener = restoreFocusOnClose(dlg);
    document.body.append(dlg);
  }
  dlg.rememberOpener();
  dlg.showModal();
  dlg.focus();
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

/** R7-perf-1：首屏必需的接口在字典下载期间就先发出去。路径必须和页面里 getJSON 的完全一致才会被取用；
 *  市场用上次选的（没有就 cn），猜错了只是白发一个请求。目前只有今日页有表。 */
const PREFETCH = {
  today: (m) => ['/api/meta', `/api/summary?market=${m}`, `/api/tracking?market=${m}`, '/api/main_strategy', '/api/member/me'],
};
function prefetchFirstScreen() {
  const list = PREFETCH[page];
  if (!list) return;
  let m = 'cn';
  try { m = localStorage.getItem('qb-ui-market') || 'cn'; } catch { /* 隐私模式读不到就用默认 */ }
  if (!/^[a-z]{2,4}$/.test(m)) m = 'cn';
  for (const path of list(m)) prefetchJSON(path);
}

async function boot() {
  initTheme();
  prefetchFirstScreen();
  try {
    await initI18n();
  } catch (err) {
    console.warn('字典加载失败，按页面里写的中文显示', err);
  }
  drawNav();
  mountHelp(page);
  initPalette({
    pages: NAV.map((n) => ({ key: n.key, href: navTarget(n), group: 'palette.group.pages' })),
    stockHref: (code) => stockPage(code),   // 新个股页 w/stock.html；不带 market 时用默认市场；预览模式带上 ?mock=1（G01）
  });
  // 自定义鼠标圆圈已去掉（不再调用 initCursor），保留系统鼠标
  document.addEventListener('keydown', onKey);
  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-action="retry"]')) renderPage();
  });
  window.addEventListener('langchange', () => { drawNav(); mountHelp(page); renderPage(); });
  await renderPage();
}

boot();
