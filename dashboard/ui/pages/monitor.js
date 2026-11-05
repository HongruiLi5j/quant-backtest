/* ==========================================================
 * 文件: ui/pages/monitor.js
 * 用途: Workspace · 盯盘（A 股）与美股盯盘共用的页面逻辑。地址 monitor.html 是 A 股，monitor.html?market=us 是美股
 *   （nav.js 的「美股盯盘」就指向后者；对应旧界面 #/monitor 与 #/usmonitor）
 *   控制卡（状态 / 开始 / 停止 / 配置）+ 实时分析（pages/monitor-ia.js）+ 监控股票表 + 告警历史
 * 接口: A 股 /api/market_monitor/{quotes,alerts,start,stop,config}；美股 /api/us_market_monitor/ 同名
 * 说明: 页面打开期间每 10 秒轮询本机接口；换语言 / 按 R 重新渲染时旧定时器会先清掉。报价只放内存
 * ========================================================== */

import { el } from '../shared/dom.js';
import { t } from '../shared/i18n.js';
import { getJSON, ApiError } from '../shared/api.js';
import { messageBox, errKey, post, isNum, pctText, tone, span, table, td, hasKey } from './u3-common.js';
import { intradaySection, clock, eventLine } from './monitor-ia.js';
import { buildConfig } from './monitor-config.js';

const POLL_MS = 10000;
let timer = null;

const marketOf = () => (new URLSearchParams(window.location.search).get('market') === 'us' ? 'us' : 'cn');
const keyIf = (key, fallback) => (hasKey(key) ? key : fallback);

/** 状态 / 阶段 / 原因 / 质量 的词条：美股优先用 usmon.*，找不到回退 */
function stateKeys(us, s) {
  return {
    state: keyIf(`mon.state.${s.state}`, 'mon.state.stopped'),
    phase: us ? keyIf(`usmon.phase.${s.phase}`, 'usmon.phase.idle') : keyIf(`mon.phase.${s.phase}`, 'mon.phase.idle'),
    reason: us ? keyIf(`usmon.reason.${s.reason}`, 'usmon.reason.provider_error') : keyIf(`mon.reason.${s.reason}`, 'mon.reason.provider_error'),
  };
}

function statusLines(s, us, cfg) {
  if (!s) return [];
  const k = stateKeys(us, s);
  const lines = [
    el('p', {}, t('mon.status.line', { state: t(k.state), phase: t(k.phase) })),
    el('p', { class: 'muted' }, t('mon.status.stats', { used: s.requests_used, budget: s.request_budget, count: s.watch_count, max: s.max_watch, rounds: s.rounds })),
  ];
  if (s.state === 'paused' && s.reason) lines.push(el('p', { class: 'u3-warn' }, t('mon.reason.line', { reason: t(k.reason) })));
  // D15（C27）：参数是否可行。估算由后端按现有限速规则给出（scheduler._estimate），前端不重复写限速常数
  const e = s.estimate;
  if (e && isNum(e.round_seconds) && isNum(e.rounds_affordable)) {
    const bad = e.round_seconds > 3600 || e.rounds_affordable < 1;
    const mins = e.round_seconds >= 120 ? t('c15.minutes', { n: Math.round(e.round_seconds / 60) }) : t('c15.seconds', { n: e.round_seconds });
    lines.push(el('p', { class: bad ? 'u3-warn' : 'muted' },
      t(e.basis === 'watch' ? 'c15.est_watch' : 'c15.est_max', { n: e.count, round: mins, req: e.requests_per_round, left: e.rounds_affordable }),
      bad ? ` ${t('c15.est_bad')}` : ''));
  }
  if (s.truncated) lines.push(el('p', { class: 'u3-warn' }, t('mon.status.truncated')));
  if (!us) {
    // 降级（批量连续失败，实际在用 tencent）与「请求了批量但没装 easyquotation」是两种不同状态
    if (s.degraded) lines.push(el('p', { class: 'u3-warn' }, t('mon.status.degraded')));
    else if (s.batch_requested === true && s.batch_mode === false) lines.push(el('p', { class: 'u3-warn' }, t('mon.status.easy_missing')));
  }
  return lines;
}

