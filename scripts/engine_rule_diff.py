"""C09 三引擎（自研 / backtrader / RQAlpha）策略规则一致性：在合成行情上做动态对照，给静态核对表提供实测证据。

背景：CLAUDE.md 要求 src/strategies.py、engines/bt_strategies.py、engines/rq_strategies.py 三处规则一致。
  tests/test_engines.py 只用一只真实股票、只对 MA+ADX 原版比了成交，且需要 data/ 里的行情，所以覆盖不到其余 6 个策略。
做法（全程只读，不改任何策略规则；不读 data/、不联网）：
  1. 合成行情：分段随机漂移 + 随机波动 + 隔夜跳空，价格四舍五入到分（和真实行情一样会出现并列价），结束日 2026-03-31。
  2. 信号层：同一份行情喂给三个引擎，取「每天收盘后定下的目标仓位」逐日比较。
       自研  = registry.target_weights（和网页回测/每日任务同一入口）
       RQAlpha = rq_strategies 里的类，逐根 on_bar（和 rq_base.build_funcs 一样把仓位夹到 0~1）
       backtrader = 真的跑一遍 cerebro，子类只记录 AShareStrategy.target，不下单
     比较区间：第 260 根之后（和 common.WARMUP 一样，统计从这里开始）；另外单独统计前 260 根（预热期）的不一致。
  3. 指标层：SMA / ATR / ADX / RSI / 前 N 日最高最低价 / 已实现波动率，三个实现逐值比较，并看差异随时间衰减。
  4. 参数层：从 registry 的合法范围里随机抽参数，看三个引擎在非默认参数下还一致不一致。
  5. 边界情形：波动率为 0、均线刚好并列（touch）的上穿判定。
  6. 执行层：三个引擎端到端各跑一遍（RQAlpha 的交易日历换成临时文件），比成交次数和统计。

用法：
  PYTHONPATH=.engines_lib .venv/bin/python -B scripts/engine_rule_diff.py --out-dir DIR [--seeds 20] [--n 1500]
                                                                          [--param-draws 8] [--exec-seeds 2]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from engines import common, rq_base, rq_strategies  # noqa: E402
from src import indicators, registry  # noqa: E402
from src import strategies as S  # noqa: E402

CODE = "600000"                 # 主板，涨跌停 10%
END = "2026-03-31"              # 研究结束日上限
WARM = common.WARMUP            # 260
TOL = 1e-9
PAIRS = (("src", "rq"), ("src", "bt"), ("rq", "bt"))


# ---------------------------------------------------------------- 参数默认值核对（静态，自动读出）
SRC_FUNCS = {"ma_adx_original": "ma_adx_original", "ma_adx_plus": "ma_adx_plus", "trend_vol": "trend_vol_target",
             "trend_ens": "trend_ensemble", "donchian_s1": "donchian_s1", "rsi2": "rsi2_reversion"}


def defaults_table() -> dict:
    """把「注册表 / 自研函数签名 / backtrader params / RQAlpha __init__ 签名」里每个参数的默认值读出来并排放。
    返回 {策略: {参数: {来源: 值}}}；mismatch = 至少有两个来源给出的值不相等。
    说明：自研 donchian_s1 的 atr_len 在函数体里写死为 20（调用 donchian_breakout 时传入），vol_len 用 donchian_breakout 的默认值；
    这两项是读源码后手工补进来的，在表里标成 'src*'。blend2 的三个子策略参数都写在代码里，无法自动读出，靠信号层的行为对照覆盖。"""
    import inspect
    from engines import bt_strategies

    out: dict = {}
    for name in common.STRATS:
        rows: dict = {}
        for p in registry.STRATEGIES[name]["params"]:
            rows.setdefault(p["key"], {})["registry"] = p["default"]
        if name in SRC_FUNCS:
            for k, prm in inspect.signature(getattr(S, SRC_FUNCS[name])).parameters.items():
                if k not in ("df", "prices") and prm.default is not inspect.Parameter.empty:
                    rows.setdefault(k, {})["src"] = prm.default
            if name == "donchian_s1":
                rows.setdefault("atr_len", {})["src*"] = 20
                rows.setdefault("vol_len", {})["src*"] = inspect.signature(S.donchian_breakout).parameters["vol_len"].default
        for k, v in dict(bt_strategies.STRATEGY_CLASSES[name].params._getitems()).items():
            rows.setdefault(k, {})["bt"] = v
        for k, prm in inspect.signature(rq_strategies.STRATEGY_CLASSES[name].__init__).parameters.items():
            if k != "self" and prm.default is not inspect.Parameter.empty:
                rows.setdefault(k, {})["rq"] = prm.default
        if name == "trend_ens":
            rows["lens"] = {"src": inspect.signature(S.trend_ensemble).parameters["lens"].default,
                            "bt": bt_strategies.TrendEnsemble.LENS, "rq": rq_strategies.TrendEnsemble.LENS}
        if name == "blend2":
            for k in ("w_trend", "w_rsi", "w_donchian"):
                rows.setdefault(k, {})
        for k, r in rows.items():
            vals = [v for v in r.values() if v is not None]
            r["mismatch"] = bool(len(vals) > 1 and any(
                (abs(float(a) - float(b)) > 1e-12 if isinstance(a, (int, float)) and isinstance(b, (int, float)) else a != b)
                for a in vals for b in vals))
        out[name] = rows
    return out


# ---------------------------------------------------------------- 合成行情
def synth(seed: int, n: int = 1500, lead: int = 0) -> pd.DataFrame:
    """分段漂移 + 随机波动的日线（OHLC 四舍五入到分）。只用来比对引擎，不代表任何真实品种。
    lead>0：前 lead 根强制为低波动的强上升趋势（压力测试：让预热期末尾趋势类策略已经在持仓）。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=END, periods=n)
    mu, sg = np.zeros(n), np.zeros(n)
    t = 0
    while t < n:
        L = int(rng.integers(60, 200))
        mu[t:t + L] = rng.choice([-0.0010, 0.0, 0.0008, 0.0015])
        sg[t:t + L] = rng.uniform(0.008, 0.025)
        t += L
    if lead:
        mu[:lead], sg[:lead] = 0.0025, 0.010
    gap = rng.normal(0, 0.004, n)
    intra = np.clip(rng.normal(mu, sg), -0.08, 0.08)
    o, c = np.empty(n), np.empty(n)
    prev = 20.0
    for i in range(n):
        o[i] = prev * (1 + gap[i])
        c[i] = o[i] * (1 + intra[i])
        prev = c[i]
    hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.4, n)) * sg)
    lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.4, n)) * sg)
    df = pd.DataFrame({"Open": o, "High": hi, "Low": lo, "Close": c, "Volume": 1e6}, index=idx).round(2)
    assert (df[["Open", "High", "Low", "Close"]] > 0.5).all().all(), f"seed {seed} 价格跌到 0.5 以下，换个种子"
    assert df.index[-1] <= pd.Timestamp(END)
    return df


