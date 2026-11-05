"""权威因子库（T006 S3c）：24 个只用日线 OHLCV + 行业/名称的向量化打分函数。

来源（"精确可查"原则）：只有 wq12、wq54 是 WorldQuant 101 Alphas（Kakushadze 2016）的原式，写作 "Alpha#NN"，原公式贴在函数注释里；
其余要么是有明确文献的经典因子（Amihud 2002、Jegadeesh-Titman 1993、Ang et al. 2006 等），要么是"自行实现、参考 GTJA Alpha191 / MSCI Barra 方法论"
的简化版——没有核对过原报告的编号和页码，所以不写编号，也不声称是官方公式。公式在 FACTORS 里逐个写明；这里是"当日选股/监控用的打分函数"，不是经过验证的 Alpha：
    - 没有任何一个因子在本项目里做过样本外检验，也没有扣交易成本和多重检验（24 个因子同时看，总会有几个"碰巧显著"）；
    - 日线是后复权价，所以 价格×成交量 只是成交额的近似（复权倍数会放大它），"成交额>1 亿"的过滤因此偏宽松。
面板 Panel：宽表（行=交易日，列=代码），每个因子函数 fn(panel) -> 同形状的宽表（score 因子为数值，flag/filter 因子为 bool）。
未来函数：第 t 行只能用第 t 行及以前的数据；check_no_lookahead() 对任意因子做"截断重算"断言（截到 t 行算出的第 t 行必须与全量算出的一致）。
"""
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import pandas as pd

MIN_AMOUNT = 1e8          # 风险过滤：近 20 日平均成交额（元）下限


@dataclass(frozen=True)
class FactorMeta:
    id: str
    name: str
    category: str        # liquidity / price_volume / momentum / volatility / trend / risk
    kind: str            # score（数值，取分位触发）/ flag（布尔，True 即触发）/ filter（布尔，True=通过过滤）
    formula: str
    source: str
    direction: int       # +1：值越大越"好"；-1：值越小越"好"（触发集取方向调整后的前 quantile）；flag/filter 为 +1
    quantile: float      # 建议分位：score 因子取方向调整后排名在前 (1-quantile) 的股票；flag/filter 为 0
    min_bars: int        # 至少需要多少根日线才有值（之前为 NaN）


@dataclass
class Panel:
    """面板数据。open/high/low/close/volume 是同形状宽表（volume 单位：股）；其余可选。"""
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    names: Optional[pd.Series] = None          # 代码 -> 名称（ST 过滤用）
    industry: Optional[pd.Series] = None       # 代码 -> 行业（残差动量用）
    market_ret: Optional[pd.Series] = None     # 大盘日收益；缺省用全市场等权收益（沪深300 本地没有）

    def __post_init__(self):
        # D99：坏复权数据（如 600595 在 2019-05~2020-05 前复权价 ≤0）：OHLC 任一非正则整行置 NaN，
        # 复牌后的第一天收益因 pct_change(fill_method=None) 也变 NaN，不会出现 +1867% 的假收益
        bad = (self.open <= 0) | (self.high <= 0) | (self.low <= 0) | (self.close <= 0)
        if bool(bad.to_numpy().any()):
            self.open, self.high, self.low, self.close = (x.mask(bad) for x in (self.open, self.high, self.low, self.close))
            self.volume = self.volume.mask(bad)

    @property
    def ret(self):
        return self.close.pct_change(fill_method=None)

    @property
    def amount(self):
        return self.close * self.volume        # 近似：后复权价×股数

    def mkt_ret(self):
        if self.market_ret is not None:
            return self.market_ret.reindex(self.close.index)
        return self.ret.mean(axis=1)

    def truncate(self, n):
        """只保留前 n 行（用于未来函数断言）。"""
        return Panel(self.open.iloc[:n], self.high.iloc[:n], self.low.iloc[:n], self.close.iloc[:n], self.volume.iloc[:n],
                     self.names, self.industry, None if self.market_ret is None else self.market_ret.iloc[:n])


def _clean(df):
    """把 ±inf 变成 NaN（除零产生）。"""
    return df.replace([np.inf, -np.inf], np.nan)


