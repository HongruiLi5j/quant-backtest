"""风险与绩效报告：参照机构回测平台（如 QuantConnect）常用的统计口径，一次回测给出分组的指标。

输入是策略每日收益 r、基准（买入持有）每日收益 b、无风险日收益 rf（可为 None）、逐笔交易表和每日权重。
返回 [{"group": 组名, "items": [{"k": 名称, "v": 数值或 None, "f": 格式, "tip": 说明}]}]，
格式 f：pct=百分比（值已乘 100）、num=小数、int=整数、days=天数。算不出来的给 None，网页显示 "—"。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

N = 252


def _f(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) else None


def _month_ret(r: pd.Series) -> pd.Series:
    try:
        return (1 + r).resample("ME").prod() - 1
    except ValueError:
        return (1 + r).resample("M").prod() - 1


def report(r: pd.Series, b: pd.Series, rf: pd.Series | None, trades, weights) -> list:
    r = r.dropna()
    b = b.reindex(r.index).fillna(0.0)
    rfd = rf.reindex(r.index).fillna(0.0) if rf is not None else pd.Series(0.0, index=r.index)
    eq = (1 + r).cumprod()
    years = max(len(r) / N, 1e-9)
    total = eq.iloc[-1] - 1
    cagr = eq.iloc[-1] ** (1 / years) - 1
    vol = r.std() * np.sqrt(N)
    down = r[r < 0].std() * np.sqrt(N)
    dd = eq / eq.cummax() - 1
    under = (dd < 0).astype(int)
    longest = int(under.groupby((under == 0).cumsum()).cumsum().max()) if len(under) else 0
    m = _month_ret(r)
    q5 = np.percentile(r, 5)
    ex, exb = r - rfd, b - rfd
    vb = b.var()
    beta = r.cov(b) / vb if vb > 0 else np.nan
    alpha = (ex.mean() - beta * exb.mean()) * N if np.isfinite(beta) else np.nan
    te = (r - b).std() * np.sqrt(N)
    up, dn = b > 0, b < 0
    sharpe0 = r.mean() / r.std() * np.sqrt(N) if r.std() > 0 else np.nan
    exs = ex.mean() / ex.std() * np.sqrt(N) if ex.std() > 0 else np.nan

    groups = []
    groups.append({"group": "收益", "items": [
        {"k": "累计收益", "v": _f(total * 100), "f": "pct"},
        {"k": "年化收益 (CAGR)", "v": _f(cagr * 100), "f": "pct"},
        {"k": "最好的一天", "v": _f(r.max() * 100), "f": "pct"},
        {"k": "最差的一天", "v": _f(r.min() * 100), "f": "pct"},
        {"k": "最好的一个月", "v": _f(m.max() * 100), "f": "pct"},
        {"k": "最差的一个月", "v": _f(m.min() * 100), "f": "pct"},
        {"k": "上涨月份占比", "v": _f((m > 0).mean() * 100), "f": "pct"}]})
    groups.append({"group": "风险", "items": [
        {"k": "年化波动", "v": _f(vol * 100), "f": "pct"},
        {"k": "年化方差", "v": _f(vol ** 2), "f": "num", "tip": "年化波动的平方"},
        {"k": "下行波动", "v": _f(down * 100), "f": "pct", "tip": "只算下跌日的波动"},
        {"k": "最大回撤", "v": _f(dd.min() * 100), "f": "pct"},
        {"k": "当前回撤", "v": _f(dd.iloc[-1] * 100), "f": "pct"},
        {"k": "最长回撤时间", "v": longest, "f": "days", "tip": "净值连续低于前高的最长天数"},
        {"k": "VaR 95%（单日）", "v": _f(-q5 * 100), "f": "pct", "tip": "95% 的日子亏损不会超过这个数"},
        {"k": "CVaR 95%（单日）", "v": _f(-r[r <= q5].mean() * 100), "f": "pct", "tip": "最差 5% 的日子平均亏多少"},
        {"k": "偏度", "v": _f(r.skew()), "f": "num"},
        {"k": "峰度", "v": _f(r.kurt()), "f": "num", "tip": "越大，极端行情越多"}]})
    groups.append({"group": "风险调整收益", "items": [
        {"k": "Sharpe（无风险利率 0）", "v": _f(sharpe0), "f": "num"},
        {"k": "超额 Sharpe", "v": _f(exs), "f": "num", "tip": "扣除短债收益后的 Sharpe"},
        {"k": "Sortino", "v": _f(r.mean() * N / down) if down > 0 else None, "f": "num"},
        {"k": "Calmar", "v": _f(cagr / -dd.min()) if dd.min() < 0 else None, "f": "num"},
        {"k": "Treynor", "v": _f(ex.mean() * N / beta) if np.isfinite(beta) and beta != 0 else None, "f": "num"}]})
    groups.append({"group": "相对买入持有", "items": [
        {"k": "Alpha（年化）", "v": _f(alpha * 100), "f": "pct", "tip": "扣除承担市场风险带来的收益之后，剩下的超额收益"},
        {"k": "Beta", "v": _f(beta), "f": "num"},
        {"k": "相关系数", "v": _f(r.corr(b)), "f": "num"},
        {"k": "跟踪误差", "v": _f(te * 100), "f": "pct"},
        {"k": "信息比率", "v": _f((r - b).mean() * N / te) if te > 0 else None, "f": "num"},
        {"k": "上涨捕获率", "v": _f(r[up].mean() / b[up].mean() * 100) if up.any() and b[up].mean() != 0 else None, "f": "pct", "tip": "基准上涨日，策略吃到了多少"},
        {"k": "下跌捕获率", "v": _f(r[dn].mean() / b[dn].mean() * 100) if dn.any() and b[dn].mean() != 0 else None, "f": "pct", "tip": "基准下跌日，策略承担了多少（越低越好）"}]})
    items = []
    try:
        t = trades
        if t is not None and len(t):
            t = t[(t.entry_date >= r.index[0]) & (t.entry_date <= r.index[-1])]
        if t is not None and len(t):
            wins, losses = t[t.ret > 0], t[t.ret <= 0]
            aw = wins.ret.mean() * 100 if len(wins) else 0.0
            al = losses.ret.mean() * 100 if len(losses) else 0.0
            wr = len(wins) / len(t)
            items = [
                {"k": "交易次数", "v": len(t), "f": "int"},
                {"k": "胜率", "v": _f(wr * 100), "f": "pct"},
                {"k": "平均盈利", "v": _f(aw), "f": "pct"},
                {"k": "平均亏损", "v": _f(al), "f": "pct"},
                {"k": "盈亏比（均盈/均亏）", "v": _f(aw / -al) if al < 0 else None, "f": "num"},
                {"k": "期望收益（每笔）", "v": _f(wr * aw + (1 - wr) * al), "f": "pct", "tip": "胜率×平均盈利 + 败率×平均亏损"},
                {"k": "Profit Factor", "v": _f(wins.ret.sum() / -losses.ret.sum()) if losses.ret.sum() < 0 else None, "f": "num"},
                {"k": "平均持仓天数", "v": _f(t.bars.mean()), "f": "num"}]
        w = weights.reindex(r.index).fillna(0.0)
        wsum = w.sum(axis=1) if hasattr(w, "sum") and getattr(w, "ndim", 1) > 1 else w
        items.append({"k": "持仓时间占比", "v": _f((wsum > 0).mean() * 100), "f": "pct"})
        turn = w.diff().abs().sum(axis=1).iloc[1:].sum() / 2 / years * 100 if getattr(w, "ndim", 1) > 1 else None
        items.append({"k": "年化换手率", "v": _f(turn), "f": "pct", "tip": "每年买卖掉相当于账户多少比例"})
    except Exception:
        pass
    if items:
        groups.append({"group": "交易与持仓", "items": items})
    return groups
