/* ==========================================================
 * 文件: ui/pages/holdings-form.js
 * 用途: 持仓页的「录入表单」和「历史成交」两块（从 holdings.js 拆出，保持单文件 ≤ 500 行）
 *   - 录入：先显示确认摘要，点「确认入账」才发 POST /api/holdings/add；client_id 保证同一次提交重试是幂等的
 *   - 撤销：点「撤销」后原地换成确认提示，再点才发 POST /api/holdings/void
 *   - 输入规则（A15）：数量 / 价格 / 费用的格式检查和整手提示在 holdings-rules.js；费用留空 = 后端自动估算（分红除外，记 0 元）
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { setAriaLabel, closeOnEscape, tMaybe } from '../shared/a11y.js';
import { num, table, td, span, errKey, post } from './u3-common.js';
import { cleanNumber, checkInputs, lotHint } from './holdings-rules.js';

const SIDES = ['buy', 'sell', 'bonus', 'dividend'];

/** 北京日期：浏览器不在东八区时也不会差一天 */
const today = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());

function newClientId() {
  const c = window.crypto || globalThis.crypto;
  if (c?.randomUUID) return c.randomUUID();
  return `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`.slice(0, 40).padEnd(8, '0');
}

/** 台账里已有的名称（持仓 / 成交 / 已清仓） */
function knownName(snap, code) {
  const hit = (snap?.positions || []).find((p) => p.code === code) || (snap?.trades || []).find((x) => x.code === code)
    || (snap?.closed || []).find((c) => c.code === code);
  return hit && hit.name && hit.name !== code ? hit.name : '';
}

function field(labelText, control, hint) {
  const text = el('span', {}, labelText);
  const hintNode = hint instanceof Node ? hint : hint ? el('small', { class: 'ws-note' }, hint) : null;
  const label = el('label', { class: 'u3-field' }, text, control, hintNode);
  return { label, text };
}

