"""RQAlpha 版的 7 个策略（可以在网页「代码」页直接修改，保存后下一次运行就生效）。

写法约定：
  1. 每个策略是一个类，__init__ 接收参数（参数名和 src/registry.py 里登记的一致）
  2. on_bar(o, h, l, c)：每个交易日收盘后调用一次，传入今天的开高低收，返回明天的目标仓位 0~1
     —— 只能用今天及以前的数据；指标用 rq_base 里的流式指标（每天 update 一次）
  3. 下单、整手、涨跌停、T+1、手续费、印花税都交给 RQAlpha 处理（见 rq_base.build_funcs）

规则和 src/strategies.py（自研引擎版）逐条对应，指标算法也完全一样，
所以 RQAlpha 和自研引擎的差别，只来自成交细节（整手、最低佣金、真实印花税、资金不足等）。
"""
from engines.rq_base import ADX, ATR, RSI, SMA, Cross, PriorExtreme, RealizedVol, ok, vol_scale


# ================================================================ 可复用的"零件"
class TrendState:
    """单条均线 + 迟滞区间：价格高于均线 (1+buffer) 才进，低于均线 (1-buffer) 才出。"""

    def __init__(self, n, buffer):
        self.ma, self.buffer, self.on = SMA(n), buffer, False

    def update(self, c) -> float:
        m = self.ma.update(c)
        if ok(m):
            if not self.on and c > m * (1 + self.buffer):
                self.on = True
            elif self.on and c < m * (1 - self.buffer):
                self.on = False
        return 1.0 if self.on else 0.0


class DonchianState:
    """收盘创 N 日新高（不含今天）进场；跌破 M 日低点（不含今天）或从最高收盘回撤 k×ATR 出场。"""

    def __init__(self, entry_len, exit_len, atr_len, atr_mult):
        self.hi, self.lo = PriorExtreme(entry_len, "max"), PriorExtreme(exit_len, "min")
        self.atr, self.k = ATR(atr_len), atr_mult
        self.on, self.peak = False, 0.0

    def update(self, h, l, c) -> float:
        hi, lo, a = self.hi.update(h), self.lo.update(l), self.atr.update(h, l, c)
        if ok(hi) and ok(a):
            if not self.on:
                if c > hi:
                    self.on, self.peak = True, c
            else:
                self.peak = max(self.peak, c)
                if (ok(lo) and c < lo) or c < self.peak - self.k * a:
                    self.on = False
        return 1.0 if self.on else 0.0


class Rsi2State:
    """收盘 > 趋势均线 且 RSI < 阈值 买入；收盘 > 短均线 或 持有满 max_hold 天 卖出。"""

    def __init__(self, trend_len, rsi_len, entry_th, exit_ma, max_hold):
        self.trend, self.rsi, self.ex = SMA(trend_len), RSI(rsi_len), SMA(exit_ma)
        self.th, self.max_hold = entry_th, max_hold
        self.on, self.held = False, 0

    def update(self, c) -> float:
        t, r, e = self.trend.update(c), self.rsi.update(c), self.ex.update(c)
        if ok(t) and ok(r):
            if not self.on:
                if c > t and r < self.th:
                    self.on, self.held = True, 0
            else:
                self.held += 1
                if c > e or self.held >= self.max_hold:
                    self.on = False
        return 1.0 if self.on else 0.0


# ================================================================ 1. MA + ADX（原版）
class MaAdxOriginal:
    """快线上穿慢线 且 ADX > 阈值 → 满仓；快线下穿慢线 → 清仓。"""

    def __init__(self, fast=5, slow=20, adx_min=20.0, adx_len=14):
        self.mf, self.ms, self.adx = SMA(fast), SMA(slow), ADX(adx_len)
        self.up, self.down = Cross(), Cross()
        self.adx_min, self.on = adx_min, False

    def on_bar(self, o, h, l, c):
        f, s, a = self.mf.update(c), self.ms.update(c), self.adx.update(h, l, c)
        up, down = self.up.update(f, s), self.down.update(s, f)
        if not self.on and up and ok(a) and a > self.adx_min:
            self.on = True
        elif self.on and down:
            self.on = False
        return 1.0 if self.on else 0.0


