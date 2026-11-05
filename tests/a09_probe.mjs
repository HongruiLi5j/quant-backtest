// A09 探针：在 node 里用极简假 DOM + 假接口真的渲染回测实验室（pages/backtest.js 原文件），
// 模拟敲键盘 / 选下拉 / 点按钮，读出「提示文字」「按钮是否禁用」「发给 /api/backtest 的参数」，以 JSON 打印给 tests/test_backtest_validate_a09.py 断言。
// 不联网、不读 data/。结果区两个模块（backtest-result.js / backtest-analysis.js）由测试换成空桩，只测表单。
// 用法：node --experimental-default-type=module a09_probe.mjs <临时 ui 目录> <meta.json> <merge:0|1>
import fs from 'node:fs';

const [ui, metaPath, merge] = process.argv.slice(2);
const uiUrl = new URL(`file://${ui}/`);
const META = JSON.parse(fs.readFileSync(metaPath, 'utf8'));

/* ---------- 极简假 DOM（比 a08_probe 多了 classList / removeAttribute / validity / 嵌套 option；disabled 的按钮 click() 不触发）---------- */
class FNode { constructor() { this.parent = null; this.kids = []; } }
class FText extends FNode { constructor(s) { super(); this.s = String(s); } get textContent() { return this.s; } }
class FEl extends FNode {
  constructor(tag) {
    super();
    this.tag = tag; this.attrs = {}; this.dataset = {}; this.listeners = {}; this.hidden = false; this.disabled = false;
    this.className = ''; this._value = null; this._bad = false;
  }
  setAttribute(k, v) { this.attrs[k] = v; if (k === 'hidden') this.hidden = true; }
  removeAttribute(k) { delete this.attrs[k]; }
  getAttribute(k) { return this.attrs[k] ?? null; }
  get classList() {
    const self = this;
    return {
      toggle(c, on) { const s = new Set(self.className.split(/\s+/).filter(Boolean)); (on ? s.add(c) : s.delete(c)); self.className = [...s].join(' '); },
      contains(c) { return self.className.split(/\s+/).includes(c); },
    };
  }
  get validity() { return { badInput: this._bad }; }
  get id() { return this.attrs.id; }   // 真 DOM 里 .id 反射 id 属性
  append(...c) { for (const y of c) { const x = y instanceof FNode ? y : new FText(y); x.parent = this; this.kids.push(x); } }   // 和真 DOM 一样：字符串当文本节点
  replaceChildren(...c) { this.kids = []; this.append(...c); }
  set textContent(s) { this.replaceChildren(new FText(s)); }
  get textContent() { return this.kids.map((k) => k.textContent).join(''); }
  get options() { return this.descendants().filter((k) => k.tag === 'option'); }
  get value() {
    if (this.tag === 'select') return this._value ?? (this.options[0]?.attrs.value ?? '');
    return this._value ?? this.attrs.value ?? '';
  }
  set value(v) {
    if (this.tag === 'select') this._value = this.options.some((o) => o.attrs.value === v) ? v : '';
    else this._value = v;
  }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  fire(type, extra = {}) { for (const fn of this.listeners[type] ?? []) fn({ type, target: this, key: '', ...extra }); }
  click() { if (!this.disabled) this.fire('click'); }   // 真浏览器里 disabled 按钮的 click() 什么也不做
  descendants() { return this.kids.flatMap((k) => (k instanceof FEl ? [k, ...k.descendants()] : [])); }
}
globalThis.Node = FNode;
globalThis.document = { createElement: (t) => new FEl(t), createTextNode: (s) => new FText(s), addEventListener() {},
  documentElement: { dataset: {}, lang: '' }, querySelectorAll: () => [] };
const loc = { pathname: '/w/backtest.html', search: '' };
globalThis.window = { location: loc, dispatchEvent() {}, addEventListener() {} };
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };

/* ---------- 假接口：/api/meta 返回测试传入的 meta（来自真实 registry）；/api/backtest 记录参数并返回固定的最新数据日 ---------- */
const DATA_END = '2026-10-23';
const calls = [];
let withMerge = merge === '1';
globalThis.fetch = async (path) => {
  const s = String(path);
  const ok = (body) => ({ ok: true, status: 200, json: async () => body });
  if (s.startsWith('file:')) {   // i18n 字典
    const lang = s.endsWith('/en.json') ? 'en' : 'zh';
    const base = JSON.parse(fs.readFileSync(new URL(s), 'utf8'));
    const part = withMerge ? JSON.parse(fs.readFileSync(`${ui}/i18n/parts/A09.${lang}.json`, 'utf8')) : {};
    return ok({ ...base, ...part });
  }
  const [p, qs = ''] = s.split('?');
  if (p === '/api/meta') return ok(META);
  if (p === '/api/backtest') {
    const q = Object.fromEntries(new URLSearchParams(qs));
    calls.push(q);
    return ok({ strategy: q.strategy, symbol: q.symbol, start: '2020-01-02', end: DATA_END });
  }
  return ok({});
};

