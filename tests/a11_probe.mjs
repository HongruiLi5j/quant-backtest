// A11 探针：在 node 里用极简假 DOM 真的跑 shared/a11y.js 的四个工具，以及持仓页「录入确认区」「撤销确认」两处页内确认的焦点行为
// （pages/holdings-form.js 原文件，不换桩）。读出「焦点在哪个元素」「朗读标签」「确认区是否收起」，以 JSON 打印给 tests/test_ui_a11y_static.py 断言。
// 不联网、不读 data/；字典走 file: 地址从 ui 目录读，merge=1 时再叠上 A11 词条（模拟早上合并）。
// 用法：node --experimental-default-type=module a11_probe.mjs <ui 目录> <merge:0|1>
import fs from 'node:fs';

const [ui, merge] = process.argv.slice(2);
const uiUrl = new URL(`file://${ui}/`);

/* ---------- 极简假 DOM：带 focus() / isConnected / select 选项 ---------- */
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
  get children() { return this.kids.filter((k) => k instanceof FEl); }   // 真 DOM 的 children 只含元素节点（askVoid 靠它记住原来的按钮）
  get options() { return this.descendants().filter((k) => k.tag === 'option'); }
  get value() { return this.tag === 'select' ? (this._value ?? this.options[0]?.attrs.value ?? '') : (this._value ?? this.attrs.value ?? ''); }
  set value(v) { this._value = v; }
  get isConnected() { let n = this; while (n.parent) { if (!n.parent.kids.includes(n)) return false; n = n.parent; } return true; }
  addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); }
  fire(type, extra = {}) {
    const ev = { type, target: this, key: '', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, ...extra };
    for (const fn of this.listeners[type] ?? []) fn(ev);
    return ev;
  }
  click() { if (!this.disabled) this.fire('click'); }
  focus() { globalThis.document.activeElement = this; }
  descendants() { return this.kids.flatMap((k) => (k instanceof FEl ? [k, ...k.descendants()] : [])); }
}
globalThis.Node = FNode;
globalThis.document = { createElement: (t) => new FEl(t), createTextNode: (s) => new FText(s), addEventListener() {},
  documentElement: { dataset: {}, lang: '' }, querySelectorAll: () => [], activeElement: null };
globalThis.window = { location: { pathname: '/w/holdings.html', search: '' }, dispatchEvent() {}, addEventListener() {} };
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };

let withMerge = merge === '1';
// i18n 部件已退役（parts/ 移到 _to_delete/，词条全部在基础字典里）：「合并前」= 基础字典去掉该任务的键（键名单见 tests/fixtures/i18n_parts_keys.json）
const PART_KEYS = JSON.parse(fs.readFileSync(new URL('./fixtures/i18n_parts_keys.json', import.meta.url), 'utf8'));
const without = (d, keys) => Object.fromEntries(Object.entries(d).filter(([k]) => !keys.includes(k)));
globalThis.fetch = async (path) => {
  const s = String(path);
  const lang = s.endsWith('/en.json') ? 'en' : 'zh';
  const base = JSON.parse(fs.readFileSync(new URL(s), 'utf8'));
  const body = withMerge ? base : without(base, PART_KEYS.A11);
  return { ok: true, status: 200, json: async () => body };
};

const i18n = await import(new URL('shared/i18n.js', uiUrl));
const a11y = await import(new URL('shared/a11y.js', uiUrl));
const form = await import(new URL('pages/holdings-form.js', uiUrl));
const out = {};
const name = (n) => (n ? `${n.tag}:${n.textContent}` : null);
const all = (root) => root.descendants();

