// A13 探针：在 node 里直接 import shared/palette-match.js（纯函数，不需要假 DOM），跑一批用例，
// 把结果以 JSON 打印给 tests/test_palette_match_a13.py 断言。不联网、不读 data/，用的全是下面写死的假数据。
// 用法：node --experimental-default-type=module a13_probe.mjs <ui 目录>
const [ui] = process.argv.slice(2);
const mod = await import(new URL(`file://${ui}/shared/palette-match.js`));
const { rankStocks, stockRank, normalizeQuery, RANK } = mod;

const codes = (rows) => rows.map((r) => r.code);
const out = { RANK, exports: Object.keys(mod).sort() };

/* 服务端返回顺序：ETF（代码包含命中）排在全市场清单（代码前缀命中）前面，和 app.py search() 的拼接顺序一致 */
const server = [
  { code: '510300', name: '沪深300ETF', kind: 'ETF' },
  { code: '600531', name: '豫光金铅', kind: '沪市主板' },
  { code: '300750', name: '宁德时代', kind: '创业板' },
  { code: '600519', name: '贵州茅台', kind: '沪市主板' },
  { code: '600036', name: '招商银行', kind: '沪市主板' },
  { code: '000001', name: '平安银行', kind: '深市主板' },
];
const snapshot = JSON.stringify(server);

out.prefix600 = codes(rankStocks('600', server, 8));
out.prefix600Limit2 = codes(rankStocks('600', server, 2));
out.contains300 = codes(rankStocks('300', server, 8));
out.fullwidth = codes(rankStocks('６００', server, 8));
out.padded = codes(rankStocks('  600  ', server, 8));
out.emptyQuery = codes(rankStocks('', server, 3));
out.nonArray = [rankStocks('600', null), rankStocks('600', undefined), rankStocks('600', 'abc')];
out.notMutated = JSON.stringify(server) === snapshot;
out.keepsAllRows = rankStocks('600', server, 99).length === server.length;

/* 服务端最多返回 15 条：第 13 条是前缀命中，也必须排到前 8 条里（先排序再截断） */
const fifteen = Array.from({ length: 15 }, (_, i) => ({ code: `51${String(i).padStart(4, '0')}`, name: `ETF${i}` }));
fifteen[12] = { code: '600888', name: '新疆众和' };
out.lateHit = codes(rankStocks('600', fifteen, 8))[0];
out.lateHitNotDropped = codes(rankStocks('600', fifteen, 8)).includes('600888');

/* 同档保持服务端顺序（稳定排序） */
out.stableOrder = codes(rankStocks('60', [
  { code: '600003', name: 'a' }, { code: '600001', name: 'b' }, { code: '600002', name: 'c' },
], 8));

/* 档位 */
const q = normalizeQuery;
out.ranks = {
  exact: stockRank(q('600519'), { code: '600519', name: '贵州茅台' }),
  prefix: stockRank(q('6005'), { code: '600519', name: '贵州茅台' }),
  name: stockRank(q('茅台'), { code: '600519', name: '贵州茅台' }),
  nameInEtf300: stockRank(q('300'), { code: '510300', name: '沪深300ETF' }),
  codeContains: stockRank(q('300'), { code: '510300', name: '某基金' }),
  other: stockRank(q('xyz'), { code: '600519', name: '贵州茅台' }),
  upperQuery: stockRank(q('SPY'), { code: 'spy', name: 'SPDR S&P 500' }),
  upperCode: stockRank(q('spy'), { code: 'SPY', name: 'SPDR S&P 500' }),
  emptyQuery: stockRank('', { code: '600519', name: '贵州茅台' }),
};
/* 拼音首字母：当前接口行里没有拼音字段，gzmt 不命中贵州茅台（落到 OTHER，行仍保留）。后端补字段后这条断言要连同实现一起改 */
out.pinyinNotSupported = {
  rank: stockRank(q('gzmt'), { code: '600519', name: '贵州茅台' }),
  kept: codes(rankStocks('gzmt', [{ code: '600519', name: '贵州茅台' }], 8)),
};

/* 异常输入不抛错 */
out.weird = {
  noCode: stockRank(q('600'), { name: '无代码' }),
  nullRow: stockRank(q('600'), null),
  numericCode: stockRank(q('600'), { code: 600519, name: 'x' }),
  nonStringQuery: [normalizeQuery(null), normalizeQuery(600), normalizeQuery(undefined)],
};
out.normalize = [q('  ＳＰＹ '), q('６００'), q('ABC')];

console.log(JSON.stringify(out));
