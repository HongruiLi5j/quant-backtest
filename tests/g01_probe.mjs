// G01 探针：在 node 里用最少的假浏览器全局对象，真的导入 shared/stock-link.js、shared/t-fallback.js、pages/U1-common.js，
// 把函数的输出以 JSON 打印给 tests/test_ux_g01.py 断言。不联网、不读 data/；只读 dashboard/ui 下的字典文件。
// 用法：node g01_probe.mjs <dashboard/ui 绝对路径> <base|sentinel>
//   base     = 基础字典原样（A02 词条还没合并，检验兜底文字）
//   sentinel = 往中文基础字典里塞一个特殊值，检验「字典里有词条时优先用字典」
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';

const [ui, mode = 'base'] = process.argv.slice(2);
const mem = new Map();
globalThis.localStorage = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, String(v)) };
globalThis.window = { location: { search: '?mock=1', hostname: '127.0.0.1' }, matchMedia: () => ({ matches: false, addEventListener() {} }), dispatchEvent() {} };
globalThis.CustomEvent = class { constructor(type, opt) { this.type = type; this.detail = opt?.detail; } };
globalThis.document = { addEventListener() {}, documentElement: { dataset: {}, lang: '' }, querySelectorAll: () => [] };
globalThis.fetch = async (url) => {
  const file = new URL(url).pathname;
  const body = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (mode === 'sentinel' && file.endsWith('/i18n/zh.json')) body['a02.sep.list'] = '§';
  return { ok: true, json: async () => body };
};

const load = (rel) => import(pathToFileURL(`${ui}/${rel}`).href);
const i18n = await load('shared/i18n.js');
const { stockPage, rememberStockStrategy } = await load('shared/stock-link.js');
const { tDefault } = await load('shared/t-fallback.js');
const A02_KEYS = Object.keys(JSON.parse(fs.readFileSync(`${ui}/i18n/parts/A02.zh.json`, 'utf8')));
const { pageHref, stockHref } = await load('pages/U1-common.js');

const out = { links: {}, fallback: {}, store: {} };
out.links.mock = stockPage('600000', 'cn');
out.links.mockNoMarket = stockPage('600000');
out.links.u1Default = stockHref('600222');
out.links.u1Us = stockHref('SPY', 'us');
out.links.weird = stockPage('x&mock=0#y/../', 'cn');
out.links.pageMock = pageHref('signals.html', { sg_t: 'blend2', sg_k: 'buy' });
out.links.pagePlainMock = pageHref('signals.html');
window.location.search = '';
out.links.live = stockPage('600000', 'cn');
out.links.livePage = pageHref('signals.html', { sg_t: 'blend2', sg_k: 'buy' });
out.links.livePagePlain = pageHref('signals.html');

rememberStockStrategy('rsi2');
out.store = { newKey: localStorage.getItem('qb-u2-stock-strategy'), oldKey: localStorage.getItem('dStrategy') };
rememberStockStrategy('');
out.store.afterEmpty = localStorage.getItem('qb-u2-stock-strategy');

for (const lang of ['zh', 'en']) {
  await i18n.setLang(lang);
  out.fallback[lang] = Object.fromEntries(A02_KEYS.map((k) => [k, tDefault(k)]));
  out.fallback[`${lang}_vars`] = tDefault('a02.fmt.name_code', { name: '示例银行', code: '600000' });
  out.fallback[`${lang}_unknown`] = tDefault('zz.not.a.key');
  out.fallback[`${lang}_existing`] = tDefault('u2.bt.run');
}
console.log(JSON.stringify(out));
