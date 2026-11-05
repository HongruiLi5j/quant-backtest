/* ==========================================================
 * 文件: ui/pages/research-factor.js —— 研究 › Factor 标签：T008 因子检验「成本墙」（N24）
 * 数据: ../research-data/factor_view.json（scripts/export_factor_view.py 从 T008 训练段冻结结果导出的静态文件，
 *       结果不会再变，所以不走 /api）。只读，不发任何 /api 请求。
 * 内容: 总览数字 → 散点图（横轴月换手对数刻度，纵轴年化超额，可切换扣成本前/后）→ 点一个点看「体检卡」
 *       （分 5 组收益柱图、交易成本、四条淘汰规则）。文字全部走 t('n24.…')，红涨绿跌跟随 --fill-rise/--fill-fall。
 * ========================================================== */

import { el, svg } from '../shared/dom.js';
import { t, lang } from '../shared/i18n.js';
import { fmt } from './u4-util.js';

const DATA_URL = new URL('../research-data/factor_view.json', import.meta.url);
const W = 900, H = 380, PL = 56, PR = 16, PT = 16, PB = 40;
const X_TICKS = [0.1, 0.3, 1, 3, 10, 30];
let cache = null;
const ui = { mode: 'gross', sel: null };

async function load() {
  if (cache) return cache;
  const res = await fetch(DATA_URL, { cache: 'no-store' });
  if (!res.ok) throw new Error(String(res.status));
  const data = await res.json();
  if (data?.schema !== 'factor_view/1' || !Array.isArray(data.rows)) throw new Error('schema');
  cache = data;
  return data;
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const sign = (v, d, unit = '') => (isNum(v) ? fmt(v, d, unit, true) : '—');
const plain = (v, d) => (isNum(v) ? fmt(v, d) : '—');
const toPct = (v) => (isNum(v) ? `${Math.round(v * 100)}%` : '—');
const tone = (v) => (!isNum(v) ? 'is-flat' : v > 0 ? 'is-rise' : v < 0 ? 'is-fall' : 'is-flat');
const label = (r) => (lang() === 'zh' && r.name ? `${r.name} · ${r.key}` : r.key);

function metric(k, v, cls = '') {
  return el('div', { class: 'metric' }, el('span', { class: 'metric__label' }, t(k)), el('span', { class: `metric__value ${cls}` }, v));
}

function summary(d) {
  const rows = d.rows;
  return el('div', { class: 'ws-grid ws-grid--kpi' },
    metric('n24.fv.k_total', String(d.total)),
    metric('n24.fv.k_surv', String(d.survivors)),
    metric('n24.fv.k_mono', String(rows.filter((r) => r.mono_passed).length)),
    metric('n24.fv.k_gross', String(rows.filter((r) => r.gross_pct > 0).length)),
    metric('n24.fv.k_net', String(rows.filter((r) => r.net_pct > 0).length)));
}

function niceStep(raw) { const p = 10 ** Math.floor(Math.log10(raw)); const n = raw / p; return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * p; }

function scatter(d, onPick) {
  const pts = d.rows.filter((r) => r.turnover > 0 && isNum(r.gross_pct) && isNum(r.net_pct));
  const ys = pts.flatMap((r) => [r.gross_pct, r.net_pct]);
  const ymin = Math.floor(Math.min(0, ...ys) / 20) * 20, ymax = Math.ceil(Math.max(0, ...ys) / 20) * 20;
  const lx = Math.log10(X_TICKS[0]), hx = Math.log10(X_TICKS[X_TICKS.length - 1]);
  const X = (v) => PL + (Math.log10(Math.min(Math.max(v, X_TICKS[0]), X_TICKS[X_TICKS.length - 1])) - lx) / (hx - lx) * (W - PL - PR);
  const Y = (v) => PT + (ymax - v) / (ymax - ymin || 1) * (H - PT - PB);
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'n24-scatter', role: 'img', 'aria-label': t('n24.fv.chart_aria', { n: pts.length }) });
  for (let v = ymin; v <= ymax; v += 20) {
    root.append(svg('line', { x1: PL, x2: W - PR, y1: Y(v), y2: Y(v), class: 'n24-grid' }));
    const tx = svg('text', { x: PL - 8, y: Y(v) + 4, 'text-anchor': 'end' }); tx.textContent = `${v > 0 ? '+' : ''}${v}%`; root.append(tx);
  }
  for (const v of X_TICKS) {
    root.append(svg('line', { x1: X(v), x2: X(v), y1: PT, y2: H - PB, class: 'n24-grid' }));
    const tx = svg('text', { x: X(v), y: H - PB + 16, 'text-anchor': 'middle' }); tx.textContent = `${Math.round(v * 100)}%`; root.append(tx);
  }
  const xl = svg('text', { x: (W + PL) / 2, y: H - 6, 'text-anchor': 'middle' }); xl.textContent = t('n24.fv.x_axis'); root.append(xl);
  root.append(svg('line', { x1: PL, x2: W - PR, y1: Y(0), y2: Y(0), class: 'n24-zero' }));
  const cap = 1;   // 规则 2：月换手上限 100%
  root.append(svg('line', { x1: X(cap), x2: X(cap), y1: PT, y2: H - PB, class: 'n24-cap' }));
  const cl = svg('text', { x: X(cap) + 6, y: PT + 12, class: 'n24-cap-label' }); cl.textContent = t('n24.fv.cap'); root.append(cl);
  const key = ui.mode === 'gross' ? 'gross_pct' : 'net_pct';
  for (const r of pts) {
    const v = r[key];
    const c = svg('circle', {
      cx: X(r.turnover), cy: Y(v), r: r === ui.sel ? 7 : 4, tabindex: 0,
      class: `n24-dot ${v > 0 ? 'is-up' : 'is-down'}${r.mono_passed ? ' is-mono' : ''}${r === ui.sel ? ' is-sel' : ''}`,
    });
    const tip = svg('title', {});
    tip.textContent = t('n24.fv.dot_tip', { key: r.key, to: toPct(r.turnover), v: sign(v, 1, '%') });
    c.append(tip);
    c.addEventListener('click', () => onPick(r));
    c.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPick(r); } });
    root.append(c);
  }
  return el('div', { class: 'n24-chart-wrap' }, root);
}

