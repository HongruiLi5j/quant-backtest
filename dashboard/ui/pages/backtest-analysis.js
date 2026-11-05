/* ==========================================================
 * 文件: ui/pages/backtest-analysis.js
 * 用途: 回测「分析」区（对应旧 analysis.js 的 renderAnalysis），数据来自 r.analysis（src/analytics.py）：
 *   假设与局限 / 核心指标 / 收益分布 / 样本内外 / 月度收益 / 滚动 Sharpe / 仓位占用 / 多基准
 *   / 交易分布与直方图 / 手续费敏感性 / 蒙特卡洛 / 参数敏感性 / CSV 导出
 *   图表部分在 backtest-ana-charts.js
 * ========================================================== */
import { el, fmt } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { withInfo as wi } from '../shared/glossary.js';   // D111：指标名后面的 ⓘ
import { renderHeatmap } from '../shared/chart-heatmap.js';
import { card, downloadCSV, isNum, pct, table, tb, toneClass } from './u2-lib.js';
import { chartSection, histogram } from './backtest-ana-charts.js';

const num = (v, d = 2, unit = '') => ({ v: fmt(v, d, unit), num: true });
/** 指标行：策略值比持有好时加高亮 */
function metricRow(label, v, b, d = 2, unit = '', good = 'high') {
  const better = good === 'high' && isNum(v) && isNum(b) && v > b;
  return [label, { v: fmt(v, d, unit), num: true, cls: better ? 'is-rise' : '' }, num(b, d, unit)];
}

/** D76（revquant 标签约束）：只在 rf=0 的 Sharpe 旁边写「± 标准误」（后端 sharpe_se 就是这一个口径的，
 *  页面顶部扣现金收益的 Sharpe 没有 se，不能挂上去）；不说「两个 Sharpe 差距小于 se 就没区别」——两条收益相关，差的误差另算 */
const withSe = (v, se) => ({ v: isNum(v) && isNum(se) ? `${fmt(v, 2)} ± ${fmt(se, 2)}` : fmt(v, 2), num: true });

function assumptions(a) {
  return card(t('u2.ana.assume'), t('u2.ana.assume_sub'),
    el('dl', { class: 'ws-dl' }, ...(a.assumptions ?? []).flatMap(([k, v]) => [el('dt', {}, tb(k)), el('dd', { class: 'u2-ass' }, v)])));
}

function coreCards(a) {
  const f = a.full ?? {}; const bf = a.bench_full ?? {}; const ex = a.extra ?? {};
  const core = table([{ text: t('u2.ana.metric') }, { text: t('chart.strategy'), num: true }, { text: t('u2.bt.hold'), num: true }], [
    metricRow(wi(t('u2.ana.total'), 'total'), f.total, bf.total, 1, '%'), [wi(t('metric.cagr'), 'cagr'), { v: fmt(f.cagr, 1, '%', true), num: true, cls: isNum(f.cagr) && isNum(bf.cagr) && f.cagr > bf.cagr ? 'is-rise' : '' }, { v: fmt(bf.cagr, 1, '%', true), num: true }],
    metricRow(wi(t('metric.vol'), 'vol'), f.vol, bf.vol, 1, '%', 'low'),
    [wi(t('u2.ana.s_sharpe_rf0'), 'sharpe_rf0'), withSe(f.sharpe, f.sharpe_se), withSe(bf.sharpe, bf.sharpe_se)],   // rf=0 的 Sharpe（analytics.py:34）+ D76 标准误
    metricRow(wi(t('u2.ana.sortino'), 'sortino'), f.sortino, bf.sortino), metricRow(wi(t('u2.ana.calmar'), 'calmar'), f.calmar, bf.calmar),
    metricRow(wi(t('metric.maxdd'), 'maxdd'), f.maxdd, bf.maxdd, 1, '%', '')]);
  const dist = table([], [
    [t('u2.ana.best_day'), { v: pct(ex.best_day, 2), num: true, cls: 'is-rise' }], [t('u2.ana.worst_day'), { v: pct(ex.worst_day, 2), num: true, cls: 'is-fall' }],
    // D29（revquant A7）：后端 VaR 是负数，标签已写「至少亏」，这里显示绝对值，避免「至少亏 -0.55%」
    [wi(t('u2.ana.var'), 'var95'), { v: isNum(ex.var95) ? fmt(Math.abs(ex.var95), 2, '%') : '—', num: true }], [wi(t('u2.ana.skew'), 'skew'), num(ex.skew)], [wi(t('u2.ana.kurt'), 'kurt'), num(ex.kurt)],
    [t('u2.ana.pos_months'), num(ex.pos_months, 0, '%')]]);
  const seNote = isNum(f.sharpe_se) ? el('p', { class: 'ws-note' }, t('c76.se_note')) : null;
  return el('div', { class: 'ws-grid ws-grid--2' }, card(t('u2.ana.core'), null, core, seNote), card(t('u2.ana.dist'), null, dist));
}

