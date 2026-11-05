/* ==========================================================
 * 文件: ui/pages/stock-compare.js（D85 / R5-ux-3）
 * 用途: 个股页「这只股票，哪个策略好？」表里勾 2–4 个策略，把它们的净值曲线叠到「净值」图上比较。
 *   - 同一只股票、同一个区间（各策略的 /api/stock 返回的 dates 完全一样），同一根纵轴；买入持有始终画出来作基准
 *   - 恰好勾 2 个时，下面再画一条「A − B」净值差线
 *   - 下方固定显示 revquant 签字的提醒（多重检验、不是样本外；reviews/revquant_wording.md「D85 final」）
 *   - 每个勾选的策略各请求一次 /api/stock（服务端有结果缓存）；不改任何策略规则和数字
 * ========================================================== */
import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON } from '../shared/api.js';
import { card, legend, lineChart, palette, paletteVars } from './u2-lib.js';

const MAX = 4;
const M_BY_K = { 2: '0.6', 3: '0.85', 4: '1.0' };   // revquant：k 条里挑最好的，单靠运气平均好看的标准误倍数
const fetched = new Map();   // `${code}|${period}|${strategy}` → equity 数组（本页内缓存）

/** 给对比表每个策略行（买入持有除外）加一个勾选框；勾到 MAX 个后其他的禁用 */
export function addCompareBoxes(node, sel, onChange) {
  const boxes = [];
  node.querySelectorAll('tbody tr[data-k]').forEach((tr) => {
    const k = tr.dataset.k;
    if (k === 'buy_hold') return;
    const name = tr.cells[0].textContent;
    const cb = el('input', { type: 'checkbox', class: 'u2-cmpbox', checked: sel.has(k), 'aria-label': `${t('ux.cmp.pick')} ${name}` });
    cb.addEventListener('click', (e) => e.stopPropagation());   // 勾选不触发整行的「切换当前策略」
    cb.addEventListener('change', () => {
      if (cb.checked) sel.add(k); else sel.delete(k);
      sync(); onChange();
    });
    boxes.push(cb);
    tr.cells[0].prepend(cb);
  });
  const sync = () => boxes.forEach((b) => { b.disabled = !b.checked && sel.size >= MAX; });
  sync();
}

function caveat(k) {
  // {se}（Sharpe 标准误）个股接口没有给，按 revquant 的说明去掉括号那一句；词条没进字典前不显示
  let raw = t('c85.caveat_nose', { k, m: M_BY_K[k] });
  if (raw === 'c85.caveat_nose') return null;
  // 最后一句讲的是「A − B」差值线，只有恰好勾 2 个时才画这条线；其他时候把这句去掉（revquant 附注）
  const cut = raw.search(/下面的「A − B」|The [“"]A − B[”"]/);   // 英文词条用的是弯引号
  if (k !== 2 && cut > 0) raw = raw.slice(0, cut).trim();
  return el('p', { class: 'ws-note u2-cmp-note', role: 'note' }, raw);
}

/** 净值卡：没勾或只勾 1 个时和原来一样（当前策略 vs 买入持有）；勾 2–4 个时叠加比较 */
export function equityView(d, meta, sel, ctx) {
  const label = (k) => meta.strategies[k]?.label ?? d.compare.find((c) => c.key === k)?.label ?? k;
  const box = el('div', { class: 'chart chart--sm u2-chart' });
  if (sel.size < 2) {
    queueMicrotask(() => requestAnimationFrame(() => lineChart(box, d.dates, [
      { values: d.bench, color: (c) => c.bench, width: 1 }, { values: d.equity, color: (c) => c.line, width: 2 }], {})));
    return card(t('u2.st.equity'), t('u2.st.equity_sub'), legend([{ label: label(d.strategy), color: 'var(--chart-1)' }, { label: t('u2.bt.hold'), color: 'var(--chart-bench)' }]), box);
  }
  const keys = [...sel];
  const diffBox = keys.length === 2 ? el('div', { class: 'chart chart--sm u2-chart u2-cmp-diff' }) : null;
  const body = el('div', { class: 'u2-cmp', 'aria-busy': 'true' }, box, diffBox);
  const node = card(t('u2.st.equity'), t('ux.cmp.sub', { n: keys.length }),
    legend([...keys.map((k, i) => ({ label: label(k), color: paletteVars[i % paletteVars.length] })), { label: t('u2.bt.hold'), color: 'var(--chart-bench)' }]),
    body, caveat(keys.length));
  Promise.all(keys.map(async (k) => {
    if (k === d.strategy) return d.equity;
    const id = `${ctx.code}|${d.period}|${k}`;
    if (!fetched.has(id)) {
      const r = await getJSON(`/api/stock?${new URLSearchParams({ market: ctx.market, code: ctx.code, strategy: k, period: d.period })}`, { mock: 'u2-stock' });   // 预览模式（?mock=1）用现成的假数据
      fetched.set(id, r.dates?.length === d.dates.length ? r.equity : null);   // 日期对不齐就不画，不硬凑
    }
    return fetched.get(id);
  })).then((eqs) => {
    body.removeAttribute('aria-busy');
    lineChart(box, d.dates, [{ values: d.bench, color: (c) => c.bench, width: 1 },
      ...eqs.map((values, i) => (values ? { values, color: palette[i % palette.length], width: 2 } : null)).filter(Boolean)], {});
    if (diffBox && eqs[0] && eqs[1]) {
      const diff = eqs[0].map((a, i) => (a === null || eqs[1][i] === null ? null : a - eqs[1][i]));
      diffBox.before(el('p', { class: 'ws-note' }, t('ux.cmp.diff', { a: label(keys[0]), b: label(keys[1]) })));
      lineChart(diffBox, d.dates, [{ values: diff, color: palette[0], width: 1 }], { zero: true });
    }
  }).catch(() => { body.removeAttribute('aria-busy'); body.replaceChildren(el('p', { class: 'muted' }, t('ui.state.error'))); });
  return node;
}