function groupChart(r) {
  const gw = 420, gh = 200, pl = 44, pb = 28, pt = 16, pr = 10;
  const root = svg('svg', { viewBox: `0 0 ${gw} ${gh}`, class: 'n24-groups', role: 'img', 'aria-label': t('n24.fv.groups_title') });
  const g = r.groups_pct || [];
  if (!g.length) {
    const tx = svg('text', { x: gw / 2, y: gh / 2, 'text-anchor': 'middle' }); tx.textContent = t('n24.fv.no_groups'); root.append(tx); return root;
  }
  const max = Math.max(0, ...g), min = Math.min(0, ...g), span = (max - min) || 1;
  const top = max + span * 0.14, bot = min < 0 ? min - span * 0.14 : 0;
  const y = (v) => pt + (top - v) / (top - bot) * (gh - pt - pb);
  const step = niceStep((top - bot) / 4);
  for (let v = Math.ceil(bot / step) * step; v <= top + 1e-9; v += step) {
    root.append(svg('line', { x1: pl, x2: gw - pr, y1: y(v), y2: y(v), class: 'n24-grid' }));
    const tx = svg('text', { x: pl - 6, y: y(v) + 4, 'text-anchor': 'end' }); tx.textContent = `${v.toFixed(step < 1 ? 1 : 0)}%`; root.append(tx);
  }
  const bw = (gw - pl - pr) / g.length;
  g.forEach((v, i) => {
    const x = pl + i * bw + bw * 0.18, w = bw * 0.64, last = i === g.length - 1;
    root.append(svg('rect', { x, width: w, y: Math.min(y(v), y(0)), height: Math.max(1, Math.abs(y(v) - y(0))), rx: 3, class: last ? 'n24-bar is-top' : 'n24-bar' }));
    const vl = svg('text', { x: x + w / 2, y: Math.min(y(v), y(0)) - 5, 'text-anchor': 'middle' }); vl.textContent = v.toFixed(2); root.append(vl);
    const gl = svg('text', { x: x + w / 2, y: gh - 8, 'text-anchor': 'middle' });
    gl.textContent = i === 0 ? t('n24.fv.g_low') : last ? t('n24.fv.g_high', { n: g.length }) : `G${i + 1}`;
    root.append(gl);
  });
  root.append(svg('line', { x1: pl, x2: gw - pr, y1: y(0), y2: y(0), class: 'n24-zero' }));
  return root;
}