/** 样本内 / 样本外：前 70% / 后 30%，附带判断语 */
function split(a, r) {
  const sp = a.split ?? {};
  if (!sp.date) return card(t('u2.ana.split'), null, el('p', { class: 'muted' }, t('u2.ana.split_short')));
  const seg = (k) => sp[k] ?? {};
  const row = (name, k, kb) => [name, { v: fmt(seg(k).cagr, 1, '%', true), num: true }, withSe(seg(k).sharpe, seg(k).sharpe_se), num(seg(k).maxdd, 1, '%'), { v: fmt(seg(kb).cagr, 1, '%', true), num: true }, withSe(seg(kb).sharpe, seg(kb).sharpe_se), num(seg(kb).maxdd, 1, '%')];
  const heads = ['u2.ana.period', 'u2.ana.s_cagr', 'u2.ana.s_sharpe', 'u2.ana.s_dd', 'u2.ana.h_cagr', 'u2.ana.h_sharpe', 'u2.ana.h_dd'].map((k, i) => ({ text: ['u2.ana.s_sharpe', 'u2.ana.h_sharpe'].includes(k) ? wi(t(k), 'sharpe_rf0') : t(k), num: i > 0 }));
  const si = seg('is').sharpe; const so = seg('oos').sharpe;
  const verdict = isNum(si) && isNum(so) ? t(so >= si * 0.6 ? 'u2.ana.oos_ok' : 'u2.ana.oos_bad') : '';
  return card(wi(t('u2.ana.split'), 'split70'), null,
    table(heads, [row(t('u2.ana.is', { a: r.start, b: sp.date }), 'is', 'b_is'), row(t('u2.ana.oos', { a: sp.date, b: r.end }), 'oos', 'b_oos')]),
    el('p', { class: 'ws-note' }, verdict, ' ', t('u2.ana.split_warn')));
}

/** 月度收益：热力图（策略）+ 年度对比表（策略全年 vs 持有全年） */
function monthly(a) {
  const mo = a.monthly ?? { years: [], matrix: [], year_total: [] };
  if (!mo.years.length) return null;
  const byYear = {}; const totals = {};
  mo.years.forEach((y, i) => { byYear[String(y)] = mo.matrix[i]; totals[String(y)] = mo.year_total[i]; });
  const box = el('div', { class: 'table-wrap' });
  const by = a.bench_year ?? [];
  const yearTable = table([{ text: t('u2.ana.year') }, { text: t('u2.ana.year_s'), num: true }, { text: t('u2.ana.year_h'), num: true }],
    mo.years.map((y, i) => [String(y), { v: pct(mo.year_total[i], 1), num: true, cls: toneClass(mo.year_total[i]) }, { v: pct(by[i], 1), num: true, cls: toneClass(by[i]) }]).reverse());
  queueMicrotask(() => renderHeatmap(box, byYear, totals));
  return card(t('backtest.heatmap.title'), t('u2.ana.monthly_hint'), box, el('div', { class: 'u2-gap' }, yearTable));
}

function tradeStats(a) {
  const tr = a.trades ?? {};
  if (!tr.n) return card(t('u2.ana.trades'), null, el('p', { class: 'muted' }, t('u2.ana.no_trades')));
  const rows = [
    [wi(t('metric.trades'), 'trades'), num(tr.n, 0), wi(t('metric.win'), 'win'), isNum(tr.win_lo) ? { v: t('c76.win', { w: fmt(tr.win, 1), lo: fmt(tr.win_lo, 1), hi: fmt(tr.win_hi, 1) }), num: true } : num(tr.win, 1, '%')],   // D76：胜率带 95% 区间（Wilson）
    [t('u2.ana.avg_win'), { v: pct(tr.avg_win, 2), num: true, cls: 'is-rise' }, t('u2.ana.avg_loss'), { v: pct(tr.avg_loss, 2), num: true, cls: 'is-fall' }],
    [wi(t('u2.ana.payoff'), 'payoff'), num(tr.payoff), wi(t('u2.ana.expect'), 'expect'), { v: pct(tr.expectancy, 2), num: true, cls: toneClass(tr.expectancy) }],
    [t('u2.ana.best_trade'), { v: pct(tr.best, 2), num: true, cls: 'is-rise' }, t('u2.ana.worst_trade'), { v: pct(tr.worst, 2), num: true, cls: 'is-fall' }],
    [t('u2.ana.avg_days'), num(tr.avg_days, 1), t('u2.ana.streak'), num(tr.max_lose_streak, 0)]];
  const h1 = el('div', { class: 'u2-hist' }); const h2 = el('div', { class: 'u2-hist' });
  queueMicrotask(() => requestAnimationFrame(() => { histogram(h1, tr.ret_hist, 'pct'); histogram(h2, tr.days_hist, 'days'); }));
  return card(t('u2.ana.trades'), null, table([], rows),
    el('div', { class: 'ws-grid ws-grid--2 u2-gap' },
      el('div', {}, el('p', { class: 'eyebrow' }, t('u2.ana.hist_ret')), h1), el('div', {}, el('p', { class: 'eyebrow' }, t('u2.ana.hist_days')), h2)));
}

