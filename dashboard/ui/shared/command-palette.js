/* ==========================================================
 * 文件: ui/shared/command-palette.js（来自 mockups，股票改走真实 /api/search）
 * 用途: 全局搜索 Cmd+K / Ctrl+K。原生 <dialog> 自带焦点陷阱与 Esc 关闭；
 *       ↑↓ 选择，Enter 执行。条目 = 页面导航 + 常用操作 + 股票（/api/search）。
 *       所有文本 textContent 渲染；列表是 ARIA combobox + listbox 模式。
 * ========================================================== */

import { el, qs } from './dom.js';
import { t, setLang, lang } from './i18n.js';
import { setTheme, setMarket, currentTheme } from './theme-switcher.js';
import { getJSON } from './api.js';

/** 页面条目由 page-boot 传入（nav.js 的 NAV） */
const ACTIONS = [
  { key: 'palette.action.theme', run: () => setTheme(currentTheme() === 'light' ? 'dark' : 'light'), group: 'palette.group.actions' },
  { key: 'palette.action.lang', run: () => setLang(lang() === 'zh' ? 'en' : 'zh'), group: 'palette.group.actions' },
  { key: 'palette.action.market', run: () => setMarket(document.documentElement.dataset.market === 'us' ? 'cn' : 'us'), group: 'palette.group.actions' },
];

let dialog;
let input;
let list;
let items = [];
let pageList = [];
let stockHref = (code) => `backtest.html?symbol=${code}`;
/** 股票条目：输入后查 /api/search（A 股），只显示最近一次查询的结果 */
let stocks = [];
let searchSeq = 0;
let searchTimer = 0;
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
}

function entries() {
  const stockRows = stocks.map((s) => ({ label: `${s.code} ${s.name}`, href: stockHref(s.code), group: 'palette.group.stocks' }));
  return [...pageList.map((p) => ({ ...p, label: t(p.key) })), ...ACTIONS.map((a) => ({ ...a, label: t(a.key) })), ...stockRows];
}

function render() {
  const q = input.value.trim().toLowerCase();
  items = entries().filter((it) => !q || it.group === 'palette.group.stocks' || it.label.toLowerCase().includes(q) || (it.href ?? '').includes(q));   // 股票条目是服务端按输入查出来的，不再本地过滤
  active = 0;
  list.replaceChildren();
  if (!items.length) {
    list.append(el('li', { class: 'palette__empty', role: 'presentation' }, t('palette.empty')));
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
  if (!q) { stocks = []; return; }
  searchTimer = setTimeout(async () => {
    try {
      const rows = await getJSON(`/api/search?market=cn&q=${encodeURIComponent(q)}`);
      if (seq !== searchSeq || !Array.isArray(rows)) return;
      stocks = rows.slice(0, 8);
    } catch {
      stocks = [];   // 登录过期或接口不可用：只剩页面和操作条目
    }
    if (seq === searchSeq && dialog?.open) render();
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
  else if (e.key === 'Enter') { e.preventDefault(); run(active); }
}

export function openPalette() {
  if (!dialog) build();
  input.placeholder = t('palette.placeholder');
  input.value = '';
  stocks = [];
  render();
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
