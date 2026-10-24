"""scripts/audit_recompute.py 单测(T011 / S1):只用合成数据和临时目录,不读写 data/。
运行:python3 -B tests/test_audit_recompute.py
"""
import contextlib
import csv
import datetime as dt
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("audit_recompute", ROOT / "scripts" / "audit_recompute.py")
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)

from src.data import synthetic


def write_csv(path, df, volume=True):
    cols = ["Open", "High", "Low", "Close"] + (["Volume"] if volume else [])
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date"] + cols)
        for d, r in df.iterrows():
            w.writerow([d.strftime("%Y-%m-%d")] + [repr(float(r[c])) for c in cols])


def run_main(argv):
    err, out = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        code = ar.main(argv)
    return code, err.getvalue()


def compare_args(**kw):
    base = ["--prices", "x", "--strategy", "ma_adx_original", "--out", "y"]
    a = ar.parse_args(base)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.df = synthetic(n=300, seed=2)
        self.csv = self.dir / "p.csv"
        write_csv(self.csv, self.df)

    def tearDown(self):
        self.tmp.cleanup()

    def prices(self):
        return ar.read_prices(self.csv)

    def entry_dates(self, strategy="ma_adx_original"):
        p = self.prices()
        sim = ar.simulate(p, ar.RECOMPUTE_SIGNALS[strategy](p), buy_cost=5e-4, sell_cost=5e-4,
                          engine_cost=5e-4, limit_pct=0.1, limit_mode="exact", honor_susp=True,
                          lag=1, fee_mode="notional")
        return [t["entry"] for t in sim["trades"]]

    def compare(self, strategy="ma_adx_original", **kw):
        p = self.prices()
        return ar.compare(p, strategy, compare_args(**kw), "SAMPLE")

    def edit(self, day, **cols):
        """改某一天的价格(只改 Open/Volume 等,不动 Close,信号不变)后重写 CSV。"""
        ts = dt.datetime.combine(day, dt.time())
        for k, v in cols.items():
            self.df.loc[ts, k] = v
        write_csv(self.csv, self.df)


