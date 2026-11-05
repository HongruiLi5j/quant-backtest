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


def sharpe_se(sr: float, years: float) -> float | None:
    """年化 Sharpe 的标准误（Lo 2002 的 iid 近似）：se ≈ √((1 + ½·SR²) / 年数)。
    假设日收益独立同分布；有自相关、肥尾时真实误差更大，所以只是下限式的参考。D76。"""
    if sr is None or not np.isfinite(sr) or years is None or years <= 0:
        return None
    return float(np.sqrt((1 + 0.5 * sr * sr) / years))


def wilson(k: int, n: int, z: float = 1.96):
    """比例的 95% Wilson 区间（百分数）。交易少时区间很宽，这正是要让人看到的。D76。"""
    if n <= 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return _f(max(0.0, mid - half) * 100, 1), _f(min(1.0, mid + half) * 100, 1)


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
        "sharpe_se": _f(sharpe_se(r.mean() * TD / vol if vol > 0 else np.nan, years), 3),     # D76：iid 近似
        "sharpe_se_note": "Sharpe ± 1.96×se 为 95% 范围；se 按日收益独立同分布近似（Lo 2002），真实误差通常更大",
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
        "win_lo": wilson(len(w), len(tr))[0], "win_hi": wilson(len(w), len(tr))[1],      # D76：胜率 95% Wilson 区间
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


def _survivorship_cn() -> str:
    """D31（revquant P2）：幸存者偏差的说明按全市场扫描的实际情况写，不再承诺一个不存在的「含退市对比」。
    读 data/daily/scan_cn_meta.json 的 delisted / excluded_st；读不到就用不带数字的说法。"""
    try:
        import json
        from .datasource import CACHE
        m = json.loads((CACHE / "scan_cn_meta.json").read_text(encoding="utf-8"))
        dl, st = int(m.get("delisted") or 0), int(m.get("excluded_st") or 0)
    except Exception:
        return "股票池以当前可交易股票为主，已退市的公司基本不在其中，而且按今天的 ST 名单剔除了历史：结果偏乐观。"
    if dl > 0:
        return (f"全市场统计包含 {dl} 只已退市股票（不一定完整）；按今天的 ST 名单剔除了 {st} 只股票的全部历史，"
                "结果仍可能偏乐观。")
    return f"目前全市场统计不含已退市股票（0 只），而且按今天的 ST 名单剔除了 {st} 只股票的全部历史：结果偏乐观。"


def _survivorship_cn_en() -> str:
    """D89/R8：_survivorship_cn 的英文版（同样的数字、同样的判断）。"""
    try:
        import json
        from .datasource import CACHE
        m = json.loads((CACHE / "scan_cn_meta.json").read_text(encoding="utf-8"))
        dl, st = int(m.get("delisted") or 0), int(m.get("excluded_st") or 0)
    except Exception:
        return "The universe is mostly currently tradable stocks; delisted companies are largely missing and history is cut using today's ST list: results are optimistic."
    if dl > 0:
        return (f"Full-market statistics include {dl} delisted stocks (not necessarily complete); the whole history of {st} stocks is excluded "
                "using today's ST list, so results may still be optimistic.")
    return f"Full-market statistics currently contain no delisted stocks (0), and the whole history of {st} stocks is excluded using today's ST list: results are optimistic."


