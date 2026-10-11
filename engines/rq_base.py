"""RQAlpha 框架部分：流式指标 + 把策略接到 RQAlpha 的约定函数（init / open_auction / handle_bar）。

RQAlpha（米筐开源回测框架，https://github.com/ricequant/rqalpha）内置了 A 股的交易规则：
T+1、涨跌停不能成交、100 股一手、佣金最低 5 元、卖出印花税（按当时真实税率）、停牌不能交易。
这些都由 RQAlpha 自己检查和撮合，我们不自己写，这正是用它来"复核"自研引擎的意义。

时间线（每个交易日）：
    open_auction(context, bar_dict)  集合竞价阶段：看得到今天开盘价，按昨天收盘定的目标仓位下单
                                      → RQAlpha 以开盘价撮合（开盘涨停买不进、跌停卖不出，由 RQAlpha 判断）
    handle_bar(context, bar_dict)    收盘后：拿到今天完整的 K 线，喂给策略，定出明天的目标仓位

指标用"流式"写法：每来一根 K 线更新一次状态，只用到过去的数据，天然没有未来函数。
算法和 src/indicators.py 完全一致（Wilder 平滑用前 n 个值的简单平均做种子）。
"""
from __future__ import annotations

import math
from collections import deque

NAN = float("nan")