const i18n = await import(new URL('shared/i18n.js', uiUrl));
await i18n.setLang('zh');

/* ---------- 工具 ---------- */
let runNo = 0;
async function open(search) {   // 每个场景一份全新的页面模块（模块变量 form / seeded / dataEnds 重新初始化）
  loc.search = search;
  calls.length = 0;
  const mod = await import(new URL(`pages/backtest.js?run=${++runNo}`, uiUrl));
  const root = new FEl('div');
  await mod.render(root);
  return { mod, root };
}
const all = (root) => root.descendants();
const labels = (root) => all(root).filter((e) => e.tag === 'label' && e.className.includes('u2-field'));
const ctlOf = (l) => l.kids.find((k) => k.tag === 'input' || k.tag === 'select');
const slotOf = (l) => l.kids.find((k) => k.className?.includes('u2-field__msg'));
const find = (root, key) => {   // key: 'symbol' | 'start' | 'cost' | 参数 key（页面上两个下拉依次是策略、品种）
  const ls = labels(root);
  const l = key === 'symbol' ? ls.filter((x) => ctlOf(x).tag === 'select')[1]
    : ls.find((x) => { const c = ctlOf(x); return key === 'start' ? c.attrs.type === 'date' : key === 'cost' ? c.attrs.type === 'number' && !c.dataset.key : c.dataset?.key === key; });
  return { label: l, input: ctlOf(l), slot: slotOf(l) };
};
const selects = (root) => all(root).filter((e) => e.tag === 'select');
const runBtn = (root) => all(root).find((e) => e.tag === 'button' && e.className.includes('btn--primary'));
const resetBtn = (root) => all(root).filter((e) => e.tag === 'button')[1];
const blockEl = (root) => all(root).find((e) => e.className === 'u2-err' && !e.attrs.role);
const msgEl = (root) => all(root).find((e) => e.className === 'u2-err' && e.attrs.role === 'alert');
const type = (inp, v, bad = false) => { inp.value = v; inp._bad = bad; inp.fire('input'); };
const snap = (root, keys) => ({
  run_disabled: runBtn(root).disabled,
  block: blockEl(root).textContent,
  fields: Object.fromEntries(keys.map((k) => {
    const f = find(root, k);
    return [k, { msg: f.slot.textContent, cls: f.slot.className, invalid: f.input.className.includes('is-invalid'), aria: f.input.attrs['aria-invalid'] ?? null,
      describedby: f.input.attrs['aria-describedby'] === f.slot.attrs.id }];
  })),
});
const out = {};

/* ===== 1. 首次渲染（默认值全部合法）：自动跑了一次；按钮可用；开始日期旁出现「算到最新数据日」说明 ===== */
{
  const { root } = await open('');
  out.initial = { ...snap(root, ['symbol', 'start', 'cost']), calls: calls.length, first_call: { ...calls[0] }, strategy: calls[0]?.strategy };
}

/* ===== 2. 单边成本：越界 / 非数字 / 空 / 小数 / 边界 ===== */
{
  const { root } = await open('');
  const c = find(root, 'cost');
  const cases = {};
  for (const [name, v, bad] of [['over', '150', false], ['neg', '-1', false], ['text', '', true], ['blank', '', false], ['dec', '0.5', false], ['top', '100', false], ['zero', '0', false], ['sci', '1e2', false]]) {
    type(c.input, v, bad);
    cases[name] = { disabled: runBtn(root).disabled, msg: c.slot.textContent, invalid: c.input.className.includes('is-invalid') };
  }
  out.cost = cases;
  // 越界时：点按钮（被禁用）和绕过禁用直接触发 click，都不能发请求
  type(c.input, '150');
  const before = calls.length;
  runBtn(root).click();
  runBtn(root).fire('click');
  out.cost_blocked_calls = calls.length - before;
  // 改好之后能发请求，带的是合法的成本
  type(c.input, '12');
  runBtn(root).click();
  await new Promise((r) => setTimeout(r, 5));
  out.cost_fixed = { calls: calls.length - before, cost: calls.at(-1)?.cost, disabled: runBtn(root).disabled };
}

