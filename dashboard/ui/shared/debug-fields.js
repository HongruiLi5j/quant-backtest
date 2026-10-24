/* ==========================================================
 * 文件: ui/shared/debug-fields.js
 * 用途: 地址栏带 ?debug=1 时，在页脚列出「接口返回里缺少的字段」（任务 N04）。
 *   - api.js 每拿到一个 GET 返回就调用 record(path, body)；这里按 SPECS 里登记的字段清单逐项检查
 *   - SPECS 的字段来自两处：后端实现（app.py / src/ 里构造返回值的代码）和页面里实际读取的字段
 *   - 路径写法：a.b 逐层；items[] 表示数组（检查前 30 项）；* 表示对象的每个键；末尾 ? 表示可选字段
 *   - 只看「键在不在」，不看值：null 算存在（例如 ytd 可能是 null，页面自己显示 —）
 *   - 返回里 empty 为 true 或 error 是文字时，认为是业务空态 / 提示，不再往下查，单独标注
 *   - 页脚只写字段名，不写任何数值；全部走 textContent（el()），不拼 HTML
 * ========================================================== */

import { el } from './dom.js';
import { t } from './i18n.js';

const MAX_ITEMS = 30;

/** 一组列表字段：prefix[].a, prefix[].b …（prefix 为空时直接用字段名） */
const list = (prefix, fields) => fields.map((f) => `${prefix}[].${f}`);
const horizon = (n) => [`n${n}`, `avg${n}`, `beat${n}`];
const trackSpec = (root) => [
  root, `${root}.total`, `${root}.since`, `${root}.latest`, `${root}.latest_price_date`, `${root}.timezone_label`, `${root}.expected_first_5`,
  ...list(`${root}.summary`, ['strategy', 'action', 'signals', ...horizon(5), ...horizon(20), ...horizon(60)]),
  ...list(`${root}.recent`, ['signal_date', 'code', 'name', 'strategy', 'action', 'r5', 'r20', 'r60', 'r_now', 'x_now']),
];
const monitorQuotes = ['ok', ...list('quotes', ['code', 'name', 'price', 'pct', 'received_at', 'alert', 'sources', 'quality']),
  'status.state', 'status.reason', 'status.phase', 'status.requests_used', 'status.request_budget', 'status.watch_count', 'status.max_watch', 'status.rounds',
  'status.truncated', 'status.interval', 'status.top_n', 'status.degraded?', 'status.batch_requested?', 'status.batch_mode', 'intraday?', 'intraday.events?', 'intraday.tracking?'];
const monitorAlerts = ['ok', ...list('alerts', ['triggered_at', 'code', 'direction', 'threshold', 'previous_price', 'price'])];