def _roll_corr(a, b, n):
    """两个同形状宽表按列做滚动相关。"""
    return _clean(a.rolling(n, min_periods=n).corr(b))


# ---------------------------------------------------------------- 流动性 4
def amihud(p, n=20):
    """Amihud 非流动性：近 n 日 mean(|日收益| / 成交额)，数值×1e8 便于阅读。越大越不流动。"""
    return _clean(p.ret.abs() / p.amount).rolling(n, min_periods=n).mean() * 1e8


def turnover(p, n=60):
    """换手率代理：当日成交量 / 近 n 日平均成交量（无流通股本数据，不是真换手率；>1 表示放量）。"""
    return _clean(p.volume / p.volume.rolling(n, min_periods=n).mean())


def avg_amount(p, n=20):
    """日均成交额（元，近似）：近 n 日 mean(收盘价×成交量)。"""
    return p.amount.rolling(n, min_periods=n).mean()


def big_order_proxy(p, n=20):
    """大单买卖代理（Chaikin 资金流乘数）：sum(((2C-H-L)/(H-L))×V, n) / sum(V, n)；∈[-1,1]，>0 偏收在高位的放量。"""
    mult = _clean((2 * p.close - p.high - p.low) / (p.high - p.low))
    return _clean((mult * p.volume).rolling(n, min_periods=n).sum() / p.volume.rolling(n, min_periods=n).sum())


# ---------------------------------------------------------------- 价量短期 8
def wq12(p):
    """WQ Alpha#12。原式：(sign(delta(volume, 1)) * (-1 * delta(close, 1)))。放量下跌为正（反转），缩量上涨为正。"""
    return np.sign(p.volume.diff(1)) * (-p.close.diff(1))


def wq54(p):
    """WQ Alpha#54 简化版。原式：((-1 * ((low - close) * (open^5))) / ((low - high) * (close^5)))。比值取 (open/close)^5 以避免数值溢出，结果同值。"""
    return _clean(-(p.low - p.close) / (p.low - p.high) * (p.open / p.close) ** 5)


def pv_corr5(p):
    """价量 5 日相关：corr(close, volume, 5)。量价背离思路，取负向（相关越低越好）。不是 WQ101 原式（Alpha#6 是 -corr(open, volume, 10)）。"""
    return _roll_corr(p.close, p.volume, 5)


def intraday_skew(p, n=20):
    """日内偏度（日线近似）：近 n 日 (close/open-1) 的样本偏度；高偏度=彩票型，历史上偏低更好。"""
    return _clean((p.close / p.open - 1).rolling(n, min_periods=n).skew())


def gap_open(p):
    """开盘跳空：open_t / close_{t-1} - 1（后复权价下除权日也连续）。"""
    return p.open / p.close.shift(1) - 1


def hl_range(p, n=20):
    """高低点距：近 n 日最高价 / 最低价 - 1（区间振幅），越大越"疯"。"""
    return _clean(p.high.rolling(n, min_periods=n).max() / p.low.rolling(n, min_periods=n).min() - 1)


def close_position(p):
    """收盘位置：(close-low)/(high-low) ∈[0,1]；一字板（high==low）为 NaN。"""
    return _clean((p.close - p.low) / (p.high - p.low))


def reversal1(p):
    """短期反转 1 日：-(close_t/close_{t-1} - 1)。昨天跌得多的今天分高。"""
    return -p.ret


# ---------------------------------------------------------------- 动量/反转 4
def mom252_skip20(p):
    """252 日动量跳过最近 20 日：close_{t-20} / close_{t-252} - 1（Jegadeesh-Titman 12-1 动量）。"""
    return p.close.shift(20) / p.close.shift(252) - 1


def mom20(p):
    """20 日动量：close_t / close_{t-20} - 1。A 股 1 个月内通常是反转而非动量，方向取负。"""
    return p.close / p.close.shift(20) - 1