function costSens(a) {
  const cs = a.cost_sens ?? [];
  if (!cs.length) return null;
  const name = (l) => (l === '0' ? t('u2.ana.cost0') : l === '1x' ? t('u2.ana.cost1') : t('u2.ana.cost_n', { n: l.replace('x', '') }));
  const note = cs.length >= 4 && isNum(cs[3].sharpe) ? t(cs[3].sharpe > 0.3 ? 'u2.ana.cost_ok' : 'u2.ana.cost_bad') : '';
  return card(wi(t('u2.ana.cost'), 'cost_sens'), t('u2.ana.cost_sub'),
    table([t('u2.ana.fee_level'), t('u2.ana.one_way'), t('metric.cagr'), wi(t('u2.ana.s_sharpe_rf0'), 'sharpe_rf0'), t('metric.maxdd')].map((x, i) => ({ text: x, num: i > 0 })),
      cs.map((c) => [name(c.label), num(c.cost_bp, 1, 'bp'), { v: fmt(c.cagr, 1, '%', true), num: true }, num(c.sharpe), num(c.maxdd, 1, '%')])),
    el('p', { class: 'ws-note' }, note));
}

function monteCarlo(a) {
  const mc = a.mc;
  if (!mc) return null;
  return card(wi(t('u2.ana.mc'), 'mc'), t('u2.ana.mc_sub'),
    table([{ text: '' }, ...mc.pct.map((q) => ({ text: t('u2.ana.pctile', { q }), num: true }))], [
      [t('metric.cagr'), ...mc.cagr.map((v) => ({ v: fmt(v, 1, '%', true), num: true, cls: toneClass(v) }))],
      [t('metric.maxdd'), ...mc.maxdd.map((v) => ({ v: fmt(v, 1, '%'), num: true, cls: 'is-fall' }))]]),
    el('p', { class: 'ws-note' }, t('u2.ana.mc_note', { block: mc.block, n: mc.n, p: fmt(mc.p_loss, 1, '%') })));
}

/** D48（T-9 B17）：参数敏感性表头。权重 1/3 的倍数会显示成 0.13333333，统一最多保留 2 位小数并去掉多余的 0 */
const paramText = (v) => (typeof v === 'number' && Number.isFinite(v) ? String(Number(v.toFixed(2))) : String(v));

function paramSens(a) {
  const ps = a.param_sens ?? [];
  if (!ps.length) return null;
  return card(wi(t('u2.ana.param'), 'param_robust'), t('u2.ana.param_sub'),
    ...ps.map((p) => el('div', { class: 'u2-gap' }, el('p', { class: 'eyebrow' }, p.label),
      table([{ text: t('u2.ana.value') }, ...p.pts.map((x) => ({ text: paramText(x.v), num: true }))],
        [[wi(t('u2.ana.s_sharpe_rf0'), 'sharpe_rf0'), ...p.pts.map((x) => num(x.sharpe))], [t('metric.cagr'), ...p.pts.map((x) => ({ v: fmt(x.cagr, 1, '%', true), num: true }))]]))),
    el('p', { class: 'ws-note' }, t('u2.ana.param_note')));
}

/** 导出：净值 CSV、交易明细 CSV（和旧页面同样的列） */
function exports(r) {
  const id = `${r.symbol || r.code || 'result'}_${r.strategy}`;
  const eqBtn = el('button', { class: 'btn btn--sm', type: 'button' }, t('u2.ana.export_eq'));
  eqBtn.addEventListener('click', () => downloadCSV(`${t('u2.csv.eq')}_${id}.csv`,
    [['date', t('u2.csv.date')], [(x) => x.eq, t('u2.csv.eq_s')], [(x) => x.bh, t('u2.csv.eq_h')], [(x) => x.dd, t('u2.csv.dd')]],
    r.dates.map((d, i) => ({ date: d, eq: r.equity[i], bh: r.bench[i], dd: r.drawdown[i] }))));
  const btns = [eqBtn];
  if ((r.trades ?? []).length) {
    const trBtn = el('button', { class: 'btn btn--sm', type: 'button' }, t('u2.ana.export_tr'));
    trBtn.addEventListener('click', () => downloadCSV(`${t('u2.csv.trades')}_${id}.csv`,
      [['entry', t('u2.csv.entry')], [(x) => (x.open ? t('u2.bt.holding') : x.exit), t('u2.csv.exit')], ['days', t('u2.csv.days')], ['ret', t('u2.csv.ret')],
        ['entry_px', t('u2.csv.entry_px')], ['exit_px', t('u2.csv.exit_px')], ['pnl', t('u2.csv.pnl')]], r.trades));
    btns.push(trBtn);
  }
  return el('div', { class: 'ws-filter u2-exports' }, ...btns);
}

/** 返回分析区的节点数组；r.analysis 为空（接口没给）时返回 [] */
export function analysisNodes(r) {
  const a = r.analysis;
  if (!a) return [];
  const gap = (n) => (n ? el('div', { class: 'u2-gap' }, n) : null);
  return [
    assumptions(a), gap(coreCards(a)), gap(split(a, r)), gap(monthly(a)), gap(chartSection(a, r)), gap(tradeStats(a)),
    gap(costSens(a)), gap(monteCarlo(a)), gap(paramSens(a)), exports(r)].filter(Boolean);
}
