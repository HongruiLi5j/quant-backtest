"""backtrader 版的 7 个策略（可以在网页「代码」页直接修改，保存后下一次运行就生效）。

写法约定：
  1. 每个策略继承 AShareStrategy，在 __init__ 里用 backtrader 自带的指标库（bt.ind.*）建指标
  2. 实现 decide()：只能看"今天收盘为止"的数据（指标的 [0] 是今天，[-1] 是昨天），返回目标仓位 0~1
  3. 下单、整手、涨跌停、手续费都由 bt_base.AShareStrategy 统一处理，这里不用管
  4. 参数名要和 src/registry.py 里登记的一致（网页上的参数输入框就是从那里来的）

规则和 src/strategies.py（自研引擎版）逐条对应；两边的差别主要来自指标的起算方式，
例如 backtrader 的 ATR 从第 2 根 K 线开始算（需要前一天收盘价），自研版第 1 根就用 最高-最低。
"""
import math

import backtrader as bt

from engines.bt_base import AShareStrategy, VolScale


# ================================================================ 可复用的"零件"
class TrendState:
    """单条均线 + 迟滞区间：价格高于均线 (1+buffer) 才进，低于均线 (1-buffer) 才出。"""

    def __init__(self, data, n, buffer):
        self.c = data.close
        self.ma = bt.ind.SMA(data.close, period=n)
        self.buffer = buffer
        self.on = False

    def step(self) -> float:
        c, m = self.c[0], self.ma[0]
        if not self.on and c > m * (1 + self.buffer):
            self.on = True
        elif self.on and c < m * (1 - self.buffer):
            self.on = False
        return 1.0 if self.on else 0.0


class DonchianState:
    """通道突破：收盘创 N 日新高（不含今天）进场；跌破 M 日最低价（不含今天），
    或从持仓期最高收盘回撤超过 k×ATR（吊灯止损）出场。"""

    def __init__(self, data, entry_len, exit_len, atr_len, atr_mult):
        self.c = data.close
        self.hi = bt.ind.Highest(data.high(-1), period=entry_len)   # data.high(-1)：错开一天，不含今天
        self.lo = bt.ind.Lowest(data.low(-1), period=exit_len)
        self.atr = bt.ind.ATR(data, period=atr_len)
        self.k = atr_mult
        self.on, self.peak = False, 0.0

    def step(self) -> float:
        c = self.c[0]
        if not self.on:
            if c > self.hi[0]:
                self.on, self.peak = True, c
        else:
            self.peak = max(self.peak, c)
            if c < self.lo[0] or c < self.peak - self.k * self.atr[0]:
                self.on = False
        return 1.0 if self.on else 0.0


class Rsi2State:
    """长期上升趋势中买短期超跌：收盘 > 趋势均线 且 RSI < 阈值 买入；
    收盘 > 短均线 或 持有满 max_hold 天 卖出。"""

    def __init__(self, data, trend_len, rsi_len, entry_th, exit_ma, max_hold):
        self.c = data.close
        self.trend = bt.ind.SMA(data.close, period=trend_len)
        self.rsi = bt.ind.RSI(data.close, period=rsi_len, safediv=True)
        self.ex = bt.ind.SMA(data.close, period=exit_ma)
        self.th, self.max_hold = entry_th, max_hold
        self.on, self.held = False, 0

    def step(self) -> float:
        c = self.c[0]
        if not self.on:
            if c > self.trend[0] and self.rsi[0] < self.th:
                self.on, self.held = True, 0
        else:
            self.held += 1
            if c > self.ex[0] or self.held >= self.max_hold:
                self.on = False
        return 1.0 if self.on else 0.0


# ================================================================ 1. MA + ADX（原版）
class MaAdxOriginal(AShareStrategy):
    """快线上穿慢线 且 ADX > 阈值 → 买入满仓；快线下穿慢线 → 卖出。"""
    params = dict(fast=5, slow=20, adx_min=20.0, adx_len=14)

    def __init__(self):
        super().__init__()
        mf = bt.ind.SMA(self.data.close, period=self.p.fast)
        ms = bt.ind.SMA(self.data.close, period=self.p.slow)
        self.cross = bt.ind.CrossOver(mf, ms)          # +1 上穿，-1 下穿，0 没有交叉
        self.adx = bt.ind.ADX(self.data, period=self.p.adx_len)
        self.on = False

    def decide(self):
        if not self.on and self.cross[0] > 0 and self.adx[0] > self.p.adx_min:
            self.on = True
        elif self.on and self.cross[0] < 0:
            self.on = False
        return 1.0 if self.on else 0.0


