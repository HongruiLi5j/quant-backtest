/* ==========================================================
 * 文件: ui/shared/command-palette.js（来自 mockups，股票改走真实 /api/search）
 * 用途: 全局搜索 Cmd+K / Ctrl+K。原生 <dialog> 自带焦点陷阱与 Esc 关闭；
 *       ↑↓ 选择，Enter 执行。条目 = 页面导航 + 常用操作 + 股票（/api/search）。
 *       所有文本 textContent 渲染；列表是 ARIA combobox + listbox 模式。
 * ========================================================== */

import { el, qs } from './dom.js';
import { t, setLang, lang } from './i18n.js';
import { setTheme, setMarket } from './theme-switcher.js';
import { getJSON } from './api.js';
import { restoreFocusOnClose } from './a11y.js';
import { rankStocks } from './palette-match.js';

/** 页面条目由 page-boot 传入（nav.js 的 NAV） */
// D83：主题改成三个明确的动作（深色 / 浅色 / 终端），不再只能来回切；标签由「主题」+ 主题名拼成，复用设置页词条（分隔用半角 ·，代码里不留中文标点）
const themeAction = (name) => ({ label: () => `${t('u4.set.theme')} · ${t(`u4.set.theme.${name}`)}`, run: () => setTheme(name), group: 'palette.group.actions' });
const ACTIONS = [
  themeAction('dark'), themeAction('light'), themeAction('terminal'),
  { key: 'palette.action.lang', run: () => setLang(lang() === 'zh' ? 'en' : 'zh'), group: 'palette.group.actions' },
  { key: 'palette.action.market', run: () => setMarket(document.documentElement.dataset.market === 'us' ? 'cn' : 'us'), group: 'palette.group.actions' },
];

let dialog;
let rememberOpener = () => {};   // build() 里换成 restoreFocusOnClose(dialog) 返回的函数
let input;
let list;
let items = [];
let pageList = [];
let stockHref = (code) => `backtest.html?symbol=${code}`;
/** 股票条目：输入后查 /api/search（A 股），只显示最近一次查询的结果 */
let stocks = [];
let searchSeq = 0;
let searchTimer = 0;
let searching = false;     // D04：/api/search 还没回来
let pendingEnter = false;  // D04：结果回来前按了 Enter，结果一到就执行第一条股票
let active = 0;

function build() {
  input = el('input', {
    class: 'palette__input', type: 'text', role: 'combobox', 'aria-expanded': 'true',
    'aria-controls': 'palette-list', 'aria-autocomplete': 'list', autocomplete: 'off',
    placeholder: t('palette.placeholder'), 'aria-label': t('palette.placeholder'),
  });
  list = el('ul', { class: 'palette__list', id: 'palette-list', role: 'listbox' });
  dialog = el('dialog', { class: 'dialog palette', 'aria-label': t('palette.title') },
    el('div', { class: 'palette__search' }, el('span', { class: 'kbd', 'aria-hidden': 'true' }, '⌘K'), input),
    list);
  document.body.append(dialog);

  input.addEventListener('input', onInput);
  input.addEventListener('keydown', onKey);
  list.addEventListener('click', (e) => {
    const li = e.target.closest('[role="option"]');
    if (li) run(Number(li.dataset.index));
  });
  dialog.addEventListener('click', (e) => { if (e.target === dialog) dialog.close(); });   // 点遮罩关闭
  rememberOpener = restoreFocusOnClose(dialog);   // 关闭后焦点回到打开前的元素（A11）
}

function entries() {
  const stockRows = stocks.map((s) => ({ label: `${s.code} ${s.name}`, href: stockHref(s.code), group: 'palette.group.stocks' }));
  // D83：本页操作——页面上标了 data-palette 的链接 / 按钮（例如个股页「在回测实验室调参数」），打开面板时现读，执行 = 点它
  const here = [...document.querySelectorAll('main [data-palette]')].filter((n) => n.offsetParent !== null)
    .map((n) => ({ label: n.dataset.palette || n.textContent.trim(), run: () => n.click(), group: 'palette.group.here' }));
  return [...here, ...pageList.map((p) => ({ ...p, label: t(p.key) })),
    ...ACTIONS.map((a) => ({ ...a, label: typeof a.label === 'function' ? a.label() : t(a.key) })), ...stockRows];
}