def resid_mom(p, n=20):
    """残差动量（扣行业）：n 日收益 - 同行业 n 日收益均值。无行业数据时退化为扣全市场均值。"""
    r = p.close / p.close.shift(n) - 1
    if p.industry is None:
        return r.sub(r.mean(axis=1), axis=0)
    ind = p.industry.reindex(r.columns).fillna('未知')
    grp = r.T.groupby(ind.values).transform('mean').T
    return r - grp


def rel_strength(p, n=20):
    """相对强弱（扣大盘）：n 日个股收益 - n 日大盘累计收益（大盘缺省为全市场等权，沪深300 不在本地）。"""
    r = p.close / p.close.shift(n) - 1
    m = (1 + p.mkt_ret()).rolling(n, min_periods=n).apply(np.prod, raw=True) - 1
    return r.sub(m, axis=0)


# ---------------------------------------------------------------- 波动率 2
def low_vol(p, n=60):
    """低波动因子：近 n 日日收益标准差（越小越好，方向 -1）。MSCI Barra 的 Volatility 风格因子的简化。"""
    return p.ret.rolling(n, min_periods=n).std()


def idio_vol(p, n=60):
    """特质波动率：近 n 日对大盘回归残差的标准差（Ang-Hodrick-Xing-Zhang 2006）；β 用同一窗口估计。"""
    m = p.mkt_ret()
    r = p.ret
    cov = r.rolling(n, min_periods=n).cov(m)
    var = m.rolling(n, min_periods=n).var()
    beta = cov.div(var, axis=0)
    total = r.rolling(n, min_periods=n).var()
    resid_var = (total - beta ** 2 * var.values[:, None]).clip(lower=0)      # Var(e)=Var(r)-β²Var(m)
    return np.sqrt(resid_var)


# ---------------------------------------------------------------- 趋势 3
def ma_bull(p):
    """均线多头排列：MA5 > MA20 > MA60（全部严格大于）。"""
    c = p.close
    m5, m20, m60 = (c.rolling(k, min_periods=k).mean() for k in (5, 20, 60))
    return ((m5 > m20) & (m20 > m60)).where(m60.notna(), False)


def breakout20(p):
    """20 日突破：close_t > 此前 20 日（不含当日）最高收盘价。"""
    prior = p.close.shift(1).rolling(20, min_periods=20).max()
    return (p.close > prior).where(prior.notna(), False)


def vol_breakout(p):
    """量能放大突破：20 日突破 且 当日成交量 > 此前 20 日（不含当日）均量 × 1.5。"""
    base = p.volume.shift(1).rolling(20, min_periods=20).mean()
    return (breakout20(p) & (p.volume > 1.5 * base)).where(base.notna(), False)


# ---------------------------------------------------------------- 风险过滤 3（True=通过）
def liquidity_filter(p):
    """流动性过滤：近 20 日日均成交额 > 1 亿元（近似值，见模块说明）。"""
    return (avg_amount(p) > MIN_AMOUNT).fillna(False)


def st_filter(p):
    """ST 过滤：名称含 ST 或 *ST 的剔除（名称是当前快照，历史日期的 ST 状态不精确）。无名称数据时全部通过。"""
    ok = pd.Series(True, index=p.close.columns)
    if p.names is not None:
        names = p.names.reindex(p.close.columns).fillna('').astype(str)
        ok = ~names.str.upper().str.contains('ST', regex=False)
    return pd.DataFrame(np.tile(ok.values, (len(p.close), 1)), index=p.close.index, columns=p.close.columns)


def suspension_filter(p):
    """停牌过滤：当日有成交（成交量>0 且收盘价存在）才算在交易。数据里停牌日没有 K 线（NaN）。"""
    return ((p.volume > 0) & p.close.notna()).fillna(False)


def _m(id_, name, cat, kind, fn, formula, source, direction, quantile, min_bars):
    return FactorMeta(id_, name, cat, kind, formula, source, direction, quantile, min_bars), fn


_WQ = 'WorldQuant 101 Formulaic Alphas (Kakushadze 2016) '        # 只用于原式：后面必须跟 "Alpha#NN"
_GTJA = '自行实现，思路参考国泰君安 Alpha191 同类价量因子（未核对原报告编号，非逐式复现）'
_MSCI = '自行实现，参考 MSCI Barra China (CNE5) 风格因子方法论（简化版，非官方公式）'