# ================================================================ 2. MA + ADX 改进版
class MaAdxPlus:
    """原版 + 200 日均线之上才做多 + 吊灯跟踪止损 + 波动率目标仓位。"""

    def __init__(self, fast=5, slow=20, adx_min=20.0, atr_mult=3.0, target_vol=0.15,
                 adx_len=14, regime_len=200, atr_len=14, vol_len=20):
        self.mf, self.ms, self.adx = SMA(fast), SMA(slow), ADX(adx_len)
        self.up, self.down = Cross(), Cross()
        self.reg, self.atr, self.vol = SMA(regime_len), ATR(atr_len), RealizedVol(vol_len)
        self.adx_min, self.k, self.tv = adx_min, atr_mult, target_vol
        self.on, self.peak = False, 0.0

    def on_bar(self, o, h, l, c):
        f, s, a = self.mf.update(c), self.ms.update(c), self.adx.update(h, l, c)
        up, down = self.up.update(f, s), self.down.update(s, f)
        reg, atr, vol = self.reg.update(c), self.atr.update(h, l, c), self.vol.update(c)
        if ok(reg) and ok(atr):
            if not self.on:
                if up and ok(a) and a > self.adx_min and c > reg:
                    self.on, self.peak = True, c
            else:
                self.peak = max(self.peak, c)
                if down or c < self.peak - self.k * atr or c < reg:
                    self.on = False
        return vol_scale(self.tv, vol) if self.on else 0.0


# ================================================================ 3. 趋势 + 波动率目标（单均线）
class TrendVol:
    def __init__(self, sma_len=200, buffer=0.01, target_vol=0.15, vol_len=20):
        self.tr, self.vol, self.tv = TrendState(sma_len, buffer), RealizedVol(vol_len), target_vol

    def on_bar(self, o, h, l, c):
        return self.tr.update(c) * vol_scale(self.tv, self.vol.update(c))


# ================================================================ 4. 趋势集成（4 条均线）
class TrendEnsemble:
    LENS = (100, 150, 200, 250)

    def __init__(self, target_vol=0.15, vol_len=20, buffer=0.01):
        self.states = [TrendState(n, buffer) for n in self.LENS]
        self.vol, self.tv = RealizedVol(vol_len), target_vol

    def on_bar(self, o, h, l, c):
        avg = sum(s.update(c) for s in self.states) / len(self.states)
        return avg * vol_scale(self.tv, self.vol.update(c))


# ================================================================ 5. 海龟系统 1
class DonchianS1:
    def __init__(self, entry_len=20, exit_len=10, atr_mult=2.0, target_vol=0.15, atr_len=20, vol_len=20):
        self.dc = DonchianState(entry_len, exit_len, atr_len, atr_mult)
        self.vol, self.tv = RealizedVol(vol_len), target_vol

    def on_bar(self, o, h, l, c):
        return self.dc.update(h, l, c) * vol_scale(self.tv, self.vol.update(c))


# ================================================================ 6. RSI(2) 均值回归
class Rsi2:
    def __init__(self, trend_len=200, rsi_len=2, entry_th=10.0, exit_ma=5, max_hold=10):
        self.st = Rsi2State(trend_len, rsi_len, entry_th, exit_ma, max_hold)

    def on_bar(self, o, h, l, c):
        return self.st.update(c)


# ================================================================ 7. 组合：趋势集成 + RSI2 + 海龟
class Blend2:
    def __init__(self, w_trend=1 / 3, w_rsi=1 / 3, w_donchian=1 / 3):
        self.trend, self.rsi, self.dc = TrendEnsemble(), Rsi2(), DonchianS1()
        tot = w_trend + w_rsi + w_donchian
        self.mix = (1 / 3,) * 3 if tot <= 0 else (w_trend / tot, w_rsi / tot, w_donchian / tot)

    def on_bar(self, o, h, l, c):
        ws = (self.trend.on_bar(o, h, l, c), self.rsi.on_bar(o, h, l, c), self.dc.on_bar(o, h, l, c))
        return sum(m * w for m, w in zip(self.mix, ws))


STRATEGY_CLASSES = {
    "ma_adx_original": MaAdxOriginal,
    "ma_adx_plus": MaAdxPlus,
    "trend_vol": TrendVol,
    "trend_ens": TrendEnsemble,
    "donchian_s1": DonchianS1,
    "rsi2": Rsi2,
    "blend2": Blend2,
}
