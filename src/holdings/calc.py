"""持仓计算:把成交记录按 (日期, 编号) 整体重放,得出每只股票的持仓状态。

纯逻辑模块:不读写文件、不联网、不打印、不写日志。
所有金额用 Decimal 计算,只在输出时四舍五入(金额 2 位、均价 3 位、比例 4 位,ROUND_HALF_UP)。
口径见 docs/holdings_design.md 第 2 节:
  - 加权平均成本含费用,卖出不改变均价;
  - 本轮首次买入日 = 持股从 0 变为正的那笔买入的日期;卖光后再买重新计;
  - bonus(送转)摊薄均价;dividend(现金分红)单列,不冲减成本;
  - 浮动盈亏不扣预估卖出费用;
  - 现价取不到时市值/浮盈/占比一律为 None,绝不用成本价或 0 顶替。
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

MILLI = Decimal(1000)
CENT = Decimal(100)

# 固定错误码对应的固定中文提示:绝不拼接用户输入、数量、价格或异常文本
MESSAGES = {
    "HOLDINGS_BAD_CODE": "证券代码不合法",
    "HOLDINGS_BAD_SIDE": "方向不合法",
    "HOLDINGS_BAD_DATE": "日期格式不合法",
    "HOLDINGS_FUTURE_DATE": "日期不能晚于今天",
    "HOLDINGS_BAD_QTY": "数量不合法",
    "HOLDINGS_BAD_LOT": "买入数量不符合整手规则",
    "HOLDINGS_BAD_PRICE": "价格不合法",
    "HOLDINGS_BAD_FEE": "费用不合法",
    "HOLDINGS_BAD_NOTE": "备注不合法",
    "HOLDINGS_BAD_SIGNAL_REF": "来源信号格式不合法",
    "HOLDINGS_BAD_CAPITAL": "总资金不合法",
    "HOLDINGS_BAD_ID": "记录编号不合法",
    "HOLDINGS_BAD_CLIENT_ID": "请求标识不合法",
    "HOLDINGS_OVERSELL": "卖出数量大于持有数量",
    "HOLDINGS_NO_POSITION": "没有足够的持股可做送转或分红",
    "HOLDINGS_VOID_BREAKS_HISTORY": "撤销会让后续记录变得不合法,请先撤销后面的记录",
    "HOLDINGS_ALREADY_VOIDED": "这笔记录已经撤销",
    "HOLDINGS_TRADE_NOT_FOUND": "找不到这笔记录",
    "HOLDINGS_CLIENT_ID_CONFLICT": "同一请求标识对应了不同内容",
    "HOLDINGS_STORE_UNSAFE": "持仓库文件不安全",
    "HOLDINGS_INTERNAL": "内部错误",
}


class HoldingsError(Exception):
    """只携带固定错误码(和可选字段名),str() 也只返回错误码。"""

    def __init__(self, code, field=None):
        super().__init__(code)
        self.code = code
        self.field = field
        self.message = MESSAGES.get(code, MESSAGES["HOLDINGS_INTERNAL"])


def q2(x):
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def q3(x):
    return x.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def q4(x):
    return x.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def default_fee_estimate(code, side, amount):
    """默认费用估算(可被调用者注入的函数替换,例如 simtrade.fees)。

    佣金 0.025% 最低 5 元;沪市(6 开头或 5 开头 ETF)过户费 0.001%;卖出印花税 0.05%(ETF 免)。
    费率尚未与真实账户核对,仅是估算,页面应标"估算"。
    """
    amount = Decimal(amount)
    fee = max(amount * Decimal("0.00025"), Decimal(5))
    if code[:1] in ("5", "6"):
        fee += amount * Decimal("0.00001")
    is_etf = code[:2] in ("51", "56", "58") or code.startswith("159")
    if side == "sell" and not is_etf:
        fee += amount * Decimal("0.0005")
    return q2(fee)


def _sort_key(t):
    return (t["trade_date"], t.get("id", 10 ** 18))


def replay(trades):
    """整体重放未撤销的记录,返回 {code: 状态} 与已清仓历史。

    任一时点卖出大于持股、bonus/dividend 面对零持股或超持股,抛 HoldingsError。
    """
    states = {}
    closed = []
    for t in sorted((t for t in trades if not t.get("voided")), key=_sort_key):
        code, side, qty = t["code"], t["side"], int(t["qty"])
        price = Decimal(t["price_milli"]) / MILLI
        fee = Decimal(t["fee_cent"]) / CENT
        st = states.setdefault(code, {
            "shares": 0, "cost": Decimal(0), "first_buy": None,
            "realized": Decimal(0), "dividends": Decimal(0),
            "round_realized": Decimal(0), "round_dividends": Decimal(0),
        })
        if side == "buy":
            if st["shares"] == 0:
                st["first_buy"] = t["trade_date"]
                st["round_realized"] = Decimal(0)
                st["round_dividends"] = Decimal(0)
            st["cost"] += qty * price + fee
            st["shares"] += qty
        elif side == "sell":
            if qty > st["shares"]:
                raise HoldingsError("HOLDINGS_OVERSELL")
            # 摊销成本 = 总成本 × 卖出数 / 持股数(只做一次除法,减少舍入误差)
            amortized = st["cost"] * qty / st["shares"]
            gain = qty * price - fee - amortized
            st["realized"] += gain
            st["round_realized"] += gain
            st["cost"] -= amortized
            st["shares"] -= qty
            if st["shares"] == 0:
                closed.append({
                    "code": code, "first_buy": st["first_buy"], "last_sell": t["trade_date"],
                    "pnl": st["round_realized"], "dividends": st["round_dividends"],
                })
                st["cost"] = Decimal(0)
                st["first_buy"] = None
        elif side == "bonus":
            if st["shares"] == 0:
                raise HoldingsError("HOLDINGS_NO_POSITION")
            st["shares"] += qty
        elif side == "dividend":
            if st["shares"] == 0 or qty > st["shares"]:
                raise HoldingsError("HOLDINGS_NO_POSITION")
            cash = qty * price - fee
            st["dividends"] += cash
            st["round_dividends"] += cash
        else:
            raise HoldingsError("HOLDINGS_BAD_SIDE")
    return states, closed


def _to_date(d):
    return d if isinstance(d, date) else date.fromisoformat(d)


def _price_of(last_price_fn, code):
    """调用注入函数取现价,返回 (Decimal 价格, 日期字符串) 或 (None, None)。

    函数可返回 None、数字、或 (价格, 日期);抛异常、非正数、非有限数都当作取不到。
    """
    if last_price_fn is None:
        return None, None
    try:
        got = last_price_fn(code)
        px_date = None
        if isinstance(got, tuple):
            got, px_date = got
        if got is None or isinstance(got, bool):
            return None, None
        px = got if isinstance(got, Decimal) else Decimal(str(got))
        if not px.is_finite() or px <= 0:
            return None, None
        return px, (str(px_date) if px_date is not None else None)
    except (InvalidOperation, ValueError, TypeError, ArithmeticError, LookupError):
        return None, None


def _weight_key(p):
    # 占比降序,缺失排最后,同占比按代码
    return (p["weight"] is None, -(p["weight"] or 0), p["code"])


def positions(trades, last_price_fn, today, capital=None):
    """由成交记录算出当前持仓、已清仓历史与汇总。

    trades: 记录字典列表(含 voided 的也行,会被忽略),字段 id/trade_date/code/side/qty/price_milli/fee_cent。
    last_price_fn(code) -> None | 价格 | (价格, 日期);取不到就是缺失。
    today: date 或 YYYY-MM-DD(北京日期)。capital: 总资金(元,Decimal/数字)或 None。
    """
    today = _to_date(today)
    cap = None
    if capital is not None:
        cap = capital if isinstance(capital, Decimal) else Decimal(str(capital))
        if cap <= 0:
            cap = None
    states, closed_rounds = replay(trades)

    out, total_cost, total_mv = [], Decimal(0), Decimal(0)
    total_float, missing = Decimal(0), 0
    for code, st in states.items():
        if st["shares"] <= 0:
            continue
        shares, cost = st["shares"], st["cost"]
        px, px_date = _price_of(last_price_fn, code)
        item = {
            "code": code, "shares": shares, "cost": q2(cost),
            "avg_cost": q3(cost / shares),
            "first_buy": st["first_buy"],
            "held_days": (today - _to_date(st["first_buy"])).days,
            "price": None, "price_date": px_date, "market_value": None,
            "floating_pnl": None, "floating_pct": None, "weight": None,
            "realized": q2(st["realized"]), "dividends": q2(st["dividends"]),
        }
        total_cost += cost
        if px is None:
            missing += 1
        else:
            mv = shares * px
            item["price"] = q3(px)
            item["market_value"] = q2(mv)
            item["floating_pnl"] = q2(mv - cost)
            item["floating_pct"] = q4((mv - cost) / cost) if cost > 0 else None
            if cap is not None:
                item["weight"] = q4(mv / cap)
            total_mv += mv
            total_float += mv - cost
        out.append(item)
    out.sort(key=_weight_key)

    closed = []
    for r in closed_rounds:
        closed.append({
            "code": r["code"], "first_buy": r["first_buy"], "last_sell": r["last_sell"],
            "held_days": (_to_date(r["last_sell"]) - _to_date(r["first_buy"])).days,
            "pnl": q2(r["pnl"]), "dividends": q2(r["dividends"]),
        })
    closed.sort(key=lambda r: (r["last_sell"], r["code"]), reverse=True)

    summary = {
        "market_value": q2(total_mv),
        "total_cost": q2(total_cost),
        "floating_pnl": q2(total_float),
        "realized": q2(sum((s["realized"] for s in states.values()), Decimal(0))),
        "dividends": q2(sum((s["dividends"] for s in states.values()), Decimal(0))),
        "invested_ratio": q4(total_mv / cap) if cap is not None else None,
        "missing_price_count": missing,
    }
    return {"positions": out, "closed": closed, "summary": summary}
