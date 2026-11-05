"""A 股成本与成交约束模型(T007 / S2)。纯计算:不联网、不读写任何文件。

覆盖:佣金(双边,带最低收费)、卖出印花税、过户费、滑点(按比例,涨跌停附近可加大)、
T+1(当日买入不可当日卖出)、涨停不可买 / 跌停不可卖、停牌不可成交、100 股整手。

重要:CostParams 的默认值只是"占位参考",复制自 src/simtrade.py 的 FEE["stock"]
(该处 FEE_VERIFIED=False,尚未与真实账户核对)。**需所有者核对当前费率**
(佣金、印花税、过户费的规定会调整),不要把默认值当成事实。

约定:
  - 成交价 = 报价 ×(1 ± 滑点比例):买入向上、卖出向下,滑点成本在结果里单列。
  - 买入、卖出都按"成交价 × 股数"计金额,佣金 = max(金额 × 费率, 最低佣金),
    过户费 = 金额 × 费率,印花税只在卖出收取。各项不做四舍五入(便于单调性与手算核对)。
  - 持仓成本用移动平均法;只统计已平仓部分的已实现盈亏,未平仓部分不计入。
  - 卖出不能超过"已可卖"持仓(不做空):超出部分被截断;截断后如不等于全部可卖持仓,
    则向下取整到整手(A 股允许一次性卖出不足一手的零股尾数)。
  - 被拦截的成交不产生费用,也不改变持仓(没有"顺延到下一天"的逻辑,那属于持仓路径层)。
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Iterable, Mapping, Optional, Sequence

from .. import costs as rates    # 费率常量的单一来源(src/costs.py);本模块也叫 costs,所以起别名避免混淆

__all__ = [
    "CostParams", "Trade", "FillResult", "CostResult",
    "lot_round", "trade_fees", "apply_costs", "stress_table",
]

# 拦截原因(固定字符串,便于测试与报告引用)
BLOCK_SUSPENDED = "停牌不可成交"
BLOCK_LIMIT_UP = "涨停不可买"
BLOCK_LIMIT_DOWN = "跌停不可卖"
BLOCK_T1 = "T+1 当日买入不可卖"
BLOCK_LOT = "不足一手"
BLOCK_NO_POSITION = "无可卖持仓"


@dataclass(frozen=True)
class CostParams:
    """成本与约束参数。费率默认值需所有者核对当前费率,不是事实。"""
    commission_rate: float = rates.COMMISSION_RATE      # 佣金率(双边);来源 src/costs.py(原复制自 simtrade.FEE["stock"]),未核对
    min_commission: float = rates.MIN_COMMISSION        # 最低佣金(元/笔);来源同上,未核对
    transfer_rate: float = rates.TRANSFER_RATE          # 过户费率(双边);来源同上,未核对
    stamp_rate: float = rates.STAMP_DUTY_RATE           # 印花税率(仅卖出,现行 2023-08-28 起的税率,不分日期);来源同上,未核对
    slippage_bps: float = rates.SLIPPAGE_BPS            # 基础滑点(基点,1bp=0.01%);占位假设,非实测
    limit_extra_slippage_bps: float = rates.LIMIT_EXTRA_SLIPPAGE_BPS  # 标记 near_limit 的成交额外加大的滑点;占位假设
    lot: int = 100                        # 整手股数
    enforce_t1: bool = True
    enforce_limits: bool = True
    enforce_suspension: bool = True

    def __post_init__(self):
        for name in ("commission_rate", "min_commission", "transfer_rate", "stamp_rate",
                     "slippage_bps", "limit_extra_slippage_bps"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v or v < 0:
                raise ValueError(f"{name} 必须是非负数,收到 {v!r}")
        if not isinstance(self.lot, int) or isinstance(self.lot, bool) or self.lot <= 0:
            raise ValueError(f"lot 必须是正整数,收到 {self.lot!r}")


@dataclass(frozen=True)
class Trade:
    """一笔委托。date 可为 ISO 字符串或 date(按字符串比较,须同一种格式)。"""
    date: object
    side: str                  # "buy" / "sell"
    price: float
    shares: int
    code: str = ""
    limit_up: bool = False     # 当日涨停(买不进)
    limit_down: bool = False   # 当日跌停(卖不出)
    suspended: bool = False    # 当日停牌
    near_limit: bool = False   # 价格靠近涨跌停,滑点加大


@dataclass(frozen=True)
class FillResult:
    """逐笔结果。executed=False 时费用为 0,blocked_reason 给出原因。"""
    index: int                 # 在输入列表中的位置
    date: object
    code: str
    side: str
    requested_shares: int
    shares: int                # 实际成交股数
    price: float               # 报价
    exec_price: float          # 含滑点的成交价
    amount: float              # exec_price × shares
    commission: float
    transfer: float
    stamp: float
    slippage_cost: float       # |exec_price − price| × shares
    total_fees: float          # 佣金 + 过户费 + 印花税(不含滑点)
    executed: bool
    blocked_reason: str = ""
    realized_gross: Optional[float] = None   # 卖出才有:不含费用与滑点的已实现盈亏
    realized_net: Optional[float] = None     # 卖出才有:扣滑点与全部费用后的已实现盈亏


@dataclass(frozen=True)
class CostResult:
    fills: list = field(default_factory=list)
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    n_executed: int = 0
    n_blocked: int = 0

    @property
    def total_cost(self) -> float:
        return self.total_fees + self.total_slippage

    @property
    def blocked(self) -> list:
        return [f for f in self.fills if not f.executed]


def lot_round(shares, lot: int = 100) -> int:
    """向下取整到整手;负数或 0 返回 0。"""
    if lot <= 0:
        raise ValueError("lot 必须为正")
    s = int(shares)
    return 0 if s <= 0 else (s // lot) * lot


def trade_fees(params: CostParams, side: str, amount: float) -> dict:
    """给定成交金额的费用分项(佣金带最低收费,印花税只在卖出)。"""
    if side not in ("buy", "sell"):
        raise ValueError(f"side 必须是 buy/sell,收到 {side!r}")
    if amount <= 0:
        return {"commission": 0.0, "transfer": 0.0, "stamp": 0.0, "total": 0.0}
    commission = max(amount * params.commission_rate, params.min_commission)
    transfer = amount * params.transfer_rate
    stamp = amount * params.stamp_rate if side == "sell" else 0.0
    return {"commission": commission, "transfer": transfer, "stamp": stamp,
            "total": commission + transfer + stamp}


def _to_trade(t) -> Trade:
    if isinstance(t, Trade):
        tr = t
    elif isinstance(t, Mapping):
        allowed = {f.name for f in fields(Trade)}
        extra = set(t) - allowed
        if extra:
            raise ValueError(f"trade 含未知字段: {sorted(extra)}")
        try:
            tr = Trade(**t)
        except TypeError as e:
            raise ValueError(f"trade 缺少必需字段: {e}") from None
    else:
        raise ValueError(f"trade 必须是 Trade 或 dict,收到 {type(t).__name__}")
    if tr.side not in ("buy", "sell"):
        raise ValueError(f"side 必须是 buy/sell,收到 {tr.side!r}")
    if not isinstance(tr.price, (int, float)) or isinstance(tr.price, bool) or not tr.price > 0:
        raise ValueError(f"price 必须为正数,收到 {tr.price!r}")
    if not isinstance(tr.shares, int) or isinstance(tr.shares, bool) or tr.shares <= 0:
        raise ValueError(f"shares 必须为正整数,收到 {tr.shares!r}")
    return tr


def apply_costs(trades: Iterable, params: Optional[CostParams] = None) -> CostResult:
    """逐笔应用成交约束与成本,返回逐笔结果与汇总净收益。

    处理顺序:按 (date, 输入顺序) 稳定排序 → 对每笔依次检查
    停牌 → 涨停不可买 / 跌停不可卖 → T+1 / 持仓上限 → 整手取整。
    """
    p = params if params is not None else CostParams()
    items = [_to_trade(t) for t in trades]
    order = sorted(range(len(items)), key=lambda i: (str(items[i].date), i))

    # 每个代码的持仓:settled=已可卖股数;today=当日新买(次日才可卖);raw/net=持仓总成本
    pos = {}
    fills = [None] * len(items)
    gross = net = fees_sum = slip_sum = 0.0
    n_exec = n_block = 0

    for i in order:
        t = items[i]
        st = pos.setdefault(t.code, {"settled": 0, "today": 0, "day": None, "raw": 0.0, "net": 0.0})
        d = str(t.date)
        if st["day"] != d:                 # 新的一天:昨天买的转为可卖
            st["settled"] += st["today"]
            st["today"] = 0
            st["day"] = d

        reason = ""
        qty = t.shares
        if p.enforce_suspension and t.suspended:
            reason = BLOCK_SUSPENDED
        elif p.enforce_limits and t.side == "buy" and t.limit_up:
            reason = BLOCK_LIMIT_UP
        elif p.enforce_limits and t.side == "sell" and t.limit_down:
            reason = BLOCK_LIMIT_DOWN
        elif t.side == "sell":
            sellable = st["settled"] if p.enforce_t1 else st["settled"] + st["today"]
            if sellable <= 0:
                held = st["settled"] + st["today"]
                reason = BLOCK_T1 if (p.enforce_t1 and held > 0) else BLOCK_NO_POSITION
            else:
                qty = min(qty, sellable)
                if qty != sellable:        # 不是清仓:必须整手
                    qty = lot_round(qty, p.lot)
                if qty <= 0:
                    reason = BLOCK_LOT
        else:
            qty = lot_round(qty, p.lot)
            if qty <= 0:
                reason = BLOCK_LOT

        if reason:
            fills[i] = FillResult(i, t.date, t.code, t.side, t.shares, 0, float(t.price),
                                  float(t.price), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, False, reason)
            n_block += 1
            continue

        bps = p.slippage_bps + (p.limit_extra_slippage_bps if t.near_limit else 0.0)
        sgn = 1.0 if t.side == "buy" else -1.0
        exec_price = t.price * (1.0 + sgn * bps / 1e4)
        amount = exec_price * qty
        fee = trade_fees(p, t.side, amount)
        slip = abs(exec_price - t.price) * qty
        r_gross = r_net = None

        if t.side == "buy":
            st["raw"] += t.price * qty
            st["net"] += amount + fee["total"]
            st["today"] += qty
        else:
            held = st["settled"] + st["today"]
            frac = qty / held
            raw_basis, net_basis = st["raw"] * frac, st["net"] * frac
            r_gross = t.price * qty - raw_basis
            r_net = amount - fee["total"] - net_basis
            st["raw"] -= raw_basis
            st["net"] -= net_basis
            st["settled"] -= qty
            gross += r_gross
            net += r_net

        fees_sum += fee["total"]
        slip_sum += slip
        n_exec += 1
        fills[i] = FillResult(i, t.date, t.code, t.side, t.shares, qty, float(t.price), exec_price,
                              amount, fee["commission"], fee["transfer"], fee["stamp"], slip,
                              fee["total"], True, "", r_gross, r_net)

    return CostResult(fills=fills, gross_pnl=gross, net_pnl=net, total_fees=fees_sum,
                      total_slippage=slip_sum, n_executed=n_exec, n_blocked=n_block)


def stress_table(trades: Sequence, grid: Optional[Mapping] = None,
                 base: Optional[CostParams] = None) -> list:
    """不同滑点 × 佣金率组合下的净收益敏感表。

    grid 的键:"slippage_bps"、"commission_rate"(各为数值列表,缺省则只用 base 的值)。
    返回行列表(外层遍历佣金率、内层遍历滑点),每行含 slippage_bps、commission_rate、
    gross_pnl、net_pnl、total_cost、n_executed、n_blocked。
    """
    b = base if base is not None else CostParams()
    g = dict(grid or {})
    unknown = set(g) - {"slippage_bps", "commission_rate"}
    if unknown:
        raise ValueError(f"grid 含未知键: {sorted(unknown)}")
    slips = list(g.get("slippage_bps", [b.slippage_bps]))
    comms = list(g.get("commission_rate", [b.commission_rate]))
    if not slips or not comms:
        raise ValueError("grid 的列表不能为空")
    items = [_to_trade(t) for t in trades]
    rows = []
    for c in comms:
        for s in slips:
            res = apply_costs(items, replace(b, commission_rate=c, slippage_bps=s))
            rows.append({"slippage_bps": s, "commission_rate": c, "gross_pnl": res.gross_pnl,
                         "net_pnl": res.net_pnl, "total_cost": res.total_cost,
                         "n_executed": res.n_executed, "n_blocked": res.n_blocked})
    return rows