# ---------------------------------------------------------------- 三个引擎的目标仓位
def tgt_src(df: pd.DataFrame, name: str, params: dict) -> np.ndarray:
    return registry.target_weights(name, {CODE: df}, CODE, params, common.CN).iloc[:, 0].to_numpy(float)


def tgt_rq(df: pd.DataFrame, name: str, params: dict) -> np.ndarray:
    s = rq_strategies.STRATEGY_CLASSES[name](**params)
    out = []
    for o, h, l, c in df[["Open", "High", "Low", "Close"]].itertuples(index=False):
        w = s.on_bar(o, h, l, c)
        out.append(min(max(float(w), 0.0), 1.0) if rq_base.ok(w) else 0.0)     # 和 rq_base.build_funcs 一致
    return np.array(out)


def tgt_bt(df: pd.DataFrame, name: str, params: dict) -> np.ndarray:
    import backtrader as bt
    from engines import bt_strategies
    cls = bt_strategies.STRATEGY_CLASSES[name]
    log: list[float] = []

    class Rec(cls):
        def prenext(self):          # 预热期：bt 不调用 decide，目标仓位保持 0
            super().prenext()
            log.append(self.target)

        def next(self):
            super().next()
            log.append(self.target)

        def next_open(self):        # 只记信号，不下单
            pass

    cerebro = bt.Cerebro(stdstats=False, cheat_on_open=True)
    cerebro.adddata(bt.feeds.PandasData(dataname=df, open="Open", high="High", low="Low", close="Close",
                                        volume="Volume", openinterest=None), name=CODE)
    kw = {k: v for k, v in params.items() if k in cls.params._getkeys()}
    cerebro.addstrategy(Rec, **kw)
    cerebro.run()
    assert len(log) == len(df), (len(log), len(df))
    return np.array(log)