class TestNoDifference(Base):
    def test_ma_adx_matches_engine_exactly(self):
        res = self.compare(limit=0.0)
        self.assertGreater(len(res["engine"]["trades"]), 0)
        self.assertFalse(any(r["diff"] for r in res["rows"]))
        self.assertLess(max(res["d_eq"]), 1e-9)

    def test_signals_match_engine_signals(self):
        """独立实现的信号与引擎 strategies 逐日一致(三个策略)。"""
        from src import strategies
        p = self.prices()
        df = synthetic(n=300, seed=2)
        for name in ar.STRATEGIES:
            eng = getattr(strategies, name)(df).to_numpy()
            mine = ar.RECOMPUTE_SIGNALS[name](p)
            self.assertLess(max(abs(a - b) for a, b in zip(eng, mine)), 1e-9, name)

    def test_rsi2_has_signals_and_matches_engine(self):
        """rsi2 需要 ≥ 260 根且样本要真的出信号,否则对账是空的。seed=3、500 根有 57 个持仓日。"""
        from src import strategies
        self.df = synthetic(n=500, seed=3)
        write_csv(self.csv, self.df)
        p = self.prices()
        eng = strategies.rsi2_reversion(self.df).to_numpy()
        mine = ar.RECOMPUTE_SIGNALS["rsi2_reversion"](p)
        self.assertGreater(sum(mine), 0)
        self.assertLess(max(abs(a - b) for a, b in zip(eng, mine)), 1e-9)
        res = self.compare("rsi2_reversion", limit=0.0)
        self.assertGreater(len(res["engine"]["trades"]), 0)
        self.assertFalse(any(r["diff"] for r in res["rows"]))
        self.assertLess(max(res["d_eq"]), 1e-9)

    def test_main_writes_report_and_refuses_overwrite(self):
        out = self.dir / "r.md"
        code, _ = run_main(["--prices", str(self.csv), "--strategy", "ma_adx_original", "--out", str(out)])
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        for key in ("逐笔交易对比", "逐日净值", "T+1", "预注册"):
            self.assertIn(key, text)
        before = out.read_text(encoding="utf-8")
        code, err = run_main(["--prices", str(self.csv), "--strategy", "ma_adx_original", "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertIn("拒绝覆盖", err)
        self.assertEqual(out.read_text(encoding="utf-8"), before)


class TestDifferences(Base):
    def test_t1_lag_difference(self):
        res = self.compare(limit=0.0, lag=2)
        diffs = [r for r in res["rows"] if r["diff"]]
        self.assertTrue(diffs)
        self.assertTrue(all("lag" in r["causes"] for r in diffs))
        self.assertFalse(any(r["residual"] for r in diffs))

    def test_t1_check_flags_same_day_exit(self):
        d = dt.date(2020, 1, 2)
        self.assertEqual(ar.check_t1([{"entry": d, "exit": d}]), [d])
        self.assertEqual(ar.check_t1([{"entry": d, "exit": d + dt.timedelta(days=1)}]), [])
        res = self.compare(limit=0.0)
        self.assertEqual(ar.check_t1(res["sim"]["trades"]), [])

    def test_limit_up_open_blocks_entry_engine_without_limits(self):
        day = self.entry_dates()[0]
        p = self.prices()
        i = p["dates"].index(day)
        up = ar.round_half_up(p["close"][i - 1] * 1.10)
        self.edit(day, Open=up, High=max(up, p["high"][i]), Low=min(up, p["low"][i]))
        res = self.compare(engine_limit="off")
        self.assertTrue(any(ev[1] == "涨停买不进" for ev in res["sim"]["events"]))
        diffs = [r for r in res["rows"] if r["diff"]]
        self.assertTrue(diffs)
        self.assertTrue(any("limit" in r["causes"] for r in diffs))
        self.assertFalse(any(r["residual"] for r in diffs))

    def test_limit_up_open_engine_with_limits_agrees(self):
        day = self.entry_dates()[0]
        p = self.prices()
        i = p["dates"].index(day)
        up = ar.round_half_up(p["close"][i - 1] * 1.10)
        self.edit(day, Open=up, High=max(up, p["high"][i]), Low=min(up, p["low"][i]))
        res = self.compare(engine_limit="on")
        self.assertTrue(res["sim"]["events"])
        self.assertFalse(any(r["diff"] for r in res["rows"]))

    def test_cost_difference(self):
        res = self.compare(limit=0.0, recompute_cost=0.0015)
        diffs = [r for r in res["rows"] if r["diff"]]
        self.assertTrue(diffs)
        self.assertTrue(all(r["causes"] == ["cost"] for r in diffs))
        self.assertFalse(any(r["residual"] for r in diffs))
        self.assertGreater(res["effect"]["cost"], 0)      # 对齐为较低费率后净值上升

    def test_stamp_tax_counts_as_cost(self):
        res = self.compare(limit=0.0, stamp_tax=0.001)
        diffs = [r for r in res["rows"] if r["diff"]]
        self.assertTrue(diffs)
        self.assertTrue(all(r["causes"] == ["cost"] for r in diffs))

    def test_suspension_difference(self):
        day = self.entry_dates()[0]
        self.edit(day, Volume=0)
        res = self.compare(limit=0.0)
        self.assertTrue(any(ev[1] == "停牌" for ev in res["sim"]["events"]))
        diffs = [r for r in res["rows"] if r["diff"]]
        self.assertTrue(any("suspend" in r["causes"] for r in diffs))
        self.assertFalse(any(r["residual"] for r in diffs))

    def test_continuous_weight_strategy_only_drift_fee(self):
        """donchian_s1 是连续仓位:预期只有"漂移再平衡费用"一类差异,不应出现疑似 bug。"""
        res = self.compare("donchian_s1", limit=0.0)
        self.assertGreater(len(res["engine"]["trades"]), 0)
        self.assertFalse(any(r["residual"] for r in res["rows"]))
        self.assertLess(max(res["d_eq_aligned"]), 1e-9)
        for r in res["rows"]:
            if r["diff"]:
                self.assertEqual(r["causes"], ["drift_fee"])

    def test_limit_down_open_blocks_exit(self):
        """跌停开盘卖不出:复算保留持仓下次再试;引擎传 limits 时一致,不传时是建模缺失。"""
        p = self.prices()
        sim = ar.simulate(p, ar.RECOMPUTE_SIGNALS["ma_adx_original"](p), buy_cost=5e-4, sell_cost=5e-4,
                          engine_cost=5e-4, limit_pct=0.1, limit_mode="exact", honor_susp=True,
                          lag=1, fee_mode="notional")
        day = sim["trades"][0]["exit"]
        i = p["dates"].index(day)
        dn = ar.round_half_up(p["close"][i - 1] * 0.90)
        self.edit(day, Open=dn, Low=min(dn, p["low"][i]))
        res = self.compare(engine_limit="on")
        self.assertTrue(any(ev[1] == "跌停卖不出" for ev in res["sim"]["events"]))
        self.assertFalse(any(r["diff"] for r in res["rows"]))
        res = self.compare(engine_limit="off")
        diffs = [r for r in res["rows"] if r["diff"]]
        self.assertTrue(diffs)
        self.assertTrue(any("limit" in r["causes"] for r in diffs))
        self.assertFalse(any(r["residual"] for r in diffs))
        self.assertEqual(ar.classify(diffs[0], res["limit_kind"])[0].split("/")[0][:2], "建模")

    def test_low_price_limit_up_missed_by_engine_tolerance(self):
        """审计 F2:低价股上引擎 0.002 容差漏判涨停。昨收 1.04、开盘 1.14 是精确涨停(1.144 舍入),
        但跳空只有 9.6% < 9.8%。引擎传了 limits 仍分歧 → 归"定义差异:涨跌停"。"""
        p = self.prices()
        day = self.entry_dates()[0]
        i = p["dates"].index(day)
        s = 1.04 / p["close"][i - 1]
        for k in ("Open", "High", "Low", "Close"):
            self.df[k] = self.df[k] * s
        write_csv(self.csv, self.df)
        p = self.prices()
        self.assertEqual(ar.limit_hits(1.04, 1.14, 0.10, "exact"), (True, False))
        self.assertEqual(ar.limit_hits(1.04, 1.14, 0.10, "engine"), (False, False))
        self.edit(day, Open=1.14, High=max(1.14, p["high"][i]), Low=min(1.14, p["low"][i]))
        res = self.compare(engine_limit="on")
        self.assertTrue(any(ev[1] == "涨停买不进" for ev in res["sim"]["events"]))
        diffs = [r for r in res["rows"] if r["diff"]]
        self.assertTrue(diffs)
        self.assertTrue(any("limit" in r["causes"] for r in diffs))
        self.assertFalse(any(r["residual"] for r in diffs))
        self.assertEqual(res["limit_kind"], "定义差异")
        self.assertTrue(any(ar.classify(r, res["limit_kind"])[0] == "定义差异" for r in diffs))

    def test_missing_row_is_listed_but_invisible_to_both(self):
        """停牌缺行:脚本没有交易日历,只能在"数据缺口"里列出;两套实现都看不见,所以不产生疑似 bug。"""
        day = self.df.index[150]
        self.df = self.df.drop(day)
        write_csv(self.csv, self.df)
        p = self.prices()
        gaps = ar.find_gaps(p["dates"])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0][2], 1)
        self.assertEqual(ar.find_gaps(synthetic(n=50, seed=1)["Close"].index.date.tolist()), [])
        res = self.compare(limit=0.0)
        self.assertFalse(any(r["residual"] for r in res["rows"]))
        out = self.dir / "gap.md"
        code, _ = run_main(["--prices", str(self.csv), "--strategy", "ma_adx_original", "--out", str(out)])
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        self.assertIn("数据缺口", text)
        self.assertIn(str(gaps[0][0]), text)

    def test_t1_check_flags_fill_before_signal_on_either_side(self):
        """T+1 核对对引擎和复算两侧的交易表都有效:成交早于信号会被抓到。"""
        p = self.prices()
        w = ar.RECOMPUTE_SIGNALS["ma_adx_original"](p)
        i = next(k for k in range(1, len(w)) if w[k - 1] <= 0 and w[k] > 0)
        good = {"entry": p["dates"][i + 1], "exit": p["dates"][i + 5]}
        early = {"entry": p["dates"][i], "exit": p["dates"][i + 5]}
        self.assertEqual(ar.check_fill_after_signal(p, w, [good]), [])
        self.assertEqual(ar.check_fill_after_signal(p, w, [early]), [early["entry"]])

    def test_unexplained_difference_is_flagged_as_bug(self):
        row = {"diff": True, "causes": [], "residual": True}
        self.assertEqual(ar.classify(row)[0], "疑似bug")

    def test_engine_error_is_flagged_as_bug_end_to_end(self):
        """模拟引擎出错:把引擎某笔收益改动一点,整条链路(compare → 报告)应标疑似 bug。"""
        real = ar.run_engine

        def broken(*a, **k):
            out = real(*a, **k)
            out["trades"][0]["ret"] += 0.01
            return out

        ar.run_engine = broken
        try:
            res = self.compare(limit=0.0)
            out = self.dir / "bug.md"
            code, _ = run_main(["--prices", str(self.csv), "--strategy", "ma_adx_original", "--out", str(out)])
        finally:
            ar.run_engine = real
        bad = [r for r in res["rows"] if r["diff"]]
        self.assertEqual(len(bad), 1)
        self.assertTrue(bad[0]["residual"])
        self.assertEqual(ar.classify(bad[0], res["limit_kind"])[0], "疑似bug")
        self.assertEqual(code, 0)
        self.assertIn("疑似bug", out.read_text(encoding="utf-8"))

    def test_backtest_py_reconciliation_matches(self):
        """backtest.run(整股 + ATR 止损)与独立复算逐笔一致,且报告里有该章节。"""
        res = ar.compare_backtest_py(self.prices(), compare_args(limit=0.1))
        self.assertGreater(len(res["engine"]["trades"]), 0)
        self.assertEqual(len(res["engine"]["trades"]), len(res["mine"]["trades"]))
        self.assertFalse(any(r["diff"] for r in res["rows"]))
        self.assertLess(res["d_eq"], 1e-6)
        self.assertEqual(res["t1_bad_engine"] + res["t1_bad_mine"], [])
        out = self.dir / "bt.md"
        code, _ = run_main(["--prices", str(self.csv), "--strategy", "ma_adx_original", "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("backtest.py 对账", out.read_text(encoding="utf-8"))

    def test_backtest_py_stop_and_exposure(self):
        """有止损出场的样本:原因(signal/stop/stop_gap/end_of_data)也要一致;并统计涨停开盘暴露。"""
        reasons = set()
        for seed in (1, 2, 3, 4, 5, 6):
            self.df = synthetic(n=500, seed=seed)
            write_csv(self.csv, self.df)
            res = ar.compare_backtest_py(self.prices(), compare_args(limit=0.1))
            self.assertFalse(any(r["diff"] for r in res["rows"]), seed)
            reasons |= {t["reason"] for t in res["mine"]["trades"]}
        self.assertIn("signal", reasons)
        self.assertTrue(reasons & {"stop", "stop_gap"}, reasons)
        # 在进场日植入涨停开盘,暴露次数 +1,但 backtest.py 仍照常成交(建模缺失)
        p = self.prices()
        d = ar.recompute_backtest_py(p)["trades"][0]["entry"]
        i = p["dates"].index(d)
        up = ar.round_half_up(p["close"][i - 1] * 1.10)
        self.edit(d, Open=up, High=max(up, p["high"][i]), Low=min(up, p["low"][i]))
        res = ar.compare_backtest_py(self.prices(), compare_args(limit=0.1))
        self.assertGreaterEqual(res["limit_exposed"], 1)

    def test_backtest_py_difference_is_flagged(self):
        real = ar.run_backtest_py

        def broken(p):
            out = real(p)
            out["trades"][0]["shares"] += 1
            return out

        ar.run_backtest_py = broken
        try:
            res = ar.compare_backtest_py(self.prices(), compare_args(limit=0.1))
        finally:
            ar.run_backtest_py = real
        self.assertEqual(sum(r["diff"] for r in res["rows"]), 1)

    def test_rsi2_all_strategies_run_on_long_sample(self):
        self.df = synthetic(n=400, seed=1)
        write_csv(self.csv, self.df)
        out = self.dir / "all.md"
        code, _ = run_main(["--prices", str(self.csv), "--strategy", "all", "--out", str(out)])
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        for s in ar.STRATEGIES:
            self.assertIn(f"`{s}`", text)


class TestInvalidInput(Base):
    def run_bad(self, extra=None, expect="错误"):
        out = self.dir / "o.md"
        argv = ["--prices", str(self.csv), "--strategy", "ma_adx_original", "--out", str(out)] + (extra or [])
        code, err = run_main(argv)
        self.assertEqual(code, 2, err)
        self.assertIn(expect, err)
        self.assertFalse(out.exists())

    def test_missing_file(self):
        self.csv.unlink()
        self.run_bad(expect="不存在")

    def test_missing_column(self):
        self.csv.write_text("Date,Open,Close\n" + "\n".join(f"2020-01-{d:02d},1,1" for d in range(1, 29)) * 2)
        self.run_bad(expect="缺少列")

    def test_non_numeric(self):
        lines = self.csv.read_text().splitlines()
        parts = lines[5].split(",")
        parts[1] = "abc"
        lines[5] = ",".join(parts)
        self.csv.write_text("\n".join(lines))
        self.run_bad(expect="不是数字")

    def test_non_positive_price(self):
        lines = self.csv.read_text().splitlines()
        parts = lines[5].split(",")
        parts[4] = "-1"
        lines[5] = ",".join(parts)
        self.csv.write_text("\n".join(lines))
        self.run_bad(expect="正的有限数")

    def test_duplicate_or_unsorted_dates(self):
        lines = self.csv.read_text().splitlines()
        lines[6] = lines[5]
        self.csv.write_text("\n".join(lines))
        self.run_bad(expect="不严格递增")

    def test_inconsistent_ohlc(self):
        lines = self.csv.read_text().splitlines()
        parts = lines[5].split(",")
        parts[2] = "0.5"      # High < Low
        lines[5] = ",".join(parts)
        self.csv.write_text("\n".join(lines))
        self.run_bad(expect="不自洽")

    def test_too_few_rows(self):
        lines = self.csv.read_text().splitlines()[:10]
        self.csv.write_text("\n".join(lines))
        self.run_bad(expect="行数太少")

    def test_unknown_strategy(self):
        out = self.dir / "o.md"
        code, err = run_main(["--prices", str(self.csv), "--strategy", "nope", "--out", str(out)])
        self.assertEqual(code, 2)
        self.assertIn("未知策略", err)

    def test_bad_numbers(self):
        self.run_bad(["--cost", "-1"], expect="--cost")
        self.run_bad(["--limit", "1.5"], expect="--limit")

    def test_data_dir_refused_by_default(self):
        with self.assertRaises(ar.InputError):
            ar.read_prices(ROOT / "data" / "not_there.csv")

    def test_output_into_data_dir_refused(self):
        target = ROOT / "data" / "_audit_should_not_exist.md"
        self.assertFalse(target.exists())
        code, err = run_main(["--prices", str(self.csv), "--strategy", "ma_adx_original", "--out", str(target)])
        self.assertEqual(code, 2)
        self.assertIn("默认不写 data/", err)
        self.assertFalse(target.exists())

    def test_output_dir_missing(self):
        code, err = run_main(["--prices", str(self.csv), "--strategy", "ma_adx_original",
                              "--out", str(self.dir / "nodir" / "o.md")])
        self.assertEqual(code, 2)
        self.assertIn("输出目录不存在", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
