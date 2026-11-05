"""C10 Python 版 MA+ADX+ATR（src/strategy.py + src/backtest.py）与 amibroker/ma_cross_adx_atr.afl 的规则一致性核对。

背景：CLAUDE.md 要求 Python 版和 AFL 规则一致。这台机器上没有 AmiBroker，仓库里也没有 AmiBroker 的导出文件
（amibroker/ 下只有 .afl，没有 SPY_ab_trades.csv / SPY_ab_explore.csv），所以 compare_amibroker.py 没有历史对比结果可引用。
能做的是「静态 + 模拟」两层，全程只读，不改任何策略规则，不读 data/，不联网：
  1. 静态：用正则从 AFL 文本里读出 Param 默认值、SetOption、关键语句，和 Params / Config 的默认值逐项比。
  2. 模拟：把 AFL 里的 for 循环逐行翻译成 Python（afl_loop），在合成行情上和 src.backtest.run 产生的交易比
     入场日 / 入场价 / 出场日 / 出场价 / 出场原因（和 compare_amibroker.compare_trades 比的是同一批字段）。
     局限：翻译是我写的，不是 AmiBroker 真的跑出来的，只能证明「按我对 AFL 语义的理解，两边循环等价」。
  3. 边界：Wilder 平滑种子敏感度、ADX 在整段价格不动时的 NaN 传播、单价高到买不起 1 股时两边的状态差异。

用法：
  .venv/bin/python -B scripts/afl_python_consistency.py --out-dir /Users/lihongrui/Documents/AQ-o44/run/outputs/C10 [--seeds 30] [--n 2000]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src import backtest, indicators  # noqa: E402
from src.strategy import Params, generate_signals  # noqa: E402

AFL_PATH = ROOT / "amibroker" / "ma_cross_adx_atr.afl"


# ---------------------------------------------------------------- 1. 静态：从 AFL 文本读出配置
def parse_afl(text: str) -> dict:
    """从 AFL 源码读出 Param 默认值和 SetOption；读不到的键不会出现在结果里，由调用方判断。"""
    out: dict = {"params": {}, "options": {}}
    for m in re.finditer(r'(\w+)\s*=\s*Param\(\s*"([^"]+)"\s*,\s*([-\d.]+)\s*,', text):
        out["params"][m.group(1)] = float(m.group(3))
    for m in re.finditer(r'SetOption\(\s*"(\w+)"\s*,\s*([^)]+?)\s*\)', text):
        out["options"][m.group(1)] = m.group(2)
    out["trade_delays"] = re.search(r'SetTradeDelays\(([^)]*)\)', text).group(1).replace(" ", "")
    out["position_size"] = re.search(r'SetPositionSize\(([^)]*)\)', text).group(1).replace(" ", "")
    return out


def afl_statements(text: str) -> dict[str, bool]:
    """AFL 里决定「时点」和「是否用前一根」的关键语句是否存在（去掉空白后做子串匹配）。"""
    t = re.sub(r"\s+", "", re.sub(r"//[^\n]*", "", text))
    return {
        "MA 用收盘价简单均线 MA(C,n)": "MAf=MA(C,Fast)" in t and "MAs=MA(C,Slow)" in t,
        "ADX 用库函数 ADX(n)": "ADXv=ADX(ADXLen)" in t,
        "ATR 用库函数 ATR(n)": "ATRv=ATR(ATRLen)" in t,
        "买入信号 = Cross(快,慢) AND ADX>下限": "BuySig=Cross(MAf,MAs)ANDADXv>ADXMin" in t,
        "卖出信号 = Cross(慢,快)": "SellSig=Cross(MAs,MAf)" in t,
        "循环从第 1 根开始": "for(i=1;i<BarCount;i++)" in t,
        "卖出读前一根信号 SellSig[i-1]": "if(SellSig[i-1])" in t,
        "买入读前一根信号 BuySig[i-1]": "BuySig[i-1]" in t,
        "买入要求前一根 ATR 非 Null": "NOTIsNull(ATRv[i-1])" in t,
        "卖出价 = 当根开盘价": "Sell[i]=1;SellPrice[i]=O[i];" in t,
        "买入价 = 当根开盘价": "BuyPrice[i]=O[i]" in t,
        "止损价 = 入场开盘价 - 倍数*前一根ATR": "stopLvl=O[i]-ATRMult*ATRv[i-1]" in t,
        "止损只在入场之后的 K 线检查": "elseif(i>entryBar)" in t,
        "跳空低开 O<=止损 → 按开盘价出": "if(O[i]<=stopLvl)" in t,
        "盘中触及 L<=止损 → 按止损价出": "elseif(L[i]<=stopLvl)" in t and "SellPrice[i]=stopLvl" in t,
        "卖出信号优先于止损": 0 <= t.find("if(SellSig[i-1])") < t.find("elseif(i>entryBar)"),
    }


# ---------------------------------------------------------------- 2. 模拟：AFL 循环逐行翻译
def afl_signals(df: pd.DataFrame, p: Params) -> tuple[np.ndarray, np.ndarray]:
    """独立写一遍 MA / Cross（不调用 src.indicators.sma/cross），ADX/ATR 直接用 src.indicators 的（无法在没有 AmiBroker 的情况下独立验证）。"""
    c = df["Close"].to_numpy(float)
    n = len(c)

    def ma(k: int) -> np.ndarray:
        out = np.full(n, np.nan)
        cs = np.cumsum(np.insert(c, 0, 0.0))
        out[k - 1:] = (cs[k:] - cs[:-k]) / k
        return out

    def cross(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        # AFL Cross(a,b)：今天 a>b 且昨天 a<=b；任何一边是 NaN（AFL 的 Null）都不算
        r = np.zeros(n, bool)
        r[1:] = (a[1:] > b[1:]) & (a[:-1] <= b[:-1])
        return r

    mf, ms = ma(p.fast), ma(p.slow)
    adx_ = indicators.adx(df, p.adx_len).to_numpy(float)
    with np.errstate(invalid="ignore"):
        buy = cross(mf, ms) & (adx_ > p.adx_min)
    sell = cross(ms, mf)
    return buy, sell


def afl_loop(o, h, l, buy_sig, sell_sig, atr_, atr_mult: float) -> list[dict]:
    """AFL 第 47-75 行 for 循环的逐行翻译。返回的每笔交易含 entry_i / exit_i / 价格 / 原因；
    持仓到数据末尾的那笔不返回（AmiBroker 不会强制平仓，compare_amibroker 同样先把 Python 的 end_of_data 去掉）。"""
    n = len(o)
    trades: list[dict] = []
    in_pos, entry_bar, stop_lvl, entry_px = False, -1, 0.0, 0.0
    for i in range(1, n):
        if in_pos:
            if sell_sig[i - 1]:
                trades.append(dict(entry_i=entry_bar, entry_price=entry_px, exit_i=i, exit_price=o[i], reason="signal"))
                in_pos = False
            elif i > entry_bar:
                if o[i] <= stop_lvl:
                    trades.append(dict(entry_i=entry_bar, entry_price=entry_px, exit_i=i, exit_price=o[i], reason="stop_gap"))
                    in_pos = False
                elif l[i] <= stop_lvl:
                    trades.append(dict(entry_i=entry_bar, entry_price=entry_px, exit_i=i, exit_price=stop_lvl, reason="stop"))
                    in_pos = False
        elif buy_sig[i - 1] and not np.isnan(atr_[i - 1]):
            entry_px = o[i]
            in_pos, entry_bar = True, i
            stop_lvl = o[i] - atr_mult * atr_[i - 1]
    return trades


def gap_data(seed: int, n: int, round_px: bool = True) -> pd.DataFrame:
    """合成行情：随机游走 + 随机隔夜跳空（让 stop_gap 分支也被走到）。结束日不超过 2026-03-31。
    round_px=True 时价格四舍五入到分（和真实行情一样，SMA5 与 SMA20 会出现精确并列）；False 时几乎不会并列。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-03-31", periods=n, name="Date")
    ret = rng.normal(0.0002, 0.013, n)
    close = 100 * np.exp(np.cumsum(ret))
    gap = np.where(rng.random(n) < 0.04, rng.normal(0, 0.03, n), rng.normal(0, 0.003, n))
    open_ = np.r_[close[0], close[:-1]] * np.exp(gap)
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, 0.006, n)))
    df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": 1_000_000}, index=idx)
    return df.round(2) if round_px else df