/* ===== 3. 开始日期：太晚 / 等于最新数据日 / 区间太短 / 正常 / 半截日期 / 日期不存在 ===== */
{
  const { root } = await open('');
  const s = find(root, 'start');
  const cases = {};
  for (const [name, v, bad] of [['late', '2026-12-31', false], ['equal', DATA_END, false], ['short', '2026-10-01', false], ['ok', '2020-01-01', false], ['blank', '', false], ['half', '', true], ['feb30', '2026-02-30', false]]) {
    type(s.input, v, bad);
    cases[name] = { disabled: runBtn(root).disabled, msg: s.slot.textContent, cls: s.slot.className, invalid: s.input.className.includes('is-invalid') };
  }
  out.start = cases;
  type(s.input, '2020-01-01');
  const before = calls.length;
  runBtn(root).click();
  await new Promise((r) => setTimeout(r, 5));
  out.start_sent = { n: calls.length - before, start: calls.at(-1)?.start };
}

/* ===== 4. 策略参数：换策略后按真实 registry 的范围逐项校验；清空不再被当成 0 发出去 ===== */
{
  const { root } = await open('');
  const strat = selects(root)[0];
  strat.value = 'ma_adx_original';
  strat.fire('change');
  await new Promise((r) => setTimeout(r, 5));
  out.params_after_switch = { calls: calls.length, strategy: calls.at(-1)?.strategy, disabled: runBtn(root).disabled, keys: labels(root).map((l) => ctlOf(l).dataset?.key).filter(Boolean) };
  const fast = find(root, 'fast'), slow = find(root, 'slow');
  const cases = {};
  for (const [name, v, bad] of [['blank', '', false], ['text', '', true], ['frac', '2.5', false], ['low', '1', false], ['high', '51', false], ['min', '2', false], ['max', '50', false], ['ok', '10', false]]) {
    type(fast.input, v, bad);
    cases[name] = { disabled: runBtn(root).disabled, msg: fast.slot.textContent, invalid: fast.input.className.includes('is-invalid') };
  }
  out.fast = cases;
  // 清空后不应把 0 写进 form.params：先清空（挡住），再改回 10，发出去的 fast 必须是 10
  type(fast.input, '');
  const before = calls.length;
  fast.label.parent.fire('keydown', { key: 'Enter' });   // 参数区的 keydown 监听挂在容器上：有错时按 Enter 不发请求
  out.enter_blocked_calls = calls.length - before;
  type(fast.input, '10');
  runBtn(root).click();
  await new Promise((r) => setTimeout(r, 5));
  out.fast_sent = { n: calls.length - before, params: JSON.parse(calls.at(-1)?.params ?? '{}') };
  // 快线 ≥ 慢线：只提醒，不挡
  type(fast.input, '30'); type(slow.input, '20');
  out.fast_ge_slow = { disabled: runBtn(root).disabled, slow_msg: slow.slot.textContent, slow_cls: slow.slot.className, fast_msg: fast.slot.textContent };
  // 恢复默认：回到合法状态，错误清掉
  type(fast.input, '');
  resetBtn(root).click();
  await new Promise((r) => setTimeout(r, 5));
  const fast2 = find(root, 'fast');
  out.after_reset = { disabled: runBtn(root).disabled, msg: fast2.slot.textContent, value: fast2.input.value };
}

/* ===== 5. 网址带来的品种：格式不对不发请求；换成列表里的品种后恢复 ===== */
{
  const { root } = await open('?symbol=abc');
  out.bad_symbol = { ...snap(root, ['symbol']), calls: calls.length };
  const sym = selects(root)[1];
  sym.value = META.markets.cn.main;
  sym.fire('change');
  await new Promise((r) => setTimeout(r, 5));
  out.bad_symbol_fixed = { ...snap(root, ['symbol']), calls: calls.length, symbol: calls.at(-1)?.symbol };
  const ok = await open('?symbol=600519');   // 6 位且 60 开头，格式合法（后端还会查有没有数据）
  out.ok_symbol = { ...snap(ok.root, ['symbol']), calls: calls.length };
  const us = await open('?symbol=ZZZ99999999&market=us');
  out.us_symbol = { calls: calls.length, ...snap(us.root, ['symbol']) };
}

/* ===== 6. 语言：英文提示；词条没合并时不露键名 ===== */
{
  await i18n.setLang('en');
  const { root } = await open('');
  const c = find(root, 'cost');
  type(c.input, '150');
  out.en = { msg: c.slot.textContent, block: blockEl(root).textContent, disabled: runBtn(root).disabled };
  await i18n.setLang('zh');
}
console.log(JSON.stringify(out));