def compare(a: np.ndarray, b: np.ndarray) -> dict:
    d = np.abs(a - b)
    w, p = slice(WARM, None), slice(0, WARM)
    st = (a > 0) != (b > 0)
    big = np.flatnonzero(d[w] > TOL)
    return {"max_abs": float(d[w].max()), "frac_diff": float((d[w] > TOL).mean()),
            "frac_state": float(st[w].mean()), "state_days": int(st[w].sum()),
            "warm_state_days": int(st[p].sum()), "warm_diff_days": int((d[p] > TOL).sum()),
            "first_diff": int(big[0] + WARM) if len(big) else None, "n_win": int(len(d[w]))}


def agg(rows: list[dict]) -> dict:
    return {"seeds": len(rows),
            "seeds_exact": sum(r["max_abs"] <= TOL for r in rows),
            "seeds_state_diff": sum(r["state_days"] > 0 for r in rows),
            "seeds_warm_diff": sum(r["warm_diff_days"] > 0 for r in rows),
            "mean_frac_diff_pct": float(np.mean([r["frac_diff"] for r in rows]) * 100),
            "mean_frac_state_pct": float(np.mean([r["frac_state"] for r in rows]) * 100),
            "max_frac_diff_pct": float(np.max([r["frac_diff"] for r in rows]) * 100),
            "max_abs": float(np.max([r["max_abs"] for r in rows])),
            "mean_warm_diff_days": float(np.mean([r["warm_diff_days"] for r in rows]))}


def signal_layer(seeds: int, n: int) -> dict:
    res = {name: {f"{x}-{y}": [] for x, y in PAIRS} for name in common.STRATS}
    for seed in range(seeds):
        df = synth(seed, n)
        for name in common.STRATS:
            p = common.parse_params(name, {})
            T = {"src": tgt_src(df, name, p), "rq": tgt_rq(df, name, p), "bt": tgt_bt(df, name, p)}
            for x, y in PAIRS:
                res[name][f"{x}-{y}"].append(compare(T[x], T[y]))
    return {name: {k: agg(v) for k, v in d.items()} for name, d in res.items()}


# ---------------------------------------------------------------- 参数层：随机合法参数
def draw_params(name: str, rng: np.random.Generator) -> dict:
    raw = {}
    for p in registry.STRATEGIES[name]["params"]:
        raw[p["key"]] = float(rng.uniform(p["min"], p["max"]))
    return common.parse_params(name, raw)


def param_layer(seeds: int, draws: int, n: int) -> dict:
    rng = np.random.default_rng(12345)
    out = {name: {f"{x}-{y}": [] for x, y in PAIRS} for name in common.STRATS}
    worst = {name: [] for name in common.STRATS}
    for seed in range(100, 100 + seeds):
        df = synth(seed, n)
        for name in common.STRATS:
            for _ in range(draws):
                p = draw_params(name, rng)
                T = {"src": tgt_src(df, name, p), "rq": tgt_rq(df, name, p), "bt": tgt_bt(df, name, p)}
                for x, y in PAIRS:
                    r = compare(T[x], T[y])
                    out[name][f"{x}-{y}"].append(r)
                    if r["state_days"] > 0 and (x, y) != ("rq", "bt"):
                        worst[name].append({"pair": f"{x}-{y}", "seed": seed, "params": p, "state_days": r["state_days"]})
    return {name: {"agg": {k: agg(v) for k, v in d.items()}, "state_diff_cases": worst[name][:6]} for name, d in out.items()}


# ---------------------------------------------------------------- 指标层
def bt_indicators(df: pd.DataFrame) -> dict[str, np.ndarray]:
    import backtrader as bt
    from engines.bt_base import VolScale
    rows: list[dict] = []

    class Probe(bt.Strategy):
        def __init__(self):
            c = self.data.close
            self.vs = VolScale(self.data, 0.15, 20)
            self.i = {"sma200": bt.ind.SMA(c, period=200), "sma5": bt.ind.SMA(c, period=5), "sma20": bt.ind.SMA(c, period=20),
                      "atr14": bt.ind.ATR(self.data, period=14),
                      "atr20": bt.ind.ATR(self.data, period=20), "adx14": bt.ind.ADX(self.data, period=14),
                      "rsi2": bt.ind.RSI(c, period=2, safediv=True),
                      "hi20": bt.ind.Highest(self.data.high(-1), period=20),
                      "lo10": bt.ind.Lowest(self.data.low(-1), period=10), "vol20": self.vs.vol}

        def _rec(self):
            row = {}
            for k, v in self.i.items():
                try:
                    row[k] = float(v[0])
                except Exception:
                    row[k] = float("nan")
            rows.append(row)

        def prenext(self):
            self._rec()

        def next(self):
            self._rec()

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(bt.feeds.PandasData(dataname=df, open="Open", high="High", low="Low", close="Close",
                                        volume="Volume", openinterest=None))
    cerebro.addstrategy(Probe)
    cerebro.run()
    t = pd.DataFrame(rows)
    assert len(t) == len(df), (len(t), len(df))
    return {k: t[k].to_numpy(float) for k in t.columns}


