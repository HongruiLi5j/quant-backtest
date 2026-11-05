/* 回测实验室·输入校验（A09）：全是纯函数，不碰 DOM、不调接口，所以能直接用 node 测。
 * 校验规则照着后端 /api/backtest（app.py 的 _backtest）和 src/registry.py 的 clean_params 写：
 *   - 后端遇到越界的数字不报错，而是悄悄夹到边界、整数参数悄悄四舍五入——结果就不是用户填的那个数了，
 *     所以前端把「后端会改写的输入」当成错误挡住（valid ⇔ 后端不会改它）；
 *   - 后端能原样接受的输入前端不挡：成本留空 = 用默认成本、开始日期留空 = 预热期之后开始、
 *     非整数步长的参数（0.05 这种）不要求是步长的整数倍。
 * 每个检查返回 null 或 { key, vars }（词条键 + 占位符），文字由页面用 t() 翻译；这里不写任何中文。
 * 回测没有单独的「结束日期」输入：结束日永远是该品种最新的数据日，所以「开始日早于结束日」
 * 和「结束日不晚于最新数据日」合并成一条：开始日必须早于最新数据日（dataEnd）。 */

/** 与 app.py _backtest 里 min(max(float(cost), 0.0), 100.0) 一致：单边成本 bp 的合法范围 */
export const COST_MIN = 0;
export const COST_MAX = 100;
/** 与 src/datasource.py 的 STOCK_RE 一致：沪深主板、科创板、创业板 */
export const CN_STOCK = /^(60|68|00|30)\d{4}$/;
/** 美股代码：与 app.py watch_change 的 [A-Z][A-Z0-9.-]{0,9} 同形，但大小写都放行（后端回测接口不改大小写） */
export const US_CODE = /^[A-Za-z][A-Za-z0-9.-]{0,9}$/;
/** 区间短于约 120 个交易日（≈ 175 个自然日，252/365）只提醒不挡：registry._monte_carlo 在 <120 个交易日时直接不出结果 */
export const SHORT_DAYS = 175;

const num = (x) => String(Number(Number(x).toPrecision(12)));   // 去掉 0.1+0.2 这类浮点尾巴再显示

/** 是不是真实存在的 YYYY-MM-DD（2026-02-30 这种不算） */
export function validDate(d) {
  if (typeof d !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(d)) return false;
  const [y, m, day] = d.split('-').map(Number);
  const x = new Date(Date.UTC(y, m - 1, day));
  return x.getUTCFullYear() === y && x.getUTCMonth() === m - 1 && x.getUTCDate() === day;
}

const utc = (d) => { const [y, m, day] = d.split('-').map(Number); return Date.UTC(y, m - 1, day); };
/** 两个 YYYY-MM-DD 之间隔几个自然日（用 UTC 算，避开夏令时和本地时区） */
const dayCount = (a, b) => Math.round((utc(b) - utc(a)) / 86400000);

/** 数字输入框的原始文字 → { n } 或 { err }。bad = input.validity.badInput（浏览器里有字但解析不成数字，此时 value 是空串） */
function parseNumber(raw, bad) {
  const s = String(raw ?? '').trim();
  if (s === '') return { err: { key: bad ? 'a09.err.not_number' : 'a09.err.required' } };
  const n = Number(s);
  return Number.isFinite(n) ? { n } : { err: { key: 'a09.err.not_number' } };   // NaN / Infinity：后端 float() 认，但 NaN 会让成本变 NaN，一律挡
}

/** 数值范围检查（含端点）。integer=true 时再要求整数 */
function inRange(n, min, max, integer) {
  if (n < min || n > max) return { key: 'a09.err.range', vars: { min: num(min), max: num(max) } };
  if (integer && !Number.isInteger(n)) return { key: 'a09.err.integer' };
  return null;
}

/** 策略参数：范围用 registry 给的 min / max，step 是整数的参数必须填整数（后端会 round） */
export function checkParam(raw, spec, bad = false) {
  const p = parseNumber(raw, bad);
  if (p.err) return p.err;
  return inRange(p.n, Number(spec.min), Number(spec.max), Number.isInteger(Number(spec.step)));
}