# ================================================================ 2. MA + ADX 改进版
class MaAdxPlus(AShareStrategy):
    """原版 + 只在 200 日均线之上做多 + 吊灯跟踪止损（最高收盘 - k×ATR）+ 波动率目标仓位。"""
    params = dict(fast=5, slow=20, adx_min=20.0, atr_mult=3.0, target_vol=0.15,
                  adx_len=14, regime_len=200, atr_len=14, vol_len=20)

    def __init__(self):
        super().__init__()
        mf = bt.ind.SMA(self.data.close, period=self.p.fast)
        ms = bt.ind.SMA(self.data.close, period=self.p.slow)
        self.cross = bt.ind.CrossOver(mf, ms)
        self.adx = bt.ind.ADX(self.data, period=self.p.adx_len)
        self.reg = bt.ind.SMA(self.data.close, period=self.p.regime_len)
        self.atr = bt.ind.ATR(self.data, period=self.p.atr_len)
        self.vs = VolScale(self.data, self.p.target_vol, self.p.vol_len)
        self.on, self.peak = False, 0.0

    def decide(self):
        c = self.data.close[0]
        if not self.on:
            if self.cross[0] > 0 and self.adx[0] > self.p.adx_min and c > self.reg[0]:
                self.on, self.peak = True, c
        else:
            self.peak = max(self.peak, c)
            if self.cross[0] < 0 or c < self.peak - self.p.atr_mult * self.atr[0] or c < self.reg[0]:
                self.on = False
        return self.vs.get() if self.on else 0.0


# ================================================================ 3. 趋势 + 波动率目标（单均线）
class TrendVol(AShareStrategy):
    params = dict(sma_len=200, buffer=0.01, target_vol=0.15, vol_len=20)

    def __init__(self):
        super().__init__()
        self.tr = TrendState(self.data, self.p.sma_len, self.p.buffer)
        self.vs = VolScale(self.data, self.p.target_vol, self.p.vol_len)

    def decide(self):
        return self.tr.step() * self.vs.get()


# ================================================================ 4. 趋势集成（4 条均线）
class TrendEnsemble(AShareStrategy):
    """同时看 100/150/200/250 日均线，站上几条持几成仓，再乘以波动率仓位。"""
    params = dict(target_vol=0.15, vol_len=20, buffer=0.01)
    LENS = (100, 150, 200, 250)

    def __init__(self):
        super().__init__()
        self.states = [TrendState(self.data, n, self.p.buffer) for n in self.LENS]
        self.vs = VolScale(self.data, self.p.target_vol, self.p.vol_len)

    def decide(self):
        avg = sum(s.step() for s in self.states) / len(self.states)
        return avg * self.vs.get()


# ================================================================ 5. 海龟系统 1
class DonchianS1(AShareStrategy):
    params = dict(entry_len=20, exit_len=10, atr_mult=2.0, target_vol=0.15, atr_len=20, vol_len=20)

    def __init__(self):
        super().__init__()
        self.dc = DonchianState(self.data, self.p.entry_len, self.p.exit_len, self.p.atr_len, self.p.atr_mult)
        self.vs = VolScale(self.data, self.p.target_vol, self.p.vol_len)

    def decide(self):
        return self.dc.step() * self.vs.get()


# ================================================================ 6. RSI(2) 均值回归
class Rsi2(AShareStrategy):
    params = dict(trend_len=200, rsi_len=2, entry_th=10.0, exit_ma=5, max_hold=10)

    def __init__(self):
        super().__init__()
        self.st = Rsi2State(self.data, self.p.trend_len, self.p.rsi_len, self.p.entry_th,
                            self.p.exit_ma, self.p.max_hold)

    def decide(self):
        return self.st.step()


# ================================================================ 7. 组合：趋势集成 + RSI2 + 海龟
class Blend2(AShareStrategy):
    """三个子策略（都用默认参数）按权重合成一个仓位。"""
    params = dict(w_trend=1 / 3, w_rsi=1 / 3, w_donchian=1 / 3)

    def __init__(self):
        super().__init__()
        self.trend = [TrendState(self.data, n, 0.01) for n in TrendEnsemble.LENS]
        self.trend_vs = VolScale(self.data, 0.15, 20)
        self.rsi = Rsi2State(self.data, 200, 2, 10.0, 5, 10)
        self.dc = DonchianState(self.data, 20, 10, 20, 2.0)
        self.dc_vs = VolScale(self.data, 0.15, 20)
        tot = self.p.w_trend + self.p.w_rsi + self.p.w_donchian
        self.mix = (1 / 3,) * 3 if tot <= 0 else (self.p.w_trend / tot, self.p.w_rsi / tot, self.p.w_donchian / tot)

    def decide(self):
        w_t = sum(s.step() for s in self.trend) / len(self.trend) * self.trend_vs.get()
        w_r = self.rsi.step()
        w_d = self.dc.step() * self.dc_vs.get()
        return self.mix[0] * w_t + self.mix[1] * w_r + self.mix[2] * w_d


# 策略名 → 类。网页上的策略选择框、参数名都以 src/registry.py 为准
STRATEGY_CLASSES = {
    "ma_adx_original": MaAdxOriginal,
    "ma_adx_plus": MaAdxPlus,
    "trend_vol": TrendVol,
    "trend_ens": TrendEnsemble,
    "donchian_s1": DonchianS1,
    "rsi2": Rsi2,
    "blend2": Blend2,
}