function quoteRow(q, us) {
  {
    const sources = (Array.isArray(q.sources) ? q.sources : []).map((x) => t(keyIf(`mon.src.${x}`, 'mon.src.watchlist'))).join(' / ');
    const qualityKey = us ? keyIf(`usmon.quality.${q.quality}`, 'usmon.quality.invalid') : keyIf(`mon.quality.${q.quality}`, 'mon.quality.pending');
    const bad = q.quality === 'invalid' || q.quality === 'stale';
    return el('tr', {},
      td(q.code, false, 'mono'), td(q.name || '—'),
      td(isNum(q.price) ? Number(q.price).toFixed(2) : '—', true),
      td(span(pctText(q.pct), tone(q.pct) || 'is-flat'), true),
      td(clock(q.received_at), true, 'muted'),
      td([t(keyIf(`mon.alert.${q.alert}`, 'mon.alert.none')), stopNote(q)]),
      td(sources, false, 'muted'),
      td(el('span', { class: `badge ${bad ? 'badge--warn' : ''}`.trim() }, t(qualityKey))));
  }
}

/* ---- D108：持仓止损线（固定止损 / 海龟出场线）----
 * 后端 quotes 行的 stop = [{kind, price, as_of}]，按价格从高到低；只给持仓股，不含成本。
 * 显示最高的那条（价格往下最先碰到）和现价离它多远；现价已经不高于它时标出来。规则都按收盘价判断，盘中只是预警。 */
export function stopInfo(q) {
  const lines = Array.isArray(q.stop) ? q.stop.filter((x) => isNum(x?.price) && x.price > 0) : [];
  if (!lines.length) return null;
  const top = lines.reduce((a, b) => (b.price > a.price ? b : a));
  const gap = isNum(q.price) && q.price > 0 ? (q.price / top.price - 1) * 100 : null;
  return { top, lines, gap, below: gap !== null && gap <= 0 };
}

function stopNote(q) {
  const s = stopInfo(q);
  if (!s) return null;
  const name = (x) => t(keyIf(`c108.kind.${x.kind}`, 'c108.kind.fixed'));
  const title = s.lines.map((x) => `${name(x)} ${Number(x.price).toFixed(2)}`).join(' · ') + ` — ${t('c108.basis')}`;
  const text = s.below ? t('c108.below', { k: name(s.top), p: Number(s.top.price).toFixed(2) })
    : t('c108.line', { k: name(s.top), p: Number(s.top.price).toFixed(2), g: s.gap === null ? '—' : s.gap.toFixed(1) });
  return el('span', { class: `u1-sub ${s.below ? 'is-warn' : 'muted'}`.trim(), title, style: { display: 'block' } }, text);
}

/* ---- D15（A7，C25–C29）：监控股票表的「只看异动」视图 ----
 * 以前每 10 秒把全部报价整表重建（5000 只时 55,211 个节点、页面 201 屏、每轮主线程卡约 110 ms）。
 * 现在：默认只看异动（持仓、自选、目标价已触发、涨跌幅绝对值 ≥ 3%、数据过旧 / 异常），可切到「显示全部」；
 * 每页 100 行，可排序、可按代码 / 名称搜索（搜索查全部名单）；每轮只替换数据变了的行，不重建整表。
 * 只改显示，不改轮询频率和后端限速。 */
const PAGE = 100;
const MOVE_PCT = 3;
const HEADS = ['mon.col.code', 'mon.col.name', 'mon.col.price', 'mon.col.pct', 'mon.col.time', 'mon.col.alert', 'mon.col.source', 'mon.col.quality'];
const NUM_COLS = new Set([2, 3, 4]);

/** 这只算不算「异动」（纯函数） */
export function isMover(q) {
  const src = Array.isArray(q.sources) ? q.sources : [];
  return src.includes('holding') || src.includes('watchlist') || q.alert === 'triggered'
    || (isNum(q.pct) && Math.abs(Number(q.pct)) >= MOVE_PCT) || q.quality === 'invalid' || q.quality === 'stale';
}

const SORTS = {
  abs: (a, b) => Math.abs(Number(b.pct) || 0) - Math.abs(Number(a.pct) || 0),
  up: (a, b) => (Number(b.pct) || -Infinity) - (Number(a.pct) || -Infinity),
  down: (a, b) => (Number(a.pct) || Infinity) - (Number(b.pct) || Infinity),
  code: (a, b) => String(a.code).localeCompare(String(b.code)),
};

/** 当前要显示哪些行（纯函数）：v = { all, q, sort, page } */
export function viewRows(quotes, v) {
  const key = (v.q || '').trim().toLowerCase();
  let rows = key ? quotes.filter((r) => String(r.code).toLowerCase().includes(key) || String(r.name || '').toLowerCase().includes(key))
    : (v.all ? quotes : quotes.filter(isMover));
  if (SORTS[v.sort]) rows = [...rows].sort(SORTS[v.sort]);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  const page = Math.min(Math.max(0, v.page || 0), pages - 1);
  return { rows: rows.slice(page * PAGE, (page + 1) * PAGE), total: rows.length, movers: quotes.filter(isMover).length, pages, page };
}

