"""横截面选股假设的事先登记检验（T025）：只读本地缓存，不联网，不导入 app。

假设：事先给定的 3 个因子（反转、低波动、换手率量比），每期持有因子排名前 20% 的股票（等权），
扣成本后相对「同一可交易全市场等权」的每期超额收益在确认期（2022~2023）均值 > 0。

用法（项目根目录）：
  .venv/bin/python -B scripts/strategy_lab_xs.py --out-dir DIR [--smoke]

事先登记的规则（写死，在计算任何结果之前写入 PREREG_XS.json，之后不得修改）：
  - 因子与调仓间隔：reversal1 每 5 个交易日；low_vol、turnover 每 20 个交易日。方向取因子库登记的 direction，不看结果调整。
  - 信号：调仓日 t 收盘及以前的数据；成交：t+1 开盘；持有到下一个调仓日 t' 的 t'+1 开盘（开盘到开盘）。
  - 可交易股票池：非 ST（名称为当前快照，不精确）、t 日有成交、上市不满 250 根 K 线剔除、近 20 日日均成交额 > 1 亿（近似）。
  - 买不进：t+1 开盘无价、无成交量、或开盘价相对前收涨幅 ≥ 9.5%（涨停买不进）→ 该仓位当期空仓（收益 0，保守）。
  - 成本：市场默认单边 stock_cost_bp，按换仓比例的买入侧+卖出侧计；基准（全市场等权）不扣成本，对策略不利。
  - 确认 = 确认期（信号日在 2022-01-01 ~ 2023-12-31）每期超额均值 > 0 且 Newey-West t > 2.0，
    且 2022、2023 两个年份的均值都 > 0，且成本加倍后均值仍 > 0。三个假设同时检验。
  - 随机对照只报告不入规则：1000 次打乱排名后同样选前 20%，看真实超额均值在其中的位置。
  - 局限（登记时就承认）：5223 只名单已在之前的检验里全部看过，所以这不是真正的样本外，只是新增一次检验（多重检验次数 +3）；
    名单是当前在市股票（幸存者偏差）；ST 与行业用当前快照；后复权价；确认期只有约 2 年，样本有限。
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from config import MARKETS  # noqa: E402

FACTORS = {"reversal1": 5, "low_vol": 20, "turnover": 20}      # 因子 -> 调仓间隔（交易日）
TOP_Q = 0.20
CONF_START, CONF_END = "2022-01-01", "2023-12-31"
MIN_BARS = 250
LIMIT_UP = 0.095
T_MIN = 2.0
NW_LAG = 4
N_RANDOM = 1000
SEED = 20261025


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def nw_t(x, lag=NW_LAG):
    """均值的 Newey-West t 值（Bartlett 权重）；样本太少或方差退化返回 nan。"""
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(x)
    if n < 3:
        return float("nan")
    lag = min(lag, n - 2)
    e = x - x.mean()
    var = float(e @ e) / n
    for k in range(1, lag + 1):
        var += 2 * (1 - k / (lag + 1)) * float(e[k:] @ e[:-k]) / n
    if var <= 0:
        return float("nan")
    return float(x.mean() / math.sqrt(var / n))


def rebalance_positions(n, step, first=0):
    """调仓日在日期序列里的位置：first, first+step, ...；最后一个调仓日 t 需要 t+1 存在，且下一期的 t'+1 也存在才算完整一期。"""
    pos = list(range(first, n, step))
    return [t for t in pos if t + 1 < n]


def select_top(score, ok, q=TOP_Q):
    """score 为已按方向调整（越大越好）的一行；ok 为可交易掩码；取前 q 比例（至少 1 只），平局按代码顺序，返回代码列表。"""
    s = score[ok & score.notna()]
    if len(s) == 0:
        return []
    k = max(1, int(math.floor(q * len(s))))
    order = np.lexsort((np.arange(len(s)), -s.values.astype(float)))
    return list(s.index[order[:k]])


def buyable(open_, volume, close, t):
    """t+1 日能否买进（逐股布尔 Series）：有开盘价、有成交量、开盘相对前收涨幅 < 9.5%。"""
    o, v, pc = open_.iloc[t + 1], volume.iloc[t + 1], close.iloc[t]
    ok = o.notna() & (v > 0) & pc.notna()
    gap = o / pc - 1.0
    return ok & ~(gap >= LIMIT_UP)


