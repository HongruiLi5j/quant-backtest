"""绩效指标。"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1).min())


def summarize(equity: pd.Series, trades: pd.DataFrame, close: pd.Series | None = None) -> dict:
    rets = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    total = equity.iloc[-1] / equity.iloc[0] - 1
    out = {
        "总收益 %": total * 100,
        "年化收益 CAGR %": ((1 + total) ** (1 / years) - 1) * 100 if years > 0 else np.nan,
        "年化波动 %": rets.std() * np.sqrt(TRADING_DAYS) * 100,
        "Sharpe (rf=0)": rets.mean() / rets.std() * np.sqrt(TRADING_DAYS) if rets.std() > 0 else np.nan,
        "最大回撤 %": max_drawdown(equity) * 100,
        "交易次数": len(trades),
    }
    if len(trades):
        wins = trades[trades.pnl > 0]
        losses = trades[trades.pnl <= 0]
        out.update({
            "胜率 %": len(wins) / len(trades) * 100,
            "平均盈利 %": wins.ret_pct.mean() if len(wins) else 0.0,
            "平均亏损 %": losses.ret_pct.mean() if len(losses) else 0.0,
            "盈亏比 Profit Factor": wins.pnl.sum() / -losses.pnl.sum() if losses.pnl.sum() < 0 else np.inf,
            "平均持仓天数": trades.bars.mean(),
            "持仓时间占比 %": trades.bars.sum() / len(equity) * 100,
        })
    if close is not None:
        bh = close / close.iloc[0]
        out["买入持有 总收益 %"] = (bh.iloc[-1] - 1) * 100
        out["买入持有 最大回撤 %"] = max_drawdown(bh) * 100
    return out


def sharpe(ret: pd.Series) -> float:
    s = ret.std()
    return float(ret.mean() / s * np.sqrt(TRADING_DAYS)) if s > 0 else np.nan


def stats(ret: pd.Series, trades: pd.DataFrame | None = None,
          weights: pd.DataFrame | None = None, rf: pd.Series | None = None) -> dict:
    """用每日收益序列算指标（给 portfolio.run_weights 的结果用），键名短一点方便做表。
    rf：无风险利率的日收益（如短债 ETF），给了就多算一个超额 Sharpe（ExSharpe），这是业界标准口径。"""
    ret = ret.dropna()
    eq = (1 + ret).cumprod()
    years = len(ret) / TRADING_DAYS
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    downside = ret[ret < 0].std() * np.sqrt(TRADING_DAYS)
    mdd = max_drawdown(eq)
    out = {
        "CAGR%": cagr * 100,
        "Vol%": ret.std() * np.sqrt(TRADING_DAYS) * 100,
        "Sharpe": sharpe(ret),
        "Sortino": ret.mean() * TRADING_DAYS / downside if downside > 0 else np.nan,
        "MaxDD%": mdd * 100,
        "Calmar": cagr / -mdd if mdd < 0 else np.nan,
    }
    if rf is not None:
        out["ExSharpe"] = sharpe(ret - rf.reindex(ret.index).fillna(0.0))
    if weights is not None:
        w = weights.loc[ret.index].sum(axis=1)
        out["Exposure%"] = (w > 0).mean() * 100
        out["AvgPos"] = w[w > 0].mean() if (w > 0).any() else 0.0
    if trades is not None and len(trades):
        t = trades[(trades.entry_date >= ret.index[0]) & (trades.entry_date <= ret.index[-1])]
        if len(t):
            wins, losses = t[t.ret > 0], t[t.ret <= 0]
            out.update({
                "Trades": len(t),
                "Win%": len(wins) / len(t) * 100,
                "PF": wins.ret.sum() / -losses.ret.sum() if losses.ret.sum() < 0 else np.inf,
                "AvgHold": t.bars.mean(),
            })
    return out


def format_summary(stats: dict) -> str:
    w = max(len(k) for k in stats) + 2
    lines = []
    for k, v in stats.items():
        val = f"{v:,.2f}" if isinstance(v, (float, np.floating)) else str(v)
        lines.append(f"{k:<{w}}{val:>12}")
    return "\n".join(lines)
