// A15 探针：在 node 里用极简假 DOM 真的跑 pages/holdings-rules.js（纯函数）和 pages/holdings-form.js 的录入表单（原文件，不换桩）。
// 记录：规则函数的返回值；表单里费用占位、整手提示、非法输入时有没有出确认摘要 / 标红 / 焦点；以 JSON 打印给 tests/test_holdings_form_a15.py 断言。
// 不联网、不读 data/；字典走 file: 地址从 ui 目录读，merge=1 时再叠上 A15 词条（模拟早上合并）。
// 用法：node --experimental-default-type=module a15_probe.mjs <ui 目录> <merge:0|1>
import fs from 'node:fs';

const [ui, merge] = process.argv.slice(2);
const uiUrl = new URL(`file://${ui}/`);

/* ---------- 极简假 DOM ---------- */
class FNode { constructor() { this.parent = null; this.kids = []; } }
class FText extends FNode { constructor(s) { super(); this.s = String(s); } get textContent() { return this.s; } }
class FEl extends FNode {
  constructor(tag) {
    super();
    this.tag = tag; this.attrs = {}; this.dataset = {}; this.listeners = {}; this.hidden = false; this.disabled = false;
    this.className = ''; this._value = null; this.id = '';
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); if (k === 'hidden') this.hidden = true; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  append(...c) { for (const y of c) { const x = y instanceof FNode ? y : new FText(y); x.parent = this; this.kids.push(x); } }
  replaceChildren(...c) { this.kids = []; this.append(...c); }
  set textContent(s) { this.replaceChildren(new FText(s)); }
  get textContent() { return this.kids.map((k) => k.textContent).join(''); }
  get children() { return this.kids.filter((k) => k instanceof FEl); }
  get options() { return this.descendants().filter((k) => k.tag === 'option'); }
  get value() { return this.tag === 'select' ? (this._value ?? this.options[0]?.attrs.value ?? '') : (this._value ?? this.attrs.value ?? ''); }
  set value(v) { this._value = v; }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  fire(type, extra = {}) {
    const ev = { type, target: this, key: '', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, ...extra };
    for (const fn of this.listeners[type] ?? []) fn(ev);
    return ev;
  }
  focus() { globalThis.document.activeElement = this; }
  descendants() { return this.kids.flatMap((k) => (k instanceof FEl ? [k, ...k.descendants()] : [])); }
}
globalThis.Node = FNode;
globalThis.document = { createElement: (t) => new FEl(t), createTextNode: (s) => new FText(s), addEventListener() {},
  documentElement: { dataset: {}, lang: '' }, querySelectorAll: () => [], activeElement: null };
globalThis.window = { location: { pathname: '/w/holdings.html', search: '' }, dispatchEvent() {}, addEventListener() {} };
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };

const withMerge = merge === '1';
globalThis.fetch = async (path) => {
  const s = String(path);
  if (s.startsWith('/api/search')) return { ok: true, status: 200, json: async () => [] };
  const lang = s.endsWith('/en.json') ? 'en' : 'zh';
  const base = JSON.parse(fs.readFileSync(new URL(s), 'utf8'));
  const part = withMerge ? JSON.parse(fs.readFileSync(`${ui}/i18n/parts/A15.${lang}.json`, 'utf8')) : {};
  return { ok: true, status: 200, json: async () => ({ ...base, ...part }) };
};

const i18n = await import(new URL('shared/i18n.js', uiUrl));
const rules = await import(new URL('pages/holdings-rules.js', uiUrl));
const formMod = await import(new URL('pages/holdings-form.js', uiUrl));
const out = {};

