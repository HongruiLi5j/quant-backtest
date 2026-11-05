"""C05 未来函数自动检测：.venv/bin/python tests/test_no_lookahead_all.py

覆盖面：7 个正式策略（src/scan.py 的 STRATS，和全市场扫描、信号账本同一份清单）+ src/shadow.py 的全部影子候选
（18 个真策略 + 3 个随机对照）。额外加测 dual_momentum（多品种，登记表里有但不在 7 个里）和 RQAlpha 流式版。

两条规则，分别检查：
  1. 信号只能用「当根收盘及以前」的数据——在第 t 天把 t+1 以后的数据全部删掉重算，
     t 天及以前每一天的信号必须和用全量数据算出来的逐位一致（比的是整段历史，不只是第 t 天）；
     再做一个“篡改未来”的变体：不删，而是把 t+1 以后的价格乱改，t 天及以前的信号同样不许变。
  2. 成交在信号的下一根 K 线：第 t 天收盘定的目标仓位，第 t+1 天开盘才成交（第 t 天的收益不许受它影响）。

全部用合成行情（固定随机种子），不读写 data/，不联网。
没有引入 tests/realdata_guard.py：它会对 ../ai_layer 等外部登记文件算哈希，本测试根本不碰任何文件，用不着。
发现违反时，失败信息会写明具体策略和第几天；只报告，不改策略代码（策略改动要同步三引擎，必须站主决定）。
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import MARKETS  # noqa: E402
from src import allmarket, data, registry, shadow as SH, strategies as S  # noqa: E402
from src.portfolio import apply_band, run_weights  # noqa: E402
from src.scan import COST, STRATS as FORMAL  # noqa: E402

N = 1000                 # 每条合成行情的长度（交易日）
N_CUTS = 20              # 每条行情抽多少个截断日 t
TOL = 1e-12              # 浮点容差；实测截断前后是逐位一致的（差 = 0），留这点容差只是防不同平台的末位舍入
OHLC = ["Open", "High", "Low", "Close"]
MCFG = MARKETS["cn"]


# ---------------------------------------------------------------- 合成行情
def regime_series(seed: int, n: int = N) -> pd.DataFrame:
    """分段切换行情的随机游走：牛市 / 急跌 / 震荡 / 慢牛轮着来，每段 125 天。
    纯随机游走里 RSI2、均线交叉这类策略大部分时间空仓，截断检验就成了“空对空”；分段行情让每个策略都有真实的进出场。"""
    rng = np.random.default_rng(seed)
    regimes = [(0.0010, 0.010), (-0.0015, 0.024), (0.0, 0.008), (0.0008, 0.014)]
    seg = [regimes[(i + seed) % len(regimes)] for i in range(n // 125 + 1)]
    mu = np.repeat([m for m, _ in seg], 125)[:n]
    sd = np.repeat([s for _, s in seg], 125)[:n]
    close = 100 * np.exp(np.cumsum(rng.normal(mu, sd)))
    open_ = close * np.exp(rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, 0.006, n)))
    idx = pd.bdate_range("2019-01-01", periods=n, name="Date")
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                         "Volume": rng.integers(1_000_000, 5_000_000, n)}, index=idx)


def make_series() -> dict[str, pd.DataFrame]:
    """三条走势不同的行情：一条纯随机游走（data.synthetic）+ 两条分段行情。"""
    return {"rw11": data.synthetic(n=N, seed=11, start="2019-01-01"),
            "rg12": regime_series(12), "rg13": regime_series(13)}


def pick_cuts(n: int, seed: int, k: int = N_CUTS) -> list[int]:
    """抽 k 个截断日（下标 t，保留 0..t）：k-1 个随机的（从第 100 天起，故意包含指标预热期里的几天，
    NaN 位置也要一致），再固定加上最后一个能算“下一根”的 n-2。随机种子固定，结果可重复。"""
    rng = np.random.default_rng(2026 + seed)
    cuts = rng.choice(np.arange(100, n - 2), size=k - 1, replace=False).tolist() + [n - 2]
    return sorted(int(t) for t in cuts)


# ---------------------------------------------------------------- 被检验的信号函数
def formal_fn(name: str):
    """正式策略：走和全市场扫描完全相同的入口 registry.target_weights（默认参数）。"""
    return lambda df: registry.target_weights(name, {"X": df}, "X", registry.clean_params(name, {}), MCFG)["X"]


def shadow_fn(name: str):
    fn = SH.CANDIDATES[name][2]
    return lambda df: fn(df, "600000")


SIGNALS = {**{f"formal:{n}": formal_fn(n) for n in FORMAL},
           **{f"shadow:{n}": shadow_fn(n) for n in SH.CANDIDATES}}


# ---------------------------------------------------------------- 检测器
def frames(x):
    """单个 DataFrame 或 {品种: DataFrame} 都统一成列表，方便截断 / 篡改。"""
    return list(x.values()) if isinstance(x, dict) else [x]


def cut_data(x, t: int):
    """删掉 t+1 以后的所有数据（保留 0..t）。"""
    return {k: v.iloc[:t + 1] for k, v in x.items()} if isinstance(x, dict) else x.iloc[:t + 1]


def scramble_future(x, t: int, seed: int = 0):
    """篡改未来：t+1 以后每天的 开高低收 同乘一个随机因子（保持 高>=低 的一致性），t 及以前原封不动。"""
    def one(df, salt):
        out = df.copy()
        k = np.random.default_rng(seed * 1000 + t + salt).lognormal(0, 0.3, len(df) - t - 1)
        out.iloc[t + 1:, [out.columns.get_loc(c) for c in OHLC]] = df.iloc[t + 1:][OHLC].to_numpy() * k[:, None]
        return out
    if isinstance(x, dict):
        return {key: one(v, i) for i, (key, v) in enumerate(x.items())}
    return one(x, 0)


def prefix_violations(fn, x, cuts, mutate=cut_data):
    """对每个截断日 t：重算信号，和全量信号的 0..t 段比较。返回 (违反列表, 最大绝对差)。
    违反 = (t, 首个不一致的日期, 全量值, 截断值)。mutate=cut_data 是“删掉未来”，=scramble_future 是“篡改未来”。"""
    full = fn(x)
    index = frames(x)[0].index
    bad, worst = [], 0.0
    for t in cuts:
        part = fn(mutate(x, t))
        a = np.asarray(full.iloc[:t + 1], float)
        b = np.asarray(part.iloc[:t + 1], float)
        if a.shape != b.shape:
            bad.append((t, "形状不同", a.shape, b.shape))
            continue
        nan_diff = np.isnan(a) != np.isnan(b)
        diff = np.where(np.isnan(a) | np.isnan(b), 0.0, np.abs(a - b))
        worst = max(worst, float(diff.max()) if diff.size else 0.0)
        wrong = nan_diff | (diff > TOL)
        if wrong.any():
            row = int(np.flatnonzero(wrong.reshape(len(a), -1).any(axis=1))[0])
            bad.append((t, str(index[row].date()), float(np.ravel(a[row])[0]), float(np.ravel(b[row])[0])))
    return bad, worst


def fill_violations(execute, df: pd.DataFrame, cuts: list[int]):
    """成交在下一根 K 线的“脉冲检验”：只在第 t 天收盘给一次满仓目标，其余日子目标全 0，零费用。
    正确的执行是：第 t 天没有仓位，收益 = 0；
                  第 t+1 天按开盘价买入，收益 = 收盘/开盘 - 1；
                  第 t+2 天按开盘价卖出，收益 = 开盘/前收 - 1（隔夜段）；其余日子收益全为 0。
    execute(df, target) -> 每日收益 Series。返回违反列表 (t, 哪天, 实际, 应该)。"""
    o, c = df["Open"].to_numpy(), df["Close"].to_numpy()
    bad = []
    for t in cuts:
        if t + 2 >= len(df):
            continue
        tgt = pd.Series(0.0, index=df.index)
        tgt.iloc[t] = 1.0
        r = np.asarray(execute(df, tgt), float)
        want = np.zeros(len(df))
        want[t + 1] = c[t + 1] / o[t + 1] - 1
        want[t + 2] = o[t + 2] / c[t + 1] - 1
        for i in np.flatnonzero(np.abs(r - want) > TOL):
            bad.append((t, str(df.index[i].date()), float(r[i]), float(want[i])))
    return bad


def real_execute(df, tgt):
    return run_weights({"X": df}, tgt.rename("X"), cost=0.0).returns


def describe(bad) -> str:
    return "；".join(f"截断日 t={b[0]}（{b[1]}）全量={b[2]} 截断后={b[3]}" for b in bad[:3]) + (f" …共 {len(bad)} 处" if len(bad) > 3 else "")


# ---------------------------------------------------------------- 1. 信号不用未来数据
class SignalNoLookaheadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = make_series()
        cls.cuts = {k: pick_cuts(N, i) for i, k in enumerate(cls.series)}

    def test_universe_is_complete(self):
        """清单没漏：正式 7 个、影子 18 个真策略 + 3 个随机对照，且正式清单和三引擎共用的 engines.common.STRATS 一致。"""
        from engines import common
        self.assertEqual(len(FORMAL), 7)
        self.assertEqual(sorted(FORMAL), sorted(common.STRATS))
        self.assertEqual(len(SH.CANDIDATES), 21)
        self.assertEqual(len(SH.CONTROLS), 3)
        self.assertEqual(len(SIGNALS), 28)
        self.assertTrue(all(len(c) == N_CUTS for c in self.cuts.values()))

    def test_truncate_future_signals_unchanged(self):
        """删掉 t+1 以后的数据重算：t 天及以前的信号和全量数据逐位一致。"""
        for key, fn in SIGNALS.items():
            for sname, df in self.series.items():
                with self.subTest(strategy=key, series=sname):
                    bad, _ = prefix_violations(fn, df, self.cuts[sname])
                    self.assertEqual(bad, [], f"{key} 在序列 {sname} 用到了未来数据：{describe(bad)}")

    def test_scrambled_future_signals_unchanged(self):
        """把 t+1 以后的价格乱改：t 天及以前的信号不许变（和“删掉未来”互相印证，抓得到只在长度变化时才露馅的写法）。"""
        for key, fn in SIGNALS.items():
            for sname, df in self.series.items():
                with self.subTest(strategy=key, series=sname):
                    bad, _ = prefix_violations(fn, df, self.cuts[sname][::2], mutate=scramble_future)
                    self.assertEqual(bad, [], f"{key} 在序列 {sname} 被篡改的未来数据影响：{describe(bad)}")

    def test_signals_are_not_vacuous(self):
        """防“空对空”：每个策略在三条行情上总共要有足够多的持仓天数和换仓次数，否则上面的检验什么也没验证。"""
        for key, fn in SIGNALS.items():
            days = changes = 0
            for df in self.series.values():
                w = fn(df).astype(float).fillna(0.0).to_numpy()
                days += int((w > 0).sum())
                changes += int((np.diff(w) != 0).sum())
            with self.subTest(strategy=key):
                self.assertGreaterEqual(days, 10, f"{key} 三条行情总共只有 {days} 天有仓位")
                self.assertGreaterEqual(changes, 4, f"{key} 三条行情总共只换仓 {changes} 次")

    def test_dual_momentum_no_lookahead(self):
        """加测：多品种的双动量（按月末调仓，最后一天是否算月末的判断最容易写出未来函数）。"""
        prices = {s: data.synthetic(n=N, seed=20 + i, start="2019-01-01") for i, s in enumerate(["SPY", "EFA", "IEF", "SHY"])}
        fn = lambda p: S.dual_momentum(p, lookback=126)  # noqa: E731  回看改短，保证 1000 天里有足够多次调仓
        cuts = pick_cuts(N, 99)
        for mutate in (cut_data, scramble_future):
            bad, _ = prefix_violations(fn, prices, cuts, mutate=mutate)
            self.assertEqual(bad, [], f"dual_momentum 用到了未来数据：{describe(bad)}")
        self.assertGreater(int((np.diff(fn(prices).to_numpy(), axis=0) != 0).any(axis=1).sum()), 5)

    def test_rq_streaming_version_matches_inhouse(self):
        """加测：RQAlpha 版是逐根喂数据的流式写法（结构上看不到未来），但要和自研版规则一致——合成行情上目标仓位逐天相同。"""
        from engines import common, rq_strategies
        for sname, df in self.series.items():
            for name in common.STRATS:
                s = rq_strategies.STRATEGY_CLASSES[name](**common.parse_params(name, {}))
                rq = np.nan_to_num(np.array([s.on_bar(o, h, l, c) for o, h, l, c in df[OHLC].itertuples(index=False)], float))
                mine = SIGNALS[f"formal:{name}"](df).fillna(0.0).to_numpy(float)
                days = np.flatnonzero(np.abs(rq - mine) > 1e-9)
                self.assertEqual(len(days), 0, f"{name}（序列 {sname}）RQAlpha 版和自研版第 {[str(df.index[i].date()) for i in days[:3]]} 天仓位不同")


# ---------------------------------------------------------------- 2. 成交在下一根 K 线
class FillNextBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.series = make_series()
        cls.cuts = {k: pick_cuts(N, i) for i, k in enumerate(cls.series)}

    def test_pulse_fills_at_next_open(self):
        """脉冲检验（见 fill_violations）：第 t 天收盘的目标，第 t+1 天开盘成交，第 t+2 天开盘平掉。"""
        for sname, df in self.series.items():
            bad = fill_violations(real_execute, df, self.cuts[sname])
            self.assertEqual(bad, [], f"序列 {sname} 成交不在下一根：{describe(bad)}")

    def test_formal_positions_lag_signal_by_one_bar(self):
        """7 个正式策略：实际持仓（开盘成交后）= 昨天收盘的目标仓位；用生产参数（费用、10% 调仓带）和零费用各检查一遍。"""
        for name in FORMAL:
            for sname, df in self.series.items():
                w = SIGNALS[f"formal:{name}"](df).to_frame("X")
                for label, kw in (("零费用", {"cost": 0.0, "band": 0.0}), ("生产参数", {"cost": COST, "band": 0.10})):
                    with self.subTest(strategy=name, series=sname, mode=label):
                        held = run_weights({"X": df}, w, **kw).weights["X"].to_numpy()
                        tgt = apply_band(w.fillna(0.0).clip(lower=0.0), kw["band"])["X"].to_numpy()
                        want = np.r_[0.0, tgt[:-1]]
                        days = np.flatnonzero(np.abs(held - want) > TOL)
                        self.assertEqual(len(days), 0, f"{name}（{sname}、{label}）第 {[str(df.index[i].date()) for i in days[:3]]} 天的持仓不是昨天收盘定的目标")

    def test_formal_backtest_prefix_invariant(self):
        """端到端：信号 + 成交 + 费用 + 调仓带 + 涨跌停 + 现金利息整条链路，删掉 t+1 以后的数据后，到 t+1 为止的每日收益不变。
        （用和 scan.scan_one 相同的参数；t+1 是 t 天收盘信号第一次成交的那天，必须包含在内。）"""
        for name in FORMAL:
            for sname, df in self.series.items():
                cash = pd.Series(0.0001, index=df.index)

                def backtest(d):
                    w = registry.target_weights(name, {"X": d}, "X", registry.clean_params(name, {}), MCFG)
                    return run_weights({"X": d}, w, cost=COST, band=0.10, cash_ret=cash.reindex(d.index), limits={"X": 0.10}).returns
                full = backtest(df).to_numpy()
                for t in self.cuts[sname][::2]:
                    part = backtest(df.iloc[:t + 2]).to_numpy()
                    days = np.flatnonzero(np.abs(full[:t + 2] - part) > TOL)
                    with self.subTest(strategy=name, series=sname, t=t):
                        self.assertEqual(len(days), 0, f"{name}（{sname}）截断在第 {t} 天，回测收益第 {[str(df.index[i].date()) for i in days[:3]]} 天变了")

    def test_dual_momentum_fills_next_bar(self):
        prices = {s: data.synthetic(n=N, seed=20 + i, start="2019-01-01") for i, s in enumerate(["SPY", "EFA", "IEF", "SHY"])}
        w = S.dual_momentum(prices, lookback=126)
        held = run_weights(prices, w, cost=0.0, band=0.0).weights
        want = w.clip(lower=0.0).shift(1).fillna(0.0)
        self.assertTrue(np.allclose(held.to_numpy(), want.to_numpy(), atol=TOL), "dual_momentum 持仓不是昨天收盘定的目标")

    def test_shadow_tomorrow_becomes_today(self):
        """影子通道（账本用“今日仓位 → 明日仓位”两栏）：第 D 天记的“明日仓位”，必须等于第 D+1 天记的“今日仓位”——
        明日仓位就是第 D+1 天开盘要成交的那一份。走真实的 shadow.compute_code（只把读文件换成合成行情），带真实的 tail(420) 滑动窗口。"""
        df = self.series["rg12"]
        days = [d for d in pick_cuts(N, 7) if d >= SH.MIN_ROWS]

        def ledger_rows(upto: int):
            d = df.iloc[:upto + 1]
            with mock.patch.object(allmarket, "load", return_value=d), \
                    mock.patch.object(allmarket, "_drop_unfinished", side_effect=lambda x: x):
                return {r[0]: (r[2], r[3]) for r in SH.compute_code(("600000", str(d.index[-1].date())))}

        for D in days:
            today, tomorrow = ledger_rows(D), ledger_rows(D + 1)
            self.assertEqual(sorted(today), sorted(SH.CANDIDATES))
            for name in SH.CANDIDATES:
                with self.subTest(strategy=name, D=D):
                    self.assertAlmostEqual(today[name][1], tomorrow[name][0], places=12,
                                           msg=f"影子 {name}：{df.index[D].date()} 记的明日仓位 ≠ 次日记的今日仓位")


# ---------------------------------------------------------------- 3. A 股盘中半天 K 线
class UnfinishedBarTests(unittest.TestCase):
    def test_drop_unfinished_before_1530(self):
        """北京时间 15:30 之前，当天（及以后）的 K 线不能用；15:30 起当天的才算收完。时间用 mock，不依赖真实时钟。"""
        bj = ZoneInfo("Asia/Shanghai")
        df = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0]}, index=pd.to_datetime(["2024-06-10", "2024-06-11", "2024-06-12", "2024-06-13"]))
        for (h, m), last in (((15, 29), "2024-06-11"), ((15, 30), "2024-06-12"), ((9, 31), "2024-06-11"), ((23, 0), "2024-06-12")):
            with mock.patch.object(allmarket, "_now_bj", return_value=datetime(2024, 6, 12, h, m, tzinfo=bj)):
                self.assertEqual(str(allmarket._drop_unfinished(df).index[-1].date()), last, f"{h}:{m:02d}")


# ---------------------------------------------------------------- 4. 检测器自检：故意写的“作弊”必须被抓到
def leak_next_close(df):
    c = df["Close"]
    return (c.shift(-1) > c).astype(float)                      # 明天涨就今天持有：偷看明天收盘


def leak_centered_ma(df):
    c = df["Close"]
    return (c > c.rolling(21, center=True, min_periods=1).mean()).astype(float)   # 居中均线：用到了未来 10 天


def leak_full_sample_scale(df):
    c = df["Close"]
    return ((c - c.min()) / (c.max() - c.min()) > 0.5).astype(float)              # 用全样本最高最低价做归一化


def leak_peek_last_row(df):
    return pd.Series(float(df["Close"].iloc[-1] > df["Close"].mean()), index=df.index)   # 用到“数据的最后一行”


class DetectorSelfTests(unittest.TestCase):
    """检测器本身要灵：如果连故意作弊的都抓不到，上面全绿就没有意义。"""

    @classmethod
    def setUpClass(cls):
        cls.df = regime_series(12)
        cls.cuts = pick_cuts(N, 1)

    def test_planted_signal_leaks_are_caught(self):
        for leak in (leak_next_close, leak_centered_ma, leak_full_sample_scale, leak_peek_last_row):
            for mutate in (cut_data, scramble_future):
                bad, _ = prefix_violations(leak, self.df, self.cuts, mutate=mutate)
                self.assertTrue(bad, f"{leak.__name__} 没被抓到（{mutate.__name__}）")

    def test_honest_signal_passes(self):
        honest = lambda d: (d["Close"] > d["Close"].rolling(50).mean()).astype(float)  # noqa: E731
        for mutate in (cut_data, scramble_future):
            self.assertEqual(prefix_violations(honest, self.df, self.cuts, mutate=mutate)[0], [])

    def test_planted_execution_leaks_are_caught(self):
        def same_bar(df, tgt):        # 第 t 天的目标当天就吃日内涨跌
            return tgt * (df["Close"] / df["Open"] - 1)

        def fill_at_signal_close(df, tgt):   # 按信号当天收盘价成交：第 t+1 天吃“收盘/前收”而不是“收盘/开盘”
            return tgt.shift(1).fillna(0.0) * (df["Close"] / df["Close"].shift(1) - 1).fillna(0.0)

        for fake in (same_bar, fill_at_signal_close):
            self.assertTrue(fill_violations(fake, self.df, self.cuts), f"{fake.__name__} 没被抓到")
        self.assertEqual(fill_violations(real_execute, self.df, self.cuts), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