def compare_one(df: pd.DataFrame, p: Params) -> dict:
    sig = generate_signals(df, p)
    res = backtest.run(sig, p)
    py = res.trades
    py = py[py.exit_reason != "end_of_data"].reset_index(drop=True) if len(py) else py
    buy, sell = afl_signals(df, p)
    o, h, l = (df[k].to_numpy(float) for k in ("Open", "High", "Low"))
    ab = afl_loop(o, h, l, buy, sell, sig["atr"].to_numpy(float), p.atr_mult)
    # 数据末尾仍持有的那笔：AFL 循环没有返回；Python 的 end_of_data 已去掉，两边口径一致
    sig_same = bool((sig["buy_sig"].to_numpy() == buy).all() and (sig["sell_sig"].to_numpy() == sell).all())
    # 并列诊断：两条均线差的绝对值 < 1e-9 视为精确并列；信号不一致的 K 线是否都紧邻（当根或前一根）一个并列
    tie = ((sig["ma_fast"] - sig["ma_slow"]).abs() < 1e-9).to_numpy()
    diff_bars = np.flatnonzero((sig["buy_sig"].to_numpy() != buy) | (sig["sell_sig"].to_numpy() != sell))
    diff_near_tie = all(tie[i] or tie[i - 1] for i in diff_bars)
    dates = df.index
    mism = 0
    n_cmp = min(len(py), len(ab))
    for k in range(n_cmp):
        a, b = ab[k], py.iloc[k]
        if not (dates[a["entry_i"]] == b.entry_date and dates[a["exit_i"]] == b.exit_date
                and abs(a["entry_price"] - b.entry_price) < 1e-9 and abs(a["exit_price"] - b.exit_price) < 1e-9
                and a["reason"] == b.exit_reason):
            mism += 1
    reasons = pd.Series([t["reason"] for t in ab]).value_counts().to_dict()
    return dict(py_trades=len(py), afl_trades=len(ab), count_equal=len(py) == len(ab), mismatched=mism,
                signals_identical=sig_same, reasons=reasons, tie_bars=int(tie.sum()),
                signal_diff_bars=int(len(diff_bars)), diff_all_near_tie=bool(diff_near_tie))


