"""三个引擎共用的部分：数据、价格换算、A 股交易成本、绩效统计、逐笔交易配对。

把这些放在一起，是为了保证对比公平：三个引擎吃的是同一份数据、同一套成本假设、
同一套指标算法，结果不同只可能来自"引擎怎么撮合成交"，而不是口径不同。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import MARKETS                      # noqa: E402
from src import datasource, detail, metrics     # noqa: E402
from src.registry import STRATEGIES, clean_params  # noqa: E402

CN = MARKETS["cn"]

# 7 个单品种策略（双动量是多品种 ETF 轮动，不适用于单只个股，所以不在这里）
STRATS = ["ma_adx_original", "ma_adx_plus", "trend_vol", "trend_ens", "donchian_s1", "rsi2", "blend2"]

# ------------------------------------------------------------------ A 股交易成本（两个外部引擎完全一致）
COMMISSION = 0.00025       # 佣金 万2.5（双向收取），券商常见水平
TRANSFER_FEE = 0.00001     # 过户费 十万分之一（双向，2022 年起沪深统一）
MIN_COMMISSION = 5.0       # 佣金每笔最低 5 元
STAMP_CHANGE = date(2023, 8, 28)   # 印花税减半的日子


def stamp_duty(d) -> float:
    """印花税只在卖出时收：2023-08-28 之前千分之一，之后万分之五（按成交当天的真实税率）。"""
    d = d.date() if isinstance(d, datetime) else d
    return 0.001 if d < STAMP_CHANGE else 0.0005


LIMIT_TOL = 0.002          # 开盘涨幅 ≥ 涨停幅度 - 0.2% 就认为"一字/开盘涨停"（和 src/portfolio.py 相同）
WARMUP = 260               # 预热天数：最长的指标要 250 天，之前的结果不统计（和 src/registry.py 相同）
DEFAULT_CASH = 100_000.0   # 默认初始资金 10 万元


# ------------------------------------------------------------------ 数据
def load_stock(code: str) -> tuple[pd.DataFrame, dict]:
    """读取一只 A 股（或 A 股 ETF）的日线：全市场缓存优先，其次自选股缓存。价格是后复权。"""
    if not (datasource.is_valid_stock(code) or code in CN["universe"]):
        raise ValueError("请输入 6 位沪深 A 股代码（60/68/00/30 开头）")
    df, info = detail.lookup("cn", code, CN)
    if df is None or df.empty:
        raise ValueError(f"{code} 还没有数据（全市场数据里没有这只股票）")
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float).dropna()
    df = df[(df["Open"] > 0) & (df["Close"] > 0)]
    df.index = pd.to_datetime(df.index)
    return df.sort_index(), info


_SCALE_FILE = ROOT / "data" / "daily" / "engine_price_scale.json"


def _raw_close_tencent(code: str, day: str) -> float | None:
    """从腾讯行情取某一天的不复权收盘价（只请求 1 次）。取不到返回 None。"""
    import requests
    sym = ("sh" if code.startswith(("5", "6")) else "sz") + code
    try:
        r = requests.get("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
                         params={"_var": "kline_day", "param": f"{sym},day,{day},{day},5,"},
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}, timeout=8)
        t = r.text
        data = json.loads(t[t.find("={") + 1:])["data"][sym]
        rows = data.get("day") or data.get("qfqday") or []
        for row in rows:
            if row[0] == day:
                return float(row[2])
    except Exception:
        return None
    return None


def to_forward_adjusted(code: str, df: pd.DataFrame) -> tuple[pd.DataFrame, float, str]:
    """后复权 → "以最新一天为基准的前复权"：整条价格乘以同一个系数 k，使最后一天的价格等于真实成交价。

    为什么要换算：backtrader 和 RQAlpha 都按"股数"成交，要考虑 100 股一手和每笔最低 5 元佣金。
    后复权价格可能比真实价格高很多倍（例如茅台上万元），一手的金额就被严重放大了。
    整条价格乘同一个常数，不改变任何一天的涨跌幅，所以信号和收益率都不受影响，
    只让"一手多少钱"回到真实水平。
    """
    last_day = df.index[-1].strftime("%Y-%m-%d")
    cache = {}
    try:
        cache = json.loads(_SCALE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    hit = cache.get(code)
    k, note = None, ""
    if hit and hit.get("day") == last_day and hit.get("raw"):
        k = hit["raw"] / float(df["Close"].iloc[-1])   # 不同数据文件的后复权基准可能不同，k 按当前数据重算
    else:
        raw = _raw_close_tencent(code, last_day)
        if raw:
            k = raw / float(df["Close"].iloc[-1])
            cache[code] = {"day": last_day, "k": k, "raw": raw}
            try:
                _SCALE_FILE.parent.mkdir(parents=True, exist_ok=True)
                _SCALE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
            except Exception:
                pass
    if k is None:
        # 取不到真实价格（没联网）：把最后一天的价格缩放到 50 元左右，只是为了让一手的金额合理
        last = float(df["Close"].iloc[-1])
        k = 1.0 if 5 <= last <= 300 else 50.0 / last
        note = "没取到真实股价，价格按比例缩放（不影响收益率，只影响整手取整）"
    out = df.copy()
    out[["Open", "High", "Low", "Close"]] *= k
    return out, float(k), note


def limit_pct(code: str, name: str = "") -> float:
    return datasource.limit_pct(code, name)


def eval_start(index: pd.DatetimeIndex, start: str | None) -> pd.Timestamp:
    """统计区间的起点：预热期之后，且不早于用户选的开始日期。"""
    warm = index[min(WARMUP, len(index) - 1)]
    if start:
        try:
            return max(warm, pd.Timestamp(start))
        except Exception:
            pass
    return warm


def parse_params(name: str, raw: dict | None) -> dict:
    """参数白名单 + 范围检查（和网页「回测实验室」同一套定义）。"""
    if name not in STRATS:
        raise ValueError(f"未知策略 {name}")
    return clean_params(name, raw or {})


def strategy_list() -> list[dict]:
    return [{"key": k, "label": STRATEGIES[k]["label"], "desc": STRATEGIES[k]["desc"],
             "params": STRATEGIES[k]["params"]} for k in STRATS]


# ------------------------------------------------------------------ 成交 → 逐笔交易
def pair_trades(fills: list[dict]) -> list[dict]:
    """把成交记录配成"一笔交易"：从空仓开始买入，到全部卖出回到空仓为止。
    中间的加仓/减仓（波动率调仓）都算在同一笔里。
    fills: [{date, qty(买为正、卖为负), price, fee}]，按时间排序。"""
    trades, cur, pos = [], None, 0
    for f in fills:
        q = float(f["qty"])
        if abs(q) < 1e-9:
            continue
        if pos == 0 and q > 0:
            cur = {"entry": f["date"], "entry_px": f["price"], "buy_amt": 0.0, "sell_amt": 0.0,
                   "fees": 0.0, "max_qty": 0, "fills": 0}
        if cur is None:
            continue
        amt = abs(q) * f["price"]
        if q > 0:
            cur["buy_amt"] += amt
        else:
            cur["sell_amt"] += amt
        cur["fees"] += f.get("fee", 0.0)
        cur["fills"] += 1
        pos += q
        cur["max_qty"] = max(cur["max_qty"], pos)
        if pos <= 1e-9:
            cur.update(exit=f["date"], exit_px=f["price"], open=False)
            trades.append(cur)
            cur, pos = None, 0
    if cur is not None:
        cur.update(exit=None, exit_px=None, open=True, open_qty=pos)
        trades.append(cur)
    return trades


def _finish_trades(trades: list[dict], last_px: float, dates: pd.DatetimeIndex) -> list[dict]:
    """算每笔交易的收益（扣完所有费用）和持有天数。还没卖出的按最后收盘价估值。"""
    pos_of = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(dates)}
    out = []
    for t in trades:
        sell = t["sell_amt"] + (t.get("open_qty", 0) * last_px if t["open"] else 0.0)
        pnl = sell - t["buy_amt"] - t["fees"]
        exit_d = t["exit"] or dates[-1].strftime("%Y-%m-%d")
        days = pos_of.get(exit_d, len(dates) - 1) - pos_of.get(t["entry"], 0)
        out.append({"entry": t["entry"], "exit": t["exit"], "open": t["open"],
                    "entry_px": round(t["entry_px"], 4), "exit_px": None if t["exit_px"] is None else round(t["exit_px"], 4),
                    "days": int(days), "pnl": round(pnl, 2), "fees": round(t["fees"], 2),
                    "ret": round(pnl / t["buy_amt"] * 100, 3) if t["buy_amt"] > 0 else 0.0,
                    "fills": t["fills"]})
    return out


# ------------------------------------------------------------------ 结果整理（三个引擎输出同一种格式）
def _clean(v):
    if isinstance(v, (float, np.floating)):
        return None if not np.isfinite(v) else round(float(v), 6)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def pack_result(engine: str, name: str, code: str, info: dict, params: dict, df: pd.DataFrame,
                value: pd.Series, exposure: pd.Series, fills: list[dict], start: str | None,
                cash0: float, t0: float, extra: dict | None = None) -> dict:
    """value: 每天收盘后的账户总资产；exposure: 每天收盘后股票市值占总资产的比例。"""
    value = value.astype(float).reindex(df.index).ffill().fillna(cash0)
    exposure = exposure.astype(float).reindex(df.index).fillna(0.0)
    s0 = eval_start(df.index, start)
    v = value.loc[s0:]
    r = v.pct_change().fillna(0.0)
    r.iloc[0] = 0.0
    b = df["Close"].loc[s0:].pct_change().fillna(0.0)

    trades_all = _finish_trades(pair_trades(fills), float(df["Close"].iloc[-1]), df.index)
    s0s = s0.strftime("%Y-%m-%d")
    trades = [t for t in trades_all if t["entry"] >= s0s]
    tdf = pd.DataFrame(trades)

    def pack(x: pd.Series, with_trades: bool) -> dict:
        s = metrics.stats(x)
        s["TotalRet%"] = ((1 + x).prod() - 1) * 100
        if with_trades:
            s["Exposure%"] = float((exposure.loc[s0:] > 0.001).mean() * 100)
            s["AvgPos%"] = float(exposure.loc[s0:][exposure.loc[s0:] > 0.001].mean() * 100) if (exposure.loc[s0:] > 0.001).any() else 0.0
            s["Trades"] = len(trades)
            if len(trades):
                wins = tdf[tdf.pnl > 0]
                loss = tdf[tdf.pnl <= 0]
                s["Win%"] = len(wins) / len(tdf) * 100
                s["PF"] = wins.pnl.sum() / -loss.pnl.sum() if loss.pnl.sum() < 0 else float("inf")
                s["AvgHold"] = float(tdf.days.mean())
            s["Fees"] = float(sum(f.get("fee", 0.0) for f in fills if f["date"] >= s0s))
            s["FinalValue"] = float(v.iloc[-1] / v.iloc[0] * cash0)
        return {k: _clean(val) for k, val in s.items()}

    eq = (1 + r).cumprod()
    beq = (1 + b).cumprod()
    dd = eq / eq.cummax() - 1
    out = {
        "engine": engine, "strategy": name, "label": STRATEGIES[name]["label"], "code": code,
        "name": info.get("name", code), "params": params, "cash": cash0,
        "start": s0s, "end": df.index[-1].strftime("%Y-%m-%d"),
        "dates": [d.strftime("%Y-%m-%d") for d in eq.index],
        "equity": np.round(eq.to_numpy(), 5).tolist(),
        "bench": np.round(beq.to_numpy(), 5).tolist(),
        "drawdown": np.round(dd.to_numpy() * 100, 3).tolist(),
        "exposure": np.round(exposure.loc[s0:].to_numpy(), 4).tolist(),
        "stats": pack(r, True), "bench_stats": pack(b, False),
        "trades": trades[::-1][:300],
        "fills": [f for f in fills if f["date"] >= s0s][-400:],
        "elapsed": round(time.time() - t0, 2),
    }
    try:
        from src import analytics
        a = analytics.analyze(r, b, [t["ret"] for t in trades], [t["days"] for t in trades], exposure.loc[s0:].to_numpy())
        a["assumptions"] = analytics.assumptions("cn", engine, (COMMISSION + TRANSFER_FEE) * 1e4, True)
        out["analysis"] = a
    except Exception as e:  # 分析失败不能影响回测本身
        out["analysis"] = None
    out.update(extra or {})
    return out


def returns_from_value(value: pd.Series, index: pd.DatetimeIndex, s0, s1=None) -> pd.Series:
    """优化器用：某一段时间的日收益。"""
    v = value.reindex(index).ffill().loc[s0:s1]
    r = v.pct_change().fillna(0.0)
    return r
