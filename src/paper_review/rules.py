"""纸面实盘三条硬规则(纯函数,仅标准库,无 I/O、无全局状态)。

默认值来自 PDF《量化选股网站:整体方案与策略研发路线》:
  停止标准:连续 3 个月跑输大盘 3% 即暂停实盘
  风控:单票 <= 10%、总仓 <= 80%、单日亏损 2% 当日停手
本文件只判断"是否违规/触发",不执行任何动作(不下单、不弹通知)。
"""
import math
import re
from typing import Mapping, Sequence

STOP_MONTHS = 3
STOP_UNDERPERF = 0.03
SINGLE_MAX = 0.10
TOTAL_MAX = 0.80
DAILY_LOSS = 0.02

# 浮点容差:避免 0.01-0.04 之类运算得到 -0.030000000000000002 / -0.0299999 导致边界判错
_EPS = 1e-9
_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _finite(x, name):
    """要求是有限实数(排除 bool/NaN/inf),否则抛 ValueError。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise ValueError(f"{name} 必须是有限数字,收到 {x!r}")
    return float(x)


def _month_index(month):
    """'YYYY-MM' -> 连续整数(年*12+月-1),用来判断日历上是否相邻。"""
    m = _MONTH_RE.match(month) if isinstance(month, str) else None
    if not m:
        raise ValueError(f"月份格式必须是 YYYY-MM,收到 {month!r}")
    return int(m.group(1)) * 12 + int(m.group(2)) - 1


def _result(status, triggered, months, excess, streak, reason):
    return {"triggered": triggered, "status": status, "months": months,
            "excess": excess, "streak": streak, "reason": reason}


def stop_rule(monthly_excess: Sequence[tuple], months: int = STOP_MONTHS,
              underperf: float = STOP_UNDERPERF, mode: str = "each") -> dict:
    """停止标准:连续 `months` 个月跑输大盘 `underperf`。

    monthly_excess: [(月份 'YYYY-MM', 策略月收益, 大盘月收益), ...],收益用小数(0.01=1%)。
      调用者负责只传"已完整结束且交易日足够"的月份;本函数不过滤、不修改入参,按月份排序后判断。
    mode:
      "each"(默认,读法 A):尾部连续 months 个自然月,每月 策略-大盘 <= -underperf。
      "cumulative"(读法 B):尾部 months 个自然月的累计(几何复利)超额 <= -underperf。
    边界:刚好 -underperf 算触发(偏保守,宁可多停)。
    只看"最新月份往回的连续链":月份必须日历相邻,缺月即链条中断;
    尾部连续月数不足 months 时返回"数据不足"而不是"未触发"(没数据不等于没问题)。
    返回 dict: triggered / status("触发"|"未触发"|"数据不足") / months(触发月份,否则 [])
      / excess(对应超额) / streak(尾部连续跑输月数,each 模式) / reason。
    """
    if mode not in ("each", "cumulative"):
        raise ValueError(f"mode 只能是 'each' 或 'cumulative',收到 {mode!r}")
    if isinstance(months, bool) or not isinstance(months, int) or months < 1:
        raise ValueError(f"months 必须是 >=1 的整数,收到 {months!r}")
    underperf = _finite(underperf, "underperf")
    if underperf < 0:
        raise ValueError("underperf 不能为负")

    rows = []
    for item in monthly_excess:
        if len(item) != 3:
            raise ValueError(f"每项必须是 (月份, 策略收益, 大盘收益),收到 {item!r}")
        mon, s, b = item
        idx = _month_index(mon)
        s, b = _finite(s, "策略收益"), _finite(b, "大盘收益")
        if s <= -1 or b <= -1:
            raise ValueError(f"月收益不能 <= -100%:{item!r}")
        rows.append((idx, mon, s, b))
    rows.sort(key=lambda r: r[0])
    idxs = [r[0] for r in rows]
    if len(set(idxs)) != len(idxs):
        raise ValueError("存在重复月份")

    if not rows:
        return _result("数据不足", False, [], [], 0, "没有任何月度数据")

    # 取最新月份往回的日历连续链
    j = len(rows) - 1
    while j > 0 and rows[j][0] - rows[j - 1][0] == 1:
        j -= 1
    chain = rows[j:]
    thr = underperf - _EPS

    # 尾部连续跑输月数(each 语义下的 streak)
    streak = 0
    for r in reversed(chain):
        if r[2] - r[3] <= -thr:
            streak += 1
        else:
            break

    if len(chain) < months:
        return _result("数据不足", False, [], [], streak,
                       f"尾部连续月份只有 {len(chain)} 个,不足 {months} 个(月份缺口会中断链条)")

    last = chain[-months:]
    ex = [r[2] - r[3] for r in last]
    names = [r[1] for r in last]
    if mode == "each":
        hit = streak >= months
        reason = (f"最近 {months} 个月每月都跑输大盘至少 {underperf:.2%}" if hit else
                  f"尾部连续跑输 {streak} 个月,未满 {months} 个月")
    else:
        s_cum = math.prod(1 + r[2] for r in last) - 1
        b_cum = math.prod(1 + r[3] for r in last) - 1
        cum = s_cum - b_cum
        hit = cum <= -thr
        reason = (f"最近 {months} 个月累计超额 {cum:.2%}" +
                  (f" <= -{underperf:.2%}" if hit else f" > -{underperf:.2%}"))
    if hit:
        return _result("触发", True, names, ex, streak, reason)
    return _result("未触发", False, [], [], streak, reason)


def position_limits(positions: Sequence[Mapping], equity: float,
                    single_max: float = SINGLE_MAX, total_max: float = TOTAL_MAX) -> list:
    """仓位约束检查,返回违规清单(合规返回 [])。

    positions: [{code, qty, price}] 或 [{code, value}](value 是市值)。同一 code 的多条会合并。
    分母是传入的 equity(总净值,含现金);weight = 市值 / equity。
    "严格大于上限才违规":正好 10% / 80% 合规。
    违规项: {kind: 'single'|'total', code(total 时为 None), weight, limit, excess}。
    equity<=0、价格/数量/市值缺失或为负、上限不在 (0,1] -> 抛 ValueError,不静默放行。
    """
    equity = _finite(equity, "equity")
    if equity <= 0:
        raise ValueError("equity 必须 > 0")
    single_max = _finite(single_max, "single_max")
    total_max = _finite(total_max, "total_max")
    if not (0 < single_max <= 1 and 0 < total_max <= 1):
        raise ValueError("single_max / total_max 必须在 (0, 1] 内")

    values = {}
    for p in positions:
        code = p.get("code") if hasattr(p, "get") else None
        if not code:
            raise ValueError(f"持仓缺少 code:{p!r}")
        if "value" in p:
            v = _finite(p["value"], f"{code} value")
        elif "qty" in p and "price" in p:
            q, px = _finite(p["qty"], f"{code} qty"), _finite(p["price"], f"{code} price")
            if q < 0 or px <= 0:
                raise ValueError(f"{code} 的数量不能为负、价格必须 > 0")
            v = q * px
        else:
            raise ValueError(f"{code} 需要 value 或 (qty 和 price)")
        if v < 0:
            raise ValueError(f"{code} 市值不能为负")
        values[code] = values.get(code, 0.0) + v

    out = []
    for code, v in values.items():
        w = v / equity
        if w > single_max + _EPS:
            out.append({"kind": "single", "code": code, "weight": w,
                        "limit": single_max, "excess": w - single_max})
    total = sum(values.values()) / equity
    if total > total_max + _EPS:
        out.append({"kind": "total", "code": None, "weight": total,
                    "limit": total_max, "excess": total - total_max})
    return out


def daily_loss_halt(day_pnl: float, equity: float, limit: float = DAILY_LOSS) -> bool:
    """单日亏损是否达到停手线:day_pnl / equity <= -limit 则 True(刚好 -2% 也触发)。

    equity 必须是"前一日收盘净值"(要 > 0)。不能用当日末净值:亏得越多分母越小,
    比例反而被放大/口径不一致;用前日净值才是"相对昨天亏了多少"。
    "停手"的语义建议是当日不再新开仓(卖出/减仓仍允许);本函数只返回判断结果。
    """
    day_pnl = _finite(day_pnl, "day_pnl")
    equity = _finite(equity, "equity")
    limit = _finite(limit, "limit")
    if equity <= 0:
        raise ValueError("equity(前一日收盘净值)必须 > 0")
    if limit <= 0:
        raise ValueError("limit 必须 > 0")
    return day_pnl / equity <= -limit + _EPS
