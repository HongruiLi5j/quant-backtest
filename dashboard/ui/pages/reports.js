/* ==========================================================
 * 文件: ui/pages/reports.js —— 报告页（N22，只读，样式 A：左目录 + 右正文）
 * 接口（N22 新增，只读）:
 *   GET /api/reports/list              → { reports[{path,title,kind,group,date,size,mtime}], total, truncated }
 *   GET /api/reports/file?path=<相对路径> → 上面一行的字段 + { text, truncated, max_bytes }
 * 正文：marked 排版 → DOMPurify 清洗成 DOM 片段再放进页面（不拼 HTML 字符串）；图片、表单、样式标签一律去掉。
 * 选中的报告记在地址栏 #后面，刷新后还在。其余文字一律 textContent。
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { marked } from '../vendor/marked.esm.js';
import DOMPurify from '../vendor/purify.es.js';

const KINDS = ['all', 'replay', 'factor', 'signal', 'quality', 'weekly', 'eod', 'other'];
const S = { list: [], kind: 'all', q: '', path: '', doc: null };
let docSeq = 0;

const kindOf = (k) => (KINDS.includes(k) && k !== 'all' ? k : 'other');
const kindBadge = (k) => el('span', { class: 'badge' }, t(`rep.kind.${kindOf(k)}`));
function size(n) {
  if (!Number.isFinite(n)) return '—';
  return n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}
function hashPath() {
  try { return decodeURIComponent(window.location.hash.slice(1)); } catch { return ''; }
}

/** markdown → 清洗过的 DOM 片段 */
function mdFragment(text) {
  const html = marked.parse(text, { gfm: true, async: false });
  const frag = DOMPurify.sanitize(html, {
    RETURN_DOM_FRAGMENT: true,
    FORBID_TAGS: ['img', 'style', 'form', 'input', 'button', 'iframe', 'svg', 'math'],
    FORBID_ATTR: ['style'],
  });
  for (const a of frag.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    if (/^https?:\/\//i.test(href)) { a.setAttribute('target', '_blank'); a.setAttribute('rel', 'noopener noreferrer'); }
    else if (!href.startsWith('#')) a.removeAttribute('href');      // 报告里的本地相对链接在网页上打不开，留文字去掉链接
  }
  return frag;
}

function filtered() {
  const q = S.q.trim().toLowerCase();
  return S.list.filter((r) => (S.kind === 'all' || kindOf(r.kind) === S.kind)
    && (!q || r.title.toLowerCase().includes(q) || r.path.toLowerCase().includes(q)));
}

function side(redraw, open) {
  const search = el('input', { class: 'ws-input', type: 'search', placeholder: t('rep.search'), 'aria-label': t('rep.search'), value: S.q });
  search.addEventListener('input', () => { S.q = search.value; redraw(true); });
  const seg = el('div', { class: 'segmented', role: 'tablist', 'aria-label': t('rep.filter') }, ...KINDS.map((k) => {
    const b = el('button', { class: 'segmented__btn', type: 'button', role: 'tab', 'aria-selected': String(S.kind === k) }, t(`rep.kind.${k}`));
    b.addEventListener('click', () => { S.kind = k; redraw(); });
    return b;
  }));
  const rows = filtered();
  const groups = new Map();
  for (const r of rows) {
    const g = r.group || '';
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(r);
  }
  const tree = el('div', { class: 'ws-tree', dataset: { tree: '1' } });
  if (!rows.length) tree.append(el('p', { class: 'muted ws-tree__date' }, t('rep.nomatch')));
  for (const [g, items] of groups) {
    tree.append(el('div', { class: 'ws-tree__date' }, g ? `${g} · ${items.length}` : `${t('rep.loose')} · ${items.length}`));
    tree.append(el('ul', { class: 'ws-tree__list' }, ...items.map((r) => {
      const b = el('button', { class: 'ws-tree__item', type: 'button', 'aria-current': String(r.path === S.path), title: r.path },
        el('span', { class: 'rep-item__t' }, r.title), kindBadge(r.kind));
      b.addEventListener('click', () => open(r.path));
      return el('li', {}, b);
    })));
  }
  return { node: el('aside', { class: 'card card--flush ws-reports__side' },
    el('div', { class: 'rep-search' }, search), seg, tree), search };
}

function docView() {
  const d = S.doc;
  if (!d) return el('article', { class: 'card ws-doc' }, el('p', { class: 'muted' }, S.list.length ? t('rep.pick') : t('rep.none')));
  if (d.error) return el('article', { class: 'card ws-doc' }, el('p', { class: 'muted', role: 'alert' }, d.error));
  if (d.loading) return el('article', { class: 'card ws-doc' }, el('div', { class: 'skeleton', style: { height: '320px' } }));
  const meta = el('div', { class: 'ws-doc__meta' }, kindBadge(d.kind),
    el('span', { class: 'muted' }, d.group || t('rep.loose')), el('span', { class: 'muted num' }, d.date),
    el('span', { class: 'muted num' }, size(d.size)), el('span', { class: 'muted mono rep-path' }, `reports/${d.path}`));
  const warn = d.truncated ? el('p', { class: 'card rep-warn', role: 'note' }, t('rep.truncated', { v: size(d.max_bytes) })) : null;
  const body = el('div', { class: 'ws-md' });
  try { body.append(mdFragment(d.text || '')); } catch { body.className = 'ws-md ws-md__raw'; body.textContent = d.text || ''; }
  return el('article', { class: 'card ws-doc' }, meta, warn, body);
}

export async function render(root) {
  const data = await getJSON('/api/reports/list', { mock: 'reports_list' });
  S.list = Array.isArray(data.reports) ? data.reports : [];
  const head = document.querySelector('[data-bind="meta"]');
  if (head) head.textContent = t('rep.head', { n: S.list.length, d: S.list[0]?.date || '—' });
  if (!S.list.length) return 'empty';

  const shell = el('div', { class: 'ws-reports' });
  let sideBox = null;
  const redraw = (keepFocus) => {
    const pos = keepFocus ? sideBox?.search.selectionStart : null;
    const scroll = shell.querySelector('[data-tree]')?.scrollTop || 0;
    sideBox = side(redraw, open);
    shell.replaceChildren(sideBox.node, docView());
    const tree = shell.querySelector('[data-tree]');
    if (tree) tree.scrollTop = scroll;
    if (keepFocus) { sideBox.search.focus(); sideBox.search.setSelectionRange(pos, pos); }
  };
  async function open(path) {
    const seq = ++docSeq;
    S.path = path;
    S.doc = { loading: true };
    if (hashPath() !== path) history.replaceState(null, '', `#${encodeURIComponent(path)}`);
    redraw();
    try {
      const d = await getJSON(`/api/reports/file?path=${encodeURIComponent(path)}`, { mock: 'reports_file' });
      if (seq !== docSeq) return;
      S.doc = d.error ? { error: String(d.error) } : d;
    } catch (e) {
      if (seq !== docSeq) return;
      S.doc = { error: e.status === 404 ? t('rep.gone') : t('ui.state.error') };
    }
    redraw();
    if (window.innerWidth < 900) shell.querySelector('.ws-doc')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }

  root.replaceChildren(shell);
  const want = hashPath();
  const start = S.list.find((r) => r.path === want) || S.list.find((r) => r.path === S.path) || S.list[0];
  await open(start.path);
}