_DEFS = [
    _m('amihud', 'Amihud 非流动性', 'liquidity', 'score', amihud, 'mean(|r_t|/amount_t, 20)', 'Amihud (2002) "Illiquidity and stock returns", Journal of Financial Markets 5(1)', -1, 0.9, 21),
    _m('turnover', '换手率代理(量比)', 'liquidity', 'score', turnover, 'volume_t / mean(volume, 60)', _MSCI + '；Liquidity 风格，用量比近似（无流通股本）', -1, 0.9, 60),
    _m('avg_amount', '日均成交额', 'liquidity', 'score', avg_amount, 'mean(close×volume, 20)', _MSCI + '；Size/Liquidity 风格，用成交额近似', 1, 0.9, 20),
    _m('big_order', '大单买卖代理', 'liquidity', 'score', big_order_proxy, 'sum(((2C-H-L)/(H-L))×V,20)/sum(V,20)', 'Chaikin Money Flow（技术分析教材的资金流乘数）；自行实现，非 WQ101/GTJA 原式', 1, 0.9, 20),
    _m('wq12', 'WQ Alpha#12', 'price_volume', 'score', wq12, 'sign(delta(volume,1))×(-delta(close,1))', _WQ + 'Alpha#12', 1, 0.9, 2),
    _m('wq54', 'WQ Alpha#54 简化', 'price_volume', 'score', wq54, '-(low-close)×open^5/((low-high)×close^5)', _WQ + 'Alpha#54', 1, 0.9, 1),
    _m('pv_corr5', '价量5日相关', 'price_volume', 'score', pv_corr5, 'corr(close, volume, 5)', '改编自量价相关类思路（非 WQ101 原式，WQ101 Alpha#6 为 -corr(open,volume,10)）', -1, 0.9, 5),
    _m('intraday_skew', '日内偏度(日线近似)', 'price_volume', 'score', intraday_skew, 'skew(close/open-1, 20)', 'Harvey-Siddique (2000) Conditional skewness, Journal of Finance 55(3)；' + _GTJA, -1, 0.9, 20),
    _m('gap_open', '开盘跳空', 'price_volume', 'score', gap_open, 'open_t/close_{t-1}-1', '改编自技术分析教材的跳空缺口（gap）概念；' + _GTJA, -1, 0.9, 2),
    _m('hl_range', '高低点距', 'price_volume', 'score', hl_range, 'max(high,20)/min(low,20)-1', '改编自技术分析教材的区间振幅（range）概念；' + _GTJA, -1, 0.9, 20),
    _m('close_pos', '收盘位置', 'price_volume', 'score', close_position, '(close-low)/(high-low)', 'Close Location Value（Chaikin/Williams 技术分析教材的单日形式，非 WQ101 原式）', 1, 0.9, 1),
    _m('reversal1', '短期反转1日', 'price_volume', 'score', reversal1, '-(close_t/close_{t-1}-1)', 'Jegadeesh (1990) "Evidence of predictable behavior of security returns", Journal of Finance 45(3)；' + _MSCI, 1, 0.9, 2),
    _m('mom252', '12-1 动量', 'momentum', 'score', mom252_skip20, 'close_{t-20}/close_{t-252}-1', 'Jegadeesh-Titman (1993) "Returns to buying winners and selling losers", Journal of Finance 48(1)；' + _MSCI, 1, 0.9, 253),
    _m('mom20', '20日动量(A股偏反转)', 'momentum', 'score', mom20, 'close_t/close_{t-20}-1', '改编自 Jegadeesh (1990) 月度反转；' + _GTJA, -1, 0.9, 21),
    _m('resid_mom', '残差动量(扣行业)', 'momentum', 'score', resid_mom, 'ret20 - mean_行业(ret20)', 'Blitz-Huij-Martens (2011) "Residual momentum", Journal of Empirical Finance 18(3)；行业口径为自行实现', 1, 0.9, 21),
    _m('rel_strength', '相对强弱(扣大盘)', 'momentum', 'score', rel_strength, 'ret20 - mkt_ret20', _MSCI + '；Relative Strength 思路，大盘用全市场等权近似', 1, 0.9, 21),
    _m('low_vol', '低波动', 'volatility', 'score', low_vol, 'std(r, 60)', _MSCI + '；Ang et al. (2006) 低波动异象', -1, 0.9, 61),
    _m('idio_vol', '特质波动率', 'volatility', 'score', idio_vol, 'sqrt(Var(r,60)-β²Var(m,60))', 'Ang-Hodrick-Xing-Zhang (2006) "The cross-section of volatility and expected returns", Journal of Finance 61(1)', -1, 0.9, 61),
    _m('ma_bull', '均线多头排列', 'trend', 'flag', ma_bull, 'MA5>MA20>MA60', '经典均线多头排列（技术分析教材）；自行实现，非 WQ101/GTJA 原式', 1, 0.0, 60),
    _m('breakout20', '20日突破', 'trend', 'flag', breakout20, 'close_t > max(close_{t-20..t-1})', 'Donchian channel / Turtle 规则', 1, 0.0, 21),
    _m('vol_breakout', '量能放大突破', 'trend', 'flag', vol_breakout, '20日突破 且 volume_t > 1.5×mean(volume_{t-20..t-1})', 'Donchian channel + 量能确认（技术分析教材的常见定义，阈值 1.5 倍为项目自设）', 1, 0.0, 21),
    _m('liq_filter', '流动性过滤>1亿', 'risk', 'filter', liquidity_filter, 'mean(close×volume,20) > 1e8', '实盘可成交性的常规门槛（项目自设）', 1, 0.0, 20),
    _m('st_filter', 'ST过滤', 'risk', 'filter', st_filter, "名称不含 'ST'", 'A股 ST/*ST 退市风险（主板 ST 涨跌幅 2026-07-06 起由 5% 改为 10%）', 1, 0.0, 0),
    _m('susp_filter', '停牌过滤', 'risk', 'filter', suspension_filter, 'volume_t>0 且收盘价存在', 'A股停牌不可成交', 1, 0.0, 1),
]

