"""C12 涨跌停/停牌影响脚本的测试：.venv/bin/python tests/test_limit_lock_impact.py

全部用内存里的合成行情（src.data.synthetic 随机游走，人为塞进一字板）和手算的小例子；
需要写文件的地方只用临时目录，不读写 data/，不联网。
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from config import MARKETS, TRADE_LIST_LIMIT_TOLERANCE  # noqa: E402
from src import data, registry  # noqa: E402
from src.portfolio import apply_band, run_weights  # noqa: E402
from src.trade_list import board_limit, limit_flags  # noqa: E402
import limit_lock_impact as L  # noqa: E402


def make_df(code="600000", n=3200, seed=0, start="2012-01-02", locks=()):
    """合成行情；locks = [(位置, 'up'/'dn')]：把那一根改成一字板（O=H=L=C=昨收×(1±板块幅度)）。"""
    df = data.synthetic(n=n, seed=seed, start=start).loc[:L.CUTOFF].copy()
    lim = board_limit(code)
    for i, kind in locks:
        px = df["Close"].iloc[i - 1] * (1 + lim if kind == "up" else 1 - lim)
        df.iloc[i, df.columns.get_indexer(["Open", "High", "Low", "Close"])] = px
    return df


def calendar_of(df, extra=()):
    return np.sort(np.concatenate([df.index.values, pd.DatetimeIndex(extra).values]))


class LockFlagTests(unittest.TestCase):
    def test_limit_array_follows_board_and_date(self):
        d = pd.DatetimeIndex(["2019-06-03", "2020-08-21", "2020-08-24", "2024-01-02"])
        np.testing.assert_array_equal(L.limit_array("300001", d), [0.10, 0.10, 0.20, 0.20])       # 创业板：注册制之前 10%
        np.testing.assert_array_equal(L.limit_array("301001", d), [0.10, 0.10, 0.20, 0.20])
        np.testing.assert_array_equal(L.limit_array("600000", d), [0.10] * 4)
        np.testing.assert_array_equal(L.limit_array("000001", d), [0.10] * 4)
        np.testing.assert_array_equal(L.limit_array("688001", d), [0.20] * 4)                     # 科创板一直 20%

    def test_vectorized_matches_limit_flags_bar_by_bar(self):
        """向量化版 = 原函数 limit_flags 在每一根 K 线上的结论（用 board_limit 的固定幅度比，同一条规则）。"""
        for code in ("600000", "300001", "688001"):
            lim = board_limit(code)
            df = make_df(code, n=400, locks=[(50, "up"), (51, "dn"), (120, "dn"), (121, "dn"), (300, "up")])
            # 边界：涨幅刚好差 0.1 个百分点（在容差内，仍算涨停）与差 0.5 个百分点（不算）
            for pos, gap in ((200, 0.001), (210, 0.005)):
                px = df["Close"].iloc[pos - 1] * (1 + lim - gap)
                df.iloc[pos, df.columns.get_indexer(["Open", "High", "Low", "Close"])] = px
            up, dn = L.lock_flags(df, np.full(len(df), lim))
            self.assertTrue(up[200] and not up[210])
            self.assertGreaterEqual(int(up.sum() + dn.sum()), 6)
            for i in range(1, len(df)):
                flags = limit_flags(code, df.iloc[i - 1:i + 1])
                self.assertEqual(up[i], any("涨停" in f for f in flags), (code, i))
                self.assertEqual(dn[i], any("跌停" in f for f in flags), (code, i))

    def test_not_one_word_is_not_locked(self):
        """开盘就在涨停价、但盘中有波动（H≠L），不算一字板；第一根没有昨收也不算。"""
        df = make_df(n=100)
        i = 30
        px = df["Close"].iloc[i - 1] * 1.10
        df.iloc[i, df.columns.get_indexer(["Open", "High", "Low", "Close"])] = [px, px, px * 0.98, px]
        up, dn = L.lock_flags(df, np.full(len(df), 0.10))
        self.assertFalse(up[i])
        self.assertFalse(up[0] or dn[0])

    def test_tolerance_is_the_project_constant(self):
        self.assertEqual(TRADE_LIST_LIMIT_TOLERANCE, 0.002)
        df = make_df(n=100)
        i = 40
        for gap, expect in ((0.0019, True), (0.0021, False)):          # 容差 0.002 两侧
            px = df["Close"].iloc[i - 1] * (1 + 0.10 - gap)
            df.iloc[i, df.columns.get_indexer(["Open", "High", "Low", "Close"])] = px
            up, _ = L.lock_flags(df, np.full(len(df), 0.10))
            self.assertEqual(bool(up[i]), expect)


class SuspensionTests(unittest.TestCase):
    def test_gaps_hand_computed(self):
        cal = pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08", "2020-01-09"]).values
        # 这只股票缺 01-03、01-06、01-07（停牌 3 个交易日），01-08 复牌
        mine = pd.to_datetime(["2020-01-02", "2020-01-08", "2020-01-09"]).values
        np.testing.assert_array_equal(L.suspension_gaps(mine, cal), [0, 3, 0])
        np.testing.assert_array_equal(L.suspension_gaps(cal, cal), [0] * 6)


class ExecuteTests(unittest.TestCase):
    OP = np.array([10.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0])

    def run_(self, intended, buy=(), sell=()):
        n = len(intended)
        bb, sb = np.zeros(n, bool), np.zeros(n, bool)
        bb[list(buy)] = True
        sb[list(sell)] = True
        return L.execute(np.array(intended, float), bb, sb, self.OP[:n] if n <= len(self.OP) else np.arange(n) + 10.0)

    def test_blocked_buy_is_delayed_one_bar(self):
        actual, att, blk, runs = self.run_([0, 1, 1, 1, 0, 0], buy=[1])
        np.testing.assert_array_equal(actual, [0, 0, 1, 1, 0, 0])
        np.testing.assert_array_equal(blk, [0, 1, 0, 0, 0, 0])
        self.assertEqual(runs, [(1, 1, 1, 0, 11.0 / 10.0 - 1)])            # 买：原本 10 买，顺延到 11 买，多付 10%

    def test_blocked_sell_for_two_bars(self):
        actual, att, blk, runs = self.run_([0, 1, 1, 0, 0, 0, 0], sell=[3, 4])
        np.testing.assert_array_equal(actual, [0, 1, 1, 1, 1, 0, 0])
        self.assertEqual(len(runs), 1)
        side, start, ln, status, slip = runs[0]
        self.assertEqual((side, start, ln, status), (-1, 3, 2, 0))
        self.assertAlmostEqual(slip, self.OP[5] / self.OP[3] - 1)           # 原本 12 卖，顺延到 14 卖

    def test_lock_on_the_other_side_does_not_block(self):
        """涨停不挡卖出，跌停不挡买入。"""
        actual, *_ = self.run_([0, 1, 1, 0, 0], sell=[1], buy=[3])
        np.testing.assert_array_equal(actual, [0, 1, 1, 0, 0])

    def test_signal_cancelled_while_blocked(self):
        actual, att, blk, runs = self.run_([0, 1, 0, 0, 0], buy=[1])
        np.testing.assert_array_equal(actual, [0] * 5)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][:4], (1, 1, 1, 1))                         # 状态 1 = 取消
        self.assertTrue(np.isnan(runs[0][4]))

    def test_censored_at_end(self):
        actual, att, blk, runs = self.run_([0, 0, 0, 1, 1], buy=[3, 4])
        self.assertEqual(runs[0][:4], (1, 3, 2, 2))                         # 状态 2 = 数据结束仍被卡
        np.testing.assert_array_equal(actual, [0] * 5)

    def test_partial_reduction_and_flip(self):
        actual, att, blk, runs = self.run_([0, 1, 0.5, 0.5, 0.0, 0.0], sell=[2, 3])
        np.testing.assert_array_equal(actual, [0, 1, 1, 1, 0, 0])           # 第 2、3 根都想减仓被挡，第 4 根才清掉
        self.assertEqual(runs[0][:4], (-1, 2, 2, 0))
        # 被挡的卖出之后目标又变成加仓：算取消（方向变了）
        actual, att, blk, runs = self.run_([0, 1, 0.5, 1.0, 1.0], sell=[2])
        self.assertEqual(runs[0][:4], (-1, 2, 1, 1))

    def test_no_blocks_returns_intended(self):
        rng = np.random.default_rng(0)
        intended = rng.choice([0.0, 0.5, 1.0], size=200)
        actual, att, blk, runs = L.execute(intended, np.zeros(200, bool), np.zeros(200, bool), np.arange(200) + 10.0)
        np.testing.assert_array_equal(actual, intended)
        self.assertFalse(blk.any())
        self.assertEqual(runs, [])

    def test_no_lookahead_prefix_unchanged(self):
        """第 i 根的实际仓位只取决于 i 及以前的想要仓位和一字板状态：截断后前缀逐位相同。"""
        rng = np.random.default_rng(3)
        n = 300
        intended = rng.choice([0.0, 0.5, 1.0], size=n)
        bb, sb = rng.random(n) < 0.15, rng.random(n) < 0.15
        op = np.arange(n) + 10.0
        full = L.execute(intended, bb, sb, op)[0]
        for k in (50, 123, 299):
            part = L.execute(intended[:k], bb[:k], sb[:k], op[:k])[0]
            np.testing.assert_array_equal(part, full[:k])


class ReturnPathTests(unittest.TestCase):
    """把 execute 的路径喂给 run_weights：没有一字板时必须与理想成交逐位相同；有一字板时仓位路径就是 execute 的路径。"""

    def setUp(self):
        self.code = "600000"
        self.df = make_df(self.code, locks=[(400, "dn"), (900, "up"), (901, "up")])
        self.cash = pd.Series(0.00005, index=self.df.index)

    def intended_of(self, strat):
        W = registry.target_weights(strat, {self.code: self.df}, self.code, registry.clean_params(strat, {}), MARKETS["cn"])
        banded = apply_band(W.reindex(self.df.index).fillna(0.0).clip(lower=0.0), L.BAND)
        return W, banded.shift(1).fillna(0.0).iloc[:, 0].to_numpy()

    def lock_result(self, W, intended, up, dn):
        actual = L.execute(intended, up, dn, self.df["Open"].to_numpy())[0]
        tgt = pd.DataFrame({self.code: np.append(actual[1:], 0.0)}, index=self.df.index)
        return actual, run_weights({self.code: self.df}, tgt, cost=L.STOCK_COST, band=0.0, cash_ret=self.cash)

    def test_no_locks_equals_ideal_exactly(self):
        zeros = np.zeros(len(self.df), bool)
        for strat in L.ORDER:
            W, intended = self.intended_of(strat)
            ideal = run_weights({self.code: self.df}, W, cost=L.STOCK_COST, band=L.BAND, cash_ret=self.cash, limits=None)
            actual, res = self.lock_result(W, intended, zeros, zeros)
            np.testing.assert_array_equal(actual, intended)
            np.testing.assert_array_equal(res.returns.to_numpy(), ideal.returns.to_numpy())
            np.testing.assert_array_equal(res.weights.iloc[:, 0].to_numpy(), ideal.weights.iloc[:, 0].to_numpy())

    def test_lock_path_is_what_run_weights_holds(self):
        up, dn = L.lock_flags(self.df, L.limit_array(self.code, self.df.index))
        self.assertTrue(up.sum() + dn.sum() >= 3)
        for strat in ("blend2", "trend_ens", "donchian_s1"):
            W, intended = self.intended_of(strat)
            actual, res = self.lock_result(W, intended, up, dn)
            np.testing.assert_array_equal(res.weights.iloc[:, 0].to_numpy(), actual)
            # 被挡的那几根：仓位与前一根相同
            att = np.sign(intended - np.concatenate([[0.0], actual[:-1]]))
            blocked = ((att > 0) & up) | ((att < 0) & dn)
            for i in np.flatnonzero(blocked):
                self.assertEqual(actual[i], actual[i - 1] if i else 0.0)

    def test_intended_path_has_no_lookahead(self):
        """用截断到 T 的行情算出的想要仓位 = 完整行情算出的前缀（信号只用当根收盘及以前）。"""
        for strat in ("blend2", "rsi2", "ma_adx_plus"):
            W, full = self.intended_of(strat)
            T = 1500
            short = self.df.iloc[:T]
            Ws = registry.target_weights(strat, {self.code: short}, self.code, registry.clean_params(strat, {}), MARKETS["cn"])
            part = apply_band(Ws.reindex(short.index).fillna(0.0).clip(lower=0.0), L.BAND).shift(1).fillna(0.0).iloc[:, 0].to_numpy()
            np.testing.assert_allclose(part, full[:T], atol=1e-12)


class EventTests(unittest.TestCase):
    def test_event_counts_hand_computed(self):
        # 6 根；想要仓位 0,1,1,0,1,1：第 1 根买、第 3 根卖、第 4 根买
        intended = np.array([0, 1, 1, 0, 1, 1], float)
        up = np.array([0, 1, 0, 0, 0, 0], bool)           # 第 1 根一字涨停 → 买被卡
        dn = np.array([0, 0, 0, 1, 0, 0], bool)           # 第 3 根一字跌停 → 卖被卡
        susp = np.array([0, 0, 0, 0, 2, 0])               # 第 4 根之前停牌 2 个交易日
        opens = np.array([10, 11, 11, 10, 12, 12], float)
        closes = np.array([10, 10, 11, 10, 10, 12], float)
        in_win = np.ones(6, bool)
        yr = np.zeros(6, int)
        att = np.array([0, 1, 0, -1, 1, 0], np.int8)
        blk = np.array([0, 1, 0, 1, 0, 0], bool)
        ev = L.event_counts(intended, att, blk, up, dn, susp, opens, closes, in_win, yr)
        self.assertEqual((ev["sig_buy"][0], ev["sig_sell"][0]), (2, 1))
        self.assertEqual((ev["buy_lock"][0], ev["sell_lock"][0]), (1, 1))
        self.assertEqual((ev["buy_susp"][0], ev["sell_susp"][0], ev["susp_days_buy"][0]), (1, 0, 2))
        self.assertEqual((ev["blk_days_buy"][0], ev["blk_days_sell"][0]), (1, 1))
        self.assertAlmostEqual(ev["gap_sum_buy_susp"][0], 12 / 10 - 1)       # 第 4 根：开盘 12 / 昨收 10
        self.assertAlmostEqual(ev["gap_sum_buy_free"][0], 11 / 10 - 1)       # 第 1 根：开盘 11 / 昨收 10
        self.assertEqual(ev["bars"][0], 6)
        # 窗口外的不算
        ev2 = L.event_counts(intended, att, blk, up, dn, susp, opens, closes, np.array([0, 0, 0, 1, 1, 1], bool), yr)
        self.assertEqual((ev2["sig_buy"][0], ev2["sig_sell"][0]), (1, 1))

    def test_adverse_sign(self):
        self.assertAlmostEqual(L.adverse(1, 0.10), 0.10)        # 买贵了 10%：不利 +10%
        self.assertAlmostEqual(L.adverse(-1, -0.10), 0.10)      # 卖低了 10%：不利 +10%
        self.assertAlmostEqual(L.adverse(-1, 0.05), -0.05)      # 卖高了：有利

    def test_cagr_and_bootstrap(self):
        self.assertAlmostEqual(L.cagr([0.10, -0.10]), (1.1 * 0.9) ** 126 - 1)
        lo, hi = L.boot_mean_ci([0.5] * 20, reps=50, seed=1)
        self.assertAlmostEqual(lo, 0.5)
        self.assertAlmostEqual(hi, 0.5)
        a = L.boot_mean_ci(np.arange(30.0), reps=100, seed=2)
        self.assertEqual(a, L.boot_mean_ci(np.arange(30.0), reps=100, seed=2))
        self.assertLess(a[0], 14.5)
        self.assertGreater(a[1], 14.5)
        self.assertTrue(np.isnan(L.boot_mean_ci([1.0], reps=10, seed=1)[0]))


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.code = "600000"
        self.df = make_df(self.code, locks=[(500, "dn"), (501, "dn"), (700, "up"), (1200, "dn")])
        # 日历 = 这只股票的日期 + 4 个它缺的日子（停牌）。删掉 4 个中间日期模拟停牌
        self.drop = self.df.index[[800, 801, 802, 1500]]
        self.df = self.df.drop(self.drop)
        self.cal = calendar_of(self.df, self.drop)
        self.master = self.cal[self.cal >= np.datetime64(L.WIN_START)]
        self.cash = pd.Series(0.00005, index=self.df.index)

    def test_panel_runs_and_is_consistent(self):
        r = L.stock_panel(self.df, self.code, "测试", self.cash, self.cal, self.master)
        self.assertEqual(r["check"][1], 0)                          # limit_flags 抽样核对：不一致为 0
        self.assertGreater(r["check"][0], 0)
        self.assertEqual(r["not_in_cal"], 0)
        self.assertEqual(len(r["pos"]), r["n_days"])
        self.assertLessEqual(pd.Timestamp(r["start"]), self.df.index[-1])
        self.assertGreaterEqual(pd.Timestamp(r["start"]), pd.Timestamp(L.WIN_START))
        for s in L.ORDER:
            for k in L.SCEN:
                self.assertEqual(len(r["rets"][(s, k)]), r["n_days"])
        events = dict(r["events"])
        n_susp = sum(int(events[s]["buy_susp"].sum() + events[s]["sell_susp"].sum()) for s in L.ORDER)
        self.assertGreaterEqual(n_susp, 0)
        # 一字板 K 线数：合成数据里我们塞了 3 根跌停/涨停之外的随机波动几乎不会产生一字板
        self.assertEqual(int(events["_all"]["up_days"].sum()), 1)
        self.assertEqual(int(events["_all"]["dn_days"].sum()), 3)

    def test_ideal_minus_lock_is_zero_without_hits_and_only_locks_matter(self):
        clean = make_df(self.code)
        cal = calendar_of(clean)
        r = L.stock_panel(clean, self.code, "测试", pd.Series(0.00005, index=clean.index), cal, cal[cal >= np.datetime64(L.WIN_START)])
        for s in L.ORDER:
            np.testing.assert_array_equal(r["rets"][(s, "ideal")], r["rets"][(s, "lock")])
            np.testing.assert_array_equal(r["rets"][(s, "ideal")], r["rets"][(s, "lock_buy")])
            np.testing.assert_array_equal(r["rets"][(s, "ideal")], r["rets"][(s, "lock_sell")])
        self.assertEqual(r["runs"], [])

    def test_buy_only_and_sell_only_change_different_things(self):
        r = L.stock_panel(self.df, self.code, "测试", self.cash, self.cal, self.master)
        for s in L.ORDER:
            e = dict(r["events"])[s]
            if e["buy_lock"].sum() == 0:
                np.testing.assert_array_equal(r["rets"][(s, "lock_buy")], r["rets"][(s, "ideal")])
            if e["sell_lock"].sum() == 0:
                np.testing.assert_array_equal(r["rets"][(s, "lock_sell")], r["rets"][(s, "ideal")])

    def test_rejects_data_after_cutoff(self):
        long_df = data.synthetic(n=3900, seed=0, start="2012-01-02")
        self.assertGreater(long_df.index[-1], pd.Timestamp(L.CUTOFF))
        cal = calendar_of(long_df)
        with self.assertRaises(ValueError):
            L.stock_panel(long_df, "600000", "测试", pd.Series(0.0, index=long_df.index), cal, cal)

    def test_too_short_history_is_skipped(self):
        short = make_df(n=L.MIN_BARS - 1)
        cal = calendar_of(short)
        self.assertIsNone(L.stock_panel(short, "600000", "测试", pd.Series(0.0, index=short.index), cal, cal))


class AggregationTests(unittest.TestCase):
    def build(self):
        res, rows = [], []
        for k, code in enumerate(("600000", "000001", "300001")):
            df = make_df(code, seed=k, locks=[(500, "dn"), (900, "up")])
            cal = calendar_of(df)
            master = cal[cal >= np.datetime64(L.WIN_START)]
            res.append(L.stock_panel(df, code, "测试", pd.Series(0.00005, index=df.index), cal, master))
            rows.append({"code": code, "adv_q": f"Q{k + 1}", "board": "沪市主板" if k == 0 else "深市主板", "adv": float(k + 1)})
        return res, pd.DataFrame(rows), pd.DatetimeIndex(master)

    def test_tables_shapes_and_sums(self):
        res, meta, master = self.build()
        groups = L.group_defs(meta)
        self.assertEqual(set(groups["ALL"]), {"600000", "000001", "300001"})
        self.assertEqual(list(groups["ADV_Q1"]), ["600000"])
        self.assertIn("板块_深市主板", groups)
        sums, counts = L.pool_returns(res, groups, len(master))
        # 三只同日历的股票：每天的股票数最多 3；ALL 的等权日收益 = 三只日收益的平均
        self.assertEqual(counts["ALL"].max(), 3)
        d = int(np.flatnonzero(counts["ALL"] == 3)[100])
        manual = np.mean([r["rets"][("blend2", "ideal")][list(r["pos"]).index(d)] for r in res])
        self.assertAlmostEqual(sums[("ALL", "blend2", "ideal")][d] / 3, manual)
        t = L.build_tables(res, meta, master, boot=0, seed=1)
        ev = t["events"]
        # 事件表里 ALL 的卖出信号数 = 三只股票的合计
        want = sum(float(dict(r["events"])["blend2"]["sig_sell"].sum()) for r in res)
        self.assertEqual(ev[(ev.group == "ALL") & (ev.strategy == "blend2")].sig_sell.sum(), want)
        es = L.event_summary(ev)
        self.assertTrue((es.sell_lock <= es.sig_sell).all() and (es.buy_lock <= es.sig_buy).all())
        rc = L.return_compare(t["per_stock"], sums, counts, master, boot=50, seed=1, groups=groups)
        self.assertEqual(len(rc), len(groups) * len(L.ORDER))
        row = rc[(rc.group == "ALL") & (rc.strategy == "blend2")].iloc[0]
        self.assertEqual(row.n_stocks, 3)
        ps = t["per_stock"][t["per_stock"].strategy == "blend2"]
        self.assertAlmostEqual(row.diff_lock_mean_pp, ((ps.cagr_ideal - ps.cagr_lock) * 100).mean())
        recent = L.return_compare(t["per_stock"], sums, counts, master, boot=0, seed=1, groups=groups, since=L.RECENT_START)
        self.assertEqual(len(recent), len(rc))
        by = L.by_year_table(sums, counts, master, groups)
        self.assertEqual(set(by.year) - set(L.YEARS), set())
        self.assertTrue(np.isfinite(by.diff_pp).all())
        lf = L.lock_frequency(ev)
        self.assertGreater(lf[lf.group == "ALL"].bars.iloc[0], 0)
        rs = L.run_summary(t["runs"], groups)
        self.assertEqual(len(rs), len(groups) * (len(L.ORDER) + 1) * 2)
        md = L.markdown({**t, "event_summary": es, "event_summary_recent": L.event_summary(ev, since_year=2020),
                         "lock_frequency": lf, "return_compare": rc, "return_compare_recent": recent, "run_summary": rs, "by_year": by},
                        {"generated": "x", "n_stocks_used": 3, "seed": 1})
        self.assertIn("理想成交", md)

    def test_all_dates_not_after_cutoff(self):
        res, meta, master = self.build()
        self.assertLessEqual(master.max(), pd.Timestamp(L.CUTOFF))
        for r in res:
            self.assertLessEqual(pd.Timestamp(r["start"]), pd.Timestamp(L.CUTOFF))


class UniverseTests(unittest.TestCase):
    def test_universe_excludes_st_and_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            daily = Path(tmp)
            (daily / "cn_all").mkdir()
            rows = [("600000", "浦发银行"), ("000001", "平安银行"), ("600001", "*ST甲"), ("000002", "乙退"), ("300001", "丙"), ("600009", "无文件")]
            pd.DataFrame(rows, columns=["code", "name"]).assign(ipo="", industry="", board="").to_csv(daily / "cn_all_list.csv", index=False)
            for c, _ in rows[:-1]:
                make_df(c, n=50).to_parquet(daily / "cn_all" / f"{c}.parquet")
            full = L.load_universe(daily, 0, 6)
            self.assertEqual(list(full["code"]), ["000001", "000002", "300001", "600000"])
            a = L.load_universe(daily, 2, 6)
            b = L.load_universe(daily, 2, 6)
            self.assertEqual(list(a["code"]), list(b["code"]))
            self.assertEqual(len(a), 2)
            self.assertTrue(set(a["code"]) <= set(full["code"]))

    def test_assign_groups_quintiles(self):
        adv_all = pd.Series(np.arange(1.0, 101.0), index=[f"{i:06d}" for i in range(100)])
        g = L.assign_groups(["000000", "000030", "000099", "999999"], {"000000": "创业板"}, adv_all, adv_all)
        self.assertEqual(list(g.adv_q), ["Q1", "Q2", "Q5", "NA"])
        self.assertEqual(g.board.iloc[0], "创业板")
        self.assertEqual(g.board.iloc[1], "NA")
        # 最小 5%：1..100 的 5% 分位 = 5.95，ADV 为 1 的算，ADV 为 31 的不算，没有 ADV 的不算
        g2 = L.assign_groups(["000000", "000030", "999999"], {}, adv_all, adv_all)
        self.assertEqual(list(g2.p5), [True, False, False])
        groups = L.group_defs(g2.assign(board="沪市主板"))
        self.assertEqual(list(groups["ADV_最小5%"]), ["000000"])


class ConstantsTests(unittest.TestCase):
    def test_constants(self):
        from src.scan import STRATS
        self.assertEqual(sorted(L.ORDER), sorted(STRATS))
        self.assertEqual(len(L.ORDER), 7)
        self.assertEqual(L.CUTOFF, "2026-03-31")
        self.assertEqual(L.STOCK_COST, 0.0008)
        self.assertEqual(L.BAND, 0.10)
        self.assertEqual(L.SCEN, ["ideal", "lock", "lock_buy", "lock_sell", "project"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