# ---------------------------------------------------------------- 3. 边界实验
def wilder_seed_sensitivity(df: pd.DataFrame, n: int = 14) -> dict:
    """Wilder 平滑的种子有两种常见写法：前 n 个的简单平均（Python 版）、或第 1 个值直接当初值。
    我没法确认 AmiBroker 用哪种，所以量化「换种子写法」最多改变 ATR 多少，用来判断这个未知数是否影响信号。"""
    tr = indicators.true_range(df).to_numpy(float)
    a_sma = indicators.wilder(pd.Series(tr), n).to_numpy()
    a_first = np.empty_like(tr)
    a_first[0] = tr[0]
    for t in range(1, len(tr)):
        a_first[t] = a_first[t - 1] + (tr[t] - a_first[t - 1]) / n
    rel = np.abs(a_sma - a_first) / a_sma
    res = {"theory_decay_after_100_bars": (1 - 1 / n) ** 100}
    for k in (30, 50, 100, 200):
        res[f"max_rel_diff_after_{k}_bars"] = float(np.nanmax(rel[k:]))
    return res


def adx_flat_stretch() -> dict:
    """假设：整段价格不动（停牌、一字板被前向填充）会让 TR 和 DM 的平滑值为 0，PDI/MDI = 0/0 = NaN，
    而 indicators.wilder 遇到 NaN 后会一直是 NaN → ADX 永久丢失。实测检验这个假设（结论看返回值）。
    分两种：(mid) 序列中段出现 30 根不动的 K 线；(start) 序列开头就有 30 根不动的 K 线。"""
    out: dict = {}
    for name, (lo, hi) in {"mid": (100, 130), "start": (0, 30)}.items():
        flat = gap_data(7, 300).copy()
        flat.iloc[lo:hi, :4] = flat.iloc[lo, 3]          # O=H=L=C 全部等于同一个价格
        adx_ = indicators.adx(flat, 14)
        after = adx_.iloc[hi:]
        out[name] = dict(flat_bars=hi - lo, adx_valid_total=int(adx_.notna().sum()), bars=int(len(adx_)),
                         first_valid_bar=int(np.flatnonzero(adx_.notna().to_numpy())[0]),
                         nan_after_flat=int(after.isna().sum()), permanent_nan=bool(after.isna().all()))
    base = indicators.adx(gap_data(7, 300), 14)
    out["no_flat_first_valid_bar"] = int(np.flatnonzero(base.notna().to_numpy())[0])
    return out