export const SPECS = {
  '/api/meta': ['markets', 'default_market', 'strategies', 'personal_mode',
    ...['name', 'main', 'universe', 'stocks', 'timezone', 'timezone_label', 'stock_cost_bp', 'cost_bp', 'source'].map((f) => `markets.*.${f}`),
    ...['label', 'desc', 'kind', 'params'].map((f) => `strategies.*.${f}`)],
  '/api/summary': ['market', 'market_name', 'main', 'main_name', 'data_date', 'generated', 'timezone_label', 'launch', 'health', 'notes',
    ...list('strategies', ['key', 'label', 'action', 'now', 'next', 'next_total', 'reason', 'stats', 'stats.CAGR%', 'stats.ExcessSharpe', 'stats.MaxDD%', 'stats.Vol%']),
    ...list('watch', ['symbol', 'name', 'close', 'd1', 'ytd', 'ma300_gap', 'vol', 'dd1y', 'rsi14'])],
  '/api/tracking': ['notifications', ...list('notifications', ['time', 'body']), ...trackSpec('eval'), ...trackSpec('replay')],
  '/api/main_strategy': ['main', 'reason'],
  '/api/trade_list_dates': ['dates', 'latest'],
  '/api/trade_list': ['market', 'data_date', 'generated_at', 'strategy', 'label', 'threshold', 'min_target', 'disclaimer', 'limitations', 'performance_note',
    'counts', 'counts_main', 'counts_small', 'available_strategies', ...list('available_strategies', ['key', 'label']), 'exceptions',
    ...list('rows', ['code', 'name', 'last_date', 'now', 'next', 'action', 'close', 'price_note', 'd1', 'reason', 'performance', 'flags',
      'liquidity', 'avg_turnover_20d', 'turnover_days', 'lot_cost']),
    ...list('exceptions', ['code', 'name', 'flags', 'reason'])],
  '/api/trade_list_tracking': ['strategy', 'note', 'groups', 'recorded_signals', 'latest_price_date',
    ...list('groups', ['action', 'signals', 'n5', 'avg5', 'bench_n5', 'beat5', 'n20', 'avg20', 'bench_n20', 'beat20', 'n60', 'avg60', 'bench_n60', 'beat60'])],
  '/api/trade_list_mine': ['strategy', 'member_logged_in', ...list('rows', ['code', 'name', 'action', 'now', 'next', 'in_watchlist', 'in_member_positions'])],
  '/api/ledger': ['today', 'signal_days', 'evaluated_days', 'main', 'main_pick', 'promotion', 'precision', 'notes', 'caveats',
    'main_pick.main', 'main_pick.reason', 'promotion.message', 'promotion.complete_weeks'],
  '/api/scan': ['total', 'page', 'size', 'meta', 'boards', 'industries', 'meta.data_date', 'meta.stocks', 'meta.excluded_st', 'meta.delisted', 'meta.split', 'meta.strategies',
    'meta.excluded?',
    ...['label', 'beat_bh_pct', 'buy', 'sell', 'holding_pct'].map((f) => `meta.strategies.*.${f}`),
    ...['beat_is', 'beat_oos', 'persistence', 'top_pick_oos', 'all_oos', 'with_delisted'].map((f) => `meta.strategies.*.${f}?`),
    ...list('rows', ['code', 'name', 'board', 'industry', 'last_date', 'd1', 'next', 'action', 'cagr', 'sharpe', 'maxdd', 'y1', 'win', 'b_cagr', 'b_sharpe', 'b_maxdd', 'sharpe_is', 'sharpe_oos']),
    ...list('rows', ['m1?', 'mcap?', 'mcap_rank?'])],
  '/api/table': ['pending', ...list('rows', ['symbol', 'name', 'kind', 'd1', 'next', 'action', 'cagr', 'sharpe', 'maxdd', 'y1', 'win', 'trades', 'b_cagr', 'b_sharpe', 'b_maxdd'])],
  '/api/watchlist': ['stocks', 'pending'],
  '/api/stock': ['strategy', 'period', 'cost_bp', 'dates', 'open', 'high', 'low', 'close', 'volume', 'lines', 'sub', 'ind', 'position', 'equity', 'bench', 'trades', 'compare', 'next', 'action',
    ...['code', 'name', 'kind', 'board', 'industry', 'last_date', 'd1', 'm1', 'y1', 'rows', 'first_date'].map((f) => `info.${f}`), 'info.watch?',
    ...list('lines', ['name', 'values', 'dash']), 'sub.name?', 'sub.values?',
    'ind.boll.up', 'ind.boll.mid', 'ind.boll.low', 'ind.macd.dif', 'ind.macd.dea', 'ind.macd.hist', 'ind.rsi', 'ind.kdj.k', 'ind.kdj.d', 'ind.kdj.j',
    ...list('trades', ['entry', 'exit', 'entry_px', 'exit_px', 'days', 'ret', 'open', 'px_ret']),
    ...list('compare', ['key', 'label', 'cagr', 'sharpe', 'maxdd', 'win', 'trades', 'next', 'action'])],
  '/api/backtest': ['strategy', 'symbol', 'params', 'cost_bp', 'is_stock', 'start', 'end', 'dates', 'equity', 'bench', 'drawdown', 'stats', 'bench_stats', 'recent',
    'position_now', 'position_next', 'trades', 'report', 'analysis',
    ...list('trades', ['entry', 'exit', 'days', 'ret', 'open']),
    ...['assumptions', 'full', 'bench_full', 'split', 'monthly', 'bench_year', 'rolling_sharpe', 'bench_rolling_sharpe', 'extra', 'trades', 'exposure', 'benchmarks', 'cost_sens', 'mc', 'param_sens'].map((f) => `analysis.${f}`)],
  '/api/holdings': ['ok', 'as_of', 'capital', 'summary', 'positions', 'closed', 'trades',
    ...['market_value', 'total_cost', 'floating_pnl', 'realized', 'dividends', 'invested_ratio', 'missing_price_count'].map((f) => `summary.${f}`),
    ...list('positions', ['code', 'name', 'shares', 'avg_cost', 'first_buy', 'held_days', 'price', 'price_date', 'market_value', 'floating_pnl', 'floating_pct', 'weight']),
    ...list('closed', ['code', 'name', 'first_buy', 'last_sell', 'held_days', 'pnl']),
    ...list('trades', ['id', 'trade_date', 'code', 'name', 'side', 'qty', 'price', 'fee', 'fee_estimated', 'signal_ref', 'note', 'voided'])],
  '/api/market_monitor/quotes': monitorQuotes,
  '/api/us_market_monitor/quotes': monitorQuotes,
  '/api/market_monitor/alerts': monitorAlerts,
  '/api/us_market_monitor/alerts': monitorAlerts,
};

