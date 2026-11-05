/* ==========================================================
 * 文件: ui/shared/api.js
 * 用途: 新界面访问后端的唯一入口。
 *   - getJSON(path) / postJSON(path, body)：相对路径、same-origin，每个请求都带 X-Requested-With
 *   - 401 / 403 统一抛 ApiError（err.auth === true），页面显示「登录已过期，请从首页重新进入」
 *   - 其他 HTTP 错误抛 ApiError（err.status、err.message）；HTTP 200 里的 { error: '...' }
 *     是业务提示（例如「还没有数据」），不抛错，由页面自己判断
 *   - 地址栏带 ?mock=1：读 mock-data/<名字>.json，不发任何网络请求到 /api
 *       名字规则：去掉 /api/ 和查询串，其余 / 换成 _；例如
 *         /api/summary?market=cn            → mock-data/summary.json
 *         /api/market_monitor/quotes        → mock-data/market_monitor_quotes.json
 *       页面也可以显式指定：getJSON(path, { mock: 'today-summary' })
 *       POST 在 mock 模式下：有同名文件就返回它，没有就返回 { ok: true, mock: true }，不改任何数据
 *   旧的 mockup 版 api-config.js 的 USE_MOCK 不再使用。
 * ========================================================== */

import { t, lang } from './i18n.js';   // 错误提示会显示在页面上（卡片、toast），跟随当前语言

const TIMEOUT_MS = 60000;
const MOCK_BASE = new URL('../mock-data/', import.meta.url);

export class ApiError extends Error {
  constructor(status, message, body = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
    this.auth = status === 401 || status === 403;
  }
}

/** 是否 mock 模式（开发预览用） */
export const isMock = () => new URLSearchParams(window.location.search).get('mock') === '1';

/** 只接受以 / 开头的相对路径，防止被拼成外站地址 */
function checkPath(path) {
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//')) {
    throw new Error('api 只接受以 / 开头的相对路径');
  }
}