/** 返回 { node, capture, restore }：capture 取出输入框里的值，切换语言重画页面时用 restore 放回去 */
export function buildForm({ msg, reload, getSnap }) {
  let clientId = newClientId();
  let draft = null, busy = false, nameToken = 0;

  const date = el('input', { class: 'ws-input', type: 'date', id: 'holdDate', value: today(), required: true });
  const code = el('input', { class: 'ws-input', type: 'text', id: 'holdCode', inputmode: 'numeric', maxlength: 6, pattern: '[0-9]{6}', autocomplete: 'off', placeholder: t('hold.form.code') });
  const side = el('select', { class: 'ws-input', id: 'holdSide' }, ...SIDES.map((s) => el('option', { value: s }, t(`hold.side.${s}`))));
  // 数量 / 价格 / 费用用 text + inputmode：手机弹数字键盘，同时能接住粘贴来的「1,000」（type=number 会把它悄悄清空）；合法性由 holdings-rules.js 检查
  const qty = el('input', { class: 'ws-input', type: 'text', id: 'holdQty', inputmode: 'numeric', autocomplete: 'off', 'aria-describedby': 'holdQtyHint' });
  const price = el('input', { class: 'ws-input', type: 'text', id: 'holdPrice', inputmode: 'decimal', autocomplete: 'off', 'aria-describedby': 'holdPriceHint' });
  const fee = el('input', { class: 'ws-input', type: 'text', id: 'holdFee', inputmode: 'decimal', autocomplete: 'off', 'aria-describedby': 'holdFeeHint' });
  const qtyHint = el('small', { class: 'ws-note', id: 'holdQtyHint' });
  const priceHint = el('small', { class: 'ws-note', id: 'holdPriceHint' }, tMaybe('a15.price.hint'));
  const feeHint = el('small', { class: 'ws-note', id: 'holdFeeHint' });
  const note = el('input', { class: 'ws-input', type: 'text', id: 'holdNote', maxlength: 200 });
  const nameHint = el('small', { class: 'ws-note', id: 'holdNameHint' });

  const fDate = field(t('hold.form.date'), date), fCode = field(t('hold.form.code'), code), fSide = field(t('hold.form.side'), side);
  fCode.label.append(nameHint);
  const fQty = field(t('hold.form.qty'), qty, qtyHint), fPrice = field(t('hold.form.price'), price, priceHint);
  const fFee = field(t('hold.form.fee'), fee, feeHint), fNote = field(t('hold.form.note'), note);
  const grid = el('div', { class: 'u3-form-grid' }, fDate.label, fCode.label, fSide.label, fQty.label, fPrice.label, fFee.label, fNote.label);

  const submit = el('button', { class: 'btn btn--primary', type: 'submit', id: 'holdSubmit' }, t('hold.form.submit'));
  const formMsg = el('div', { id: 'holdFormMessage', 'aria-live': 'polite' });
  const confirmBox = el('div', { class: 'u3-confirm', id: 'holdConfirm', hidden: true });
  const form = el('form', { id: 'holdEntry', novalidate: true, autocomplete: 'off' }, grid, submit, formMsg, confirmBox);

  const cancelConfirm = () => { draft = null; confirmBox.hidden = true; confirmBox.replaceChildren(); };
  // 用户主动退出确认（点「返回修改」或按 Esc）：收起确认区，焦点回到「提交」按钮；输入框 input 事件触发的 cancelConfirm 不抢焦点（A11）
  const backFromConfirm = () => { cancelConfirm(); submit.focus(); };
  closeOnEscape(confirmBox, backFromConfirm);

  /** 数量框下面的整手提示：只提示，不拦截（买入不符合规则时换成提醒文字，是否拒绝由后端决定） */
  const refreshLot = () => {
    const h = lotHint({ side: side.value, code: code.value.trim(), qty: cleanNumber(qty.value) });
    const text = h ? tMaybe(h.key) : null;   // 词条还没合并进基础字典时不显示（不露键名）
    qtyHint.textContent = text || '';
    qtyHint.dataset.warn = text && h.warn ? '1' : '';
  };

  /** 方向不同，“数量/价格”的含义不同；送转没有价格和手续费；分红费用留空记 0 元，买卖留空才是自动估算 */
  const syncSide = () => {
    const s = side.value;
    const isDiv = s === 'dividend';
    fee.setAttribute('placeholder', (isDiv ? tMaybe('a15.fee.placeholder_div') : tMaybe('a15.fee.placeholder') ?? t('hold.form.fee_auto')) || '');
    feeHint.textContent = (isDiv ? tMaybe('a15.fee.hint_div') : null) ?? t('hold.form.fee_hint');
    refreshLot();
    fQty.text.textContent = t(s === 'dividend' ? 'hold.form.qty_div' : s === 'bonus' ? 'hold.form.qty_bonus' : 'hold.form.qty');
    fPrice.text.textContent = t(s === 'dividend' ? 'hold.form.price_div' : 'hold.form.price');
    fPrice.label.hidden = fFee.label.hidden = s === 'bonus';
    if (s === 'bonus') { price.value = ''; fee.value = ''; }
  };
  side.addEventListener('change', () => { syncSide(); cancelConfirm(); });

  // 代码满 6 位后：先看台账里的名称，再用只读搜索接口提示名称；查不到也允许录入
  code.addEventListener('input', async () => {
    cancelConfirm();
    refreshLot();
    const value = code.value.trim(), token = ++nameToken;
    nameHint.textContent = '';
    if (!/^\d{6}$/.test(value)) return;
    let name = knownName(getSnap(), value);
    if (!name) {
      try {
        const items = await getJSON(`/api/search?market=cn&q=${encodeURIComponent(value)}`);
        name = (Array.isArray(items) ? items : []).find((x) => x?.code === value)?.name || '';
      } catch { name = ''; }
    }
    if (token !== nameToken) return;
    nameHint.textContent = name || t('hold.form.name_unknown');
    nameHint.dataset.known = name ? '1' : '';
  });
  const markInvalid = (inputs, on) => inputs.forEach((x) => x.setAttribute('aria-invalid', String(on)));
  [date, price, fee, qty, note].forEach((x) => x.addEventListener('input', () => { cancelConfirm(); markInvalid([x], false); }));
  qty.addEventListener('input', refreshLot);

  const readForm = () => {
    // 数量 / 价格 / 费用先去掉千分位逗号再检查和提交；后端只认纯数字
    const s = side.value, c = code.value.trim(), q = cleanNumber(qty.value), p = cleanNumber(price.value), f = cleanNumber(fee.value), n = note.value.trim();
    const body = { trade_date: date.value, code: c, side: s, qty: /^\d+$/.test(q) ? Number(q) : q };
    if (s !== 'bonus') { body.price = p; if (f) body.fee = f; }
    if (n) body.note = n;
    const complete = /^\d{6}$/.test(c) && q !== '' && !!body.trade_date && (s === 'bonus' || p !== '');
    return { body, complete, problems: checkInputs({ side: s, qty: q, price: s === 'bonus' ? '' : p, fee: s === 'bonus' ? '' : f }) };
  };

  async function doSubmit() {
    if (busy || !draft) return;
    busy = true;
    const ok = confirmBox.querySelector('#holdConfirmOk');
    if (ok) { ok.disabled = true; ok.textContent = t('hold.form.saving'); }
    const r = await post('/api/holdings/add', { ...draft, client_id: clientId });
    busy = false;
    if (!r.ok) {
      cancelConfirm();
      formMsg.replaceChildren(el('p', { class: 'u3-msg u3-msg--error', role: 'alert' }, t(errKey(r.err, ['hold']))));
      return;
    }
    // 成功：清空表单、换新的 client_id（同一次提交重试才幂等，新的一笔必须是新 id）
    const duplicate = r.data.duplicate === true;
    cancelConfirm();
    [code, qty, price, fee, note].forEach((x) => { x.value = ''; });
    nameHint.textContent = '';
    refreshLot();
    date.value = today();
    clientId = newClientId();
    formMsg.replaceChildren();
    await reload();
    msg.show(duplicate ? 'hold.form.dup' : 'hold.form.saved', 'ok');
    // G01：页顶的消息条此时在屏幕外（表单在页面下半部分），在「提交」按钮下面再说一遍，看得到才算有反馈；下次点「提交」时清掉
    formMsg.replaceChildren(el('p', { class: 'u3-msg u3-msg--ok', role: 'status' }, t(duplicate ? 'hold.form.dup' : 'hold.form.saved')));
  }

  function prepare() {
    if (busy) return;
    formMsg.replaceChildren();
    msg.clear();
    const { body, complete, problems } = readForm();
    markInvalid([qty, price, fee], false);
    if (!complete) {
      formMsg.append(el('p', { class: 'u3-msg u3-msg--error', role: 'alert' }, t('hold.form.incomplete')));
      return;
    }
    if (problems.length) {   // 数量 / 价格 / 费用不合法：逐项说明、标红，焦点移到第一个有问题的框，不出确认摘要
      const fields = { qty, price, fee };
      formMsg.append(...problems.map((x) => el('p', { class: 'u3-msg u3-msg--error', role: 'alert' }, tMaybe(x.key) ?? t(x.fallback))));
      markInvalid(problems.map((x) => fields[x.field]), true);
      fields[problems[0].field].focus();
      return;
    }
    draft = body;
    const name = nameHint.dataset.known ? nameHint.textContent : knownName(getSnap(), body.code);
    // 费用留空：买卖由后端估算，分红不估算、记 0 元（store.add_trade），摘要里别写成「按费率估算」
    const feeText = body.fee ? t('hold.form.fee_value', { fee: body.fee }) : body.side === 'dividend' ? t('hold.form.fee_value', { fee: '0' }) : t('hold.form.fee_auto');
    const text = t(body.side === 'bonus' ? 'hold.form.confirm_text_bonus' : 'hold.form.confirm_text',
      { side: t(`hold.side.${body.side}`), code: body.code, name, qty: body.qty, price: body.price ?? '', fee: feeText, date: body.trade_date });
    const ok = el('button', { class: 'btn btn--primary', type: 'button', id: 'holdConfirmOk' }, t('hold.form.confirm_ok'));
    const back = el('button', { class: 'btn btn--ghost', type: 'button', id: 'holdConfirmBack' }, t('hold.form.back'));
    ok.addEventListener('click', doSubmit);
    back.addEventListener('click', backFromConfirm);
    confirmBox.hidden = false;
    confirmBox.replaceChildren(el('h3', { class: 'card__title', id: 'holdConfirmTitle' }, t('hold.form.confirm_title')), el('p', {}, text), el('div', { class: 'u3-btn-row' }, ok, back));
    // 确认区是页内展开的，不是 <dialog>：焦点移进来，Esc 可退出；默认落在「返回修改」，多按一次回车也不会入账
    confirmBox.setAttribute('role', 'group');
    confirmBox.setAttribute('aria-labelledby', 'holdConfirmTitle');
    back.focus();
  }
  form.addEventListener('submit', (e) => { e.preventDefault(); prepare(); });
  syncSide();

  const all = { date, code, side, qty, price, fee, note };
  return {
    node: form,
    capture: () => Object.fromEntries(Object.entries(all).map(([k, x]) => [k, x.value])),
    restore: (vals) => { Object.entries(vals).forEach(([k, v]) => { all[k].value = v; }); syncSide(); },
  };
}

