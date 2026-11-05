/* ==========================================================
 * 文件: ui/shared/stock-link.js（G01）
 * 用途: 各页面「点股票名 → 个股详情」统一进新个股页 w/stock.html，不再跳旧界面 /#/stock/<代码>。
 *   以前今日 / 信号 / 全市场 / 自选 / 持仓 / 研究页的股票链接都去旧界面（换一套界面、没有新顶栏），
 *   只有顶部搜索（⌘K）去新个股页；现在全站一致。旧个股页仍可从右上角「旧版界面」进去。
 *   - market 不传时由个股页用默认市场；预览模式（?mock=1）把 mock=1 带过去
 *   - 只拼本站相对地址，参数走 URLSearchParams 编码，不会被拼成外站地址
 * ========================================================== */

import { isMock } from './api.js';
import { store } from './dom.js';

/** 新个股页地址（相对 w/ 目录） */
export function stockPage(code, market = '') {
  const q = new URLSearchParams({ code: String(code) });
  if (market) q.set('market', market);
  if (isMock()) q.set('mock', '1');
  return `stock.html?${q}`;
}

/** 点进个股页时带上当前页选的策略：写新个股页自己的偏好键；旧界面的 dStrategy 照旧写，两套界面一致 */
export function rememberStockStrategy(key) {
  if (!key) return;
  store.set('qb-u2-stock-strategy', key);
  store.set('dStrategy', key);
}
