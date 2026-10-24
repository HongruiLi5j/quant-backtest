"""市场状态（Regime-Aware）与残差 Z-score（T006 S3c）。纯函数，输入 pd.Series / DataFrame，不读写文件。

状态规则（按优先级）：
    1. 截至 as_of 的数据不足            -> unknown（敞口 0%）
    2. 20 日已实现波动率 > 历史 75 分位   -> high_vol（敞口 25%）；严格大于，恰好等于不算
    3. 60 日累计收益 >= +5%             -> trend_up（60%）
    4. 60 日累计收益 <= -5%             -> trend_down（60%）
    5. 其他                              -> range（100%）
敞口系数是"建议的仓位上限比例"，不是预测；阈值（5%、75 分位）是约定值，没有调参，也没有样本外验证。
"""
import numpy as np
import pandas as pd

TREND_WINDOW = 60
VOL_WINDOW = 20
TREND_THRESHOLD = 0.05
VOL_QUANTILE = 0.75
MIN_HISTORY = TREND_WINDOW + VOL_WINDOW + 40      # 至少 120 根：60 日趋势 + 20 日波动率 + 40 个波动率历史点
EPS = 1e-12                                        # 比较容差，避免 105/100-1 这类浮点误差

SCALING = {'range': 1.0, 'trend_up': 0.6, 'trend_down': 0.6, 'high_vol': 0.25, 'unknown': 0.0}


def get_scaling(regime):
    """状态 -> 建议敞口系数；未知状态名按 unknown（0）处理。"""
    return SCALING.get(regime, 0.0)


def _is_high_vol(current, history):
    """当前波动率是否严格高于历史 75 分位（线性插值分位）。"""
    hist = pd.Series(history).dropna()
    if hist.empty or current is None or not np.isfinite(current):
        return False
    return bool(current - float(hist.quantile(VOL_QUANTILE)) > EPS)


def identify_regime(mkt_series, as_of):
    """mkt_series：大盘指数/净值水平序列（索引为日期，值>0）；as_of：判断日（只用 <= as_of 的数据）。"""
    if not isinstance(mkt_series, pd.Series):
        return 'unknown'
    s = mkt_series.dropna()
    s = s[s.index <= pd.Timestamp(as_of)]
    if len(s) < MIN_HISTORY or (s <= 0).any():
        return 'unknown'
    rets = s.pct_change().dropna()
    vol = rets.rolling(VOL_WINDOW, min_periods=VOL_WINDOW).std().dropna()
    if _is_high_vol(vol.iloc[-1], vol.values):
        return 'high_vol'
    cum = s.iloc[-1] / s.iloc[-1 - TREND_WINDOW] - 1
    if cum - TREND_THRESHOLD >= -EPS:
        return 'trend_up'
    if cum + TREND_THRESHOLD <= EPS:
        return 'trend_down'
    return 'range'


def get_residual_zscore(stock_ret, mkt_ret, hist_window=60):
    """滚动贝塔 -> 残差 -> Z-score。stock_ret 可为 Series 或 DataFrame（列=股票），mkt_ret 为 Series。

    第 t 天：β_t = 窗口 [t-W, t-1] 内 Cov(s,m)/Var(m)（不含当天，避免用当天信息估计）；
    残差 e_t = s_t - β_t·m_t；Z_t = (e_t - mean(e_{t-W..t-1})) / std(e_{t-W..t-1})。
    前 2W 天左右为 NaN（先要有 W 个 β，才有 W 个历史残差）。Z 大 = 今天比"按大盘应有的涨幅"多涨得多。
    """
    w = int(hist_window)
    if w < 5:
        raise ValueError('hist_window 太小')
    m = mkt_ret.reindex(stock_ret.index)
    cov = stock_ret.rolling(w, min_periods=w).cov(m)
    var = m.rolling(w, min_periods=w).var()
    beta = cov.div(var.replace(0, np.nan), axis=0).shift(1)
    resid = stock_ret.sub(beta.mul(m, axis=0))
    past_mean = resid.rolling(w, min_periods=w).mean().shift(1)
    past_std = resid.rolling(w, min_periods=w).std().shift(1)
    return resid.sub(past_mean).div(past_std.replace(0, np.nan))
