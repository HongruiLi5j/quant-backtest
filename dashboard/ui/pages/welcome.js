/* ==========================================================
 * 文件: ui/pages/welcome.js（D72 / R3-ux-2）
 * 用途: 今日页最上面的「第一次来？每页做什么」提示条。只显示一次：点「知道了」后写 localStorage，以后不再出现。
 *   - 每页一行说明来自 content 写的词条 c72.page.<导航 id>（顺序 = 顶栏顺序），前 3 行直接显示，其余收进「展开全部」
 *   - 最后一行提示快捷键（? / ⌘K / 1–9）；不弹窗、不挡操作，键盘可达（按钮 + details）
 *   - 词条还没进字典时整条不显示（不露键名）
 * ========================================================== */
import { el, store } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { NAV, navTarget } from '../nav.js';

const KEY = 'qb-ux-welcome-done';

export function welcomeStrip() {
  if (store.get(KEY) === '1') return null;
  const title = t('ux.welcome.title');
  if (title === 'ux.welcome.title') return null;
  const lines = NAV.map((n) => [n, t(`c72.page.${n.id}`)]).filter(([n, s]) => s !== `c72.page.${n.id}`)
    .map(([n, s]) => el('li', {}, el('a', { href: navTarget(n) }, s)));
  if (!lines.length) return null;
  const close = el('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, t('ux.welcome.close'));
  const box = el('section', { class: 'card ux-welcome', 'aria-label': title },
    el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, title), close),
    el('ul', { class: 'ux-welcome__list' }, ...lines.slice(0, 3)),
    lines.length > 3 ? el('details', {}, el('summary', {}, t('ux.welcome.more')), el('ul', { class: 'ux-welcome__list' }, ...lines.slice(3))) : null,
    el('p', { class: 'ws-note' }, t('ux.welcome.keys')));
  close.addEventListener('click', () => {
    store.set(KEY, '1');
    box.remove();
    document.querySelector('main h1, main [tabindex="-1"], main')?.focus?.();   // 关掉后焦点不丢到 body
  });
  return box;
}
