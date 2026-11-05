"""C04 七个正式策略的「通缩夏普」（Deflated Sharpe Ratio，DSR）表。

问题：同一份数据上试过很多策略/参数之后，挑出来的最好那个的 Sharpe 天然偏高（选择偏差）。
  DSR（Bailey & López de Prado 2014, "The Deflated Sharpe Ratio"）回答：考虑到一共试了 m 次、
  收益分布还有偏度和肥尾，这个 Sharpe 真的大于 0 的概率有多大。
做法：
  1. 回测完全复用项目已有函数：registry.clean_params（默认参数）→ registry.target_weights（策略仓位）
     → portfolio.run_weights（次日开盘成交、扣费、10% 调仓阈值、空仓现金计息、A 股一字板），口径和 registry.run 一样。
     行情在算信号之前就截到 CUTOFF（2026-03-31），之后的 K 线一根都不进入计算。
  2. 去掉前 260 根预热期（与 registry.run 相同），日收益减去现金 ETF（美股 SHY、A 股 511880）的日收益 = 超额收益，
     分样本内 2013-01-01~2019-12-31、样本外 2020-01-01~2026-03-31 两段，各自估计日 Sharpe、偏度、峰度。
  3. DSR = PSR(SR0)，SR0 = m 次纯噪声试验里「最好的那个」Sharpe 的期望：
       SR0 = sqrt(V) × [(1-γ)·Φ⁻¹(1-1/m) + γ·Φ⁻¹(1-1/(m·e))]，γ = 0.5772（欧拉常数）
       PSR(SR0) = Φ( (SR - SR0)·sqrt(T-1) / sqrt(1 - 偏度·SR + (峰度-1)/4·SR²) )
     其中 SR 是日频（不年化）Sharpe，峰度是非超额峰度（正态 = 3），V 是各次试验 Sharpe 估计值的方差。
     V 有两种取法，两种都算，两种都 ≥ 0.95 才算「通过」（取最难通过的）：
       null  ：纯噪声下单个 Sharpe 估计的抽样方差 1/(T-1)（真实 Sharpe 全为 0 时的理论值）
       cross ：同一品种、同一段里 7 个正式策略日 Sharpe 的样本方差（论文里的做法，但只有 7 个点，很粗糙）
  4. m 主值 = 7 个正式 + 18 个影子候选 + T008 的 490 次 = 515；另给 m 取一串值的敏感性，
     以及「m 最大到多少还能通过」（盈亏平衡试验次数 m*）。
  5. --calibrate：纯噪声模拟（不读任何行情），看 m 个零 Sharpe 策略里最好那个被 DSR 判「通过」的比例。

只读 data/ 下的行情 CSV，不写 data/；不联网。

用法：
  .venv/bin/python -B scripts/deflated_sharpe_table.py --out-dir DIR [--data-dir data] [--calibrate]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import MARKETS  # noqa: E402
from src import registry  # noqa: E402
from src.datasource import limit_pct  # noqa: E402
from src.metrics import TRADING_DAYS  # noqa: E402
from src.portfolio import run_weights  # noqa: E402
from src.scan import STRATS  # noqa: E402  7 个正式策略，和全市场扫描、信号账本同一份清单

CUTOFF = "2026-03-31"                       # 队列红线：任何研究的结束日期不得晚于此
PERIODS = {"IS": ("2013-01-01", "2019-12-31"), "OOS": ("2020-01-01", CUTOFF)}
WARM = 260                                  # 预热期根数，与 registry.run 相同
N_SHADOW, N_T008 = 18, 490
M_MAIN = len(STRATS) + N_SHADOW + N_T008    # 515
M_GRID = (1, 7, 25, 100, M_MAIN, 1000, 5000, 10_000, 100_000)
PASS = 0.95
EULER = 0.5772156649015329
BENCH = "buy_hold"                          # 只作参照行，不计入 m，也不进 cross 方差
ND = NormalDist()


# ------------------------------------------------------------------ DSR 公式
def check_end(end: str) -> None:
    if pd.Timestamp(end) > pd.Timestamp(CUTOFF):
        raise ValueError(f"结束日期 {end} 晚于研究截止日 {CUTOFF}")


def moments(x: np.ndarray) -> dict:
    """日频 Sharpe（样本标准差，ddof=1，与 metrics.sharpe 一致）、偏度、非超额峰度（总体矩，正态 = 3）。"""
    x = np.asarray(x, float)
    T = len(x)
    mu, sd = x.mean(), x.std(ddof=1)
    c = x - mu
    m2 = (c ** 2).mean()
    skew = (c ** 3).mean() / m2 ** 1.5 if m2 > 0 else np.nan
    kurt = (c ** 4).mean() / m2 ** 2 if m2 > 0 else np.nan
    return {"T": T, "sr": mu / sd if sd > 0 else np.nan, "skew": skew, "kurt": kurt}


def expected_max_sr(m: float, var_sr: float) -> float:
    """m 次独立、真实 Sharpe 都是 0 的试验里，最大 Sharpe 估计值的期望（论文的近似式）。m=1 时没有挑选，基准就是 0。"""
    if m <= 1:
        return 0.0
    return math.sqrt(var_sr) * ((1 - EULER) * ND.inv_cdf(1 - 1 / m)
                                + EULER * ND.inv_cdf(1 - 1 / (m * math.e)))


def psr(sr: float, sr0: float, T: int, skew: float, kurt: float) -> float:
    """Probabilistic Sharpe Ratio：真实 Sharpe 大于 sr0 的概率（考虑样本长度、偏度、肥尾）。"""
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if not (np.isfinite(sr) and np.isfinite(denom)) or denom <= 0 or T < 2:
        return np.nan
    return ND.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(denom))


def dsr(sr: float, T: int, skew: float, kurt: float, m: float, var_sr: float) -> float:
    return psr(sr, expected_max_sr(m, var_sr), T, skew, kurt)


def breakeven_m(sr: float, T: int, skew: float, kurt: float, var_sr: float,
                level: float = PASS, m_max: int = 10 ** 9) -> float:
    """DSR ≥ level 时 m 最大能取到多少（SR0 随 m 单调增，二分查找）。
    0 = m=1（不校正）也不过；inf = m 到 10⁹ 还能过。"""
    if not dsr(sr, T, skew, kurt, 1, var_sr) >= level:
        return 0
    if dsr(sr, T, skew, kurt, m_max, var_sr) >= level:
        return math.inf
    lo, hi = 1, m_max
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if dsr(sr, T, skew, kurt, mid, var_sr) >= level:
            lo = mid
        else:
            hi = mid
    return lo


# ------------------------------------------------------------------ 数据与回测
def find_daily_dir(data_dir: Path) -> Path:
    """行情缓存在 <data>/daily/<市场>/<代码>.csv；本副本的 data 拷贝多套了一层 data/data，两种都认。"""
    for d in (data_dir / "daily", data_dir / "data" / "daily"):
        if (d / "us").is_dir() or (d / "cn").is_dir():
            return d
    raise FileNotFoundError(f"在 {data_dir} 下找不到 daily/us 或 daily/cn")


def load_prices(daily_dir: Path, market: str, symbols) -> tuple[dict, dict]:
    """只读 CSV，读完立刻截到 CUTOFF。返回 (行情, 每个文件的 sha256)。"""
    prices, digest = {}, {}
    for s in symbols:
        p = daily_dir / market / f"{s}.csv"
        if not p.exists():
            continue
        raw = p.read_bytes()
        digest[f"{market}/{s}.csv"] = hashlib.sha256(raw).hexdigest()
        df = pd.read_csv(p, index_col=0, parse_dates=True).sort_index()
        prices[s] = df.loc[:CUTOFF]
    return prices, digest


def strategy_excess(name: str, prices: dict, symbol: str, mcfg: dict, market: str) -> pd.Series:
    """跑一次默认参数回测，返回预热期之后的日超额收益（策略 − 现金 ETF）。口径同 registry.run。"""
    for df in prices.values():
        check_end(str(df.index[-1].date()))
    params = registry.clean_params(name, {})
    W = registry.target_weights(name, prices, symbol, params, mcfg)
    used = {s: prices[s] for s in W.columns}
    cost = mcfg["cost_bp"] / 1e4             # 这里只用 universe 里的 ETF，按 ETF 费率
    cash = prices[mcfg["cash"]]["Close"].pct_change() if mcfg["cash"] in prices else None
    limits = {s: limit_pct(s) for s in set(W.columns) | {symbol}} if market == "cn" else None
    band = 0.10 if name not in ("buy_hold", "dual_momentum") else 0.0
    res = run_weights(used, W, cost=cost, band=band, cash_ret=cash, limits=limits)
    r = res.returns
    r = r.loc[r.index[min(WARM, len(r) - 1)]:]
    rf = cash.reindex(r.index).fillna(0.0) if cash is not None else 0.0
    return (r - rf).rename(name)


def instrument_rows(prices: dict, symbol: str, mcfg: dict, market: str) -> list[dict]:
    """一个品种 × (7 个正式策略 + 买入持有参照) × (样本内, 样本外)。"""
    rows = []
    ex = {n: strategy_excess(n, prices, symbol, mcfg, market) for n in [*STRATS, BENCH]}
    for per, (a, b) in PERIODS.items():
        check_end(b)
        part = {n: x.loc[a:b] for n, x in ex.items()}
        mom = {n: moments(x.to_numpy()) for n, x in part.items()}
        formal_sr = [mom[n]["sr"] for n in STRATS if np.isfinite(mom[n]["sr"])]
        var_cross = float(np.var(formal_sr, ddof=1)) if len(formal_sr) > 1 else np.nan
        for n in [*STRATS, BENCH]:
            x, mo = part[n], mom[n]
            T, sr, sk, ku = mo["T"], mo["sr"], mo["skew"], mo["kurt"]
            var_null = 1 / (T - 1)
            row = {"market": market, "symbol": symbol, "strategy": n, "formal": n != BENCH, "period": per,
                   "start": str(x.index[0].date()), "end": str(x.index[-1].date()), "T": T,
                   "sr_ann": sr * math.sqrt(TRADING_DAYS), "skew": sk, "kurt": ku,
                   "psr0": psr(sr, 0.0, T, sk, ku),
                   "var_null": var_null, "var_cross": var_cross}
            for tag, v in (("null", var_null), ("cross", var_cross)):
                ok = np.isfinite(v)
                row[f"sr0_ann_{tag}"] = expected_max_sr(M_MAIN, v) * math.sqrt(TRADING_DAYS) if ok else np.nan
                row[f"dsr_{tag}"] = dsr(sr, T, sk, ku, M_MAIN, v) if ok else np.nan
                row[f"m_star_{tag}"] = breakeven_m(sr, T, sk, ku, v) if ok else np.nan
            row["pass"] = bool(row["dsr_null"] >= PASS and row["dsr_cross"] >= PASS)
            rows.append(row)
    return rows


def sensitivity_rows(rows: list[dict]) -> list[dict]:
    """m 取不同值时的 DSR；需要每行的日 Sharpe、偏度、峰度，从 sr_ann 还原日频。"""
    out = []
    for r in rows:
        sr = r["sr_ann"] / math.sqrt(TRADING_DAYS)
        for tag in ("null", "cross"):
            v = r[f"var_{tag}"]
            item = {k: r[k] for k in ("market", "symbol", "strategy", "period", "T", "sr_ann")} | {"var": tag}
            for m in M_GRID:
                item[f"m={m}"] = dsr(sr, r["T"], r["skew"], r["kurt"], m, v) if np.isfinite(v) else np.nan
            out.append(item)
    return out


# ------------------------------------------------------------------ 纯噪声校准
def calibrate(ms=(7, 25, 100, M_MAIN), T: int = 1750, reps: int = 300, seed: int = 4,
              log=print) -> list[dict]:
    """m 个真实 Sharpe = 0 的正态噪声策略，取 Sharpe 最大的那个算 DSR，统计被判「通过」的比例（假阳性率）。
    同时记录最大 Sharpe 的平均值和公式给的 SR0，检查期望最大值近似式本身准不准。"""
    rng = np.random.default_rng(seed)
    out = []
    for m in ms:
        hits = {"null": 0, "cross": 0}
        max_srs, sr0s = [], []
        for _ in range(reps):
            x = rng.standard_normal((m, T)) * 0.01
            srs = x.mean(1) / x.std(1, ddof=1)
            i = int(np.argmax(srs))
            mo = moments(x[i])
            max_srs.append(mo["sr"])
            v_null = 1 / (T - 1)
            v_cross = float(np.var(srs, ddof=1)) if m > 1 else v_null   # m=1 时不挑选，两种方差都用不上
            sr0s.append(expected_max_sr(m, v_null))
            hits["null"] += dsr(mo["sr"], T, mo["skew"], mo["kurt"], m, v_null) >= PASS
            hits["cross"] += dsr(mo["sr"], T, mo["skew"], mo["kurt"], m, v_cross) >= PASS
        row = {"m": m, "T": T, "reps": reps,
               "mean_max_sr_ann": float(np.mean(max_srs)) * math.sqrt(TRADING_DAYS),
               "sr0_formula_ann": float(np.mean(sr0s)) * math.sqrt(TRADING_DAYS),
               "false_pass_null": hits["null"] / reps, "false_pass_cross": hits["cross"] / reps}
        log(f"  校准 m={m}: {row}")
        out.append(row)
    return out


# ------------------------------------------------------------------ 输出
def _f(v, nd=2):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "∞" if v == math.inf else "—"
    return f"{v:.{nd}f}"


def _m(v):
    if v == math.inf:
        return ">10⁹"
    return "—" if not np.isfinite(v) else f"{int(v):,}"


def markdown(main: pd.DataFrame, allrows: pd.DataFrame, sens: pd.DataFrame, calib: list[dict], meta: dict) -> str:
    L = [f"# 七个正式策略的通缩夏普（DSR）表", "",
         f"生成时间 {meta['generated']}；行情截止 {CUTOFF}；m 主值 = {meta['m_main']}"
         f"（{len(STRATS)} 正式 + {N_SHADOW} 影子 + {N_T008} T008）；通过线 DSR ≥ {PASS}，null 与 cross 两种方差都要过。", "",
         "Sharpe 是相对现金 ETF 的超额年化 Sharpe；SR0 是 m 次纯噪声试验里最好那个的期望 Sharpe（年化）；"
         "PSR(0) 是不做多重检验校正时「真实 Sharpe > 0」的概率；m* 是还能通过的最大试验次数。", ""]
    for sym, g in main.groupby("symbol", sort=False):
        L += [f"## {sym}（{g['market'].iloc[0]}）", "",
              "| 策略 | 段 | 起止 | T | Sharpe | 偏度 | 峰度 | PSR(0) | SR0 null | DSR null | m* null | SR0 cross | DSR cross | m* cross | 通过 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in g.itertuples():
            nm = r.strategy + ("（参照）" if not r.formal else "")
            L.append(f"| {nm} | {r.period} | {r.start}~{r.end} | {r.T} | {_f(r.sr_ann)} | {_f(r.skew)} | {_f(r.kurt, 1)} | "
                     f"{_f(r.psr0, 3)} | {_f(r.sr0_ann_null)} | {_f(r.dsr_null, 3)} | {_m(r.m_star_null)} | "
                     f"{_f(r.sr0_ann_cross)} | {_f(r.dsr_cross, 3)} | {_m(r.m_star_cross)} | "
                     f"{'是' if r.pass_ else '否'} |")
        L.append("")
    L += ["## m 敏感性（主品种，null 方差）", ""]
    cols = [f"m={m}" for m in M_GRID]
    s = sens[(sens["var"] == "null") & sens["symbol"].isin(main["symbol"].unique())]
    L += ["| 品种 | 策略 | 段 | Sharpe | " + " | ".join(cols) + " |", "|---" * (4 + len(cols)) + "|"]
    for r in s.itertuples(index=False):
        d = r._asdict()
        L.append(f"| {r.symbol} | {r.strategy} | {r.period} | {_f(r.sr_ann)} | "
                 + " | ".join(_f(v, 3) for v in list(d.values())[-len(cols):]) + " |")
    L.append("")
    f = allrows[allrows["formal"]]
    L += ["## 全部 ETF 汇总（正式策略）", "",
          "| 段 | 行数 | PSR(0)≥0.95 | DSR null≥0.95 | DSR cross≥0.95 | 两种都过 |", "|---|---|---|---|---|---|"]
    for per, g in f.groupby("period", sort=False):
        L.append(f"| {per} | {len(g)} | {(g.psr0 >= PASS).sum()} | {(g.dsr_null >= PASS).sum()} | "
                 f"{(g.dsr_cross >= PASS).sum()} | {g['pass'].sum()} |")
    L.append("")
    if calib:
        L += ["## 纯噪声校准（m 个真实 Sharpe=0 的策略，取最好那个）", "",
              "| m | T | 次数 | 最大 Sharpe 均值(年化) | 公式 SR0(年化) | 误判通过率 null | 误判通过率 cross |",
              "|---|---|---|---|---|---|---|"]
        for c in calib:
            L.append(f"| {c['m']} | {c['T']} | {c['reps']} | {_f(c['mean_max_sr_ann'], 3)} | "
                     f"{_f(c['sr0_formula_ann'], 3)} | {_f(c['false_pass_null'], 3)} | {_f(c['false_pass_cross'], 3)} |")
        L.append("")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--calibrate", action="store_true", help="另跑纯噪声校准（不读行情）")
    ap.add_argument("--calib-reps", type=int, default=300)
    a = ap.parse_args(argv)
    for _, b in PERIODS.values():
        check_end(b)
    t0 = time.time()
    daily = find_daily_dir(a.data_dir.resolve())
    print(f"行情目录 {daily}")
    rows, digests, mains = [], {}, []
    for market in ("us", "cn"):
        mcfg = MARKETS[market]
        prices, dg = load_prices(daily, market, mcfg["universe"])
        digests |= dg
        mains.append(mcfg["main"])
        for sym in mcfg["universe"]:
            if sym == mcfg["cash"] or sym not in prices:
                continue
            rows += instrument_rows(prices, sym, mcfg, market)
            print(f"  {market} {sym} 完成 ({time.time() - t0:.0f}s)")
    allrows = pd.DataFrame(rows)
    sens = pd.DataFrame(sensitivity_rows(rows))
    main_df = allrows[allrows["symbol"].isin(mains)]
    calib = calibrate(reps=a.calib_reps) if a.calibrate else []
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "cutoff": CUTOFF, "periods": PERIODS,
            "m_main": M_MAIN, "m_grid": list(M_GRID), "strategies": STRATS, "warm": WARM,
            "pass_level": PASS, "daily_dir": str(daily), "inputs_sha256": digests,
            "versions": {"pandas": pd.__version__, "numpy": np.__version__}}
    a.out_dir.mkdir(parents=True, exist_ok=True)
    allrows.to_csv(a.out_dir / "dsr_all_etf.csv", index=False)
    main_df.to_csv(a.out_dir / "dsr_main.csv", index=False)
    sens.to_csv(a.out_dir / "dsr_m_sensitivity.csv", index=False)
    if calib:
        pd.DataFrame(calib).to_csv(a.out_dir / "dsr_noise_calibration.csv", index=False)
    (a.out_dir / "dsr_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    md = markdown(main_df.rename(columns={"pass": "pass_"}), allrows, sens, calib, meta)
    (a.out_dir / "dsr_table.md").write_text(md, encoding="utf-8")
    print(f"写出 {a.out_dir}（{time.time() - t0:.0f}s）")


if __name__ == "__main__":
    main()