for (const lang of ['zh', 'en']) {
  await i18n.setLang(lang);

  /* 1. 朗读标签：词条没合并时不加（不露键名），合并后按语言出 */
  const b = new FEl('button');
  a11y.setAriaLabel(b, 'a11y.watch_add', { n: '豫光金铅' });
  out[`${lang}.label`] = b.getAttribute('aria-label');
  out[`${lang}.tMaybe_missing`] = a11y.tMaybe('a11y.no_such_key');

  /* 2. activateOnKey：Enter / 空格触发；其他键、带修饰键、焦点在行内别的元素上都不触发 */
  const row = new FEl('tr'), inner = new FEl('a');
  row.append(inner);
  let hits = 0;
  a11y.activateOnKey(row, () => { hits += 1; });
  const enter = row.fire('keydown', { key: 'Enter' });
  const space = row.fire('keydown', { key: ' ' });
  row.fire('keydown', { key: 'a' });
  row.fire('keydown', { key: 'Enter', shiftKey: true });
  row.fire('keydown', { key: ' ', metaKey: true });
  const afterNoise = hits;
  for (const fn of row.listeners.keydown) fn({ target: inner, key: 'Enter', preventDefault() {} });   // 事件从行内的链接冒泡上来
  out[`${lang}.activate`] = { hits: afterNoise, hitsAfterInner: hits, enterPrevented: enter.defaultPrevented, spacePrevented: space.defaultPrevented };

  /* 3. restoreFocusOnClose：关闭后焦点回到打开前的元素；元素已不在页面上就不动 */
  const dlg = new FEl('dialog'), opener = new FEl('button'), page = new FEl('div');
  page.append(opener);
  const remember = a11y.restoreFocusOnClose(dlg);
  opener.focus(); remember();
  document.activeElement = null;
  dlg.fire('close');
  out[`${lang}.restore`] = name(document.activeElement);
  const dlg2 = new FEl('dialog'), orphan = new FEl('button'), page2 = new FEl('div');
  page2.append(orphan);
  const remember2 = a11y.restoreFocusOnClose(dlg2);
  orphan.focus(); remember2();
  page2.replaceChildren();   // 打开弹层期间，原来的按钮被页面重绘掉了
  document.activeElement = null;
  dlg2.fire('close');
  out[`${lang}.restore_orphan`] = document.activeElement;

  /* 4. closeOnEscape */
  const box = new FEl('div'); let esc = 0;
  a11y.closeOnEscape(box, () => { esc += 1; });
  box.fire('keydown', { key: 'x' });
  const e2 = box.fire('keydown', { key: 'Escape' });
  out[`${lang}.escape`] = { esc, prevented: e2.defaultPrevented };

  /* 5. 持仓页录入：提交 → 确认区出现、焦点进入「返回修改」；Esc 收起、焦点回「提交」 */
  const f = form.buildForm({ msg: { show() {}, clear() {} }, reload: async () => {}, getSnap: () => null });
  const nodes = f.node.descendants();
  const byId = (id) => nodes.find((n) => n.id === id);
  byId('holdCode').value = '600531'; byId('holdQty').value = '100'; byId('holdPrice').value = '8.5';
  f.node.fire('submit');
  const box2 = byId('holdConfirm');
  out[`${lang}.confirm_open`] = { hidden: box2.hidden, role: box2.getAttribute('role'), labelledby: box2.getAttribute('aria-labelledby'),
    titleExists: all(box2).some((n) => n.id === box2.getAttribute('aria-labelledby')), focus: name(document.activeElement) };
  box2.fire('keydown', { key: 'Escape' });
  out[`${lang}.confirm_esc`] = { hidden: box2.hidden, kids: box2.kids.length, focusIsSubmit: document.activeElement === byId('holdSubmit') };
  f.node.fire('submit');
  all(box2).find((n) => n.id === 'holdConfirmBack').click();
  out[`${lang}.confirm_back`] = { hidden: box2.hidden, focusIsSubmit: document.activeElement === byId('holdSubmit') };

  /* 6. 持仓页历史：点「撤销」→ 焦点落在「取消」，Esc / 取消都换回原按钮并还焦点 */
  const snap = { trades: [{ id: 'T7', trade_date: '2026-03-02', code: '600531', name: '豫光金铅', side: 'buy', qty: 100, price: 8.5, fee: 5, voided: false }] };
  const card = form.historyCard(snap, { includeVoided: false, msg: { show() {} }, reload: async () => {}, setVoided() {} });
  const cell = all(card).filter((n) => n.tag === 'td').at(-1);
  const voidBtn = cell.kids[0];
  out[`${lang}.void_label`] = voidBtn.getAttribute('aria-label');
  voidBtn.click();
  const cancel = cell.kids.find((k) => k.tag === 'button' && k.getAttribute('aria-describedby'));
  const ask = cell.kids[0];
  out[`${lang}.void_open`] = { kids: cell.kids.length, focus: name(document.activeElement), describedbyMatches: cancel.getAttribute('aria-describedby') === ask.id };
  cell.fire('keydown', { key: 'Escape' });
  out[`${lang}.void_esc`] = { kids: cell.kids.length, restored: cell.kids[0] === voidBtn, focusIsOriginal: document.activeElement === voidBtn };
  voidBtn.click();
  cell.kids.find((k) => k.tag === 'button' && k.getAttribute('aria-describedby')).click();
  out[`${lang}.void_cancel`] = { restored: cell.kids[0] === voidBtn, focusIsOriginal: document.activeElement === voidBtn };
  cell.fire('keydown', { key: 'Escape' });   // 已经换回去了：再按 Esc 不应有副作用
  out[`${lang}.void_esc_again_ok`] = cell.kids[0] === voidBtn && cell.kids.length === 1;
}
console.log(JSON.stringify(out));