/** 建一次控件和表头，之后 update(quotes) 只换变了的行 */
function quotesView(us) {
  const v = { all: false, q: '', sort: 'abs', page: 0 };
  let quotes = [], shown = [], state = null;   // shown: [{ code, sig, tr }]；state：运行状态（stopped / running / paused…）
  const toggle = el('button', { class: 'btn btn--sm', type: 'button', 'aria-pressed': 'false' }, t('c15.show_all', { n: 0 }));   // 文字在 paint() 里按实际只数更新
  const search = el('input', { class: 'ws-input', type: 'search', placeholder: t('c15.search_ph'), 'aria-label': t('c15.search_ph'), style: { 'min-width': '240px' } });
  const sort = el('select', { class: 'ws-input', 'aria-label': t('c15.sort') },
    ...Object.keys(SORTS).map((k) => el('option', { value: k }, t(`c15.sort.${k}`))));
  const prev = el('button', { class: 'btn btn--sm', type: 'button' }, t('c15.prev'));
  const next = el('button', { class: 'btn btn--sm', type: 'button' }, t('c15.next'));
  const info = el('span', { class: 'muted', role: 'status' });
  const empty = el('p', { class: 'ws-note u3-empty', role: 'status' });
  const tbody = el('tbody');
  const tbl = el('div', { class: 'table-wrap', tabindex: 0 }, el('table', { class: 'table' },
    el('thead', {}, el('tr', {}, ...HEADS.map((k, i) => el('th', { scope: 'col', class: NUM_COLS.has(i) ? 'num' : null }, t(k))))), tbody));
  const bar = el('div', { class: 'u3-btn-row c15-bar', style: { 'flex-wrap': 'wrap', gap: '8px', 'align-items': 'center', margin: '8px 0' } },
    toggle, search, sort, prev, next, info);
  const node = el('div', {}, bar, empty, tbl);
  const again = (resetPage) => { if (resetPage) v.page = 0; paint(); };
  toggle.addEventListener('click', () => { v.all = !v.all; again(true); });
  search.addEventListener('input', () => { v.q = search.value; again(true); });
  sort.addEventListener('change', () => { v.sort = sort.value; again(true); });
  prev.addEventListener('click', () => { v.page -= 1; again(false); });
  next.addEventListener('click', () => { v.page += 1; again(false); });

  function paint() {
    const r = viewRows(quotes, v);
    v.page = r.page;
    toggle.textContent = v.all ? t('c15.only_movers', { n: r.movers }) : t('c15.show_all', { n: quotes.length });
    toggle.setAttribute('aria-pressed', String(v.all));
    // 用 display 控制显隐：.u3-empty / .table-wrap 等类自带 display，会盖过 hidden 属性
    const show = (node, on) => node.style.setProperty('display', on ? '' : 'none');
    show(bar, quotes.length > 0);
    prev.disabled = r.page <= 0; next.disabled = r.page >= r.pages - 1;
    info.textContent = quotes.length ? t('c15.info', { shown: r.rows.length, total: r.total, all: quotes.length, p: r.page + 1, pages: r.pages }) : '';
    show(tbl, r.rows.length > 0);
    show(empty, r.rows.length === 0);
    // 散户第二遍待办 2：没在运行时以前也说「名单都为空」，让人以为持仓 / 自选丢了；没运行就直说没运行
    const idle = !state || state === 'stopped' || state === 'idle';
    empty.textContent = !quotes.length
      ? (idle ? t(us ? 'c42.us_idle' : 'c42.idle') : t(us ? 'usmon.q.empty' : 'mon.q.empty'))
      : t(v.q ? 'c15.no_match' : 'c15.no_movers', { n: quotes.length });
    // 只替换变了的行：同一位置、同一代码、数据签名相同就保留原来的 <tr>
    const next_ = r.rows.map((q, i) => {
      const sig = JSON.stringify([q.price, q.pct, q.received_at, q.alert, q.quality, q.sources, q.name]);
      const old = shown[i];
      if (old && old.code === q.code && old.sig === sig) return old;
      const tr = quoteRow(q, us);
      if (old) old.tr.replaceWith(tr); else tbody.append(tr);
      return { code: q.code, sig, tr };
    });
    shown.slice(r.rows.length).forEach((o) => o.tr.remove());
    shown = next_;
  }
  return { node, update(qs, st) { quotes = qs || []; state = st?.state || null; paint(); } };
}

