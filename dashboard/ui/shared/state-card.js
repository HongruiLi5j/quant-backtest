/* ==========================================================
 * 文件: ui/shared/state-card.js（A03）
 * 用途: 出错 / 空数据卡片标题下面的说明行，由 page-boot.js 调用。
 *   - stateLines(state, err)：返回要显示的词条 [{ key, vars }]（纯函数，便于测试）
 *       error + 登录过期（err.auth）：不加说明（标题已经说清楚，按钮是「回到首页」）
 *       error + ApiError：通用说明 + 「原因：…」（err.message，例如 HTTP 404、请求超时）
 *       error + 其他异常（页面脚本自己出错）：通用说明 + 「页面脚本出错，详情见浏览器控制台」
 *       error + 没有 err（?state=error 强制预览）：只有通用说明
 *       empty：空状态说明
 *   - resolveLines(lines)：翻译成文字；词条还没合并进基础字典时 t() 会原样返回键名，
 *     这种行直接丢掉，不把 a03.xxx 这样的键名露给用户
 *   - fillStateCard(card, texts)：把说明写进卡片，一律 textContent（el 建节点）；重复调用先清掉旧的
 * ========================================================== */

import { el } from './dom.js';
import { t } from './i18n.js';

export function stateLines(state, err = null) {
  if (state === 'empty') return [{ key: 'a03.state.empty_hint' }];
  if (state !== 'error' || err?.auth === true) return [];
  const lines = [{ key: 'a03.state.error_hint' }];
  if (err?.name === 'ApiError') {
    if (typeof err.message === 'string' && err.message) lines.push({ key: 'a03.state.error_reason', vars: { msg: err.message } });
  } else if (err) {
    lines.push({ key: 'a03.state.error_script' });
  }
  return lines;
}

export function resolveLines(lines) {
  return lines.map(({ key, vars }) => {
    const text = t(key, vars);
    return text === key ? '' : text;
  }).filter(Boolean);
}

export function fillStateCard(card, texts) {
  if (!card) return;
  for (const old of card.querySelectorAll('[data-bind="state-hint"]')) old.remove();
  if (!texts.length) return;
  const nodes = texts.map((s) => el('p', { class: 'state__hint', 'data-bind': 'state-hint' }, s));
  const title = card.querySelector('.state__title');
  if (title) title.after(...nodes);
  else card.append(...nodes);
}