def rq_indicators(df: pd.DataFrame) -> dict[str, np.ndarray]:
    sma, a14, a20, adx = rq_base.SMA(200), rq_base.ATR(14), rq_base.ATR(20), rq_base.ADX(14)
    s5, s20 = rq_base.SMA(5), rq_base.SMA(20)
    rsi, hi, lo, vol = rq_base.RSI(2), rq_base.PriorExtreme(20, "max"), rq_base.PriorExtreme(10, "min"), rq_base.RealizedVol(20)
    out = {k: [] for k in ("sma200", "sma5", "sma20", "atr14", "atr20", "adx14", "rsi2", "hi20", "lo10", "vol20")}
    for o, h, l, c in df[["Open", "High", "Low", "Close"]].itertuples(index=False):
        out["sma200"].append(sma.update(c)); out["atr14"].append(a14.update(h, l, c)); out["atr20"].append(a20.update(h, l, c))
        out["sma5"].append(s5.update(c)); out["sma20"].append(s20.update(c))
        out["adx14"].append(adx.update(h, l, c)); out["rsi2"].append(rsi.update(c)); out["hi20"].append(hi.update(h))
        out["lo10"].append(lo.update(l)); out["vol20"].append(vol.update(c))
    return {k: np.array(v, float) for k, v in out.items()}


def src_indicators(df: pd.DataFrame) -> dict[str, np.ndarray]:
    return {"sma200": indicators.sma(df["Close"], 200).to_numpy(float),
            "sma5": indicators.sma(df["Close"], 5).to_numpy(float), "sma20": indicators.sma(df["Close"], 20).to_numpy(float),
            "atr14": indicators.atr(df, 14).to_numpy(float), "atr20": indicators.atr(df, 20).to_numpy(float),
            "adx14": indicators.adx(df, 14).to_numpy(float), "rsi2": indicators.rsi(df["Close"], 2).to_numpy(float),
            "hi20": df["High"].rolling(20).max().shift(1).to_numpy(float),
            "lo10": df["Low"].rolling(10).min().shift(1).to_numpy(float),
            "vol20": indicators.realized_vol(df["Close"], 20).to_numpy(float)}


def indicator_layer(seeds: int, n: int) -> dict:
    """比较两两指标：rel = |a-b| / |a|；first = 两边都有值之后的前 60 根，late = 第 260 根之后。
    first_valid_gap：两个实现第一个有值的下标之差（0 = 同一根 K 线开始有值）。"""
    names = ("sma200", "sma5", "sma20", "atr14", "atr20", "adx14", "rsi2", "hi20", "lo10", "vol20")
    acc = {(x, y, k): {"nan_mismatch": 0, "first": [], "late": [], "gap": []} for x, y in (("src", "rq"), ("src", "bt")) for k in names}
    for seed in range(seeds):
        df = synth(seed, n)
        I = {"src": src_indicators(df), "rq": rq_indicators(df), "bt": bt_indicators(df)}
        for x, y in (("src", "rq"), ("src", "bt")):
            for k in names:
                a, b = I[x][k], I[y][k]
                fa, fb = int(np.argmax(~np.isnan(a))), int(np.argmax(~np.isnan(b)))
                both = ~np.isnan(a) & ~np.isnan(b)
                rel = np.where(both, np.abs(a - b) / np.maximum(np.abs(a), 1e-12), np.nan)
                s = max(fa, fb)
                A = acc[(x, y, k)]
                A["gap"].append(fb - fa)
                A["first"].append(float(np.nanmax(rel[s:s + 60])))
                A["late"].append(float(np.nanmax(rel[WARM:])))
                A["nan_mismatch"] += int((np.isnan(a[WARM:]) != np.isnan(b[WARM:])).sum())
    return {f"{x}-{y}:{k}": {"first_valid_gap_bars": sorted(set(v["gap"])), "max_rel_first60": max(v["first"]),
                              "max_rel_after260": max(v["late"]), "nan_mismatch_after260": v["nan_mismatch"]}
            for (x, y, k), v in acc.items()}