def period_returns(open_ff, t, t2):
    """开盘到开盘：open[t+1] -> open[t2+1]；open_ff 是前向填充后的开盘价（停牌中途按上一有效价，不超前）。"""
    return open_ff.iloc[t2 + 1] / open_ff.iloc[t + 1] - 1.0


def backtest(score, ok, open_, volume, close, step, cost_bp, q=TOP_Q, signal_dates=None, pick=None):
    """逐期回测。返回 DataFrame：signal_date、port、bench、excess、replaced（换仓比例）。
    pick(score_row, ok_row) 可替换选股函数（随机对照用）；默认 select_top。"""
    pick = pick or (lambda s, o: select_top(s, o, q))
    open_ff = open_.ffill()
    pos = rebalance_positions(len(open_), step)
    rows, prev = [], set()
    for a, b in zip(pos[:-1], pos[1:]):
        if signal_dates is not None and not signal_dates(open_.index[a]):
            continue
        held = pick(score.iloc[a], ok.iloc[a])
        if not held:
            continue
        can = buyable(open_, volume, close, a)
        rets = period_returns(open_ff, a, b)
        r = rets.reindex(held)
        r = r.where(can.reindex(held).fillna(False), 0.0).fillna(0.0)     # 买不进→空仓，收益 0
        port = float(r.mean())
        bench_codes = ok.iloc[a][ok.iloc[a]].index
        bench = float(rets.reindex(bench_codes).dropna().mean()) if len(bench_codes) else float("nan")
        cur = set(held)
        replaced = 1.0 if not prev else 1.0 - len(cur & prev) / max(len(cur), 1)
        port_net = port - 2 * replaced * cost_bp / 1e4
        rows.append({"signal_date": open_.index[a], "port": port_net, "port_gross": port, "bench": bench,
                     "excess": port_net - bench, "replaced": replaced})
        prev = cur
    return pd.DataFrame(rows)


def judge(bt_cost1, bt_cost2):
    """事先登记的确认规则；两个 DataFrame 分别是 1 倍成本与 2 倍成本的确认期回测结果。"""
    ex = bt_cost1["excess"].dropna() if len(bt_cost1) else pd.Series(dtype=float)
    ex2 = bt_cost2["excess"].dropna() if len(bt_cost2) else pd.Series(dtype=float)
    mean, t = (float(ex.mean()) if len(ex) else float("nan")), nw_t(ex)
    years = {}
    for y in (2022, 2023):
        m = ex[pd.to_datetime(bt_cost1.loc[ex.index, "signal_date"]).dt.year == y] if len(ex) else ex
        years[y] = float(m.mean()) if len(m) else float("nan")
    m2 = float(ex2.mean()) if len(ex2) else float("nan")
    ok = bool(len(ex) > 0 and mean > 0 and t > T_MIN and all(v > 0 for v in years.values()) and m2 > 0)
    return {"n_periods": int(len(ex)), "mean_excess": mean, "nw_t": t, "mean_2022": years[2022], "mean_2023": years[2023],
            "mean_cost_x2": m2, "avg_replaced": float(bt_cost1["replaced"].mean()) if len(bt_cost1) else float("nan"),
            "confirmed": ok}


def random_percentile(real_mean, score, ok, open_, volume, close, step, cost_bp, in_conf, n=N_RANDOM, seed=SEED):
    """真实超额均值在 n 次随机选股（同样前 20%、同样成本）里的分位（0~1）。"""
    rng = np.random.default_rng(seed)
    means = []

    def rnd(_s, o):
        codes = o[o].index
        if len(codes) == 0:
            return []
        k = max(1, int(math.floor(TOP_Q * len(codes))))
        return list(rng.choice(codes, size=k, replace=False))

    for _ in range(n):
        bt = backtest(score, ok, open_, volume, close, step, cost_bp, signal_dates=in_conf, pick=rnd)
        means.append(float(bt["excess"].mean()) if len(bt) else float("nan"))
    means = np.asarray([m for m in means if math.isfinite(m)])
    return float((means < real_mean).mean()) if len(means) else float("nan"), means


def eligibility(panel, fx):
    """可交易掩码（宽表布尔）：非 ST、当日有成交、上市满 MIN_BARS 根、流动性过滤。"""
    ok = fx.st_filter(panel) & fx.suspension_filter(panel) & fx.liquidity_filter(panel)
    age = panel.close.notna().cumsum()
    return ok & (age >= MIN_BARS)