def ok(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


# ================================================================ 流式指标
class SMA:
    """简单移动平均。"""

    def __init__(self, n):
        self.n, self.q, self.s = n, deque(), 0.0
        self.value = NAN

    def update(self, x):
        self.q.append(x)
        self.s += x
        if len(self.q) > self.n:
            self.s -= self.q.popleft()
        self.value = self.s / self.n if len(self.q) == self.n else NAN
        return self.value


class Wilder:
    """Wilder 平滑：前 n 个有效值取平均做种子，之后 w = w + (x - w) / n。"""

    def __init__(self, n):
        self.n, self.seed, self.value = n, [], NAN

    def update(self, x):
        if not ok(x):
            return self.value
        if len(self.seed) < self.n:
            self.seed.append(x)
            if len(self.seed) == self.n:
                self.value = sum(self.seed) / self.n
        else:
            self.value = self.value + (x - self.value) / self.n
        return self.value


class ATR:
    """平均真实波幅。第一根 K 线的真实波幅 = 最高 - 最低（和 src/indicators.py 一致）。"""

    def __init__(self, n):
        self.w, self.prev_c, self.value = Wilder(n), None, NAN

    def update(self, h, l, c):
        tr = h - l if self.prev_c is None else max(h - l, abs(h - self.prev_c), abs(l - self.prev_c))
        self.prev_c = c
        self.value = self.w.update(tr)
        return self.value


class ADX:
    """Wilder 的 ADX（趋势强度，0~100，越大趋势越强，不分方向）。"""

    def __init__(self, n):
        self.pdm, self.mdm, self.tr, self.adx = Wilder(n), Wilder(n), Wilder(n), Wilder(n)
        self.prev = None
        self.value = NAN

    def update(self, h, l, c):
        if self.prev is None:          # 第一根没有前一天，DM/TR 记为缺失
            self.prev = (h, l, c)
            return self.value
        ph, pl, pc = self.prev
        self.prev = (h, l, c)
        up, down = h - ph, pl - l
        p = up if (up > down and up > 0) else 0.0
        m = down if (down > up and down > 0) else 0.0
        tr = max(h - l, abs(h - pc), abs(l - pc))
        ps, ms, ts = self.pdm.update(p), self.mdm.update(m), self.tr.update(tr)
        if ok(ts) and ts > 0 and ok(ps):
            pdi, mdi = 100 * ps / ts, 100 * ms / ts
            dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else NAN
            self.value = self.adx.update(dx)
        return self.value


class RSI:
    """Wilder 的 RSI；下跌平均为 0 时记为 100。"""

    def __init__(self, n):
        self.g, self.l, self.prev, self.value = Wilder(n), Wilder(n), None, NAN

    def update(self, c):
        if self.prev is None:
            self.prev = c
            return self.value
        d = c - self.prev
        self.prev = c
        g, l = self.g.update(max(d, 0.0)), self.l.update(max(-d, 0.0))
        if ok(g) and ok(l):
            self.value = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
        return self.value


class RealizedVol:
    """过去 n 天日收益率的样本标准差（除以 n-1），年化。"""

    def __init__(self, n):
        self.n, self.q, self.prev, self.value = n, deque(), None, NAN

    def update(self, c):
        if self.prev is not None:
            self.q.append(c / self.prev - 1)
            if len(self.q) > self.n:
                self.q.popleft()
        self.prev = c
        if len(self.q) == self.n:
            m = sum(self.q) / self.n
            var = sum((x - m) ** 2 for x in self.q) / (self.n - 1)
            self.value = math.sqrt(var) * math.sqrt(252)
        return self.value


class PriorExtreme:
    """过去 n 天（不含今天）的最高价 / 最低价。
    update(今天的值) 返回的是"不含今天"的过去 n 天极值，然后才把今天放进窗口，留给明天用。"""

    def __init__(self, n, kind="max"):
        self.n, self.q, self.f = n, deque(), max if kind == "max" else min
        self.value = NAN

    def update(self, x):
        self.value = self.f(self.q) if len(self.q) == self.n else NAN
        self.q.append(x)
        if len(self.q) > self.n:
            self.q.popleft()
        return self.value


class Cross:
    """a 今天 > b 且 昨天 a <= b（和 AmiBroker 的 Cross 一致）。"""

    def __init__(self):
        self.prev = None

    def update(self, a, b):
        res = False
        if ok(a) and ok(b) and self.prev is not None:
            pa, pb = self.prev
            res = ok(pa) and ok(pb) and a > b and pa <= pb
        self.prev = (a, b)
        return res


def vol_scale(target_vol, vol) -> float:
    """波动率目标仓位：目标为 0 表示不调仓位（满仓）。"""
    if not target_vol:
        return 1.0
    if not ok(vol) or vol <= 0:
        return 0.0
    return min(1.0, target_vol / vol)


# ================================================================ 接到 RQAlpha
def build_funcs(strategy, order_book_id: str, band: float = 0.10):
    """把一个"每根 K 线返回目标仓位"的策略对象，包装成 RQAlpha 的约定函数。"""
    from rqalpha.apis import get_position, order_shares
    from rqalpha.const import ORDER_STATUS

    def init(context):
        context.s = strategy
        context.oid = order_book_id
        context.target = 0.0       # 昨天收盘后定下的目标仓位
        context.applied = 0.0      # 最近一次成功执行的目标仓位
        context.pending = None     # 今天开盘下的单
        context.daily = []         # (日期, 总资产, 股票市值占比)
        context.events = []

    def open_auction(context, bar_dict):
        tgt, cur = context.target, context.applied
        if not (abs(tgt - cur) > band or ((tgt == 0) != (cur == 0))):
            return
        bar = bar_dict[context.oid]
        o = bar.open
        if not ok(o) or o <= 0:
            return                                   # 停牌：今天不交易，明天再试
        pos = int(get_position(context.oid).quantity)
        value = context.stock_account.cash + pos * o   # 按开盘价估算的总资产
        # 目标股数 = 总资产 × 目标仓位 ÷ 开盘价，向下取整到 100 股（和 backtrader 版完全相同的算法）
        want = 0 if tgt == 0 else int(value * tgt / o // 100) * 100
        if want > pos:
            # 和 backtrader 版相同：扣掉佣金、过户费和最低佣金后买得起多少手就买多少手。
            # 否则整手取整后剩下的现金不够付手续费时，RQAlpha 会整笔拒单（低价股更容易发生）
            from engines.common import COMMISSION, MIN_COMMISSION, TRANSFER_FEE
            afford = int((context.stock_account.cash - MIN_COMMISSION) / (o * (1 + COMMISSION + TRANSFER_FEE)) // 100) * 100
            want = pos + min(want - pos, max(afford, 0))
        order = order_shares(context.oid, want - pos) if want != pos else None
        # 资金不够时 RQAlpha 会自动少买（按整手），停牌、涨跌停时 RQAlpha 会拒单
        context.pending = (order, tgt, context.now.strftime("%Y-%m-%d"))

    def handle_bar(context, bar_dict):
        if context.pending is not None:
            order, tgt, day = context.pending
            context.pending = None
            if order is None:
                context.applied = tgt                  # 变化不足一手，不需要下单
            elif order.status in (ORDER_STATUS.REJECTED, ORDER_STATUS.CANCELLED) and order.filled_quantity == 0:
                context.events.append((day, (order.message or "订单未成交")[:120]))
            else:
                context.applied = tgt
        bar = bar_dict[context.oid]
        tv = context.portfolio.total_value
        mv = get_position(context.oid).market_value
        context.daily.append((context.now.strftime("%Y-%m-%d"), tv, mv / tv if tv > 0 else 0.0))
        if getattr(bar, "suspended", False) or not ok(bar.close):
            return                                     # 停牌日没有 K 线，指标不更新
        w = context.s.on_bar(bar.open, bar.high, bar.low, bar.close)
        context.target = min(max(float(w), 0.0), 1.0) if ok(w) else 0.0

    return {"init": init, "open_auction": open_auction, "handle_bar": handle_bar}