# ---------------------------------------------------------------- 分歧归因：并列价（均线刚好等于收盘价）
def tie_layer(seeds: int, n: int) -> list[dict]:
    """默认参数下 rsi2 / ma_adx_original 在个别种子上有 1~2 天开关分歧。找到第一个分歧日，把三个引擎在这天的
    关键量打印出来：如果「收盘价 - 5 日均线」或「5 日均线 - 20 日均线」在某个引擎里恰好是 0、在另一个里是 ±1e-15，
    就说明是浮点并列价造成的（严格大于/小于的判断在并列处被舍入误差翻转），不是规则写得不一样。"""
    rows = []
    for seed in range(seeds):
        df = synth(seed, n)
        I = {"src": src_indicators(df), "rq": rq_indicators(df), "bt": bt_indicators(df)}
        c = df["Close"].to_numpy(float)
        for name in ("rsi2", "ma_adx_original"):
            p = common.parse_params(name, {})
            T = {"src": tgt_src(df, name, p), "rq": tgt_rq(df, name, p), "bt": tgt_bt(df, name, p)}
            for x, y in PAIRS:
                d = np.flatnonzero(np.abs(T[x] - T[y])[WARM:] > TOL)
                if not len(d):
                    continue
                i = int(d[0] + WARM)
                if name == "rsi2":
                    q = {e: {"close_minus_sma5": float(c[i] - I[e]["sma5"][i]), "rsi2": float(I[e]["rsi2"][i])} for e in T}
                else:
                    q = {e: {"sma5_minus_sma20_prev": float(I[e]["sma5"][i - 1] - I[e]["sma20"][i - 1]),
                             "sma5_minus_sma20": float(I[e]["sma5"][i] - I[e]["sma20"][i])} for e in T}
                rows.append({"strategy": name, "seed": seed, "pair": f"{x}-{y}", "first_diff_bar": i,
                             "targets": {e: float(T[e][i]) for e in T}, "quantities": q})
    return rows


# ---------------------------------------------------------------- 预热期压力测试
def warmup_stress(seeds: int, n: int) -> dict:
    """bt 的 next() 要等所有指标都有值才开始调用（全局最长的 minperiod），所以 trend_ens / blend2 里各个子状态机
    在 bt 里都是第 250 根才从零开始，而自研 / RQAlpha 里每个子状态机各自更早起步（hysteresis / 海龟持仓有「记忆」）。
    默认的随机行情里这个差异没触发，这里造出两种更容易触发的行情：
      A. 前 400 根是低波动强上升趋势（预热末尾趋势类策略必然已持仓）
      B. 迟滞区间取注册表上限 0.05（trend_ens 的 buffer，区间越宽「记忆」越长）
    比较区间仍是第 260 根之后。"""
    out = {}
    cases = [("blend2", {}, 400), ("trend_ens", {}, 400), ("trend_ens", {"buffer": 0.05}, 0), ("trend_ens", {"buffer": 0.05}, 400)]
    for name, raw, lead in cases:
        rows, donch_on = [], 0
        for seed in range(300, 300 + seeds):
            df = synth(seed, n, lead=lead)
            p = common.parse_params(name, raw)
            T = {"src": tgt_src(df, name, p), "rq": tgt_rq(df, name, p), "bt": tgt_bt(df, name, p)}
            if name == "blend2":
                donch_on += int(S.donchian_s1(df).iloc[WARM - 1] > 0)
            rows.append({f"{x}-{y}": compare(T[x], T[y]) for x, y in PAIRS})
        key = f"{name}|{raw or 'default'}|lead={lead}"
        out[key] = {f"{x}-{y}": agg([r[f"{x}-{y}"] for r in rows]) for x, y in PAIRS}
        if name == "blend2":
            out[key]["seeds_with_src_donchian_on_at_bar259"] = donch_on
    return out


