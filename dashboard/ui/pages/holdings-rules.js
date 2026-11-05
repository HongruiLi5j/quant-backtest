/* ==========================================================
 * 文件: ui/pages/holdings-rules.js
 * 用途: 持仓录入表单的「输入规则」：纯函数，不碰 DOM，方便单独测试（A15）
 *   - cleanNumber：去掉千分位逗号（1,000 → 1000）
 *   - checkInputs：数量 / 价格 / 费用是否合法；返回 [{ field, key, fallback }]，没问题返回空数组
 *   - lotHint：按「买 / 卖、代码前缀」给出整手提示（只提示，不拦截；真正的整手校验在后端 store._check_qty）
 * 规则和后端 src/holdings/store.py 保持一致：数量正整数 ≤ 1 亿；价格 > 0、≤ 100000、最多 3 位小数；
 * 费用 ≥ 0、最多 2 位小数、不超过成交金额；送转没有价格和费用。
 * ========================================================== */

const MAX_QTY = 100_000_000;     // 同 store.MAX_QTY
const MAX_PRICE = 100000;        // 同 store._check_price
const NUM_RE = /^\d+(\.\d+)?$/;  // 同 store._NUM_RE：不接受符号、科学计数法、空格

/** 只去「1,234,567.89」这种明确的千分位逗号；其他带逗号的写法原样保留，交给后面的格式检查报错（不猜欧式小数逗号） */
export function cleanNumber(raw) {
  const s = String(raw ?? '').trim();
  return /^\d{1,3}(,\d{3})+(\.\d+)?$/.test(s) ? s.replace(/,/g, '') : s;
}

/** 小数位数不超过 max（末尾补的 0 不算，和后端一致：1.2300 对 2 位小数是合法的） */
function decimalsOk(s, max) {
  const frac = (s.split('.')[1] || '').replace(/0+$/, '');
  return frac.length <= max;
}

/** 数量：正整数、不超过上限（字符串里只放数字） */
function qtyProblem(s) {
  if (!/^\d+$/.test(s) || /^0+$/.test(s)) return 'qty';
  return s.length > 9 || Number(s) > MAX_QTY ? 'qty_max' : null;
}

/**
 * 逐项检查；入参都是「已经 cleanNumber 过」的字符串。
 * 空的数量 / 价格由调用方的「请填写」提示负责，这里只管「填了但不合法」。
 */
export function checkInputs({ side, qty, price, fee }) {
  const out = [];
  const bad = (field, name, fallback) => out.push({ field, key: `a15.err.${name}`, fallback });
  if (qty !== '') { const p = qtyProblem(qty); if (p) bad('qty', p, 'hold.err.HOLDINGS_BAD_QTY'); }
  if (side === 'bonus') return out;           // 送转没有价格和费用（输入框已隐藏并清空）
  let priceNum = null;
  if (price !== '') {
    if (!NUM_RE.test(price) || !decimalsOk(price, 3) || Number(price) <= 0 || Number(price) > MAX_PRICE) bad('price', 'price', 'hold.err.HOLDINGS_BAD_PRICE');
    else priceNum = Number(price);
  }
  if (fee !== '') {
    if (!NUM_RE.test(fee) || !decimalsOk(fee, 2)) bad('fee', 'fee', 'hold.err.HOLDINGS_BAD_FEE');
    else if (priceNum !== null && !qtyProblem(qty) && Number(fee) > priceNum * Number(qty)) bad('fee', 'fee_over', 'hold.err.HOLDINGS_BAD_FEE');
  }
  return out;
}

/**
 * 数量框下面的整手提示 → { key, warn } 或 null（不需要提示）。
 *   买入：普通 A 股、ETF 都是 100 股（份）一手，数量须是 100 的整数倍；科创板（688 / 689 开头的股票）至少 200 股，之后可按 1 股递增
 *   卖出：不受整手限制（零股可以一次性卖出）
 *   分红 / 送转：数量是持股数 / 新增股数，不提示
 * warn=true 表示「当前填的数量不符合上面的规则」：文字换成提醒，但不阻止提交（是否拒绝由后端决定）
 */
export function lotHint({ side, code, qty }) {
  if (side === 'sell') return { key: 'a15.lot.sell', warn: false };
  if (side !== 'buy') return null;
  const n = /^\d+$/.test(qty) ? Number(qty) : null;
  if (/^68[89]/.test(code)) return { key: n !== null && n < 200 ? 'a15.lot.warn_star' : 'a15.lot.star', warn: n !== null && n < 200 };
  if (/^\d{6}$/.test(code)) return { key: n !== null && n % 100 !== 0 ? 'a15.lot.warn_buy' : 'a15.lot.buy', warn: n !== null && n % 100 !== 0 };
  return { key: 'a15.lot.generic', warn: false };
}