/** 单边成本 bp：留空合法（后端用默认成本），由调用方显示提示；填了就必须在 [COST_MIN, COST_MAX] */
export function checkCost(raw, bad = false) {
  if (String(raw ?? '').trim() === '' && !bad) return null;
  const p = parseNumber(raw, bad);
  return p.err || inRange(p.n, COST_MIN, COST_MAX, false);
}

/** 开始日期 → { error, warn, hint }。dataEnd = 该品种最新数据日（还不知道就传 null，跳过和它比较的那几条）
 *  留空合法（从预热期之后开始）；后端对「开始日晚于最新数据日」会崩（IndexError），等于最新数据日则只剩 1 天收益，
 *  所以要求严格早于 dataEnd */
export function checkStart(raw, dataEnd = null, bad = false) {
  const res = { error: null, warn: null, hint: null };
  const s = String(raw ?? '').trim();
  const endOk = validDate(dataEnd);
  if (s === '' && !bad) {
    if (endOk) res.hint = { key: 'a09.hint.end', vars: { end: dataEnd } };
    return res;
  }
  if (!validDate(s)) { res.error = { key: 'a09.err.date_bad' }; return res; }
  if (!endOk) return res;
  if (s >= dataEnd) res.error = { key: 'a09.err.start_late', vars: { end: dataEnd } };   // ISO 日期的字符串大小就是日期先后
  else if (dayCount(s, dataEnd) < SHORT_DAYS) res.warn = { key: 'a09.warn.short', vars: { n: dayCount(s, dataEnd) } };
  else res.hint = { key: 'a09.hint.end', vars: { end: dataEnd } };
  return res;
}

/** 品种：必填；不在已知列表（known=true）的代码（只可能来自网址 ?symbol=）要符合后端认的代码格式 */
export function checkSymbol(symbol, market, known) {
  const s = String(symbol ?? '').trim();
  if (s === '') return { key: 'a09.err.symbol_required' };
  if (known) return null;
  if (market === 'cn') return CN_STOCK.test(s) ? null : { key: 'a09.err.symbol_cn' };
  if (market === 'us') return US_CODE.test(s) ? null : { key: 'a09.err.symbol_us' };
  return null;
}

/** 整张表单一起检查。
 *  raw：{ symbol, start, cost, params: { key: 输入框文字 } }；bad：{ start, cost, params: { key: badInput } }
 *  specs：当前策略的参数规格（registry 的 params）；known：品种是否在已知列表；
 *  返回每个输入的 { error, warn, hint }，以及 errors（挡住运行的错误个数） */
export function validateAll({ market, raw, bad = {}, specs = [], known = false, dataEnd = null, costDefault = null }) {
  const blank = () => ({ error: null, warn: null, hint: null });
  const out = { symbol: blank(), start: checkStart(raw.start, dataEnd, bad.start), cost: blank(), params: {}, errors: 0 };
  out.symbol.error = checkSymbol(raw.symbol, market, known);
  out.cost.error = checkCost(raw.cost, bad.cost);
  if (!out.cost.error && String(raw.cost ?? '').trim() === '' && costDefault !== null) {
    out.cost.hint = { key: 'a09.hint.cost_blank', vars: { v: num(costDefault) } };
  }
  for (const spec of specs) {
    out.params[spec.key] = { ...blank(), error: checkParam(raw.params?.[spec.key], spec, bad.params?.[spec.key]) };
  }
  // 快线 ≥ 慢线：后端不拦（范围允许 fast=50、slow=5），所以只提醒不挡
  const f = out.params.fast, s = out.params.slow;
  if (f && s && !f.error && !s.error && Number(raw.params.fast) >= Number(raw.params.slow)) {
    s.warn = { key: 'a09.warn.fast_slow', vars: { fast: num(raw.params.fast), slow: num(raw.params.slow) } };
  }
  const all = [out.symbol, out.start, out.cost, ...Object.values(out.params)];
  out.errors = all.filter((r) => r.error).length;
  return out;
}