function render() {
  const q = input.value.trim().toLowerCase();
  items = entries().filter((it) => !q || it.group === 'palette.group.stocks' || it.label.toLowerCase().includes(q) || (it.href ?? '').includes(q));   // 股票条目是服务端按输入查出来的，不再本地过滤
  active = 0;
  list.replaceChildren();
  if (!items.length) {
    // D04：还在搜的时候不说「没有匹配」，说「搜索中」；词条还没进字典时只显示省略号，不露键名
    const busy = t('palette.searching');
    list.append(el('li', { class: 'palette__empty', role: 'presentation', 'aria-live': 'polite' }, searching ? (busy === 'palette.searching' ? '…' : busy) : t('palette.empty')));
    return;
  }
  let lastGroup = '';
  items.forEach((it, i) => {
    if (it.group !== lastGroup) {
      list.append(el('li', { class: 'palette__group', role: 'presentation' }, t(it.group)));
      lastGroup = it.group;
    }
    list.append(el('li', {
      class: 'palette__item', role: 'option', id: `palette-opt-${i}`,
      'aria-selected': String(i === active), dataset: { index: i },
    }, it.label, el('span', { class: 'palette__hint' }, it.href ? '↵' : t('palette.run'))));
  });
  input.setAttribute('aria-activedescendant', 'palette-opt-0');
}

function onInput() {
  render();
  clearTimeout(searchTimer);
  const q = input.value.trim();
  const seq = ++searchSeq;
  pendingEnter = false;
  if (!q) { stocks = []; searching = false; return; }
  searching = true;
  if (!items.length) render();   // 立刻显示「搜索中」
  searchTimer = setTimeout(async () => {
    try {
      const rows = await getJSON(`/api/search?market=cn&q=${encodeURIComponent(q)}`);
      if (seq !== searchSeq || !Array.isArray(rows)) return;
      stocks = rankStocks(q, rows, 8);   // 代码前缀命中排最前，再取前 8 条（A13）
    } catch {
      stocks = [];   // 登录过期或接口不可用：只剩页面和操作条目
    }
    if (seq !== searchSeq) return;
    searching = false;
    if (!dialog?.open) return;
    render();
    if (pendingEnter) {   // 结果到之前按过 Enter：按完整结果执行第一条（和平时按 Enter 一样：页面 → 操作 → 股票的顺序）
      pendingEnter = false;
      run(active);
    }
  }, 180);
}

function move(delta) {
  if (!items.length) return;
  active = (active + delta + items.length) % items.length;
  for (const opt of list.querySelectorAll('[role="option"]')) {
    const on = Number(opt.dataset.index) === active;
    opt.setAttribute('aria-selected', String(on));
    if (on) opt.scrollIntoView({ block: 'nearest' });
  }
  input.setAttribute('aria-activedescendant', `palette-opt-${active}`);
}

function run(index) {
  const it = items[index];
  if (!it) return;
  dialog.close();
  if (it.run) it.run();
  else window.location.href = it.href;
}

function onKey(e) {
  if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
  else if (e.key === 'Enter') {
    e.preventDefault();
    const q = input.value.trim();
    // 列表里已经有条目（含用方向键选中的那条）就立刻执行；只有列表空着、还在搜时才等结果（revtech C-D04）
    if (items.length || !searching) { run(active); return; }
    if (/^\d{6}$/.test(q)) {   // D04 快速通道：6 位 A 股代码不等搜索结果，直接去个股页（带 market=cn）
      const href = stockHref(q);
      dialog.close();
      window.location.href = href.includes('market=') ? href : `${href}${href.includes('?') ? '&' : '?'}market=cn`;
      return;
    }
    pendingEnter = true;   // 其他输入：等结果回来再执行
  }
}

export function openPalette() {
  if (!dialog) build();
  input.placeholder = t('palette.placeholder');
  input.value = '';
  stocks = [];
  render();
  rememberOpener();
  dialog.showModal();
  input.focus();
}

export function initPalette(options = {}) {
  if (options.pages) pageList = options.pages;
  if (options.stockHref) stockHref = options.stockHref;
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if (dialog?.open) dialog.close();
      else openPalette();
    }
  });
  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-action="open-palette"]')) openPalette();
  });
  qs('[data-action="open-palette"]')?.setAttribute('aria-haspopup', 'dialog');
}