function costBlock(r) {
  if (!isNum(r.gross_pct) || !isNum(r.net_pct)) return el('p', { class: 'muted' }, t('n24.fv.no_cost'));
  const cost = r.gross_pct - r.net_pct;
  const m = Math.max(Math.abs(r.gross_pct), Math.abs(r.net_pct), Math.abs(cost), 1);
  const row = (k, v, cls, text) => el('div', { class: 'n24-cost-row' },
    el('span', {}, t(k)),
    el('span', { class: 'n24-cost-bar' }, el('i', { class: cls, style: { width: `${Math.abs(v) / m * 100}%` } })),
    el('span', { class: `num ${k === 'n24.fv.cost' ? '' : tone(v)}` }, text));
  return el('div', { class: 'n24-cost' },
    row('n24.fv.gross', r.gross_pct, r.gross_pct >= 0 ? 'is-up' : 'is-down', sign(r.gross_pct, 1, '%')),
    row('n24.fv.cost', cost, 'is-cost', `−${plain(Math.abs(cost), 1)}%`),
    row('n24.fv.net', r.net_pct, r.net_pct >= 0 ? 'is-up' : 'is-down', sign(r.net_pct, 1, '%')),
    el('p', { class: 'muted n24-small' }, t('n24.fv.turnover_line', { to: toPct(r.turnover) })));
}

function rulesBlock(r) {
  return el('ul', { class: 'n24-rules' }, ...[1, 2, 3, 4].map((n) => {
    const hit = r.rules_hit.includes(n);
    return el('li', { class: hit ? 'is-hit' : 'is-pass' }, `${hit ? '✗' : '✓'} ${t(`n24.fv.rule${n}`)}`);
  }));
}

function card(r) {
  return el('section', { class: 'card n24-card', 'aria-label': r.key },
    el('div', { class: 'card__head' },
      el('h2', { class: 'card__title n24-key' }, label(r)),
      el('span', { class: 'n24-badges' },
        el('span', { class: 'badge badge--warn' }, t('n24.fv.eliminated')),
        r.mono_passed ? el('span', { class: 'badge badge--accent' }, t('n24.fv.mono_ok')) : null)),
    el('div', { class: 'ws-grid n24-mini' },
      metric('n24.fv.ic', plain(r.ic, 3)), metric('n24.fv.rho', plain(r.rho, 2)),
      metric('n24.fv.spread', sign(r.spread_pct, 2, '%')), metric('n24.fv.p', plain(r.p_value, 3))),
    el('div', { class: 'ws-grid ws-grid--2 n24-detail' },
      el('div', { class: 'n24-panel' }, el('h3', { class: 'n24-h3' }, t('n24.fv.groups_title')), groupChart(r)),
      el('div', { class: 'n24-panel' }, el('h3', { class: 'n24-h3' }, t('n24.fv.cost_title')), costBlock(r),
        el('h3', { class: 'n24-h3' }, t('n24.fv.rules_title')), rulesBlock(r))));
}

/** 默认选中：扣成本后最好的组合（体检卡里能看到它为什么仍被淘汰） */
function pickDefault(rows) {
  return [...rows].filter((r) => isNum(r.net_pct)).sort((a, b) => b.net_pct - a.net_pct)[0] || rows[0];
}

function view(d) {
  const box = el('div', { class: 'n24' });
  const draw = () => {
    const seg = el('div', { class: 'segmented', role: 'group', 'aria-label': t('n24.fv.mode_aria') },
      ...['gross', 'net'].map((m) => {
        const b = el('button', { class: 'segmented__btn', type: 'button', 'aria-pressed': String(ui.mode === m) }, t(`n24.fv.mode_${m}`));
        b.addEventListener('click', () => { ui.mode = m; draw(); });
        return b;
      }));
    box.replaceChildren(
      el('p', { class: 'muted n24-intro' }, t('n24.fv.intro', { a: d.segment_range?.[0] ?? '—', b: d.segment_range?.[1] ?? '—', n: d.total, h: d.horizon_days ?? '—' })),
      summary(d),
      el('section', { class: 'card n24-chart-card' },
        el('div', { class: 'card__head' }, el('h2', { class: 'card__title' }, t('n24.fv.chart_title')), seg),
        scatter(d, (r) => { ui.sel = r; draw(); }),
        el('p', { class: 'muted n24-small' }, t('n24.fv.legend'))),
      card(ui.sel),
      el('p', { class: 'muted n24-small' }, t('n24.fv.source', { run: d.source?.run ?? '—', sha: (d.source?.record_sha256 || '').slice(0, 12) || '—' })));
  };
  if (!ui.sel || !d.rows.includes(ui.sel)) ui.sel = pickDefault(d.rows);
  draw();
  return box;
}

/** 给 research.js 用：先返回占位，数据到了再换成正文；失败显示固定文案 */
export function factorView() {
  const host = el('div', { class: 'n24-host' }, el('div', { class: 'skeleton', style: { height: '320px' } }));
  load().then((d) => host.replaceChildren(view(d))).catch(() => host.replaceChildren(
    el('div', { class: 'card state' }, el('p', { class: 'state__title' }, t('n24.fv.err_title')), el('p', { class: 'muted' }, t('n24.fv.err_hint')))));
  return host;
}
