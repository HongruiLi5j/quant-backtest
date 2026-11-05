/* ==========================================================
 * 文件: ui/shared/palette-match.js
 * 用途: 命令面板「股票条目」的匹配/排序纯函数（A13）。
 *       不 import 任何模块、不碰 DOM、不联网，node 里可以直接 import 做测试。
 *
 * 为什么只排序、不过滤：
 *   股票条目是服务端 /api/search 按输入查出来的，服务端已经决定了"哪些算命中"；
 *   前端再过滤一遍，一旦两边规则有细微差别（大小写、全角半角）就会把服务端的合法结果吞掉。
 *   所以这里只做一件事：把"代码前缀命中"排到最前面，再截断到要显示的条数。
 *   （服务端对 ETF / 自选股用的是"代码包含"，对全市场清单才是"代码前缀"；
 *    不排序的话，输入 300 时 510300 这类"包含"命中会挤掉 300xxx 的前缀命中。）
 *
 * 拼音首字母：当前 /api/search 返回的行里没有拼音字段（只有 code / name / kind），
 *   本模块不做拼音匹配；需要后端补字段，见 docs/A13_palette_search.md。
 * ========================================================== */

/** 排序档位：数字越小越靠前 */
export const RANK = Object.freeze({ EXACT: 0, PREFIX: 1, NAME: 2, CODE_CONTAINS: 3, OTHER: 4 });

/**
 * 归一化查询：NFKC（全角数字/字母转半角，如 "６００" → "600"）、去首尾空白、转小写。
 * 非字符串当空串处理。
 */
export function normalizeQuery(q) {
  return typeof q === 'string' ? q.normalize('NFKC').trim().toLowerCase() : '';
}

/**
 * 一行股票对查询的匹配档位。
 * @param {string} q    已经 normalizeQuery 过的查询
 * @param {{code?: string, name?: string}} row
 */
export function stockRank(q, row) {
  const code = String(row?.code ?? '').normalize('NFKC').toLowerCase();
  const name = String(row?.name ?? '').normalize('NFKC').toLowerCase();
  if (!q) return RANK.OTHER;
  if (code === q) return RANK.EXACT;
  if (code.startsWith(q)) return RANK.PREFIX;
  if (name.includes(q)) return RANK.NAME;
  if (code.includes(q)) return RANK.CODE_CONTAINS;
  return RANK.OTHER;
}

/**
 * 按匹配档位稳定排序（同档保持服务端给的顺序），再取前 limit 条。
 * 先排序再截断：服务端最多返回 15 条，前缀命中不能因为排在后面被 slice 丢掉。
 * 不会丢弃任何行（只是 OTHER 档排最后）；不修改传入的数组。
 */
export function rankStocks(query, rows, limit = 8) {
  if (!Array.isArray(rows)) return [];
  const q = normalizeQuery(query);
  if (!q) return rows.slice(0, limit);
  return rows
    .map((row, i) => ({ row, i, r: stockRank(q, row) }))
    .sort((a, b) => a.r - b.r || a.i - b.i)
    .slice(0, limit)
    .map((x) => x.row);
}
