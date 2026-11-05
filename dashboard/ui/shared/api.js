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

import { t } from './i18n.js';   // 错误提示会显示在页面上（卡片、toast），跟随当前语言

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

async function request(path, init, opts) {
  checkPath(path);
  if (isMock()) {
    return debugRecord(path, init, await readMock(mockName(path, opts.mock), init.method === 'POST' ? { ok: true, mock: true } : undefined));
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
  return debugRecord(path, init, body);
}

/** GET，返回解析后的 JSON */
export function getJSON(path, opts = {}) {
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
