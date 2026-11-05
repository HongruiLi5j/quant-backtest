/* ==========================================================
 * 文件: ui/pages/stock.js
 * 用途: 个股详情（旧 #/stock/<代码>）。地址：stock.html?code=600000&market=cn
 *   头部（名称 / 代码 / 今日·近1月·近1年 / 明日仓位与动作）→ 工具条（策略 / 区间 / 技术指标 / 去回测实验室 / 自选）
 *   → K 线（stock-chart.js）→ 策略对比表 → 净值曲线 → 交易记录
 *   接口：GET /api/stock、GET /api/meta、POST /api/watchlist
 *   策略、区间、指标的选择记在 localStorage（换股票也保留）
 * ========================================================== */
import { industryName } from '../shared/industry.js';   // R8：英文界面的行业名
import { withInfo } from '../shared/glossary.js';   // D111
import { el, fmt, store } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON, postJSON, isMock } from '../shared/api.js';
import { toast } from '../shared/toast.js';
import { canZoomRecent, kChart } from './stock-chart.js';
import { addCompareBoxes, equityView } from './stock-compare.js';   // D85：勾 2–4 个策略叠加净值
import { actionBadge, card, legend, lineChart, paletteVars, pct, table, tb, toneClass } from './u2-lib.js';

const PERIODS = ['6m', '1y', '3y', '5y', 'all'];
const INDS = ['all', 'macd', 'rsi', 'kdj', 'off'];
const VIEWS = ['recent', 'all'];     // 数据多时 K 线的可见范围：最近约 1 年 / 全部
const KEY = { strategy: 'qb-u2-stock-strategy', period: 'qb-u2-stock-period', ind: 'qb-u2-stock-ind', view: 'qb-a12-stock-view' };

let data = null;      // 当前 /api/stock 返回，切换指标时只重画 K 线用
let cmp = { code: null, sel: new Set() };   // D85：勾选的对比策略（换股票清空；换区间保留）
let kc = null;        // 当前 K 线的 { chart, showRange }，切换可见范围时不重画、只移动窗口

function head(d, meta, market) {
  const i = d.info;
  const sub = [tb(i.kind), tb(i.board), i.industry ? industryName(i.industry) : ''].filter(Boolean).join(' · ');
  const stat = (label, node) => el('div', { class: 'u2-stat' }, el('span', { class: 'muted' }, label), node);
  const num = (v, dg) => el('b', { class: `num ${toneClass(v)}` }, pct(v, dg));
  return el('section', { class: 'card u2-shead' },
    el('div', {}, el('p', { class: 'muted' }, sub), el('h2', { class: 'u2-shead__name' }, i.name, el('span', { class: 'u2-code num' }, i.code))),
    el('div', { class: 'u2-stats' },
      stat(t('u2.st.today'), num(i.d1, 2)), stat(t('u2.st.m1'), num(i.m1)), stat(t('u2.st.y1'), num(i.y1)),
      stat(t('u2.st.tomorrow', { s: meta.strategies[d.strategy]?.label ?? d.strategy }), el('span', {}, el('b', { class: 'num' }, fmt(d.next * 100, 0, '%')), ' ', actionBadge(d.action)))));
}

function select(options, value, label, onChange) {
  const s = el('select', { class: 'ws-select', 'aria-label': label }, ...options.map(([v, text]) => el('option', { value: v }, text)));
  s.value = value;
  s.addEventListener('change', () => onChange(s.value));
  return s;
}