/** 撤销二次确认：点「撤销」后在原地换成确认提示，再点「确认撤销」才发请求 */
function askVoid(cell, id, { msg, reload }) {
  const original = [...cell.children];
  const ask = span(t('hold.hist.void_ask'));
  ask.id = `hold-void-ask-${id}`;
  const ok = el('button', { class: 'btn btn--sm btn--primary', type: 'button' }, t('hold.hist.void_ok'));
  const cancel = el('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, t('hold.hist.void_cancel'));
  // 取消 / Esc / 请求失败都换回原来的「撤销」按钮并把焦点还给它，否则按钮被换掉后焦点会掉回页面顶部（A11）
  const restore = () => { if (!ask.isConnected) return; cell.replaceChildren(...original); original[0]?.focus?.(); };
  cancel.setAttribute('aria-describedby', ask.id);   // 焦点落在「取消」上时，读屏会接着读「确认撤销吗」
  cancel.addEventListener('click', restore);
  closeOnEscape(cell, restore);
  ok.addEventListener('click', async () => {
    ok.disabled = true;
    const r = await post('/api/holdings/void', { id });
    if (!r.ok) { restore(); msg.show(errKey(r.err, ['hold'])); return; }
    await reload();
    msg.show('hold.hist.void_done', 'ok');
  });
  cell.replaceChildren(ask, ok, cancel);
  cancel.focus();   // 默认焦点放在更安全的「取消」上
}

/** 历史成交卡片；ctx = { includeVoided, msg, reload, setVoided } */
export function historyCard(snap, ctx) {
  const toggle = el('input', { type: 'checkbox', id: 'holdShowVoided', checked: ctx.includeVoided });
  toggle.addEventListener('change', () => ctx.setVoided(toggle.checked));
  const filter = el('label', { class: 'ws-filter u3-toggle' }, toggle, el('span', {}, t('hold.hist.show_voided')));
  const rows = (snap.trades || []).map((x) => {
    const action = el('td', {});
    if (x.voided) action.append(el('small', { class: 'muted' }, t('hold.hist.voided')));
    else {
      const btn = setAriaLabel(el('button', { class: 'btn btn--sm btn--ghost', type: 'button' }, t('hold.hist.void')), 'a11y.void_trade', { date: x.trade_date, code: x.code });   // 每行都叫「撤销」，读屏要带上是哪一笔
      btn.addEventListener('click', () => askVoid(action, x.id, ctx));
      action.append(btn);
    }
    const stock = x.name && x.name !== x.code ? `${x.code} ${x.name}` : x.code;
    const feeCell = td(el('span', {}, num(x.fee), x.fee_estimated ? el('small', { class: 'muted' }, ` ${t('hold.hist.est')}`) : null), true);
    return el('tr', { class: x.voided ? 'is-voided' : null },
      td(x.trade_date, false, 'muted'), td(t(`hold.side.${x.side}`)), td(stock), td(num(x.qty, 0), true),
      td(x.side === 'bonus' ? '—' : num(x.price, 3), true), feeCell, td(x.note || '', false, 'muted'), action);
  });
  const heads = ['hold.hist.date', 'hold.hist.side', 'hold.hist.stock', 'hold.hist.qty', 'hold.hist.price', 'hold.hist.fee', 'hold.hist.note', 'hold.hist.void'];
  return el('section', { class: 'card card--flush', 'aria-labelledby': 'hold-hist-t' },
    el('div', { class: 'card__head' }, el('h2', { class: 'card__title', id: 'hold-hist-t' }, t('hold.hist.title'))),
    filter,
    table(heads, rows, 'hold.hist.empty', { numCols: [3, 4, 5], id: 'holdHistoryTable' }));
}
