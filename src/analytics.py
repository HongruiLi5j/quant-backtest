"""回测结果的扩展分析：月度收益表、年度收益、滚动 Sharpe、样本内/外、交易分布、成本敏感性。

只用已有的每日收益和交易列表计算，不需要新数据。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TD = 252


def _f(v, d=4):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, d) if np.isfinite(v) else None


def basic(r: pd.Series) -> dict:
    """一段收益的核心指标（百分数口径和网站其他地方一致）。"""
    r = r.dropna()
    if len(r) < 20:
        return {}
    eq = (1 + r).cumprod()
    years = len(r) / TD
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else np.nan
    vol = r.std() * np.sqrt(TD)
    dn = r[r < 0].std() * np.sqrt(TD)
    mdd = float((eq / eq.cummax() - 1).min())
    return {
        "days": int(len(r)), "total": _f((eq.iloc[-1] - 1) * 100, 2), "cagr": _f(cagr * 100, 2),
        "vol": _f(vol * 100, 2), "sharpe": _f(r.mean() * TD / vol if vol > 0 else np.nan, 3),
        "sortino": _f(r.mean() * TD / dn if dn > 0 else np.nan, 3), "maxdd": _f(mdd * 100, 2),
        "calmar": _f(cagr / -mdd if mdd < 0 else np.nan, 3),
    }


def monthly(r: pd.Series) -> dict:
    m = (1 + r).groupby([r.index.year, r.index.month]).prod() - 1
    years = sorted(set(i[0] for i in m.index))
    mat = [[_f(m.get((y, k), np.nan) * 100, 2) if (y, k) in m.index else None for k in range(1, 13)] for y in years]
    yr = [_f(((1 + r[r.index.year == y]).prod() - 1) * 100, 2) for y in years]
    return {"years": years, "matrix": mat, "year_total": yr}


def rolling_sharpe(r: pd.Series, win: int = 126) -> list:
    if len(r) < win + 5:
        return [None] * len(r)
    s = r.rolling(win).mean() / r.rolling(win).std() * np.sqrt(TD)
    return [_f(x, 3) for x in s.to_numpy()]


def hist(vals, bins: int = 14) -> dict:
    v = np.asarray([x for x in vals if x is not None and np.isfinite(x)], dtype=float)
    if len(v) < 3:
        return {"edges": [], "counts": []}
    lo, hi = np.percentile(v, 1), np.percentile(v, 99)
    if hi <= lo:
        hi = lo + 1
    c, e = np.histogram(np.clip(v, lo, hi), bins=bins, range=(lo, hi))
    return {"edges": [_f(x, 2) for x in e], "counts": c.astype(int).tolist()}


def trade_stats(trade_rets: list, days: list) -> dict:
    tr = np.asarray(trade_rets, dtype=float)
    if len(tr) == 0:
        return {}
    w, l = tr[tr > 0], tr[tr <= 0]
    mx_loss = 0
    cur = 0
    for x in tr:
        cur = cur + 1 if x <= 0 else 0
        mx_loss = max(mx_loss, cur)
    return {
        "n": int(len(tr)), "win": _f(len(w) / len(tr) * 100, 1), "avg_win": _f(w.mean() if len(w) else 0, 2),
        "avg_loss": _f(l.mean() if len(l) else 0, 2), "best": _f(tr.max(), 2), "worst": _f(tr.min(), 2),
        "payoff": _f(w.mean() / -l.mean() if len(w) and len(l) and l.mean() < 0 else np.nan, 2),
        "expectancy": _f(tr.mean(), 2), "max_lose_streak": int(mx_loss),
        "avg_days": _f(np.mean(days) if len(days) else np.nan, 1),
        "ret_hist": hist(tr), "days_hist": hist(days, 12),
    }


def analyze(r: pd.Series, b: pd.Series, trade_rets=None, trade_days=None, exposure=None,
            split: float = 0.7, extra_bench: dict | None = None) -> dict:
    """r、b：策略和买入持有的每日收益（同一段区间）。trade_rets：每笔收益 %。"""
    n = len(r)
    cut = int(n * split)
    out = {
        "full": basic(r), "bench_full": basic(b),
        "split": {
            "date": r.index[cut].strftime("%Y-%m-%d") if 0 < cut < n else None,
            "is": basic(r.iloc[:cut]), "oos": basic(r.iloc[cut:]),
            "b_is": basic(b.iloc[:cut]), "b_oos": basic(b.iloc[cut:]),
        },
        "monthly": monthly(r), "bench_year": monthly(b)["year_total"],
        "rolling_sharpe": rolling_sharpe(r), "bench_rolling_sharpe": rolling_sharpe(b),
        "extra": {
            "skew": _f(r.skew(), 2), "kurt": _f(r.kurt(), 2),
            "var95": _f(np.percentile(r, 5) * 100, 2), "best_day": _f(r.max() * 100, 2),
            "worst_day": _f(r.min() * 100, 2),
            "pos_months": _f(float((pd.Series([x for row in monthly(r)["matrix"] for x in row if x is not None]) > 0).mean() * 100), 1)
            if monthly(r)["matrix"] else None,
        },
    }
    if trade_rets is not None:
        out["trades"] = trade_stats(list(trade_rets), list(trade_days or []))
    if exposure is not None:
        out["exposure"] = [_f(x, 3) for x in np.asarray(exposure, dtype=float)]
    if extra_bench:
        out["benchmarks"] = extra_bench
    return out


def assumptions(market: str | None, engine: str, cost_bp: float, is_stock: bool) -> list:
    """每个回测页固定显示的假设与局限。键 → (名称, 内容)。"""
    cn = market == "cn"
    rows = [
        ["执行时点", "策略在第 t 天收盘后产生信号，第 t+1 天开盘价成交；止损也按收盘判断、次日开盘执行。不用当天未来数据。"],
        ["手续费与滑点", f"单边成本 {cost_bp:g}bp，已包含佣金、印花税和滑点的估计，按每次换手扣除。真实成交会因流动性和行情而不同。"],
    ]
    if engine == "inhouse" or engine == "lab":
        rows.append(["仓位口径", "按目标权重（比例）计算，允许零股，不做整手取整；资金量很小时与真实成交会有差异。"])
    else:
        rows.append(["整手与最低佣金", "按 100 股一手和每笔最低 5 元佣金成交（A 股）；资金不足一手时无法买入。" if cn else "按股数成交，不足一股的部分取整。"])
    if cn:
        rows.append(["涨跌停", "一字涨停买不进、一字跌停卖不出；非一字板按正常成交处理，盘中涨停排队等真实情况未模拟。"])
        rows.append(["复权方式", "使用前复权价格计算；价格和行情软件显示的不同，但收益率是连续可比的。"])
        rows.append(["幸存者偏差", "股票池以当前可交易股票为主，已退市的公司大多不在其中，结果会偏乐观；全市场扫描页会单独列出含退市的对比。"])
    else:
        rows.append(["涨跌停与停牌", "美股没有涨跌停；熔断、停牌、盘前盘后价格未模拟。"])
        rows.append(["复权方式", "使用 Yahoo 的 Adj Close（含拆股和分红）计算收益。"])
        rows.append(["幸存者偏差", "股票池来自今天的上市名单，已退市公司不在其中，历史回测会偏乐观。"])
    rows.append(["数据来源", "免费行情源，偶有缺日或坏数据；缺失和异常已尽量检查，但不保证与付费数据完全一致。"])
    rows.append(["不构成建议", "历史回测不代表未来；参数是事后选的，存在过拟合风险。请重点看样本外表现。"])
    return rows