function toolbar(d, meta, market, code, st, actions) {
  const singles = Object.entries(meta.strategies).filter(([, v]) => v.kind === 'single').map(([k, v]) => [k, v.label]);
  const period = el('div', { class: 'segmented', role: 'group', 'aria-label': t('u2.st.period') },
    ...PERIODS.map((p) => el('button', { class: 'segmented__btn', type: 'button', 'aria-pressed': String(p === st.period), dataset: { p } }, t(`u2.st.p.${p}`))));
  period.addEventListener('click', (e) => { const b = e.target.closest('[data-p]'); if (b) actions.set('period', b.dataset.p); });
  const view = canZoomRecent(d.dates.length)
    ? el('div', { class: 'u2-field' }, el('span', { class: 'muted' }, t('a12.range.label')),
      el('div', { class: 'segmented', role: 'group', 'aria-label': t('a12.range.label') },
        ...VIEWS.map((v) => el('button', { class: 'segmented__btn', type: 'button', 'aria-pressed': String(v === st.view), dataset: { v } }, t(`a12.range.${v}`)))))
    : null;
  if (view) view.addEventListener('click', (e) => { const b = e.target.closest('[data-v]'); if (b) actions.set('view', b.dataset.v); });
  const labQ = new URLSearchParams({ market, symbol: code, strategy: st.strategy });
  if (isMock()) labQ.set('mock', '1');   // G01：预览模式把 ?mock=1 带过去（以前丢了，预览里点过去是 404 错误卡片）
  // D83：data-palette 让 ⌘K 面板里出现「在回测实验室打开 代码」这一条本页操作
  const lab = el('a', { class: 'btn btn--sm', href: `backtest.html?${labQ}`, dataset: { palette: t('ux.palette.to_lab', { c: code }) === 'ux.palette.to_lab' ? t('u2.st.to_lab') : t('ux.palette.to_lab', { c: code }) } }, t('u2.st.to_lab'));
  const nodes = [
    el('label', { class: 'u2-field' }, el('span', { class: 'muted' }, t('u2.bt.strategy')), select(singles, st.strategy, t('u2.bt.strategy'), (v) => actions.set('strategy', v))),
    el('div', { class: 'u2-field' }, el('span', { class: 'muted' }, t('u2.st.period')), period),
    view,
    el('label', { class: 'u2-field' }, el('span', { class: 'muted' }, t('u2.st.ind')), select(INDS.map((k) => [k, t(`u2.st.ind.${k}`)]), st.ind, t('u2.st.ind'), (v) => actions.set('ind', v))),
    el('span', { class: 'ws-head__spacer' }), lab];
  if (d.info.kind === '个股') {
    const w = el('button', { class: 'btn btn--sm', type: 'button' }, t(d.info.watch ? 'u2.st.unwatch' : 'u2.st.watch'));
    w.addEventListener('click', () => actions.watch(!d.info.watch));
    nodes.push(w);
  }
  return el('div', { class: 'ws-filter u2-toolbar' }, ...nodes);
}

function chartCard(d, meta, market, st, actions, code) {
  const box = el('div', { class: 'chart u2-k', role: 'figure', 'aria-label': t('u2.st.chart_aria', { n: d.info.name }) });
  const items = d.lines.map((L, k) => ({ label: L.name, color: L.dash ? 'var(--chart-dd)' : paletteVars[k % 4] }));
  const markers = [{ label: t('u2.st.legend_buy'), color: 'var(--color-rise)' }, { label: t('u2.st.legend_sell'), color: 'var(--color-fall)' }, { label: t('u2.st.legend_pos'), color: 'var(--chart-2)' }];
  const i = d.info;
  const note = t('u2.st.note', { first: i.first_date, last: i.last_date, rows: i.rows, tz: tb(meta.markets[market]?.timezone_label ?? ''), cost: fmt(d.cost_bp, 0) });
  queueMicrotask(() => requestAnimationFrame(() => { kc = kChart(box, d, st.ind, st.view); }));
  box.dataset.kchart = '1';
  const hint = canZoomRecent(d.dates.length) ? el('p', { class: 'ws-note' }, t('a12.range.hint', { n: d.dates.length })) : null;
  // D86：数据区间里没有成交的交易日超过 20 天（多半是停牌）时，说明年化和现金利息的口径（revquant 签字的措辞；
  // 天数由后端按交易所交易日历算，src/detail.py _suspended_days）
  const susp = Number(i.suspended_days) > 20 ? el('p', { class: 'ws-note u3-warn' }, t('c86.suspended', { n: i.suspended_days })) : null;
  // D110：数据质量标记。mask 段 = 复权价不可能（日涨跌幅超出任何涨跌停制度允许的范围或价格近零），全市场统计里已按规则屏蔽；flag 段只提示
  const q = (i.quality ?? []).map((g) => (g.kind === 'mask' ? t('c110.mask', { a: g.from, b: g.to }) : t('c110.flag', { a: g.from, b: g.to, n: g.n })));
  const qual = q.length ? el('p', { class: 'ws-note is-warn', role: 'note' }, el('strong', {}, t('c110.title')), ' ', q.join(' ')) : null;
  return el('section', { class: 'card' }, toolbar(d, meta, market, code, st, actions), legend([...items, ...markers]), box, el('p', { class: 'ws-note' }, note), qual, susp, hint);
}

