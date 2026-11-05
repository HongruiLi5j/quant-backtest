/* 无障碍小工具（A11）：可点击的非按钮元素的键盘激活、弹层关闭后还焦点、按钮的朗读标签。
 * 只做行为，不改任何样式。文本一律走 setAttribute / textContent，不拼 HTML。
 * 词条在 i18n/parts/A11.*.json；还没合并进基础字典时 t() 会原样返回键名，
 * 这种标签直接不加（按钮仍有自己的可见文字），不把键名读给用户（和 A03 / A07 一样）。 */
import { t } from './i18n.js';

/** 词条翻成文字；词条没合并进字典（t() 返回键名）时返回 null */
export function tMaybe(key, vars = {}) {
  const text = t(key, vars);
  return text === key ? null : text;
}

/** 给按钮补一个更具体的朗读标签（表格每行都叫「添加」「撤销」，读屏分不清是哪一行）。词条缺失就不加 */
export function setAriaLabel(node, key, vars = {}) {
  const text = tMaybe(key, vars);
  if (text) node.setAttribute('aria-label', text);
  return node;
}

/** 可点击的非按钮元素（带 tabindex 的表格行）：Enter 和空格都触发，和原生按钮一致。
 *  只响应焦点就在元素本身的按键，行内的链接 / 按钮自己处理自己的按键；带修饰键的不管。
 *  空格要 preventDefault，否则页面会往下滚。 */
export function activateOnKey(node, handler) {
  node.addEventListener('keydown', (e) => {
    if (e.target !== node || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); handler(e); }
  });
}

/** 原生 <dialog> 关闭后把焦点还给打开它之前的元素（Esc 关闭、点遮罩关闭、按钮关闭都走 close 事件）。
 *  浏览器通常会自己还，但 Safari 老版本和元素被重绘过的情况不可靠，这里显式补一次。
 *  返回「记下当前焦点」的函数，在 showModal() 之前调用。 */
export function restoreFocusOnClose(dialog) {
  let opener = null;
  dialog.addEventListener('close', () => {
    if (opener?.isConnected && typeof opener.focus === 'function') opener.focus();
    opener = null;
  });
  return () => { opener = document.activeElement; };
}

/** 页内展开的确认区（不是 <dialog>）：焦点在区域内按 Esc 关闭，等价于点「取消」 */
export function closeOnEscape(node, handler) {
  node.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); handler(e); }
  });
}
