/* ==========================================================
 * 文件: ui/shared/asset-v.js（D39）
 * 用途: 给脚本里按地址取用的静态文件（字典 json、图标 svg）加 ?v=内容哈希，让浏览器长期缓存。
 *   哈希由服务器写在页面的 <meta name="qb-asset-v" content="i18n/zh.json=abc;...">；
 *   没有这个 meta（比如 mock 预览、离线打开）就原样返回地址，退回原来的 no-cache 行为，不会出错。
 * ========================================================== */

let table = null;

function versions() {
  if (table) return table;
  table = {};
  let raw = '';
  try {   // 测试里的假 document 可能没有 querySelector：当作没有 meta，原样返回地址
    raw = document.querySelector('meta[name="qb-asset-v"]')?.getAttribute('content') || '';
  } catch { raw = ''; }
  for (const pair of raw.split(';')) {
    const i = pair.indexOf('=');
    if (i > 0) table[pair.slice(0, i)] = pair.slice(i + 1);
  }
  return table;
}

/** url：URL 对象或地址（相对当前页面）；按它在 /ui/ 下的路径（如 i18n/zh.json）查哈希，返回带 ?v= 的地址（字符串）。
 *  键从地址本身算，不在调用处另写一份路径（另写的裸路径会被 scripts/ui_asset_report.py 当成引用去解析）。 */
export function versioned(url) {
  let u;
  try { u = new URL(url, globalThis.location?.href); } catch { return String(url); }   // 相对地址又没有页面地址（测试环境）：原样返回
  const v = versions()[u.pathname.replace(/^\/ui\//, '')];
  if (v) u.searchParams.set('v', v);
  return u.href;
}