function alertsTable(alerts) {
  const rows = alerts.slice().reverse().map((a) => el('tr', {},
    td(clock(a.triggered_at), true, 'muted'), td(a.code, false, 'mono'),
    td(a.line ? t(keyIf(`c108.dir.${a.line}`, 'c108.dir.fixed')) : t(keyIf(`mon.dir.${a.direction}`, 'mon.dir.up'))),
    td(Number(a.threshold).toFixed(2), true), td(`${Number(a.previous_price).toFixed(2)} → ${Number(a.price).toFixed(2)}`, true)));
  return table(['mon.a.col.time', 'mon.a.col.code', 'mon.a.col.dir', 'mon.a.col.threshold', 'mon.a.col.price'], rows, 'mon.a.empty', { numCols: [0, 3, 4] });
}

/* ---- D68（R3-content-2）：「提醒规则」说明 + 页面内示例 ----
 * 规则和阈值来自后端 status.rules（scheduler._rules，就是盘中分析实际用的 intraday.DEFAULT_THRESHOLDS），前端不抄常数。
 * D108：目标价改为持仓的自动止损线（status.rules.stops / stop_pct）；清单目标价仍然没有，不再写"不会触发"。
 * 「看看提醒长什么样」只在页面里画一条示例：不启动盯盘、不发 Mac 通知、不联网、不写文件。 */
function rulesBlock(status, market) {
  const r = status?.rules;
  const box = el('div', { class: 'c68-rules' });
  if (!r?.thresholds) return box;   // 美股盯盘没有这组规则
  const th = r.thresholds;
  const pct = (x) => `${(x * 100).toFixed(0)}%`;
  const items = [
    t('c68.r.window', { m: th.window_minutes }),
    t('c68.r.limit'),
    t('c68.r.drop', { p: th.drop_pct }),
    t('c68.r.new', { m: th.momentum_minutes, mom: pct(th.momentum), vol: th.volume_ratio, br: pct(th.breakout) }),
  ];
  const sample = el('div', { class: 'c68-sample', 'aria-live': 'polite' });
  const btn = el('button', { class: 'btn btn--sm', type: 'button' }, t('c68.preview'));
  btn.addEventListener('click', () => {
    if (sample.childNodes.length) { sample.replaceChildren(); return; }
    const code = '600000';
    const ev = { kind: 'holding_drop', code, severity: 'danger', timestamp: new Date().toISOString().slice(0, 11) + '10:05:00', params: { pct: -(th.drop_pct + 0.2) } };
    sample.replaceChildren(el('p', { class: 'muted' }, t('c68.preview_note')), eventLine(ev, market));
  });
  box.append(
    el('ul', { class: 'u1-list' }, ...items.map((x) => el('li', {}, x))),
    el('p', { class: 'muted' }, t('c108.rule', { p: isNum(r.stop_pct) ? (r.stop_pct * 100).toFixed(0) : '15' })),
    el('p', { class: 'muted' }, r.stops ? t('c108.count', { n: r.stops }) : t('c108.none')),
    ...(r.targets ? [el('p', { class: 'muted' }, t('c68.targets_some', { n: r.targets }))] : []),   // append(null) 会写出文字 "null"
    el('div', { class: 'u3-btn-row' }, btn), sample);
  return box;
}

/** 顶上的 A 股 / 美股切换（普通链接，保留 ?mock=1） */
function marketTabs(market) {
  const mock = new URLSearchParams(window.location.search).get('mock') === '1';
  const href = (m) => `monitor.html${[m === 'us' ? 'market=us' : '', mock ? 'mock=1' : ''].filter(Boolean).map((x, i) => (i ? '&' : '?') + x).join('')}`;
  return el('div', { class: 'segmented u3-tabs', role: 'tablist', 'aria-label': t('mon.title') },
    ...['cn', 'us'].map((m) => el('a', { class: 'segmented__btn', role: 'tab', href: href(m), 'aria-selected': String(m === market) }   /* axe：role=tab 不允许 aria-pressed */, t(`u3.mon.tab.${m}`))));
}

function section(titleKey, ...children) {
  return el('section', { class: 'card', 'aria-label': t(titleKey) }, el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t(titleKey))), ...children);
}