def assumptions(market: str | None, engine: str, cost_bp: float, is_stock: bool) -> list:
    """每个回测页固定显示的假设与局限。键 → (名称, 内容)。"""
    cn = market == "cn"
    rows = [
        ["执行时点", "策略在第 t 天收盘后产生信号，第 t+1 天开盘价成交；止损也按收盘判断、次日开盘执行。不用当天未来数据。"],
        ["手续费与滑点", f"单边成本 {cost_bp:g}bp，已包含佣金、印花税和滑点的估计，按每次换手扣除。真实成交会因流动性和行情而不同。"],
    ]
    if engine == "inhouse" or engine == "lab":
        # D32（revquant P3，src/portfolio.py:89-104）：非整数仓位每天开盘被调回目标比例，这部分调仓没计手续费
        rows.append(["仓位口径", "按目标权重（比例）计算，允许零股，不做整手取整；资金量很小时与真实成交会有差异。"
                     "非整数仓位按「每天开盘调回目标比例」计算，这部分调仓没有计手续费；真实持有时不会天天调，收益和费用都会有差别。"])
    else:
        rows.append(["整手与最低佣金", "按 100 股一手和每笔最低 5 元佣金成交（A 股）；资金不足一手时无法买入。" if cn else "按股数成交，不足一股的部分取整。"])
    if cn:
        # revquant PD_after N2：引擎按开盘价判断（portfolio.py:76，开盘跳空到涨停 / 跌停 ±0.2pp 即不能成交），不是「一字板」
        rows.append(["涨跌停", "开盘价已到涨停（跌停）就当作买不进（卖不出），按最新的涨跌停规则；盘中开板、排队等真实情况未模拟。"])
        # revquant PD_after N1：磁盘上是后复权（allmarket.py:6-7），只有显示时换算成前复权（display_price.py）
        rows.append(["复权方式", "回测用后复权价格（含分红送股，收益连续）；页面显示的价格换算成以最新一天为基准的前复权，和行情软件一致。"])
        # revquant PD_after：单只 ETF 回测不涉及选股，全市场的幸存者说明不适用
        rows.append(["幸存者偏差", _survivorship_cn() if is_stock else "单只 ETF 回测不涉及选股，幸存者偏差不适用；全市场统计见全市场页。"])
    else:
        rows.append(["涨跌停与停牌", "美股没有涨跌停；熔断、停牌、盘前盘后价格未模拟。"])
        rows.append(["复权方式", "使用 Yahoo 的 Adj Close（含拆股和分红）计算收益。"])
        # D87（revquant_us.md U1-A / U4）：美股样本用的是今天的条件（市值名单、股价、成交额），已退市的数据里没有
        rows.append(["幸存者偏差", "样本是今天市值名单里、今天股价 ≥ 1 美元、最近成交活跃的股票，已退市和后来跌没了的公司不在里面（数据里没有退市股）；历史结果明显偏好。"
                     if is_stock else "单只 ETF 回测不涉及选股，幸存者偏差不适用；全市场统计见全市场页。"])
    rows.append(["数据来源", "免费行情源，偶有缺日或坏数据；缺失和异常已尽量检查，但不保证与付费数据完全一致。"])
    # revquant PD_after N3：今晚统一口径是「没有干净的样本外」
    rows.append(["不构成建议", "历史回测不代表未来；参数是事后选的，存在过拟合风险。请重点看后 30% 区间，以及信号核对（前向跟踪）的结果。"])
    return rows


def assumptions_en(market: str | None, engine: str, cost_bp: float, is_stock: bool) -> list:
    """D89/R8：assumptions() 的英文版，条数、顺序、条件完全一致（前端英文模式用 assumptions_en 整体替换）。"""
    cn = market == "cn"
    etf_note = "A single-ETF backtest involves no stock selection, so survivorship bias does not apply; see the full-market page for market-wide statistics."
    rows = [
        ["Execution timing", "The signal is generated after the close of day t and filled at the open of day t+1; stops are judged on the close and executed at the next open. No same-day future data is used."],
        ["Fees and slippage", f"One-way cost {cost_bp:g}bp, an estimate that includes commission, stamp duty and slippage, charged on every turnover. Real fills vary with liquidity and market conditions."],
    ]
    if engine == "inhouse" or engine == "lab":
        rows.append(["Position sizing", "Computed on target weights (proportions), fractional shares allowed, no lot rounding; with very small capital real fills will differ. "
                     "Non-integer positions are rebalanced to the target proportion at every open and that rebalancing is not charged fees; a real holder would not rebalance daily, so return and fees would differ."])
    else:
        rows.append(["Lots and minimum commission", "Filled in 100-share lots with a 5 CNY minimum commission per trade (A-shares); less than one lot cannot be bought." if cn else "Filled by share count; fractions of a share are rounded."])
    if cn:
        rows.append(["Price limits", "If the open is already at the limit-up (limit-down) price, the order is treated as unfillable, using the latest price-limit rules; intraday limit openings and queueing are not simulated."])
        rows.append(["Price adjustment", "The backtest uses back-adjusted prices (dividends and splits included, returns continuous); displayed prices are converted to forward-adjusted prices based on the latest day, matching quote software."])
        rows.append(["Survivorship bias", _survivorship_cn_en() if is_stock else etf_note])
    else:
        rows.append(["Price limits and halts", "US stocks have no price limits; circuit breakers, halts and pre/after-market prices are not simulated."])
        rows.append(["Price adjustment", "Returns use Yahoo's Adj Close (splits and dividends included)."])
        rows.append(["Survivorship bias", "The sample is stocks on today's market-cap list, priced at $1 or more today and recently actively traded; delisted companies and those that later collapsed are not in it (the data has no delisted stocks), so historical results are clearly flattered." if is_stock else etf_note])
    rows.append(["Data source", "Free market-data sources, occasionally missing days or bad data; gaps and anomalies are checked as far as possible but may not match paid data exactly."])
    rows.append(["Not advice", "Past backtests do not predict the future; parameters were chosen with hindsight, so overfitting is a risk. Focus on the last 30% of the period and on the signal-check (forward tracking) results."])
    return rows
