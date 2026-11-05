"""C14 参数稳定性热力图：7 个正式策略，主参数在默认值 ±50% 的 5×5 网格上，只用样本内（2013~2019）算夏普。

问题：默认参数附近的表现是「一片平台」（参数稍微挪一挪，夏普差不多）还是「孤立尖峰」（只有某一格好看，邻居都差）？
  尖峰 = 过拟合的典型信号：最优点多半是在噪声里挑出来的，参数一变就没了；平台 = 策略的赚钱逻辑对参数不敏感，更可信。
  这一项只是诊断：不拿样本外选参数，不改任何默认参数，不改任何策略规则。

做法（全程复用项目已有函数，不改任何已有文件）：
  1. 行情读进来立刻截到 IS_END（2019-12-31，比队列红线 2026-03-31 更早），之后才算信号：
     registry.target_weights（收盘出信号）→ portfolio.run_weights（次日开盘成交、扣费、10% 调仓阈值、空仓现金计息、A 股一字板），
     口径同 C04 / C08 / C11 / registry.run。样本外（2020 年以后）的数据根本没有进入计算。
     每个品种前 WARM=260 根作预热，统计窗口起点 = max(2013-01-01, 第 260 根)，终点 = 2019-12-31。
  2. 每个策略选 2 个主参数（SPECS，选法见报告），在默认值的 ×0.5 / ×0.75 / ×1 / ×1.25 / ×1.5 上做 5×5 = 25 格；
     整数参数四舍五入（0.5 向上），其余参数（ATR 倍数、缓冲、目标波动）保持小数；没选的参数保持默认值。
     中间那一格（第 3 行第 3 列）永远是默认参数。
  3. 指标：夏普 = 日超额收益（策略 − 现金 ETF：美 SHY、A 股 511880）均值/标准差 × √252（metrics.sharpe，同 C04/C08/C11）；另存年化收益。
  4. 对象：SPY、510300（两个市场的主信号品种）、us_ew / cn_ew（各市场 ETF 池里除现金外的品种，策略各自独立跑，日收益等权平均），
     以及可选的 cn_stock（固定种子抽的 A 股个股，等权，--n-stocks）。
  5. 「平台还是尖峰」的量化（surface_metrics，阈值写在常量里，是约定而不是统计推导，报告里会说明）：
       best      = 25 格里夏普最高的一格；neighbor_mean = 它周围（最多 8 个）邻格的夏普平均；
       drop      = best − neighbor_mean；drop_ratio = drop / best（best > 0 才有定义）：最优点比它的邻居高出多少比例；
       plateau_share = 夏普 ≥ 0.8 × best 的格子占比（平台越大越高）；
       opt_gain  = best − 默认格夏普：在这个网格里「调参」能多挖出的夏普，也是在样本内能得到的最大「过拟合红利」上限；
       smooth_p  = 置换检验：把 25 个格子的值随机打乱放回网格 N 次，看打乱后「相邻格差异的平均绝对值」比真实的更小的比例；
                   越小说明真实曲面比随机排列平滑得多（参数一动，夏普是连续变化而不是乱跳）；
       edge      = 最优点在网格边缘（说明真正的最优可能在网格外，这时 drop 的意义变弱）。
       rho_1 / rho_2 = 夏普与第一/第二个参数档位（0~4）的斯皮尔曼秩相关：接近 ±1 说明沿这个参数方向单调变化（斜坡）。
     分类（按顺序判断）：best ≤ 0 →「无正夏普」；drop_ratio < PLATEAU_DROP 且 plateau_share ≥ PLATEAU_SHARE →「平台」；
       最优点在网格边缘且 max(|rho|) ≥ SLOPE_RHO →「斜坡」（曲面沿某个参数单调爬升，真正的最优在网格之外，不是孤立尖峰，
       但也说明默认值不在最优处、对该参数有方向性的敏感）；drop_ratio ≥ SPIKE_DROP →「尖峰」；其余「过渡」。
     说明：「斜坡」这一类是看过第一版输出后加的——第一版没有它，会把「爬到网格角上」的斜坡误判成尖峰（35 张网格里 32 张最优点在边缘）。
           所以分类阈值是事后约定的描述性标签，不是预先设定的检验。

只读 data/ 下的行情，不写 data/；不联网。

用法：
  .venv/bin/python -B scripts/param_stability.py --out-dir DIR [--data-dir data] [--n-stocks 0] [--seed 14] [--perm 5000]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from config import MARKETS  # noqa: E402
from src import registry  # noqa: E402
from src.datasource import limit_pct  # noqa: E402
from src.metrics import TRADING_DAYS, sharpe  # noqa: E402
from src.portfolio import run_weights  # noqa: E402
from src.scan import STRATS  # noqa: E402  7 个正式策略，和全市场扫描、信号账本同一份清单
from deflated_sharpe_table import check_end, find_daily_dir, load_prices  # noqa: E402  C04：截止日检查、找行情目录、读行情

CUTOFF = "2026-03-31"                       # 队列红线：任何研究的结束日期不得晚于此
IS_START = "2013-01-01"                     # 样本内起点（与 C04 / C08 一致）
IS_END = "2019-12-31"                       # 样本内终点；行情读进来就截到这天，样本外数据不进计算
WARM = 260                                  # 预热期根数，与 registry.run 相同
BAND = 0.10                                 # 调仓阈值，与 registry.run / scan.py 相同
ORDER = ["blend2", "trend_ens", "rsi2", "donchian_s1", "trend_vol", "ma_adx_original", "ma_adx_plus"]
FACTORS = (0.5, 0.75, 1.0, 1.25, 1.5)       # 默认值的倍数：±50%，5 档；中间一档 = 默认
N = len(FACTORS)
MID = N // 2
GROUPS = ["SPY", "510300", "us_ew", "cn_ew", "cn_stock"]
STOCK_LISTED_BY = "2011-12-31"              # 个股要在这天之前上市，才能在 2013-01-01 就有完整的 260 根预热
assert sorted(ORDER) == sorted(STRATS)

# 每个策略的两个主参数（键名 = registry.STRATEGIES 里登记的参数名；第一个在网格的行方向，第二个在列方向）
SPECS = {
    "blend2": ("w_trend", "w_rsi"),           # 三个子策略权重（w_donchian 固定 1/3，三个权重会被归一化，所以只看相对比例）
    "trend_ens": ("target_vol", "buffer"),    # 4 条均线的长度 (100/150/200/250) 没有在登记表里暴露，不在这里动
    "rsi2": ("trend_len", "entry_th"),
    "donchian_s1": ("entry_len", "exit_len"),
    "trend_vol": ("sma_len", "buffer"),
    "ma_adx_original": ("fast", "slow"),
    "ma_adx_plus": ("slow", "atr_mult"),
}
assert sorted(SPECS) == sorted(ORDER)

# 平台/尖峰的分类阈值：约定值，不是统计推导（报告里写明）
SPIKE_DROP = 0.25        # 最优点比邻居均值高出 25% 以上 → 尖峰
PLATEAU_DROP = 0.10      # 高出不到 10%，且……
PLATEAU_SHARE = 0.50     # ……至少一半的格子夏普 ≥ 0.8×最优 → 平台
SLOPE_RHO = 0.60         # 最优在边缘，且夏普与某个参数档位的秩相关绝对值 ≥ 0.6 → 斜坡
NEAR_BEST = 0.8          # 「接近最优」= 夏普 ≥ 0.8 × best
MIN_SPAN = 0.3           # 画图时每个面板色阶至少覆盖 0.3 的夏普，免得很平的曲面被拉伸成「很陡」的假象


# ------------------------------------------------------------------ 网格（纯函数，测试里单独验）
def round_half_up(x: float) -> int:
    """四舍五入，0.5 向上（Python 的 round 是银行家舍入：round(2.5)=2，会让 ×0.5 档和 ×0.75 档在小整数上挤到一起）。"""
    return int(math.floor(x + 0.5))


def param_spec(name: str, key: str) -> dict:
    for p in registry.STRATEGIES[name]["params"]:
        if p["key"] == key:
            return p
    raise KeyError(f"{name} 没有登记参数 {key}")


def grid_values(name: str, key: str, factors=FACTORS) -> list:
    """一个参数的 5 档取值 = 默认值 × factors；整数参数取整（0.5 向上），小数参数保留 6 位。"""
    p = param_spec(name, key)
    out = []
    for f in factors:
        v = p["default"] * f
        if f == 1.0:                           # 中间档必须精确等于默认值（不能被 round 成 0.333333 之类）
            out.append(p["default"])
        else:
            out.append(round_half_up(v) if float(p["step"]).is_integer() else round(v, 6))
    return out


def grid_axes(name: str) -> tuple[str, list, str, list]:
    k1, k2 = SPECS[name]
    return k1, grid_values(name, k1), k2, grid_values(name, k2)


def neighbours(i: int, j: int, n: int = N) -> list[tuple[int, int]]:
    """格子 (i, j) 周围最多 8 个邻格（含对角线），不含自己；边缘格子邻居更少。"""
    return [(a, b) for a in range(max(0, i - 1), min(n, i + 2)) for b in range(max(0, j - 1), min(n, j + 2)) if (a, b) != (i, j)]


def adjacent_pairs(n: int = N) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """上下左右相邻的格子对（不含对角线），用来算「曲面有多崎岖」。5×5 共 40 对。"""
    out = []
    for i in range(n):
        for j in range(n):
            if i + 1 < n:
                out.append(((i, j), (i + 1, j)))
            if j + 1 < n:
                out.append(((i, j), (i, j + 1)))
    return out


def roughness(G: np.ndarray) -> float:
    """上下左右相邻格子的夏普之差的平均绝对值。越小曲面越平滑。"""
    return float(np.mean([abs(G[a] - G[b]) for a, b in adjacent_pairs(G.shape[0])]))


def smooth_pvalue(G: np.ndarray, reps: int, seed: int) -> float:
    """置换检验：把 25 个值随机打乱放回网格 reps 次，p = 打乱后崎岖度 ≤ 真实崎岖度 的比例（加 1 平滑，不会是 0）。
    原假设：格子位置和夏普没有关系（参数一动夏普就乱跳）。p 很小 → 真实曲面比随机排列平滑得多，邻居确实相似。
    注意：这只证明「平滑」，不证明「好」——一整片都是负夏普的平滑曲面也会得到很小的 p。"""
    if not np.all(np.isfinite(G)):
        return float("nan")
    n = G.shape[0]
    pa = adjacent_pairs(n)
    ia = np.array([a[0] * n + a[1] for a, _ in pa])
    ib = np.array([b[0] * n + b[1] for _, b in pa])
    real = np.abs(G.ravel()[ia] - G.ravel()[ib]).mean()
    rng = np.random.default_rng(seed)
    flat = G.ravel()
    cnt = 0
    for _ in range(reps):
        s = rng.permutation(flat)
        cnt += np.abs(s[ia] - s[ib]).mean() <= real + 1e-15
    return float((cnt + 1) / (reps + 1))


def surface_metrics(G: np.ndarray, reps: int = 5000, seed: int = 14) -> dict:
    """一张 5×5 夏普网格 → 平台/尖峰指标。G[MID, MID] 是默认参数。"""
    G = np.asarray(G, float)
    n = G.shape[0]
    if not np.isfinite(G).any():
        return {"cls": "无数据"}
    flat = np.where(np.isfinite(G), G, -np.inf)
    bi, bj = np.unravel_index(int(np.argmax(flat)), G.shape)
    best = float(G[bi, bj])
    nb = [G[a, b] for a, b in neighbours(bi, bj, n) if np.isfinite(G[a, b])]
    nbr = float(np.mean(nb)) if nb else float("nan")
    default = float(G[MID, MID])
    fin = G[np.isfinite(G)]
    out = {"best": best, "best_i": int(bi), "best_j": int(bj), "default": default,
           "neighbor_mean": nbr, "drop": best - nbr,
           "drop_ratio": (best - nbr) / best if best > 0 else float("nan"),
           "plateau_share": float((fin >= NEAR_BEST * best).mean()) if best > 0 else float("nan"),
           "opt_gain": best - default if np.isfinite(default) else float("nan"),
           "default_rank": int((fin > default).sum() + 1) if np.isfinite(default) else float("nan"),
           "grid_min": float(fin.min()), "grid_median": float(np.median(fin)), "grid_max": float(fin.max()),
           "grid_std": float(fin.std()), "frac_positive": float((fin > 0).mean()),
           "roughness": roughness(G) if np.all(np.isfinite(G)) else float("nan"),
           "smooth_p": smooth_pvalue(G, reps, seed),
           "edge": bool(bi in (0, n - 1) or bj in (0, n - 1))}
    out["rho_1"], out["rho_2"] = axis_rho(G)
    out["cls"] = classify(out)
    return out


def axis_rho(G: np.ndarray) -> tuple[float, float]:
    """夏普与「第一个参数档位 i」「第二个参数档位 j」的斯皮尔曼秩相关（25 个格子一起算）。常数网格记 0。"""
    n = G.shape[0]
    ii, jj = np.divmod(np.arange(n * n), n)
    ok = np.isfinite(G.ravel())
    out = []
    for ax in (ii, jj):
        y = G.ravel()
        if ok.sum() < 3 or np.ptp(y[ok]) == 0:
            out.append(0.0)
        else:
            out.append(float(sps.spearmanr(ax[ok], y[ok])[0]))
    return out[0], out[1]


def classify(m: dict) -> str:
    if m["best"] <= 0:
        return "无正夏普"
    if m["drop_ratio"] < PLATEAU_DROP and m["plateau_share"] >= PLATEAU_SHARE:
        return "平台"
    if m["edge"] and max(abs(m["rho_1"]), abs(m["rho_2"])) >= SLOPE_RHO:
        return "斜坡"
    if m["drop_ratio"] >= SPIKE_DROP:
        return "尖峰"
    return "过渡"


# ------------------------------------------------------------------ 单品种回测
def cagr(x) -> float:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(np.prod(1.0 + x) ** (TRADING_DAYS / len(x)) - 1.0) if len(x) else float("nan")


class Inst:
    """一个品种。df 已截到 IS_END；cost 是单边成本（小数）；cash 是现金 ETF 的日收益。"""

    def __init__(self, df: pd.DataFrame, symbol: str, mcfg: dict, cash: pd.Series | None, cost: float, limit: float | None):
        df = df.loc[:IS_END]
        check_end(str(df.index[-1].date()))
        assert df.index[-1] <= pd.Timestamp(IS_END), "样本内行情超过 IS_END"
        self.symbol, self.df, self.mcfg, self.cash, self.cost, self.limit = symbol, df, mcfg, cash, cost, limit
        self.prices = {symbol: df}
        idx = df.index
        self.start = max(pd.Timestamp(IS_START), idx[min(WARM, len(idx) - 1)])      # 预热 + 样本内起点

    def bh_returns(self) -> pd.Series:
        """买入持有在同一样本内窗口的日总收益，只作对照（不进网格）：满仓、不设调仓阈值，成本只在预热期付一次。"""
        W = pd.Series(1.0, index=self.df.index, name=self.symbol).to_frame()
        lim = {self.symbol: self.limit} if self.limit else None
        res = run_weights(self.prices, W, cost=self.cost, band=0.0, cash_ret=self.cash, limits=lim)
        return res.returns.loc[self.start:pd.Timestamp(IS_END)]

    def cell_returns(self, name: str, params: dict) -> pd.Series:
        """一组参数 → 样本内窗口的日总收益（已扣费）。其余参数保持登记表默认值。"""
        prm = registry.clean_params(name, params)
        W = registry.target_weights(name, self.prices, self.symbol, prm, self.mcfg)
        lim = {self.symbol: self.limit} if self.limit else None
        res = run_weights(self.prices, W, cost=self.cost, band=BAND, cash_ret=self.cash, limits=lim)
        return res.returns.loc[self.start:pd.Timestamp(IS_END)]


def inst_cells(inst: Inst) -> dict:
    """一个品种：{(策略, i, j): 日收益}。7 个策略 × 25 格 = 175 次回测。"""
    out = {}
    for name in ORDER:
        k1, v1, k2, v2 = grid_axes(name)
        for i, a in enumerate(v1):
            for j, b in enumerate(v2):
                out[(name, i, j)] = inst.cell_returns(name, {k1: a, k2: b})
    return out


def group_grids(cells_list: list[dict], cash: pd.Series | None) -> dict:
    """一个组（1 个或多个品种）→ {策略: (夏普网格, 年化收益网格)}。多个品种：各自日收益等权平均，再对现金算超额夏普。"""
    out = {}
    for name in ORDER:
        S = np.full((N, N), np.nan)
        A = np.full((N, N), np.nan)
        for i in range(N):
            for j in range(N):
                r = pd.DataFrame({k: c[(name, i, j)] for k, c in enumerate(cells_list)}).mean(axis=1, skipna=True)
                rf = cash.reindex(r.index).fillna(0.0) if cash is not None else pd.Series(0.0, index=r.index)
                S[i, j] = sharpe(r - rf)
                A[i, j] = cagr(r.to_numpy())
        out[name] = (S, A)
    return out


def group_bh_sharpe(insts: list, cash: pd.Series | None) -> float:
    """一个组里买入持有的样本内夏普（多品种等权），对照用：策略的「平台」是不是只是一个比买入持有还低的平台。"""
    r = pd.DataFrame({k: i.bh_returns() for k, i in enumerate(insts)}).mean(axis=1, skipna=True)
    rf = cash.reindex(r.index).fillna(0.0) if cash is not None else pd.Series(0.0, index=r.index)
    return sharpe(r - rf)


# ------------------------------------------------------------------ 数据
def load_market(daily: Path, market: str, digests: dict):
    mcfg = MARKETS[market]
    prices, dg = load_prices(daily, market, mcfg["universe"])
    digests.update(dg)
    cash = prices[mcfg["cash"]]["Close"].pct_change().loc[:IS_END] if mcfg["cash"] in prices else None
    insts = {}
    for sym in mcfg["universe"]:
        if sym == mcfg["cash"] or sym not in prices:
            continue
        insts[sym] = Inst(prices[sym], sym, mcfg, cash, mcfg["cost_bp"] / 1e4, limit_pct(sym) if market == "cn" else None)
    return {"insts": insts, "cash": cash, "main": mcfg["main"]}


def sample_stocks(daily: Path, n: int, seed: int, digests: dict, cash: pd.Series | None) -> tuple[list, pd.DataFrame]:
    """cn_all 里按固定种子打乱后依次取，直到凑够 n 只。合格 = 非 ST/退、STOCK_LISTED_BY 前已上市、样本内期末前后都有行情。
    只含现存股票 → 有幸存者偏差（报告里写明）。个股用 stock_cost_bp。"""
    lst = pd.read_csv(daily / "cn_all_list.csv", dtype=str).fillna("")
    lst = lst[~lst["name"].str.upper().str.contains("ST") & ~lst["name"].str.contains("退")]
    codes = sorted(lst["code"])
    names = dict(zip(lst["code"], lst["name"]))
    mcfg = MARKETS["cn"]
    cost = mcfg.get("stock_cost_bp", mcfg["cost_bp"]) / 1e4
    insts, rows = [], []
    for i in np.random.default_rng(seed).permutation(len(codes)):
        if len(insts) >= n:
            break
        code = codes[i]
        p = daily / "cn_all" / f"{code}.parquet"
        if not p.exists():
            continue
        df = pd.read_parquet(p).sort_index().loc[:IS_END]          # 读进来立刻截断
        if len(df) < WARM + 500 or df.index[0] > pd.Timestamp(STOCK_LISTED_BY) or df.index[-1] < pd.Timestamp(IS_END) - pd.Timedelta(days=10):
            continue
        digests[f"cn_all/{code}.parquet"] = hashlib.sha256(p.read_bytes()).hexdigest()
        insts.append(Inst(df, code, mcfg, cash, cost, limit_pct(code, names[code])))
        rows.append({"code": code, "name": names[code], "first": str(df.index[0].date()), "last": str(df.index[-1].date())})
    return insts, pd.DataFrame(rows)


# ------------------------------------------------------------------ 输出：表
def _f(v, nd=2):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{v:.{nd}f}"


def fv(v) -> str:
    """参数值的显示格式：3 位有效数字（1/3 显示成 0.333，整数不变），只用于显示，计算用的是精确值。"""
    return str(v) if isinstance(v, int) else f"{v:.3g}"


def params_label(name: str, i: int, j: int) -> str:
    k1, v1, k2, v2 = grid_axes(name)
    return f"{k1}={fv(v1[i])}, {k2}={fv(v2[j])}"


def long_table(group: str, grids: dict) -> pd.DataFrame:
    rows = []
    for name in ORDER:
        k1, v1, k2, v2 = grid_axes(name)
        S, A = grids[name]
        for i in range(N):
            for j in range(N):
                rows.append({"group": group, "strategy": name, "param1": k1, "value1": v1[i], "param2": k2, "value2": v2[j],
                             "i": i, "j": j, "is_default": bool(i == MID and j == MID),
                             "sharpe": S[i, j], "ann_ret": A[i, j]})
    return pd.DataFrame(rows)


def grid_text(G: np.ndarray, v1: list, v2: list, k1: str, k2: str) -> list[str]:
    head = f"{k1}\\{k2}".ljust(14) + "".join(f"{fv(v):>8}" for v in v2)
    lines = [head]
    for i, a in enumerate(v1):
        lines.append(fv(a).ljust(14) + "".join(f"{G[i, j]:>8.2f}" for j in range(len(v2))))
    return lines


def markdown(summary: pd.DataFrame, grids_by_group: dict, meta: dict) -> str:
    L = ["# 参数稳定性热力图：七个正式策略的主参数邻域（C14）", "",
         f"生成时间 {meta['generated']}；样本内 {IS_START}~{IS_END}（行情读入即截断到 {IS_END}，样本外数据不进计算）；"
         f"网格 = 默认值 × {list(FACTORS)}；指标 = 对现金的超额夏普；置换检验 {meta['perm']} 次。", "",
         f"分类（按顺序判断，阈值是事后约定的描述性标签）：平台 = drop_ratio < {PLATEAU_DROP} 且 ≥{NEAR_BEST:.0%}×最优的格子占比 ≥ {PLATEAU_SHARE:.0%}；"
         f"斜坡 = 最优在网格边缘且 max(|rho|) ≥ {SLOPE_RHO}；尖峰 = drop_ratio ≥ {SPIKE_DROP}；其余过渡；最优夏普 ≤ 0 记「无正夏普」。", ""]
    for g in GROUPS:
        s = summary[summary.group == g]
        if s.empty:
            continue
        L += [f"## {g}", "", f"同窗口买入持有夏普（对照）：{_f(float(s.bh_sharpe.iloc[0]))}", "",
              "| 策略 | 主参数 | 默认格夏普 | 最优格夏普（参数） | 邻居均值 | drop_ratio | ≥0.8×最优占比 | 默认格名次/25 | 调参增益 | smooth_p | rho_1 / rho_2 | 最优在边缘 | 判定 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for name in ORDER:
            r = s[s.strategy == name]
            if r.empty:
                continue
            r = r.iloc[0]
            L.append(f"| {name} | {r.param1}, {r.param2} | {_f(r.default)} | {_f(r.best)}（{params_label(name, int(r.best_i), int(r.best_j))}） | "
                     f"{_f(r.neighbor_mean)} | {_f(r.drop_ratio)} | {_f(r.plateau_share)} | {_f(r.default_rank, 0)} | {_f(r.opt_gain)} | "
                     f"{_f(r.smooth_p, 4)} | {_f(r.rho_1)} / {_f(r.rho_2)} | {'是' if r.edge else '否'} | {r.cls} |")
        L.append("")
        L.append("夏普网格（行 = 第一个参数，列 = 第二个参数；中间一格 = 默认）：")
        L.append("")
        for name in ORDER:
            k1, v1, k2, v2 = grid_axes(name)
            L += ["```", f"[{g}] {name}", *grid_text(grids_by_group[g][name][0], v1, v2, k1, k2), "```", ""]
    return "\n".join(L)


# ------------------------------------------------------------------ 输出：图
def make_figures(summary: pd.DataFrame, grids_by_group: dict, out: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    files = []
    for g, grids in grids_by_group.items():
        fig, axes = plt.subplots(2, 4, figsize=(17, 8.6), constrained_layout=True)
        axes = axes.ravel()
        for ax, name in zip(axes, ORDER):
            k1, v1, k2, v2 = grid_axes(name)
            G = grids[name][0]
            m = summary[(summary.group == g) & (summary.strategy == name)].iloc[0]
            lo, hi = float(np.nanmin(G)), float(np.nanmax(G))
            if hi - lo < MIN_SPAN:                      # 很平的曲面不要被色阶拉伸成「很陡」
                mid = (hi + lo) / 2
                lo, hi = mid - MIN_SPAN / 2, mid + MIN_SPAN / 2
            ax.imshow(G, cmap="Blues", vmin=lo, vmax=hi, aspect="equal")
            for i in range(N):
                for j in range(N):
                    t = (G[i, j] - lo) / (hi - lo)
                    star = int(m.best_i) == i and int(m.best_j) == j
                    ax.text(j, i, f"{G[i, j]:.2f}" + ("★" if star else ""), ha="center", va="center", fontsize=9,
                            fontweight="bold" if star else "normal", color="white" if t > 0.6 else "#1a1a1a")
            ax.add_patch(Rectangle((MID - 0.5, MID - 0.5), 1, 1, fill=False, ec="#d62728", lw=2.2))   # 红框 = 默认参数
            ax.set_xticks(range(N), [fv(v) for v in v2], fontsize=8)
            ax.set_yticks(range(N), [fv(v) for v in v1], fontsize=8)
            ax.set_xlabel(k2, fontsize=9)
            ax.set_ylabel(k1, fontsize=9)
            ax.set_title(f"{name}  [{m.cls}]\ndrop_ratio={_f(m.drop_ratio)}  调参增益={_f(m.opt_gain)}", fontsize=10)
            for sp in ax.spines.values():
                sp.set_visible(False)
        ax = axes[-1]
        ax.axis("off")
        ax.text(0, 0.95, "怎么读", fontsize=11, fontweight="bold", va="top")
        ax.text(0, 0.82, "格子里的数字 = 样本内夏普（对现金超额）\n红框 = 默认参数（网格中心）\n★ = 25 格里夏普最高的一格\n"
                         "网格 = 每个参数取默认值的 ×0.5/0.75/1/1.25/1.5\n色阶每个面板独立（深 = 夏普高），\n但至少覆盖 0.3 夏普，避免把很平的\n曲面拉成很陡。\n"
                         "drop_ratio = 最优格比它邻居均值\n高出的比例；越大越像孤立尖峰。\n调参增益 = 最优格 − 默认格。",
                fontsize=9, va="top", linespacing=1.5)
        fig.suptitle(f"{g}：主参数 5×5 邻域的样本内夏普（{IS_START[:4]}~{IS_END[:4]}，只做诊断，不改默认参数）", fontsize=13)
        f = f"heatmap_{g}.png"
        fig.savefig(out / f, dpi=130)
        plt.close(fig)
        files.append(f)

    # 汇总：策略 × 组 的 drop_ratio
    gs = [g for g in GROUPS if (summary.group == g).any()]
    M = np.full((len(ORDER), len(gs)), np.nan)
    T = [["" for _ in gs] for _ in ORDER]
    for a, name in enumerate(ORDER):
        for b, g in enumerate(gs):
            r = summary[(summary.group == g) & (summary.strategy == name)].iloc[0]
            M[a, b] = r.drop_ratio
            T[a][b] = f"{_f(r.drop_ratio)}\n{r.cls}"
    fig, ax = plt.subplots(figsize=(1.9 * len(gs) + 2.5, 5.6), constrained_layout=True)
    VMAX = 0.7                                  # 色阶上限：drop_ratio ≥ 0.7 同色，免得 2.0 这种「最优夏普接近 0」的极端值把其余格子都压成浅色
    ax.imshow(M, cmap="Blues", vmin=0, vmax=VMAX, aspect="auto")
    for a in range(len(ORDER)):
        for b in range(len(gs)):
            v = M[a, b]
            ax.text(b, a, T[a][b], ha="center", va="center", fontsize=9, color="white" if np.isfinite(v) and v > 0.6 * VMAX else "#1a1a1a")
    ax.set_xticks(range(len(gs)), gs)
    ax.set_yticks(range(len(ORDER)), ORDER)
    ax.set_title("最优格比邻居均值高出的比例 drop_ratio（越深越像尖峰，≥0.7 同色）\n最优夏普接近 0 时比例会被放大（分母很小），读数时要结合夏普水平", fontsize=11)
    for sp in ax.spines.values():
        sp.set_visible(False)
    fig.savefig(out / "fig_summary_drop_ratio.png", dpi=130)
    plt.close(fig)
    files.append("fig_summary_drop_ratio.png")
    return files


# ------------------------------------------------------------------ 主流程
def run(data_dir: Path, out_dir: Path, n_stocks: int, seed: int, perm: int, log=print) -> dict:
    check_end(CUTOFF)
    check_end(IS_END)
    t0 = time.time()
    daily = find_daily_dir(data_dir.resolve())
    log(f"行情目录 {daily}")
    digests: dict = {}
    mk = {m: load_market(daily, m, digests) for m in ("us", "cn")}
    members = {"SPY": ("us", ["SPY"]), "510300": ("cn", ["510300"]), "us_ew": ("us", list(mk["us"]["insts"])),
               "cn_ew": ("cn", list(mk["cn"]["insts"]))}
    cell_cache: dict = {}                      # (市场, 品种) → 175 格日收益，SPY 和 us_ew 共用
    grids_by_group, longs, sums = {}, [], []

    def do_group(g, cells_list, cash, insts):
        grids = group_grids(cells_list, cash)
        bh = group_bh_sharpe(insts, cash)
        grids_by_group[g] = grids
        longs.append(long_table(g, grids))
        for name in ORDER:
            m = surface_metrics(grids[name][0], reps=perm, seed=seed)
            sums.append({"group": g, "strategy": name, "param1": SPECS[name][0], "param2": SPECS[name][1], "bh_sharpe": bh, **m})

    for g, (market, syms) in members.items():
        cl = []
        for s in syms:
            key = (market, s)
            if key not in cell_cache:
                cell_cache[key] = inst_cells(mk[market]["insts"][s])
                log(f"  {market}/{s} 完成 175 次回测（{time.time() - t0:.0f}s）")
            cl.append(cell_cache[key])
        do_group(g, cl, mk[market]["cash"], [mk[market]["insts"][s] for s in syms])
        log(f"{g} 完成（{len(syms)} 个品种，{time.time() - t0:.0f}s）")
    sample = pd.DataFrame()
    if n_stocks > 0:
        stocks, sample = sample_stocks(daily, n_stocks, seed, digests, mk["cn"]["cash"])
        log(f"个股样本 {len(stocks)} 只（{time.time() - t0:.0f}s）")
        if stocks:
            cl = []
            for k, st in enumerate(stocks):
                cl.append(inst_cells(st))
                if (k + 1) % 10 == 0:
                    log(f"  个股 {k + 1}/{len(stocks)}（{time.time() - t0:.0f}s）")
            do_group("cn_stock", cl, mk["cn"]["cash"], stocks)
            log(f"cn_stock 完成（{time.time() - t0:.0f}s）")

    long_df, summary = pd.concat(longs, ignore_index=True), pd.DataFrame(sums)
    # 窗口终点检查：所有格子的日收益都不能晚于 IS_END
    for cells in cell_cache.values():
        for r in cells.values():
            assert r.index[-1] <= pd.Timestamp(IS_END), "有结果超过样本内终点"
    out_dir.mkdir(parents=True, exist_ok=True)
    long_df.to_csv(out_dir / "grid_long.csv", index=False)
    summary.to_csv(out_dir / "surface_metrics.csv", index=False)
    if len(sample):
        sample.to_csv(out_dir / "stock_sample.csv", index=False)
    first_starts = {f"{m}/{s}": str(i.start.date()) for m in mk for s, i in mk[m]["insts"].items()}
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "cutoff": CUTOFF, "is_start": IS_START, "is_end": IS_END,
            "factors": list(FACTORS), "specs": SPECS, "grid_values": {n: {k1: v1, k2: v2} for n in ORDER for k1, v1, k2, v2 in [grid_axes(n)]},
            "warm": WARM, "band": BAND, "perm": perm, "seed": seed, "n_stocks_requested": n_stocks, "n_stocks_used": int(len(sample)),
            "thresholds": {"slope_rho": SLOPE_RHO, "spike_drop": SPIKE_DROP, "plateau_drop": PLATEAU_DROP, "plateau_share": PLATEAU_SHARE, "near_best": NEAR_BEST},
            "window_start_per_instrument": first_starts, "daily_dir": str(daily),
            "versions": {"pandas": pd.__version__, "numpy": np.__version__}, "input_sha256": digests}
    meta["figures"] = make_figures(summary, grids_by_group, out_dir)
    (out_dir / "param_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    (out_dir / "param_stability.md").write_text(markdown(summary, grids_by_group, meta), encoding="utf-8")
    log(f"写出 {out_dir}（{time.time() - t0:.0f}s）")
    return {"long": long_df, "summary": summary, "grids": grids_by_group, "meta": meta}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--n-stocks", type=int, default=0)
    ap.add_argument("--seed", type=int, default=14)
    ap.add_argument("--perm", type=int, default=5000)
    a = ap.parse_args(argv)
    a.out_dir.mkdir(parents=True, exist_ok=True)

    def log(msg):                       # 同时打印并追加到输出目录里的 run.log
        print(msg, flush=True)
        with (a.out_dir / "run.log").open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

    run(a.data_dir, a.out_dir, a.n_stocks, a.seed, a.perm, log=log)


if __name__ == "__main__":
    main()