/* ---------- 检查 ---------- */

const has = (o, k) => o !== null && typeof o === 'object' && Object.prototype.hasOwnProperty.call(o, k);
/** 业务空态 / 提示：这一层不再往下查 */
const skipNode = (o) => o.empty === true || typeof o.error === 'string' || o.missing === true;

function walk(node, segs, base, out) {
  if (node === null || typeof node !== 'object' || !segs.length) return;
  if (!Array.isArray(node) && skipNode(node)) return;
  const [head, ...rest] = segs;
  const isArr = head.endsWith('[]');
  const key = isArr ? head.slice(0, -2) : head;
  const keys = key === '*' ? Object.keys(node) : [key];
  for (const k of keys) {
    const path = base ? `${base}.${k}${isArr ? '[]' : ''}` : `${k}${isArr ? '[]' : ''}`;
    if (!has(node, k)) { out.add(path); continue; }
    const child = node[k];
    if (!rest.length) continue;
    if (isArr) {
      if (Array.isArray(child)) child.slice(0, MAX_ITEMS).forEach((item) => walk(item, rest, path, out));
    } else {
      walk(child, rest, path, out);
    }
  }
}

/** 返回 { missing: [...], optional: [...], note } */
export function check(spec, body) {
  const missing = new Set(); const optional = new Set();
  if (body === null || typeof body !== 'object') return { missing: ['(不是对象)'], optional: [], note: '' };
  if (!Array.isArray(body) && skipNode(body)) return { missing: [], optional: [], note: body.empty === true ? 'empty' : 'error' };
  for (const raw of spec) {
    const opt = raw.endsWith('?');
    walk(body, (opt ? raw.slice(0, -1) : raw).split('.'), '', opt ? optional : missing);
  }
  return { missing: [...missing].sort(), optional: [...optional].sort(), note: '' };
}

/* ---------- 页脚 ---------- */

const results = new Map();   // 接口路径 → 最近一次结果
let footer = null;

export function resetDebug() {
  results.clear();
  draw();
}

function draw() {
  if (!footer || !document.body.contains(footer)) {
    footer = el('section', { class: 'ws-debug', 'aria-label': t('ui.debug.title'),
      style: { margin: '24px auto 0', 'max-width': '1200px', padding: '12px 16px', 'font-size': '12px', 'line-height': '1.6', border: '1px dashed currentColor', opacity: '0.85' } });
    document.body.append(footer);
  }
  const rows = [...results.entries()].map(([path, r]) => {
    const status = r.note ? t('ui.debug.biz')
      : r.missing.length ? `${t('ui.debug.missing')}：${r.missing.join('、')}` : t('ui.debug.ok');
    return el('div', {}, el('code', {}, path), ' — ', status,
      r.optional.length ? el('div', { style: { opacity: '0.7' } }, `${t('ui.debug.optional')}：${r.optional.join('、')}`) : null);
  });
  footer.replaceChildren(el('strong', {}, t('ui.debug.title')),
    ...(rows.length ? rows : [el('div', {}, t('ui.debug.none'))]));
}

/** api.js 每个 GET 返回后调用；未登记的接口直接忽略 */
export function record(path, body) {
  const name = path.split('?')[0];
  const spec = SPECS[name];
  if (!spec) return;
  results.set(name, check(spec, body));
  draw();
}