# ---------------------------------------------------------------- 边界情形
def edge_cases() -> dict:
    import backtrader as bt
    out = {}
    # 1. 波动率为 0（价格不动 25 根）：自研 vol_scale = target/0 = inf 被夹到 1.0；bt / rq 写的是 <=0 → 0.0
    rng = np.random.default_rng(0)
    c = np.r_[40 + np.cumsum(rng.normal(0, 0.3, 60)), np.full(25, 0.0)]
    c[60:] = c[59]
    s = pd.Series(c)
    v_src = float(S.vol_scale(s, 0.15, 20).iloc[-1])
    rv = rq_base.RealizedVol(20)
    for x in c:
        rv.update(x)
    v_rq = rq_base.vol_scale(0.15, rv.value)
    from engines.bt_base import VolScale
    vs_log: list[float] = []

    class VS(bt.Strategy):
        def __init__(self):
            self.vs = VolScale(self.data, 0.15, 20)

        def next(self):
            vs_log.append(self.vs.get())

    zi = pd.bdate_range(end=END, periods=len(c))
    zc = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1.0}, index=zi)
    cb = bt.Cerebro(stdstats=False)
    cb.adddata(bt.feeds.PandasData(dataname=zc, openinterest=None))
    cb.addstrategy(VS)
    cb.run()
    out["zero_vol"] = {"src": v_src, "rq": v_rq, "bt": float(vs_log[-1]), "n_flat_bars": 25}
    # 2. 均线「碰到」后又拉开：a 从下方上穿 → 刚好相等 → 再次高于。自研/rq 把第 4 天算上穿，bt CrossOver 不算
    a = [1, 3, 2, 3, 3.5]
    b = [2.0] * 5
    f_src = indicators.cross(pd.Series(a, dtype=float), pd.Series(b, dtype=float)).tolist()
    cr, f_rq = rq_base.Cross(), []
    for x, y in zip(a, b):
        f_rq.append(cr.update(x, y))
    res: list[float] = []

    class Cx(bt.Strategy):
        def __init__(self):
            self.x = bt.ind.CrossOver(self.data0.close, self.data1.close)

        def next(self):
            res.append(float(self.x[0]))

        def prenext(self):
            res.append(float("nan"))

    idx = pd.bdate_range(end=END, periods=len(a))
    cerebro = bt.Cerebro(stdstats=False)
    for ser in (a, b):
        d = pd.DataFrame({"Open": ser, "High": ser, "Low": ser, "Close": ser, "Volume": 1.0}, index=idx)
        cerebro.adddata(bt.feeds.PandasData(dataname=d, openinterest=None))
    cerebro.addstrategy(Cx)
    cerebro.run()
    out["touch_cross"] = {"a": a, "b": b, "src": f_src, "rq": f_rq, "bt_crossover(+1上穿)": res}
    # 3. 合成行情里，MA5 与 MA20 恰好相等的天数（并列价会不会真的出现）
    ties = 0
    for seed in range(20):
        df = synth(seed)
        mf, ms = indicators.sma(df["Close"], 5), indicators.sma(df["Close"], 20)
        ties += int((mf == ms).sum())
    out["ma5_eq_ma20_days_in_20_synth_series"] = ties
    return out


# ---------------------------------------------------------------- 执行层
def trace_open_state(df: pd.DataFrame, name: str, info: dict) -> dict:
    """在 bt 与 RQAlpha 每天开盘下单之前，记录「目标仓位 / 上次执行的目标 / 持股 / 现金」，找出两边最早分叉的那一天。
    做法：临时替换 bt_strategies.STRATEGY_CLASSES[name] 与 rq_base.build_funcs（只在本进程内、用完立即还原），不改任何文件。"""
    from engines import bt_engine, bt_strategies, rq_engine
    bt_log: list[tuple] = []
    rq_log: list[tuple] = []
    orig_cls, orig_build = bt_strategies.STRATEGY_CLASSES[name], rq_base.build_funcs

    class Logged(orig_cls):
        def next_open(self):
            bt_log.append((str(self.data.datetime.date(0)), float(self.target), float(self.applied),
                           int(self.getposition(self.data).size), float(self.broker.getcash())))
            super().next_open()

    def patched(strategy, oid, band=0.10):
        f = orig_build(strategy, oid, band)
        oa = f["open_auction"]

        def open_auction(context, bar_dict):
            from rqalpha.apis import get_position
            rq_log.append((str(context.now.date()), float(context.target), float(context.applied),
                           int(get_position(context.oid).quantity), float(context.stock_account.cash)))
            return oa(context, bar_dict)
        f["open_auction"] = open_auction
        return f

    bt_strategies.STRATEGY_CLASSES[name], rq_base.build_funcs = Logged, patched
    try:
        bt_engine.run(CODE, name, {}, None, df=df, info=info)
        rq_engine.run(CODE, name, {}, None, df=df, info=info)
    finally:
        bt_strategies.STRATEGY_CLASSES[name], rq_base.build_funcs = orig_cls, orig_build
    bd, rd = {r[0]: r for r in bt_log}, {r[0]: r for r in rq_log}
    for d in sorted(set(bd) & set(rd)):
        b, r = bd[d], rd[d]
        if b[1:4] != r[1:4] or abs(b[4] - r[4]) > 1e-6:
            prev = sorted(x for x in bd if x < d)[-1] if any(x < d for x in bd) else None
            return {"first_state_diff_date": d, "bt(target,applied,pos,cash)": list(b[1:]), "rq(target,applied,pos,cash)": list(r[1:]),
                    "prev_day": prev, "bt_prev": list(bd[prev][1:]) if prev else None, "rq_prev": list(rd[prev][1:]) if prev and prev in rd else None}
    return {"first_state_diff_date": None}