export async function render(root) {
  if (timer) { clearInterval(timer); timer = null; }
  const market = marketOf(), us = market === 'us';
  const base = us ? '/api/us_market_monitor' : '/api/market_monitor';
  const h1 = document.querySelector('.ws-head__title');
  if (h1) { h1.dataset.i18n = us ? 'usmon.title' : 'mon.title'; h1.textContent = t(us ? 'usmon.title' : 'mon.title'); }
  document.title = `${t(us ? 'usmon.title' : 'mon.title')} · Workspace · Quant Backtest`;

  const st = { status: null, quotes: [], alerts: [], intraday: null, busy: false };
  const msg = messageBox();
  const statusBox = el('div', { id: 'monStatus' });
  const quotesBox = el('div', { id: 'monQuotes' });
  const qv = quotesView(us);   // D15：控件只建一次，轮询时只更新行
  quotesBox.append(qv.node);
  const alertsBox = el('div', { id: 'monAlerts' });
  const rulesBox = el('div', { id: 'monRules' });   // D68
  let rulesKey = null;   // 规则数据没变就不重建（每 10 秒重画时保留示例）
  const iaBox = el('div', { id: 'monIntraday' });

  const start = el('button', { class: 'btn btn--primary', type: 'button', id: 'monStart' }, t('mon.btn.start'));
  const stop = el('button', { class: 'btn btn--ghost', type: 'button', id: 'monStop' }, t('mon.btn.stop'));
  const cfg = buildConfig({ market, base, msg, getStatus: () => st.status, refresh: () => refresh() });

  function draw() {
    statusBox.replaceChildren(...statusLines(st.status, us));
    const running = st.status?.state === 'running';
    start.disabled = running || st.busy;
    stop.disabled = !running && st.status?.state !== 'paused';
    cfg.sync(st.status);
    qv.update(st.quotes, st.status);
    alertsBox.replaceChildren(alertsTable(st.alerts));
    const rk = JSON.stringify(st.status?.rules ?? null);
    if (rk !== rulesKey) { rulesKey = rk; rulesBox.replaceChildren(rulesBlock(st.status, market)); }
    iaBox.replaceChildren(intradaySection(st.intraday, market));
  }

  async function fetchAll() {
    const [q, a] = await Promise.all([getJSON(`${base}/quotes`), getJSON(`${base}/alerts`)]);
    if (q?.ok !== true || a?.ok !== true) throw new ApiError(200, t('a03.err.bad_reply', { api: 'monitor' }), q?.ok !== true ? q : a);   // 显示在错误卡片「原因」里
    st.status = q.status; st.quotes = q.quotes || []; st.alerts = a.alerts || []; st.intraday = q.intraday || null;
  }

  /** 轮询 / 操作后刷新：失败时清掉报价（旧价格不留在页面上）并提示 */
  async function refresh() {
    if (document.hidden) return;
    try {
      await fetchAll();
      msg.clear();
    } catch (err) {
      st.status = null; st.quotes = []; st.alerts = []; st.intraday = null;
      msg.show(errKey(err, us ? ['usmon', 'mon'] : ['mon']));
    }
    draw();
  }

  async function act(path) {
    if (st.busy) return;
    st.busy = true; draw();
    const r = await post(path, {});
    st.busy = false;
    if (!r.ok) { msg.show(errKey(r.err, us ? ['usmon', 'mon'] : ['mon'])); draw(); return; }
    await refresh();
  }
  start.addEventListener('click', () => act(`${base}/start`));
  stop.addEventListener('click', () => act(`${base}/stop`));

  await fetchAll();   // 首次失败直接抛出，由页面启动器显示错误卡片（登录过期有专门提示）
  // .ws-mon[data-market="us"] 只在本页区块内把涨跌色换成美股的绿涨红跌，不改全局偏好
  root.replaceChildren(el('div', { class: 'ws-mon', dataset: { market } },
    marketTabs(market),
    el('p', { class: 'ws-note' }, t(us ? 'usmon.note' : 'mon.note')),
    msg.node,
    section('mon.ctl.title', statusBox, el('div', { class: 'u3-btn-row' }, start, stop), cfg.node),
    // D15（C26）：告警历史放到监控股票表上面——名单很长时以前要滚过整张表才看得到告警
    us ? null : section('c68.title', rulesBox),   // 美股盯盘没有这组规则
    section('mon.a.title', alertsBox),
    section('mon.q.title', el('p', { class: 'ws-note' }, t(us ? 'usmon.q.time_note' : 'mon.q.time_note')), quotesBox),
    iaBox));
  draw();
  timer = setInterval(refresh, POLL_MS);
}