function compareCard(d, meta, actions) {
  const rows = d.compare.map((c) => (c.error
    ? { cells: [c.label, { v: c.error, span: 6, cls: 'muted' }] }
    : { cls: `u2-click ${c.key === d.strategy ? 'u2-pin' : ''}`, dataset: { k: c.key }, cells: [c.label, { v: fmt(c.cagr, 1, '%', true), num: true }, { v: fmt(c.sharpe), num: true },
      { v: fmt(c.maxdd, 1, '%'), num: true }, { v: fmt(c.win, 0, '%'), num: true }, { v: c.trades ?? '—', num: true }, actionBadge(c.action)] }));
  const HEAD_TERMS = { 'metric.cagr': 'cagr', 'u2.bt.excess_sharpe': 'sharpe_cash', 'metric.maxdd': 'maxdd', 'metric.win': 'win', 'metric.trades': 'trades' };
  const heads = ['u2.st.c.strategy', 'metric.cagr', 'u2.bt.excess_sharpe', 'metric.maxdd', 'metric.win', 'metric.trades', 'u2.st.c.next'].map((k, n) => ({ text: HEAD_TERMS[k] ? withInfo(t(k), HEAD_TERMS[k]) : t(k), num: n > 0 && n < 6 }));   // D111：表头 ⓘ
  // CT03（revtech CP2 条件）：这张表是全部历史回测（D18 起和今日页同一个函数），不随上面的「区间」变，写清楚
  const node = card(t('u2.st.compare'), t('c03.compare_sub'), table(heads, rows));
  node.classList.add('u2-compare');   // D07：紧凑列距，半宽卡片里放得下 7 列（原来 720px 塞在 638px 里，「明日」列被裁）
  // D49-2（revtech A11-03）：可点的行不再整行 tabindex，首格的策略名换成按钮（aria-pressed = 当前策略），读屏有名字、知道能按
  node.querySelectorAll('tbody tr[data-k]').forEach((tr) => {
    const cell = tr.cells[0];
    const btn = el('button', { class: 'u2-rowbtn', type: 'button', 'aria-pressed': String(tr.classList.contains('u2-pin')) }, cell.textContent);
    cell.replaceChildren(btn);
    tr.addEventListener('click', () => actions.set('strategy', tr.dataset.k));
  });
  if (actions.compare) addCompareBoxes(node, cmp.sel, actions.compare);
  return node;
}

function equityCard(d, meta, ctx) {
  return equityView(d, meta, cmp.sel, ctx);   // D85：0–1 个勾选时和原来一样；2–4 个时叠加比较（stock-compare.js）
}

function tradesCard(d) {
  const body = d.trades.length
    ? table(['u2.st.t.entry', 'u2.st.t.exit', 'backtest.trades.days', 'u2.st.t.px', 'u2.st.t.acct'].map((k, n) => ({ text: t(k), num: n > 1 })),
      d.trades.map((x) => [{ v: x.entry, num: true }, x.open ? el('span', { class: 'badge badge--accent' }, t('u2.bt.holding')) : { v: x.exit, num: true }, { v: String(x.days), num: true },
        { v: pct(x.px_ret, 2), num: true, cls: toneClass(x.px_ret) }, { v: pct(x.ret, 2), num: true, cls: toneClass(x.ret) }]), { scroll: true, label: t('u2.st.trades') })
    : el('p', { class: 'muted' }, t('u2.st.no_trades'));
  return card(t('u2.st.trades'), null, body);
}