def unaffordable_state_mismatch() -> dict:
    """单价高于 10 万本金买不起 1 股：Python 的 backtest.run 因 qty==0 不开仓；
    AFL 脚本自己的 inPos 状态仍会置 True（由 AmiBroker 决定实际下不下单，这里只模拟脚本状态）。"""
    df = gap_data(3, 800)
    df = (df * 5000).round(2)          # 价格 ~ 50 万
    df["Volume"] = 1_000_000
    p = Params()
    sig = generate_signals(df, p)
    py = backtest.run(sig, p).trades
    ab = afl_loop(df["Open"].to_numpy(float), df["High"].to_numpy(float), df["Low"].to_numpy(float),
                  sig["buy_sig"].to_numpy(), sig["sell_sig"].to_numpy(), sig["atr"].to_numpy(float), p.atr_mult)
    return dict(min_open=float(df["Open"].min()), python_trades=int(len(py)), afl_script_trades=len(ab),
                buy_signals=int(sig["buy_sig"].sum()))


def real_data_check(end: str = "2026-03-31") -> dict:
    """在本地已有的 8 只 ETF 日线上（只读，截到 end 为止）做同样的「AFL 循环翻译 vs backtest.run」比较，
    并统计 SMA5 与 SMA20 精确并列的天数。直接读 csv，不经过 data.load（后者缺文件时会联网下载）。"""
    rows = {}
    for f in sorted((ROOT / "data" / "data").glob("*.csv")):
        if f.stem.endswith("_amibroker"):
            continue
        df = pd.read_csv(f, index_col="Date", parse_dates=True).loc[:end]
        r = compare_one(df, Params())
        rows[f.stem] = dict(bars=int(len(df)), first=str(df.index[0].date()), last=str(df.index[-1].date()),
                            **{k: r[k] for k in ("py_trades", "afl_trades", "mismatched", "tie_bars", "signal_diff_bars",
                                                 "reasons")})
    return rows