def exec_layer(seeds: int, n: int, tmp: Path) -> dict:
    from engines import bt_engine, inhouse, rq_engine
    from src import allmarket
    info = {"name": "合成股票"}
    out = {}
    for seed in range(200, 200 + seeds):
        df = synth(seed, n)
        cal = tmp / f"cal_{seed}.csv"
        pd.DataFrame({"trade_date": df.index.strftime("%Y-%m-%d")}).to_csv(cal, index=False)
        allmarket.CAL_FILE = cal                       # 临时日历，避免读 data/
        for name in common.STRATS:
            R = {}
            for eng, mod in (("inhouse", inhouse), ("bt", bt_engine), ("rq", rq_engine)):
                R[eng] = mod.run(CODE, name, {}, None, df=df, info=info)
            row = {}
            for eng, r in R.items():
                s = r["stats"]
                row[eng] = {"TotalRet%": s.get("TotalRet%"), "CAGR%": s.get("CAGR%"), "Trades": s.get("Trades"),
                            "Exposure%": s.get("Exposure%"), "Fees": s.get("Fees"), "fills": len(r["fills"])}
            fb, fr = R["bt"]["fills"], R["rq"]["fills"]
            row["bt_vs_rq_same_fill_dates"] = [x["date"] for x in fb] == [y["date"] for y in fr]
            row["inhouse_vs_bt_same_fill_dates"] = [x["date"] for x in R["inhouse"]["fills"]] == [y["date"] for y in fb]
            if not row["bt_vs_rq_same_fill_dates"]:      # 记下第一处不同，方便溯源
                bd, rd = {x["date"]: x["qty"] for x in fb}, {y["date"]: y["qty"] for y in fr}
                first = next(({"date": d, "bt_qty": bd.get(d), "rq_qty": rd.get(d)}
                              for d in sorted(set(bd) | set(rd)) if bd.get(d) != rd.get(d)), None)
                row["bt_vs_rq_first_diff"] = first
                if first:      # 是信号不同，还是执行不同？看成交前一天收盘定下的目标仓位
                    row["open_state_trace"] = trace_open_state(df, name, info)
                    p = common.parse_params(name, {})
                    tb, tr_, ts = tgt_bt(df, name, p), tgt_rq(df, name, p), tgt_src(df, name, p)
                    k = int(np.flatnonzero(df.index.strftime("%Y-%m-%d") == first["date"])[0])
                    row["signal_around_first_diff"] = {
                        "dates": [df.index[j].strftime("%Y-%m-%d") for j in range(k - 2, k + 1)],
                        "src": [float(ts[j]) for j in range(k - 2, k + 1)], "rq": [float(tr_[j]) for j in range(k - 2, k + 1)],
                        "bt": [float(tb[j]) for j in range(k - 2, k + 1)],
                        "bt_fills_near": [f for f in fb if abs((pd.Timestamp(f["date"]) - pd.Timestamp(first["date"])).days) <= 40][-8:],
                        "rq_fills_near": [f for f in fr if abs((pd.Timestamp(f["date"]) - pd.Timestamp(first["date"])).days) <= 40][-8:],
                        "bt_events": R["bt"]["events"][-5:], "rq_events": R["rq"]["events"][-5:],
                        "max_abs_src_rq_whole": float(np.abs(ts - tr_)[WARM:].max()),
                        "max_abs_src_bt_whole": float(np.abs(ts - tb)[WARM:].max())}
            out[f"{name}@seed{seed}"] = row
    return out