def in_conf_period(d):
    return pd.Timestamp(CONF_START) <= d <= pd.Timestamp(CONF_END)


def run(panel, fx, cost_bp, n_random=N_RANDOM):
    ok = eligibility(panel, fx)
    res = {}
    for fid, step in FACTORS.items():
        raw = fx.compute_factor(fid, panel)
        score = raw * fx.FACTORS[fid].direction          # 因子库登记的方向：+1 越大越好，-1 越小越好；乘完后统一为越大越好
        kw = dict(score=score, ok=ok, open_=panel.open, volume=panel.volume, close=panel.close, step=step)
        b1 = backtest(cost_bp=cost_bp, signal_dates=in_conf_period, **kw)
        b2 = backtest(cost_bp=2 * cost_bp, signal_dates=in_conf_period, **kw)
        v = judge(b1, b2)
        if n_random and len(b1):
            v["random_percentile"], _ = random_percentile(v["mean_excess"], score, ok, panel.open, panel.volume, panel.close,
                                                          step, cost_bp, in_conf_period, n=n_random)
        else:
            v["random_percentile"] = float("nan")
        res[fid] = v
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--smoke", action="store_true", help="只用少量股票和 20 次随机对照做自检")
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    pf = out / "PREREG_XS.json"
    if pf.exists():
        sys.exit("PREREG_XS.json 已存在：拒绝覆盖。换一个输出目录。")
    import strategy_lab as sl                      # 只借用面板构造；导入它不会联网
    from src import allmarket, datasource
    from src.market_monitor import factors as fx
    cost_bp = MARKETS["cn"]["stock_cost_bp"]
    lst = pd.read_csv(allmarket.LIST_FILE, dtype=str)
    codes = sorted(c for c in lst["code"] if datasource.is_valid_stock(c))
    if a.smoke:
        codes = codes[:300]
    out.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"started": time.strftime("%Y-%m-%d %H:%M"), "factors": FACTORS, "top_q": TOP_Q,
                              "conf": [CONF_START, CONF_END], "cost_bp_one_side": cost_bp, "min_bars": MIN_BARS,
                              "limit_up": LIMIT_UP, "t_min": T_MIN, "nw_lag": NW_LAG, "n_random": N_RANDOM, "seed": SEED,
                              "n_codes": len(codes), "smoke": bool(a.smoke),
                              "rule": "确认期每期超额均值>0 且 NW t>2.0 且 2022、2023 两年均值都>0 且成本加倍后均值>0",
                              "note": "本文件在计算任何结果之前写入，之后不得修改；名单已在之前的检验中全部看过，这不是真正的样本外"},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"构造面板：{len(codes)} 只")
    panel = sl.build_factor_panel(codes)
    res = run(panel, fx, cost_bp, n_random=20 if a.smoke else N_RANDOM)
    (out / "xs_results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    L = ["# 横截面选股确认检验（事先登记，未经 Codex 审）", "",
         f"股票 {len(codes)} 只；确认期 {CONF_START}~{CONF_END}；单边成本 {cost_bp}bp；规则见 PREREG_XS.json。", "",
         "| 因子 | 调仓间隔 | 期数 | 每期超额均值 | NW t | 2022 | 2023 | 成本×2 均值 | 平均换仓比例 | 随机对照分位 | 结论 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for fid, v in res.items():
        L.append(f"| {fid} | {FACTORS[fid]} | {v['n_periods']} | {v['mean_excess']:+.4f} | {v['nw_t']:+.2f} | {v['mean_2022']:+.4f} | "
                 f"{v['mean_2023']:+.4f} | {v['mean_cost_x2']:+.4f} | {v['avg_replaced']:.2f} | {v['random_percentile']:.2f} | "
                 f"{'**确认**' if v['confirmed'] else '未确认'} |")
    L += ["", "## 局限", "", "- 5223 只名单已在之前的检验里全部看过，这不是真正的样本外，只是新增一次检验（多重检验次数 +3）。",
          "- 当前在市名单（幸存者偏差）；ST 用当前名称；后复权价；成交额近似；基准不扣成本；确认期约 2 年，期数有限。",
          "- 即使「确认」，也只是在这批历史数据上没有被推翻，不是样本外证明，更不构成投资建议。"]
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")
    log("完成")


if __name__ == "__main__":
    main()
