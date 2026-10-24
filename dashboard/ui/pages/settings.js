/* ==========================================================
 * 文件: ui/pages/settings.js —— 设置
 * 没有后端接口：语言、主题、涨跌色、默认市场都只存在这台浏览器（localStorage），不含任何身份信息。
 * mockup 里的「档位与用量」「默认模式」「交易费用」没有真实数据来源，先不显示。
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t, lang } from '../shared/i18n.js';
import { setTheme, setMarket, currentTheme } from '../shared/theme-switcher.js';
import { toast } from '../shared/toast.js';
import { getJSON } from '../shared/api.js';
import { dataMarket, setDataMarket } from './u4-util.js';

let meta = null;
let host = null;

function seg(label, items, current, onPick) {
  return el('div', { class: 'segmented', role: 'group', 'aria-label': label },
    ...items.map(([value, text]) => {
      const b = el('button', { class: 'segmented__btn', type: 'button', 'aria-pressed': String(value === current) }, text);
      b.addEventListener('click', () => { onPick(value); draw(); toast({ title: t('u4.set.saved') }); });
      return b;
    }));
}

function row(titleKey, control, hintKey) {
  return el('div', { class: 'ws-set__row' },
    el('div', {}, el('p', {}, t(titleKey)), hintKey ? el('p', { class: 'ws-set__hint' }, t(hintKey)) : null), control);
}

function draw() {
  const market = document.documentElement.dataset.market === 'us' ? 'us' : 'cn';
  const langBtns = ['zh', 'en'].map((l) => {
    const b = el('button', { class: 'segmented__btn', type: 'button', dataset: { action: 'set-lang', lang: l }, 'aria-pressed': String(lang() === l) }, l === 'zh' ? '中文' : 'English');
    return b;   // 点击由 i18n.js 的全局委托处理，切换后页面会自动重绘
  });
  host.replaceChildren(el('div', { class: 'u4-set' },
    el('section', { class: 'card', 'aria-labelledby': 'u4-set-ui' },
      el('h2', { class: 'card__title', id: 'u4-set-ui', style: { 'margin-bottom': 'var(--space-2)' } }, t('u4.set.ui')),
      row('u4.set.lang', el('div', { class: 'segmented', role: 'group', 'aria-label': t('u4.set.lang') }, ...langBtns)),
      row('u4.set.theme', seg(t('u4.set.theme'), [['dark', t('u4.set.theme.dark')], ['light', t('u4.set.theme.light')]], currentTheme(), setTheme)),
      row('u4.set.colors', seg(t('u4.set.colors'), [['cn', t('u4.set.colors.cn')], ['us', t('u4.set.colors.us')]], market, setMarket), 'u4.set.colors.hint'),
      row('u4.set.market', seg(t('u4.set.market'), Object.entries(meta.markets).map(([k, v]) => [k, v.name]), dataMarket(meta), setDataMarket), 'u4.set.market.hint')),
    el('section', { class: 'card', 'aria-labelledby': 'u4-set-links' },
      el('h2', { class: 'card__title', id: 'u4-set-links', style: { 'margin-bottom': 'var(--space-2)' } }, t('u4.set.links')),
      row('u4.set.classic', el('a', { class: 'btn btn--sm', href: '/' }, t('ui.nav.classic'))),
      row('u4.set.admin', el('a', { class: 'btn btn--sm', href: '/admin' }, t('ui.nav.admin')))),
    el('p', { class: 'ws-set__hint' }, t('u4.set.local_note'))));
}

export async function render(root) {
  meta = await getJSON('/api/meta', { mock: 'meta-u4' });
  host = root;
  draw();
}
