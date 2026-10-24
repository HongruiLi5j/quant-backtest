/* 页面样板的渲染器：w/_template.html 用。新页面复制这个文件改名为 <id>.js */
import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';

/** root = 页面里的 [data-ws-body]；抛错 → 错误卡片（401/403 自动变成「登录已过期」）；返回 'empty' → 空状态 */
export async function render(root) {
  const meta = await getJSON('/api/meta');
  root.replaceChildren(
    el('div', { class: 'card' },
      el('p', { class: 'card__title' }, t('ui.template.title')),
      el('p', {}, `markets: ${Object.keys(meta.markets ?? {}).join(', ')}`)));
}