FACTORS = {meta.id: meta for meta, _ in _DEFS}
FUNCS = {meta.id: fn for meta, fn in _DEFS}
assert len(FACTORS) == 24


def compute_factor(factor_id, panel) -> pd.DataFrame:
    """算一个因子的完整宽表；前 min_bars-1 行强制为 NaN/False，避免窗口不足时的伪值。"""
    meta, out = FACTORS[factor_id], FUNCS[factor_id](panel)
    out = out.reindex(index=panel.close.index, columns=panel.close.columns)
    cut = max(meta.min_bars - 1, 0)
    if cut:
        out = out.copy()
        out.iloc[:cut] = False if meta.kind != 'score' else np.nan
    return out


def trigger_mask(factor_id, values) -> pd.DataFrame:
    """触发集宽表（bool）：score 因子取方向调整后截面分位排名 > quantile（即最靠前的 1-quantile）；flag/filter 取 True。"""
    meta = FACTORS[factor_id]
    if meta.kind != 'score':
        return values.fillna(False).astype(bool)
    pct = (values * meta.direction).rank(axis=1, pct=True)
    return pct > meta.quantile


def check_no_lookahead(factor_id, panel, cut):
    """未来函数断言：只给前 cut 行重算，第 cut-1 行必须与全量计算在同一行的结果相同。违反则 AssertionError。"""
    full = compute_factor(factor_id, panel).iloc[cut - 1]
    part = compute_factor(factor_id, panel.truncate(cut)).iloc[cut - 1]
    if FACTORS[factor_id].kind == 'score':
        same = np.allclose(full.values.astype(float), part.values.astype(float), rtol=1e-9, atol=1e-12, equal_nan=True)
    else:
        same = bool((full.astype(bool).values == part.astype(bool).values).all())
    assert same, f'未来函数: 因子 {factor_id} 的第 {cut - 1} 行依赖了之后的数据'
    return True
