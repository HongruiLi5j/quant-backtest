"""backtrader 框架部分：A 股手续费、"收盘出信号、次日开盘成交"的执行方式、涨跌停、整手。

策略本身写在 bt_strategies.py 里，只需要实现 decide()：看到今天收盘为止的数据，返回目标仓位（0~1）。
下单、整手、涨跌停、手续费都由这里统一处理，这样 7 个策略的执行规则完全一样。

backtrader 的时间线（开启 cheat_on_open 之后，每个交易日）：
    next_open()  开盘前被调用，此时能看到"今天的开盘价"，但看不到今天的最高/最低/收盘
      ↓          在这里按昨天收盘定下的目标仓位下单，订单以今天开盘价成交（相当于集合竞价）
    broker 撮合
      ↓
    next()       收盘后被调用，看到今天完整的 K 线 → 调用策略的 decide() 定出明天的目标仓位
这就是"信号用 t 日收盘数据计算，t+1 日开盘成交"，不会用到未来数据。
"""
from __future__ import annotations

import math

import backtrader as bt

from engines.common import COMMISSION, LIMIT_TOL, MIN_COMMISSION, TRANSFER_FEE, stamp_duty


class AShareCommission(bt.CommInfoBase):
    """A 股费用：佣金 + 过户费（合计万 2.6，每笔最低 5 元），卖出另收印花税（按成交日真实税率）。"""
    params = (("stocklike", True), ("commtype", bt.CommInfoBase.COMM_PERC), ("percabs", True))

    today = None     # 由策略在每天开盘前设置，用来决定印花税率

    def _getcommission(self, size, price, pseudoexec):
        amount = abs(size) * price
        fee = max(amount * (COMMISSION + TRANSFER_FEE), MIN_COMMISSION)
        if size < 0 and self.today is not None:
            fee += amount * stamp_duty(self.today)
        return fee


class AShareStrategy(bt.Strategy):
    """所有 A 股策略的基类。子类实现 decide() 即可。"""
    params = dict(
        limit=0.10,      # 涨跌停幅度（主板 10%，创业板/科创板 20%，ST 5%），由引擎按股票代码传入
        band=0.10,       # 调仓阈值：目标仓位变化小于 10% 就不动，省手续费（进场和清仓总是执行）
        lot=100,         # 一手 100 股
    )

    def __init__(self):
        self.target = 0.0        # 最近一次收盘后定下的目标仓位
        self.applied = 0.0       # 最近一次真正执行过的目标仓位（用于调仓阈值）
        self.fills = []          # 成交记录
        self.daily = []          # 每天收盘后的 (日期, 总资产, 股票市值占比)
        self.events = []         # 涨跌停买不进/卖不出等提示
        self._comm = None

    # ---------------- 子类要实现的
    def decide(self) -> float:
        raise NotImplementedError

    # ---------------- 收盘：记录净值、定明天的目标仓位
    def _record(self):
        v = self.broker.getvalue()
        pos = self.getposition(self.data).size * self.data.close[0]
        self.daily.append((self.data.datetime.date(0), v, pos / v if v > 0 else 0.0))

    def prenext(self):
        self._record()           # 指标还没算出来（预热期）：只记净值，不交易

    def next(self):
        w = self.decide()
        if w is None or not math.isfinite(w):
            w = 0.0
        self.target = min(max(float(w), 0.0), 1.0)
        self._record()

    # ---------------- 开盘：按目标仓位下单
    def prenext_open(self):
        pass

    def next_open(self):
        if self._comm is None:
            self._comm = self.broker.getcommissioninfo(self.data)
        today = self.data.datetime.date(0)
        self._comm.today = today
        tgt, cur = self.target, self.applied
        change = abs(tgt - cur) > self.p.band or ((tgt == 0) != (cur == 0))
        if not change:
            return
        o, prev_close = self.data.open[0], self.data.close[-1]
        gap = o / prev_close - 1
        pos = self.getposition(self.data).size
        value = self.broker.getcash() + pos * o            # 按开盘价估算的总资产
        want = math.floor(value * tgt / o / self.p.lot) * self.p.lot
        if tgt == 0:
            want = 0
        if want > pos:
            if gap >= self.p.limit - LIMIT_TOL:
                self.events.append((str(today), "开盘涨停，买不进"))
                return
            cash = self.broker.getcash()
            afford = math.floor((cash - MIN_COMMISSION) / (o * (1 + COMMISSION + TRANSFER_FEE)) / self.p.lot) * self.p.lot
            size = min(want - pos, max(afford, 0))
            if size > 0:
                self.buy(size=size)
        elif want < pos:
            if gap <= -self.p.limit + LIMIT_TOL:
                self.events.append((str(today), "开盘跌停，卖不出"))
                return
            self.sell(size=pos - want)
        self.applied = tgt

    def notify_order(self, order):
        if order.status == order.Completed:
            self.fills.append({"date": bt.num2date(order.executed.dt).strftime("%Y-%m-%d"),
                               "qty": int(order.executed.size), "price": float(order.executed.price),
                               "fee": float(order.executed.comm)})
        elif order.status in (order.Margin, order.Rejected, order.Canceled):
            self.events.append((str(self.data.datetime.date(0)), f"订单未成交：{order.getstatusname()}"))


# ---------------------------------------------------------------- 常用"零件"（组合策略可以复用）
class VolScale:
    """波动率目标仓位：仓位 = min(1, 目标年化波动 / 过去 n 天实际年化波动)；目标为 0 表示不调，固定满仓。
    backtrader 的 StdDev 是总体标准差（除以 n），这里乘 sqrt(n/(n-1)) 换成样本标准差，和 pandas 一致。"""

    def __init__(self, data, target_vol, n=20):
        self.tv = target_vol
        if target_vol:
            ret = bt.ind.PctChange(data.close, period=1)
            self.vol = bt.ind.StdDev(ret, period=n) * math.sqrt(n / (n - 1)) * math.sqrt(252)

    def get(self) -> float:
        if not self.tv:
            return 1.0
        v = self.vol[0]
        if not math.isfinite(v) or v <= 0:
            return 0.0
        return min(1.0, self.tv / v)