def export_rounding_effect(end: str = "2026-03-31") -> dict:
    """src/data.py::export_for_amibroker 导出给 AmiBroker 的 CSV 用 float_format="%.4f"（4 位小数），
    而 Python 版读的是全精度。量化这一步取整是否会改变信号 / 交易：同一份数据，全精度 vs 取 4 位小数，各跑一遍 Python 回测再比较。"""
    rows = {}
    for f in sorted((ROOT / "data" / "data").glob("*.csv")):
        if f.stem.endswith("_amibroker"):
            continue
        full = pd.read_csv(f, index_col="Date", parse_dates=True).loc[:end]
        r4 = full.round(4)
        p = Params()
        sf, sr = generate_signals(full, p), generate_signals(r4, p)
        tf, tr = (backtest.run(s, p).trades for s in (sf, sr))
        tf, tr = (t[t.exit_reason != "end_of_data"].reset_index(drop=True) for t in (tf, tr))
        diff_sig = int(((sf["buy_sig"] != sr["buy_sig"]) | (sf["sell_sig"] != sr["sell_sig"])).sum())
        same_len = len(tf) == len(tr)
        date_diff = int(((tf.entry_date != tr.entry_date) | (tf.exit_date != tr.exit_date)).sum()) if same_len else None
        px_diff = float(max((tf.entry_price - tr.entry_price).abs().max(), (tf.exit_price - tr.exit_price).abs().max())) if same_len else None
        rows[f.stem] = dict(trades_full=len(tf), trades_r4=len(tr), signal_diff_bars=diff_sig,
                            trades_with_different_dates=date_diff, max_price_diff=px_diff)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--seeds", type=int, default=30)
    ap.add_argument("--n", type=int, default=2000)
    a = ap.parse_args()
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    text = AFL_PATH.read_text(encoding="utf-8")
    cfg = backtest.Config()
    p0 = Params()
    parsed = parse_afl(text)
    static_rows = [
        ("Fast MA", parsed["params"].get("Fast"), p0.fast),
        ("Slow MA", parsed["params"].get("Slow"), p0.slow),
        ("ADX period", parsed["params"].get("ADXLen"), p0.adx_len),
        ("ADX min", parsed["params"].get("ADXMin"), p0.adx_min),
        ("ATR period", parsed["params"].get("ATRLen"), p0.atr_len),
        ("ATR stop mult", parsed["params"].get("ATRMult"), p0.atr_mult),
        ("InitialEquity", float(parsed["options"].get("InitialEquity")), cfg.initial_equity),
        ("CommissionAmount(%)/100", float(parsed["options"].get("CommissionAmount")) / 100, cfg.commission_pct),
    ]
    static = [dict(item=k, afl=v1, python=v2, equal=bool(np.isclose(v1, v2))) for k, v1, v2 in static_rows]
    stmts = afl_statements(text)

    # 参数网格：默认 + 几组非默认，确认「循环等价」不依赖默认参数
    grids = [Params(), Params(fast=3, slow=10, adx_min=15, atr_mult=1.5), Params(fast=10, slow=50, adx_min=25, atr_mult=3.0),
             Params(adx_len=10, atr_len=20, atr_mult=0.5)]
    sim = []
    for round_px, p in [(r, p) for r in (False, True) for p in grids]:
        rows = [compare_one(gap_data(s, a.n, round_px), p) for s in range(a.seeds)]
        reasons: dict = {}
        for r in rows:
            for k, v in r["reasons"].items():
                reasons[k] = reasons.get(k, 0) + v
        sim.append(dict(params=vars(p), round_px=round_px, seeds=a.seeds, bars=a.n,
                        tie_bars=sum(r["tie_bars"] for r in rows),
                        signal_diff_bars=sum(r["signal_diff_bars"] for r in rows),
                        diff_all_near_tie=all(r["diff_all_near_tie"] for r in rows),
                        total_py_trades=sum(r["py_trades"] for r in rows),
                        total_afl_trades=sum(r["afl_trades"] for r in rows),
                        seeds_count_equal=sum(r["count_equal"] for r in rows),
                        mismatched_trades=sum(r["mismatched"] for r in rows),
                        seeds_signals_identical=sum(r["signals_identical"] for r in rows),
                        exit_reasons=reasons))

    result = dict(
        afl_parsed=parsed, static_params=static, static_statements=stmts,
        stop_on_entry_bar_default=cfg.stop_on_entry_bar, simulation=sim,
        wilder_seed=wilder_seed_sensitivity(gap_data(1, 2000)),
        adx_flat=adx_flat_stretch(), unaffordable=unaffordable_state_mismatch(),
        real_data=real_data_check(), export_rounding=export_rounding_effect(),
    )
    (out_dir / "afl_python_consistency.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