/* ---------- 1. 纯函数（和语言、词条无关，只跑一次） ---------- */
out.clean = ['1,000', '1,234,567.89', '12,34', '1,00', ' 100 ', '1.5', '', '-1,000'].map((s) => [s, rules.cleanNumber(s)]);
const chk = (o) => rules.checkInputs({ side: 'buy', qty: '100', price: '10', fee: '', ...o }).map((x) => `${x.field}:${x.key}`);
out.check = {
  ok: chk({}),
  okFee: chk({ fee: '5' }),
  okFeeZero: chk({ fee: '0' }),
  okTrailingZeros: chk({ price: '10.5000', fee: '5.2000' }),
  qtyZero: chk({ qty: '0' }), qtyZeros: chk({ qty: '000' }), qtyNeg: chk({ qty: '-100' }), qtyFrac: chk({ qty: '100.5' }),
  qtyLetters: chk({ qty: '10a' }), qtySci: chk({ qty: '1e3' }), qtyMax: chk({ qty: '100000000' }), qtyOverMax: chk({ qty: '100000001' }),
  qtyHuge: chk({ qty: '99999999999' }),
  priceZero: chk({ price: '0' }), priceZeroDec: chk({ price: '0.000' }), priceNeg: chk({ price: '-1' }), priceSci: chk({ price: '1e3' }),
  priceDec4: chk({ price: '10.1234' }), priceDot: chk({ price: '10.' }), priceDotFirst: chk({ price: '.5' }), priceBig: chk({ price: '100001' }),
  priceMaxOk: chk({ price: '100000' }), priceLetters: chk({ price: 'abc' }),
  feeNeg: chk({ fee: '-1' }), feeDec3: chk({ fee: '5.123' }), feeLetters: chk({ fee: 'abc' }), feeOver: chk({ fee: '1001' }), feeEqAmount: chk({ fee: '1000' }),
  feeOverButQtyBad: chk({ qty: '0', fee: '999999' }),
  multi: chk({ qty: '0', price: '-1', fee: 'x' }),
  bonusIgnoresPriceFee: rules.checkInputs({ side: 'bonus', qty: '100', price: 'abc', fee: 'abc' }).map((x) => x.field),
  bonusQtyStillChecked: rules.checkInputs({ side: 'bonus', qty: '0', price: '', fee: '' }).map((x) => x.field),
  dividendPriceZero: rules.checkInputs({ side: 'dividend', qty: '500', price: '0', fee: '' }).map((x) => x.field),
  sellNoLotCheck: rules.checkInputs({ side: 'sell', qty: '37', price: '10', fee: '' }).map((x) => x.field),
  fallbacks: rules.checkInputs({ side: 'buy', qty: '0', price: '0', fee: 'x' }).map((x) => x.fallback),
};
const lot = (side, code, qty) => { const h = rules.lotHint({ side, code, qty }); return h ? `${h.key}|${h.warn}` : null; };
out.lot = {
  sell: lot('sell', '600000', '37'), sellEmpty: lot('sell', '', ''),
  dividend: lot('dividend', '600000', '100'), bonus: lot('bonus', '600000', '10'),
  buyNoCode: lot('buy', '', '150'), buyPartialCode: lot('buy', '6005', '150'), buyPartialStar: lot('buy', '688', '100'),
  buyOk: lot('buy', '600000', '100'), buyEmptyQty: lot('buy', '600000', ''), buyWarn: lot('buy', '600000', '150'),
  buyEtfOk: lot('buy', '510300', '300'), buyEtfWarn: lot('buy', '510300', '50'), buyEtf159: lot('buy', '159915', '1000'),
  buyGem: lot('buy', '300750', '100'), buyShenzhen: lot('buy', '000001', '99'),
  star200: lot('buy', '688981', '200'), star201: lot('buy', '688981', '201'), star100: lot('buy', '688981', '100'), star199: lot('buy', '688981', '199'),
  star689: lot('buy', '689009', '100'), starEmpty: lot('buy', '688981', ''),
  etf588: lot('buy', '588000', '100'), etf588Warn: lot('buy', '588000', '150'),
};