# ---------------------------------------------------------------- 主程序
def fmt_agg(title: str, table: dict) -> str:
    lines = [f"### {title}", "", "| 策略 | 引擎对 | 种子数 | 完全一致 | 有开/关分歧 | 区间内不同天数占比均值 | 最大占比 | 最大仓位差 | 预热期有差异的种子 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, d in table.items():
        for pair, a in d.items():
            lines.append(f"| {name} | {pair} | {a['seeds']} | {a['seeds_exact']} | {a['seeds_state_diff']} | "
                         f"{a['mean_frac_diff_pct']:.3f}% | {a['max_frac_diff_pct']:.3f}% | {a['max_abs']:.4g} | {a['seeds_warm_diff']} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--n", type=int, default=1500)
    ap.add_argument("--param-draws", type=int, default=8)
    ap.add_argument("--exec-seeds", type=int, default=2)
    a = ap.parse_args()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    R = {"config": {"seeds": a.seeds, "n_bars": a.n, "param_draws": a.param_draws, "exec_seeds": a.exec_seeds,
                    "warmup": WARM, "tol": TOL, "end": END}}

    print("[0/5] 参数默认值核对…", flush=True)
    R["defaults"] = defaults_table()
    n_param = sum(len(d) for d in R["defaults"].values())
    bad = [(s, k, {x: y for x, y in r.items() if x != "mismatch"}) for s, d in R["defaults"].items() for k, r in d.items() if r["mismatch"]]
    print(f"共 {n_param} 个（策略, 参数）项，来源之间默认值不一致的有 {len(bad)} 项", flush=True)
    for s, k, r in bad:
        print("  不一致：", s, k, r, flush=True)
    for s, d in R["defaults"].items():
        for k, r in d.items():
            print(f"  {s:16s} {k:12s} " + "  ".join(f"{x}={y}" for x, y in r.items() if x != "mismatch"), flush=True)

    print("[1/5] 信号层（默认参数）…", flush=True)
    R["signal_default"] = signal_layer(a.seeds, a.n)
    print(fmt_agg("信号层：默认参数，三引擎目标仓位逐日对比（第 260 根之后）", R["signal_default"]), flush=True)

    print("[2/5] 指标层…", flush=True)
    R["indicators"] = indicator_layer(min(a.seeds, 8), a.n)
    for k, v in R["indicators"].items():
        print(f"{k:18s} 首个有值下标差={v['first_valid_gap_bars']} 前60根最大相对差={v['max_rel_first60']:.3g} "
              f"260根后最大相对差={v['max_rel_after260']:.3g} 260根后NaN不一致={v['nan_mismatch_after260']}", flush=True)

    print("[3/5] 参数层…", flush=True)
    R["signal_params"] = param_layer(3, a.param_draws, a.n)
    print(fmt_agg("信号层：随机合法参数", {k: v["agg"] for k, v in R["signal_params"].items()}), flush=True)
    for k, v in R["signal_params"].items():
        for c in v["state_diff_cases"]:
            print("  分歧样例", k, c, flush=True)

    print("[4/5] 边界情形…", flush=True)
    R["edge"] = edge_cases()
    print(json.dumps(R["edge"], ensure_ascii=False, indent=1), flush=True)

    print("[4b] 并列价归因 + 预热期压力测试…", flush=True)
    R["tie_diagnosis"] = tie_layer(a.seeds, a.n)
    for r in R["tie_diagnosis"]:
        print(json.dumps(r, ensure_ascii=False), flush=True)
    R["warmup_stress"] = warmup_stress(a.seeds, a.n)
    for k, v in R["warmup_stress"].items():
        print(k, json.dumps(v, ensure_ascii=False), flush=True)

    print("[5/5] 执行层（端到端）…", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        R["exec"] = exec_layer(a.exec_seeds, a.n, Path(tmp))
    for k, v in R["exec"].items():
        print(k, json.dumps(v, ensure_ascii=False), flush=True)

    (out / "engine_rule_diff.json").write_text(json.dumps(R, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"完成，用时 {time.time() - t0:.0f}s，结果写入 {out / 'engine_rule_diff.json'}")


if __name__ == "__main__":
    main()