function mockName(path, override) {
  if (override) return override;
  const clean = path.split('?')[0].replace(/^\/api\//, '').replace(/^\/+|\/+$/g, '');
  return clean.replace(/\//g, '_');
}

async function readMock(name, fallback) {
  const res = await fetch(new URL(`${name}.json`, MOCK_BASE), { cache: 'no-store' });
  if (res.ok) return res.json();
  if (fallback !== undefined) return fallback;
  throw new ApiError(res.status, t('a02.err.mock_missing', { name }));
}

/** ?debug=1 时把 GET 返回交给字段核对（debug-fields.js 才会被加载，平时不影响） */
const isDebug = () => new URLSearchParams(window.location.search).get('debug') === '1';
function debugRecord(path, init, body) {
  if (init.method === 'GET' && isDebug()) import('./debug-fields.js').then((m) => m.record(path, body)).catch(() => {});
  return body;
}

/* ---- D77：英文界面的策略名 ----
 * 策略名只有一个来源：后端 src/registry.py（中文 label + 英文 label_en）。/api/meta 带回两种名字；
 * 英文界面下，这里把 /api/meta 里的 label / desc 换成英文，并记住 键→英文名；之后别的接口返回的对象里，
 * 只要有 key 或 strategy 字段是已知策略键、又有 label 字段（今日 summary、个股 compare、清单、引擎 meta 等），就换成英文名。
 * 中文界面什么都不做。只改显示文字，不改任何数字。 */
let stratEn = null;   // { 策略键: { label, desc } }
const EN_FIELDS = ['reason', 'trigger', 'text', 'notes', 'caveats', 'assumptions', 'label', 'help'];   // D89：后端整句 / 名称的英文版字段名（前五个由 quant 定；label / help 来自 registry）
/* D111/R8：英文界面的固定名称（市场名、时区、ETF / 指数名）。字典 nm.<中文>；只换这几个显示字段，
 * 不碰 action 等后端用来比较的取值；股票名没有英文数据，保持原样。 */
const NAME_FIELDS = new Set(['name', 'market_name', 'main_name', 'timezone_label']);
const nameEn = (v) => { const k = `nm.${v}`; const r = t(k); return r === k ? v : r; };
function localizeNames(o, depth = 0) {
  if (depth > 6 || !o || typeof o !== 'object') return;
  if (Array.isArray(o)) { for (const x of o) localizeNames(x, depth + 1); return; }
  for (const [k, v] of Object.entries(o)) {
    if (typeof v === 'string') { if (NAME_FIELDS.has(k)) o[k] = nameEn(v); }
    else if (v && typeof v === 'object') {
      if (k === 'universe' && !Array.isArray(v)) { for (const [c, nme] of Object.entries(v)) if (typeof nme === 'string') v[c] = nameEn(nme); }
      else localizeNames(v, depth + 1);
    }
  }
}
function localize(path, body) {
  if (lang() !== 'en' || !body || typeof body !== 'object') return body;
  localizeNames(body);
  if (path.startsWith('/api/meta') && body.strategies && !Array.isArray(body.strategies)) {
    stratEn = {};
    for (const [k, v] of Object.entries(body.strategies)) {
      if (!v || typeof v.label_en !== 'string') continue;
      stratEn[k] = { label: v.label_en, desc: v.desc_en };
      v.label = v.label_en;
      if (typeof v.desc_en === 'string') v.desc = v.desc_en;
      // D89：参数名和提示（registry PARAM_EN）
      for (const prm of Array.isArray(v.params) ? v.params : []) {
        if (prm && typeof prm.label_en === 'string') prm.label = prm.label_en;
        if (prm && typeof prm.help_en === 'string' && prm.help) prm.help = prm.help_en;
      }
    }
    return body;
  }
  const walk = (o, depth) => {
    if (depth > 5 || !o || typeof o !== 'object') return;
    if (Array.isArray(o)) { for (const x of o) walk(x, depth + 1); return; }
    // D89：后端生成的整句同时给英文（quant：reason_en、trigger_en、text_en、notes_en、caveats_en），有就用英文；
    // 没有这些字段时（旧数据）照旧显示中文
    for (const f of EN_FIELDS) {
      const v = o[`${f}_en`];
      if (typeof v === 'string' && v && typeof o[f] === 'string') o[f] = v;
      else if (Array.isArray(v) && v.length && Array.isArray(o[f]) && v.length === o[f].length) o[f] = v;
    }
    const k = typeof o.key === 'string' ? o.key : (typeof o.strategy === 'string' ? o.strategy : null);
    const en = k && stratEn ? stratEn[k] : null;
    if (en && typeof o.label === 'string') o.label = en.label;
    if (en && typeof o.desc === 'string' && en.desc) o.desc = en.desc;
    for (const [kk, v] of Object.entries(o)) {
      // 以策略键为键的字典（例如全市场 meta.strategies = { blend2: { label… } }），值里没有 key 字段
      if (stratEn?.[kk] && v && typeof v === 'object' && !Array.isArray(v) && typeof v.label === 'string') v.label = stratEn[kk].label;
      if (v && typeof v === 'object') walk(v, depth + 1);
    }
  };
  walk(body, 0);
  return body;
}

async function request(path, init, opts) {
  checkPath(path);
  if (isMock()) {
    return debugRecord(path, init, localize(path, await readMock(mockName(path, opts.mock), init.method === 'POST' ? { ok: true, mock: true } : undefined)));
  }
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), Number.isFinite(opts?.timeoutMs) && opts.timeoutMs > 0 ? opts.timeoutMs : TIMEOUT_MS);
  let res;
  try {
    res = await fetch(path, {
      credentials: 'same-origin',
      cache: 'no-store',
      signal: ctl.signal,
      ...init,
      headers: { 'X-Requested-With': 'dashboard', ...(init.headers || {}) },
    });
  } catch (err) {
    throw new ApiError(0, t(err.name === 'AbortError' ? 'a02.err.timeout' : 'a02.err.network'));
  } finally {
    clearTimeout(timer);
  }
  let body = null;
  try { body = await res.json(); } catch { /* 不是 JSON（例如 xlsx 下载或 502 页面） */ }
  if (!res.ok) {
    const text = body && (body.message || body.error);
    throw new ApiError(res.status, typeof text === 'string' ? text : `HTTP ${res.status}`, body);
  }
  if (body === null) throw new ApiError(res.status, t('a02.err.not_json'));
  if (opts?.noLocalize) return body;   // R7-perf-1：预取的结果先放着，真正被 getJSON 取用时再做英文替换和字段核对
  return debugRecord(path, init, init.method === 'GET' ? localize(path, body) : body);
}

/* ---- R7-perf-1：预取 ----
 * 页面入口脚本在字典（i18n json）还没回来时就先把首屏必需的 GET 发出去，两件事并行而不是排队。
 * 预取只存"原始返回"，被 getJSON 取用那一刻才做 localize（保证 /api/meta 先于别的接口处理，D77 的策略英文名顺序不变）；
 * 10 秒内没人取用就作废（之后 getJSON 照常重新请求）；每个预取只能被取用一次。 */
const PRE = new Map();
const PRE_TTL_MS = 10000;
export function prefetchJSON(path) {
  if (isMock() || PRE.has(path)) return;
  const p = request(path, { method: 'GET' }, { noLocalize: true });
  p.catch(() => {});   // 没人取用时不要报未处理的拒绝
  PRE.set(path, { p, at: Date.now() });
}

/** GET，返回解析后的 JSON */
export function getJSON(path, opts = {}) {
  const hit = PRE.get(path);
  if (hit) {
    PRE.delete(path);
    if (Date.now() - hit.at < PRE_TTL_MS && !Object.keys(opts).some((k) => k !== 'mock')) {
      return hit.p.then((body) => debugRecord(path, { method: 'GET' }, localize(path, body)));
    }
  }
  return request(path, { method: 'GET' }, opts);
}

/** POST（JSON body，可省略），返回解析后的 JSON */
export function postJSON(path, body, opts = {}) {
  const init = { method: 'POST' };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers = { 'Content-Type': 'application/json' };
  }
  return request(path, init, opts);
}