export async function render(root) {
  const q = new URLSearchParams(window.location.search);
  const code = (q.get('code') || '').trim();
  if (!code) {
    root.replaceChildren(card(t('u2.st.no_code'), null, el('p', { class: 'muted' }, t('u2.st.no_code_hint')), el('a', { class: 'btn btn--sm', href: 'market.html' }, t('u2.st.goto_market'))));
    return undefined;
  }
  const meta = await getJSON('/api/meta', { mock: 'u2-meta' });
  const market = meta.markets[q.get('market')] ? q.get('market') : meta.default_market;
  const st = { strategy: store.get(KEY.strategy, 'blend2'), period: store.get(KEY.period, '1y'), ind: store.get(KEY.ind, 'all'), view: store.get(KEY.view, 'recent') };
  if (!VIEWS.includes(st.view)) st.view = 'recent';
  if (!meta.strategies[st.strategy]) st.strategy = 'blend2';
  if (!PERIODS.includes(st.period)) st.period = '1y';
  if (!INDS.includes(st.ind)) st.ind = 'all';   // D104（所有者）：技术指标默认全开，可在「技术指标」下拉里关掉（选过的会记住）

  // D80（R4-ux-2）：换策略 / 区间 / 自选后重新请求，原来旧内容原样不动、没有任何「正在加载」的样子（冷启动要好几秒，像是点了没反应）。
  // 现在请求期间整块变淡、不可点、标 aria-busy；新内容画好（或出错）后恢复。和回测页「回测中」变淡同一个做法。
  const again = () => {
    root.classList.add('u2-refetch'); root.setAttribute('aria-busy', 'true');
    return render(root).catch((err) => {
      root.replaceChildren(card(err.auth ? t('ui.auth.expired') : t('ui.state.error'), null, el('p', { class: 'muted' }, err.message ?? ''), err.auth ? el('a', { class: 'btn btn--sm', href: '/' }, t('ui.auth.home')) : null));
    }).finally(() => { root.classList.remove('u2-refetch'); root.removeAttribute('aria-busy'); });
  };
  const actions = {
    set(key, value) {
      store.set(KEY[key], value);
      if (key === 'view') {                              // 只换可见范围：不请求、不重画，移动窗口并同步按钮状态
        st.view = value;
        root.querySelectorAll('[data-v]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.v === value)));
        kc?.showRange(value);
        return;
      }
      if (key !== 'ind') { again(); return; }
      st.ind = value;                                    // 只换指标：不重新请求，直接重画 K 线
      const box = root.querySelector('[data-kchart]');
      if (box && data) kc = kChart(box, data, value, st.view);
    },
    async watch(add) {
      const r = await postJSON(`/api/watchlist?market=${market}&action=${add ? 'add' : 'remove'}&code=${encodeURIComponent(code)}`);
      if (r.error) { toast({ title: r.error }); return; }
      again();
    },
  };

  const d = await getJSON(`/api/stock?${new URLSearchParams({ market, code, strategy: st.strategy, period: st.period })}`, { mock: 'u2-stock' });
  if (d.error) {
    root.replaceChildren(card(t('u2.st.err_title'), null, el('p', { class: 'is-warn', role: 'alert' }, d.error)));
    return undefined;
  }
  data = d;
  if (cmp.code !== code) cmp = { code, sel: new Set() };
  const ctx = { code, market };
  const eqSlot = el('div', { class: 'u2-eqslot' }, equityCard(d, meta, ctx));
  actions.compare = () => eqSlot.replaceChildren(equityCard(d, meta, ctx));   // 勾选变化只重画净值卡，不重新请求整页
  root.replaceChildren(head(d, meta, market), el('div', { class: 'u2-gap' }, chartCard(d, meta, market, st, actions, code)),
    el('div', { class: 'ws-grid ws-grid--2 u2-gap' }, compareCard(d, meta, actions), el('div', { class: 'u2-stack' }, eqSlot, tradesCard(d))));
  return undefined;
}