/* ---------- 2. 表单（每种语言各跑一遍） ---------- */
const name = (n) => (n ? `${n.tag}#${n.id}` : null);
for (const lang of ['zh', 'en']) {
  await i18n.setLang(lang);
  const f = formMod.buildForm({ msg: { show() {}, clear() {} }, reload: async () => {}, getSnap: () => null });
  const nodes = f.node.descendants();
  const byId = (id) => nodes.find((n) => n.id === id);
  const set = (id, v) => { byId(id).value = v; };
  const sideTo = (v) => { set('holdSide', v); byId('holdSide').fire('change'); };
  const typeQty = (code, qty) => { set('holdCode', code); byId('holdCode').fire('input'); set('holdQty', qty); byId('holdQty').fire('input'); };
  const view = () => ({
    feePlaceholder: byId('holdFee').getAttribute('placeholder'), feeHint: byId('holdFeeHint').textContent,
    qtyHint: byId('holdQtyHint').textContent, qtyWarn: byId('holdQtyHint').dataset.warn || '', priceHint: byId('holdPriceHint').textContent,
  });
  const submit = () => {
    f.node.fire('submit');
    const box = byId('holdConfirm');
    return { confirmOpen: !box.hidden, confirmText: box.hidden ? '' : box.textContent, msg: byId('holdFormMessage').textContent,
      msgCount: byId('holdFormMessage').kids.length, invalid: ['holdQty', 'holdPrice', 'holdFee'].filter((id) => byId(id).getAttribute('aria-invalid') === 'true'),
      focus: name(document.activeElement) };
  };
  const back = () => { const b = byId('holdConfirmBack'); if (b && !byId('holdConfirm').hidden) b.fire('click'); };

  out[`${lang}.static`] = {
    qtyType: byId('holdQty').getAttribute('type'), qtyMode: byId('holdQty').getAttribute('inputmode'), qtyDesc: byId('holdQty').getAttribute('aria-describedby'),
    priceDesc: byId('holdPrice').getAttribute('aria-describedby'), feeDesc: byId('holdFee').getAttribute('aria-describedby'),
    hintIds: ['holdQtyHint', 'holdPriceHint', 'holdFeeHint'].map((id) => !!byId(id)),
  };
  out[`${lang}.initial`] = view();

  typeQty('510300', '150'); out[`${lang}.etf150`] = view();
  typeQty('510300', '300'); out[`${lang}.etf300`] = view();
  typeQty('688981', '100'); out[`${lang}.star100`] = view();
  typeQty('688981', '200'); out[`${lang}.star200`] = view();
  sideTo('sell'); out[`${lang}.sell`] = view();
  sideTo('dividend'); out[`${lang}.dividend`] = view();
  sideTo('bonus'); out[`${lang}.bonus`] = view();
  sideTo('buy'); out[`${lang}.backToBuy`] = view();

  // 非法输入：不出确认摘要，逐项说明，标红，焦点到第一个出问题的框
  const bad = (qty, price, fee) => { set('holdCode', '600000'); set('holdQty', qty); set('holdPrice', price); set('holdFee', fee); const r = submit(); back(); return r; };
  out[`${lang}.bad_qty0`] = bad('0', '10', '');
  out[`${lang}.bad_qty_frac`] = bad('100.5', '10', '');
  out[`${lang}.bad_price_neg`] = bad('100', '-1', '');
  out[`${lang}.bad_price_zero`] = bad('100', '0', '');
  out[`${lang}.bad_price_dec`] = bad('100', '10.1234', '');
  out[`${lang}.bad_fee_neg`] = bad('100', '10', '-5');
  out[`${lang}.bad_fee_over`] = bad('100', '10', '1001');
  out[`${lang}.bad_multi`] = bad('0', '-1', 'x');
  // 输入框一改，对应的标红就清掉
  set('holdQty', '100'); byId('holdQty').fire('input');
  out[`${lang}.invalid_cleared`] = ['holdQty', 'holdPrice', 'holdFee'].filter((id) => byId(id).getAttribute('aria-invalid') === 'true');
  // 不完整：仍是原来的「请填写」提示（不被新校验抢走）
  out[`${lang}.incomplete`] = bad('', '10', '');
  // 合法：带千分位逗号也能过；费用留空
  out[`${lang}.ok_commas`] = (() => { set('holdCode', '600000'); set('holdQty', '1,000'); set('holdPrice', '10.5'); set('holdFee', ''); const r = submit(); back(); return r; })();
  out[`${lang}.ok_fee_zero`] = (() => { set('holdFee', '0'); const r = submit(); back(); return r; })();
  // 整手提示不拦截：买 150 股照样出确认摘要（真正的拒绝在后端）
  out[`${lang}.lot_not_blocking`] = (() => { set('holdCode', '600000'); set('holdQty', '150'); set('holdPrice', '10'); set('holdFee', ''); const r = submit(); back(); return r; })();
  // 分红：费用留空 = 0 元，确认摘要不能写「按费率估算」
  sideTo('dividend');
  out[`${lang}.dividend_confirm`] = (() => { set('holdCode', '600000'); set('holdQty', '500'); set('holdPrice', '0.3'); set('holdFee', ''); const r = submit(); back(); return r; })();
  // 切到送转：费用 / 价格被清空且不参与检查
  sideTo('bonus'); set('holdCode', '600000'); set('holdQty', '50');
  out[`${lang}.bonus_confirm`] = (() => { const r = submit(); back(); return r; })();
  // 买入的估算说明：确认摘要里仍是「按费率估算」
  sideTo('buy'); set('holdCode', '600000'); set('holdQty', '100'); set('holdPrice', '10'); set('holdFee', '');
  out[`${lang}.buy_confirm`] = (() => { const r = submit(); back(); return r; })();
  // restore（切换语言重画页面时放回输入框的值）后提示也跟着更新
  f.restore({ date: '2026-03-02', code: '688981', side: 'buy', qty: '100', price: '5', fee: '', note: '' });
  out[`${lang}.restored`] = view();
}
console.log(JSON.stringify(out));
