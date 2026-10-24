"""adapter.py 单测(T007 / S6):只用合成数据、假回测函数(依赖注入)与临时目录,不读写项目数据目录,不联网。
运行:python3 -B tests/test_validation_adapter.py
真实 src 回测的数值一致性测试需要 pandas;没有 pandas 时自动跳过(降级路径测试不需要)。
"""
import builtins
import datetime as dt
import math
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.validation import adapter as ad
from src.validation.adapter import (AdapterError, RunRecord, from_returns, from_csv, from_trades, trades_from_csv,
                                    from_strategy, run_grid, rerun_with_held, check_calendar, align_pair)

try:
    import pandas as pd
    import numpy as np
except ImportError:
    pd = None

D = dt.date


def days(n, start=D(2020, 1, 1)):
    return [start + dt.timedelta(days=i) for i in range(n)]


class TestFromReturns(unittest.TestCase):
    def test_basic_and_meta_defaults(self):
        r = from_returns(["2020-01-01", "2020-01-02"], [0.01, -0.02])
        self.assertEqual(r.dates, (D(2020, 1, 1), D(2020, 1, 2)))
        self.assertEqual(r.returns, (0.01, -0.02))
        self.assertIsNone(r.meta["cost_included"])       # 默认未声明
        self.assertEqual(r.meta["source"], "external")
        self.assertTrue(r.has_daily)
        self.assertEqual(r.n_days, 2)

    def test_date_types(self):
        r = from_returns([D(2020, 1, 1), dt.datetime(2020, 1, 2, 15), "2020-01-03 00:00:00"], [0, 0, 0])
        self.assertEqual(r.dates[1], D(2020, 1, 2))
        self.assertEqual(r.dates[2], D(2020, 1, 3))

    def test_length_mismatch(self):
        with self.assertRaises(AdapterError):
            from_returns(days(3), [0.0, 0.0])
        with self.assertRaises(AdapterError):
            from_returns(days(3), [0.0] * 3, turnover=[0.1])

    def test_duplicate_and_unsorted(self):
        d = days(3)
        with self.assertRaisesRegex(AdapterError, "重复"):
            from_returns([d[0], d[1], d[1]], [0.0] * 3)
        with self.assertRaisesRegex(AdapterError, "乱序"):
            from_returns([d[0], d[2], d[1]], [0.0] * 3)

    def test_bad_values(self):
        for bad in (float("inf"), "abc", True, None, -1.0, -1.5):
            with self.assertRaises(AdapterError, msg=repr(bad)):
                from_returns(days(2), [0.0, bad])
        with self.assertRaises(AdapterError):
            from_returns(days(1), [0.0], base_cost=-0.1)
        with self.assertRaises(AdapterError):
            from_returns([], [])
        with self.assertRaises(AdapterError):
            from_returns(["not-a-date"], [0.0])
        with self.assertRaises(AdapterError):
            from_returns(days(1), [0.0], cost_included="yes")

    def test_nan_error_and_drop(self):
        nan = float("nan")
        with self.assertRaisesRegex(AdapterError, "NaN"):
            from_returns(days(3), [0.01, nan, 0.02])
        r = from_returns(days(3), [0.01, nan, 0.02], turnover=[0.1, 0.2, 0.3], nan_policy="drop")
        self.assertEqual(r.returns, (0.01, 0.02))
        self.assertEqual(r.turnover, (0.1, 0.3))          # turnover 同步剔除
        self.assertEqual(r.meta["n_dropped_nan"], 1)
        self.assertEqual(r.dates, (days(3)[0], days(3)[2]))
        with self.assertRaises(AdapterError):
            from_returns(days(2), [nan, nan], nan_policy="drop")   # 剔除后为空
        with self.assertRaises(AdapterError):
            from_returns(days(2), [0.0, 0.0], nan_policy="skip")

    def test_turnover_negative(self):
        with self.assertRaises(AdapterError):
            from_returns(days(2), [0.0, 0.0], turnover=[0.1, -0.1])

    def test_cost_flag_three_states(self):
        for v in (True, False, None):
            self.assertIs(from_returns(days(1), [0.0], base_cost=0.0005, cost_included=v).meta["cost_included"], v)

    def test_trades_attached_and_validated(self):
        tr = [{"entry_date": "2020-01-01", "exit_date": "2020-01-03", "ret": 0.05, "tag": "a"}]
        r = from_returns(days(3), [0.0] * 3, trades=tr)
        self.assertEqual(r.trades[0]["entry_date"], D(2020, 1, 1))
        self.assertEqual(r.trades[0]["tag"], "a")
        self.assertEqual(r.trade_returns(), [0.05])
        for bad in ([{"entry_date": "2020-01-01", "ret": 0.1}],
                    [{"entry_date": "2020-01-05", "exit_date": "2020-01-01", "ret": 0.1}],
                    [{"entry_date": "2020-01-01", "exit_date": "2020-01-02", "ret": -1}],
                    ["x"]):
            with self.assertRaises(AdapterError):
                from_returns(days(3), [0.0] * 3, trades=bad)


class TestFromTrades(unittest.TestCase):
    def test_trades_only_blocks_daily_metrics(self):
        r = from_trades([{"entry_date": "2020-01-01", "exit_date": "2020-01-05", "ret": 0.03},
                         {"entry_date": "2020-02-01", "exit_date": "2020-02-05", "ret": -0.01}])
        self.assertFalse(r.has_daily)
        self.assertEqual(r.trade_returns(), [0.03, -0.01])
        with self.assertRaisesRegex(AdapterError, "逐日"):
            r.require_daily("夏普")
        with self.assertRaises(AdapterError):
            check_calendar(r, days(3))
        with self.assertRaises(AdapterError):
            align_pair(r, r)
        self.assertFalse(r.meta["daily_available"])

    def test_empty_trades(self):
        with self.assertRaises(AdapterError):
            from_trades([])

    def test_require_daily_ok(self):
        r = from_returns(days(2), [0.0, 0.0])
        self.assertIs(r.require_daily("x"), r)


class TestCsv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def w(self, name, text):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_read_with_turnover_and_bom(self):
        p = os.path.join(self.tmp.name, "r.csv")
        with open(p, "w", encoding="utf-8-sig") as f:
            f.write("date,ret,to\n2020-01-01,0.01,0.5\n2020-01-02,-0.01,0.0\n")
        r = from_csv(p, turnover_col="to", base_cost=0.0005, cost_included=True)
        self.assertEqual(r.returns, (0.01, -0.01))
        self.assertEqual(r.turnover, (0.5, 0.0))
        self.assertTrue(r.meta["cost_included"])

    def test_missing_column_and_file(self):
        p = self.w("a.csv", "date,x\n2020-01-01,1\n")
        with self.assertRaisesRegex(AdapterError, "缺少列"):
            from_csv(p)
        with self.assertRaisesRegex(AdapterError, "无法读取"):
            from_csv(os.path.join(self.tmp.name, "nope.csv"))

    def test_empty_cell_is_nan(self):
        p = self.w("b.csv", "date,ret\n2020-01-01,0.01\n2020-01-02,\n2020-01-03,0.02\n")
        with self.assertRaises(AdapterError):
            from_csv(p)
        r = from_csv(p, nan_policy="drop")
        self.assertEqual(r.meta["n_dropped_nan"], 1)
        self.assertEqual(len(r.dates), 2)

    def test_dup_dates_in_csv(self):
        p = self.w("c.csv", "date,ret\n2020-01-01,0.01\n2020-01-01,0.02\n")
        with self.assertRaisesRegex(AdapterError, "重复"):
            from_csv(p)

    def test_trades_csv(self):
        p = self.w("t.csv", "entry_date,exit_date,ret\n2020-01-01,2020-01-04,0.02\n")
        r = from_trades(trades_from_csv(p))
        self.assertEqual(r.trade_returns(), [0.02])
        p2 = self.w("t2.csv", "a,b\n1,2\n")
        with self.assertRaises(AdapterError):
            trades_from_csv(p2)


class TestAlign(unittest.TestCase):
    def test_check_calendar(self):
        d = days(5)
        r = from_returns([d[0], d[1], d[3]], [0.0, 0.0, 0.0])
        out = check_calendar(r, d[:4] + [D(2020, 3, 1)])
        self.assertEqual(out["missing"], [d[2]])        # 区间内缺日
        self.assertEqual(out["extra"], [])
        self.assertFalse(out["ok"])
        self.assertTrue(check_calendar(r, [d[0], d[1], d[3]])["ok"])
        self.assertEqual(check_calendar(r, [d[0], d[3]])["extra"], [d[1]])

    def test_align_pair(self):
        d = days(4)
        a = from_returns(d[:3], [0.1, 0.2, 0.3])
        b = from_returns(d[1:], [0.5, 0.6, 0.7])
        o = align_pair(a, b)
        self.assertEqual(o["dates"], [d[1], d[2]])
        self.assertEqual(o["a"], [0.2, 0.3])
        self.assertEqual(o["b"], [0.5, 0.6])
        self.assertEqual(o["only_a"], [d[0]])
        self.assertEqual(o["only_b"], [d[3]])


# ---------------------------------------------------------------- 主路径(依赖注入的假回测)

class FakeRes:
    def __init__(self, returns, turnover, trades=None):
        self.returns, self.turnover, self.trades = returns, turnover, trades


class FakeDF:
    """只有 index 属性的假数据(不需要 pandas)。"""
    def __init__(self, n):
        self.index = days(n)


def fake_strategy(df, a=1, b=1):
    return {"a": a, "b": b}            # 目标仓位这里只是把参数带给 fake_run


def fake_run(prices, target, cost=0.0, band=0.0, limits=None, cash_ret=None):
    (code, df), = prices.items()
    a, b = target["a"], target["b"]
    rets = {d: 0.001 * a * (1 if i % 2 else -0.5) + 0.0001 * b for i, d in enumerate(df.index)}
    turn = {d: 0.1 * a for d in df.index}
    fake_run.calls.append((code, cost, band, limits, cash_ret))
    return FakeRes(rets, turn, [{"entry_date": df.index[0], "exit_date": df.index[1], "ret": 0.01}])


fake_run.calls = []


class MemLedger:
    """假账本:只在内存里记录,不写文件。"""
    def __init__(self):
        self.rows = []

    def record(self, strategy, params, metric_name, value, n_obs, tag="", trial_id=None, now=None):
        self.rows.append((strategy, dict(params), metric_name, value, n_obs, tag))
        return f"t{len(self.rows):06d}"


class TestFromStrategyInjected(unittest.TestCase):
    def setUp(self):
        fake_run.calls.clear()

    def test_inject_basic(self):
        rec = from_strategy(FakeDF(6), "anything", {"a": 2, "b": 3}, code="C1", cost=0.001, band=0.05, limit=0.1,
                            strategy_fn=fake_strategy, run_fn=fake_run)
        self.assertEqual(rec.n_days, 6)
        self.assertEqual(rec.meta["source"], "injected_runner")
        self.assertEqual(rec.meta["params"], {"a": 2, "b": 3})
        self.assertEqual(rec.meta["base_cost"], 0.001)
        self.assertIsNone(rec.meta["cost_included"])          # 注入时不替调用者声明
        self.assertEqual(rec.turnover[0], 0.2)
        self.assertEqual(len(rec.trades), 1)
        self.assertEqual(fake_run.calls[0], ("C1", 0.001, 0.05, {"C1": 0.1}, None))

    def test_inject_declared_cost(self):
        rec = from_strategy(FakeDF(3), "x", strategy_fn=fake_strategy, run_fn=fake_run, cost_included=True)
        self.assertTrue(rec.meta["cost_included"])

    def test_runner_failure_wrapped(self):
        def boom(*a, **k):
            raise RuntimeError("坏了")
        with self.assertRaisesRegex(AdapterError, "调用回测失败"):
            from_strategy(FakeDF(3), "x", strategy_fn=fake_strategy, run_fn=boom)

    def test_bad_args(self):
        with self.assertRaises(AdapterError):
            from_strategy(FakeDF(3), "", strategy_fn=fake_strategy, run_fn=fake_run)
        with self.assertRaises(AdapterError):
            from_strategy(FakeDF(3), "x", cost=-1, strategy_fn=fake_strategy, run_fn=fake_run)

    def test_result_validation(self):
        class NoTurn:
            returns = {D(2020, 1, 1): 0.0}
        with self.assertRaisesRegex(AdapterError, "turnover"):
            from_strategy(FakeDF(1), "x", strategy_fn=fake_strategy, run_fn=lambda *a, **k: NoTurn())

        class Mismatch:
            returns = {D(2020, 1, 1): 0.0}
            turnover = {D(2020, 1, 2): 0.0}
        with self.assertRaisesRegex(AdapterError, "不一致"):
            from_strategy(FakeDF(1), "x", strategy_fn=fake_strategy, run_fn=lambda *a, **k: Mismatch())


class TestReviewFixes(unittest.TestCase):
    """S6 返工:meta 不能覆盖已校验字段、base_cost/日期口径一致、只注入 strategy_fn 的标注。"""

    def test_meta_cannot_override_validated_fields(self):
        for key in ("source", "base_cost", "cost_included", "n_dropped_nan"):
            with self.assertRaisesRegex(AdapterError, "保留键", msg=key):
                from_returns(days(3), [0.01, 0.0, -0.01], meta={key: "x"})
            with self.assertRaisesRegex(AdapterError, "保留键", msg=key):
                from_trades([{"entry_date": "2020-01-01", "exit_date": "2020-01-02", "ret": 0.1}], meta={key: "x"})
        with self.assertRaises(AdapterError):
            from_returns(days(3), [0.01, 0.0, -0.01], meta={"cost_included": "yes", "source": "portfolio.run_weights"})
        r = from_returns(days(3), [0.01, 0.0, -0.01], meta={"note": "ok"})        # 普通键照常保留
        self.assertEqual((r.meta["note"], r.meta["source"]), ("ok", "external"))

    def test_csv_meta_reserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "r.csv")
            with open(p, "w", encoding="utf-8") as f:
                f.write("date,ret\n2020-01-01,0.01\n2020-01-02,0.02\n")
            with self.assertRaisesRegex(AdapterError, "保留键"):
                from_csv(p, meta={"cost_included": True})
            self.assertEqual(from_csv(p, meta={"k": 1}).meta["k"], 1)

    def test_from_trades_negative_base_cost(self):
        with self.assertRaises(AdapterError):
            from_trades([{"entry_date": "2020-01-01", "exit_date": "2020-01-02", "ret": 0.1}], base_cost=-1)

    def test_strict_date_parsing(self):
        for bad in ("2020-01-01xx", "2020-01-0112", "2020-01-01 junk", "2020-1-1"):
            with self.assertRaises(AdapterError, msg=bad):
                ad._to_date(bad)
        self.assertEqual(ad._to_date("2020-01-02"), D(2020, 1, 2))
        self.assertEqual(ad._to_date("2020-01-02 15:00:00"), D(2020, 1, 2))
        self.assertEqual(ad._to_date("2020-01-02T00:00:00"), D(2020, 1, 2))

    def test_only_strategy_fn_injected_uses_real_runner_labels(self):
        """只注入 strategy_fn 时实际调用真实 run_weights:来源与含费标注必须按真实路径。"""
        seen = {}

        class FakePortfolio:
            @staticmethod
            def run_weights(*a, **k):
                seen["called"] = True
                return FakeRes({d: 0.001 for d in days(3)}, {d: 0.1 for d in days(3)})
        saved = sys.modules.get("src.portfolio")
        sys.modules["src.portfolio"] = FakePortfolio
        sys.modules["src.strategies"] = sys.modules.get("src.strategies") or FakePortfolio
        try:
            rec = from_strategy(FakeDF(3), "x", strategy_fn=fake_strategy)
        finally:
            if saved is None:
                sys.modules.pop("src.portfolio", None)
            else:
                sys.modules["src.portfolio"] = saved
        self.assertTrue(seen["called"])
        self.assertEqual(rec.meta["source"], "portfolio.run_weights")
        self.assertIs(rec.meta["cost_included"], True)

    def test_rerun_first_day_held_rejected(self):
        d = days(4)
        with self.assertRaisesRegex(AdapterError, "第 0 天"):
            rerun_with_held(FakeDF(4), {x: 1.0 for x in d}, run_fn=fake_run)
        rec = rerun_with_held(FakeDF(4), dict(zip(d, [0.0, 1.0, 1.0, 0.0])), run_fn=lambda *a, **k: FakeRes(
            {x: 0.0 for x in d}, {x: 0.0 for x in d}))
        self.assertEqual(rec.meta["held"], (0.0, 1.0, 1.0, 0.0))


class TestSrcImportFailure(unittest.TestCase):
    """模拟 import src 失败:降级路径仍可用,主路径抛 AdapterError 并提示降级入口。"""

    def setUp(self):
        self.saved = {k: sys.modules.get(k) for k in ("src.portfolio", "src.strategies")}
        sys.modules["src.portfolio"] = None          # None 会让 import 抛 ImportError
        sys.modules["src.strategies"] = None
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_main_path_raises_degrade_works(self):
        with self.assertRaisesRegex(AdapterError, "无法调用现有回测,且未提供外部收益/交易数据"):
            from_strategy(FakeDF(3), "trend_vol_target")
        with self.assertRaises(AdapterError):
            rerun_with_held(FakeDF(3), {d: 0.0 for d in days(3)})
        r = from_returns(days(3), [0.01, 0.0, -0.01])          # 降级路径照常
        self.assertEqual(r.n_days, 3)
        self.assertEqual(from_trades([{"entry_date": "2020-01-01", "exit_date": "2020-01-02", "ret": 0.1}]).trade_returns(),
                         [0.1])

    def test_unknown_strategy_name(self):
        # 只注入 run_fn、不注入 strategy_fn:此时需要 src.strategies,import 失败 -> AdapterError
        with self.assertRaises(AdapterError):
            from_strategy(FakeDF(3), "trend_vol_target", run_fn=fake_run)


class TestRunGrid(unittest.TestCase):
    def setUp(self):
        fake_run.calls.clear()

    def run_it(self, grid, ledger=None, **kw):
        return run_grid(FakeDF(30), "fake", grid, ledger, strategy_fn=fake_strategy, run_fn=fake_run, **kw)

    def test_with_ledger_every_combo_recorded(self):
        led = MemLedger()
        out = self.run_it({"b": [1, 2], "a": [1, 2, 3]}, led, tag="g1")
        self.assertEqual(out["n_trials"], 6)
        self.assertEqual(len(led.rows), 6)
        self.assertFalse(out["untracked"])
        self.assertEqual(out["warnings"], [])
        # 排序后笛卡尔积:先 a 后 b,顺序确定
        self.assertEqual([r["params"] for r in out["results"]][:2], [{"a": 1, "b": 1}, {"a": 1, "b": 2}])
        self.assertEqual([r["trial_id"] for r in out["results"]], [f"t{i:06d}" for i in range(1, 7)])
        s, p, m, v, n, tag = led.rows[0]
        self.assertEqual((s, m, n, tag), ("fake", "sharpe_daily", 30, "g1"))
        self.assertTrue(math.isfinite(v))
        self.assertEqual(v, out["results"][0]["metric"])

    def test_without_ledger_untracked(self):
        out = self.run_it({"a": [1, 2]})
        self.assertTrue(out["untracked"])
        self.assertIn("这批试验未计入试验次数", out["warnings"])
        self.assertTrue(all(r["trial_id"] is None for r in out["results"]))

    def test_degenerate_sharpe_still_recorded(self):
        def const_run(prices, target, **k):
            (c, df), = prices.items()
            return FakeRes({d: 0.0 for d in df.index}, {d: 0.0 for d in df.index})
        led = MemLedger()
        out = run_grid(FakeDF(5), "f", {"a": [1, 2]}, led, strategy_fn=fake_strategy, run_fn=const_run)
        self.assertEqual(len(led.rows), 2)                   # 试验发生过,必须计数
        self.assertTrue(all(r["degenerate"] and r["metric"] is None for r in out["results"]))
        self.assertTrue(all(row[3] == 0.0 and "degenerate" in row[5] for row in led.rows))

    def test_failure_records_nothing(self):
        calls = []

        def flaky(prices, target, **k):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("第三组失败")
            return fake_run(prices, target, **k)
        led = MemLedger()
        with self.assertRaises(AdapterError):
            run_grid(FakeDF(10), "f", {"a": [1, 2, 3, 4]}, led, strategy_fn=fake_strategy, run_fn=flaky)
        self.assertEqual(led.rows, [])

    def test_grid_validation(self):
        for bad in ({}, {"a": []}, {"a": "abc"}, {"a": 3}, [1, 2]):
            with self.assertRaises(AdapterError, msg=repr(bad)):
                self.run_it(bad)
        with self.assertRaisesRegex(AdapterError, "上限"):
            self.run_it({"a": list(range(30)), "b": list(range(30))})
        with self.assertRaises(AdapterError):
            self.run_it({"a": [1]}, ledger=object())

    def test_real_triallog_in_tempdir(self):
        from src.validation.trials import TrialLog
        with tempfile.TemporaryDirectory() as tmp:
            log = TrialLog(os.path.join(tmp, "trial_log.jsonl"))
            self.run_it({"a": [1, 2, 3]}, log)
            self.assertEqual(log.count("fake"), 3)


class TestNoFileWrites(unittest.TestCase):
    def test_adapter_never_opens_for_write(self):
        real_open = builtins.open
        bad = []

        def guard(file, mode="r", *a, **k):
            if any(c in mode for c in "wax+"):
                bad.append((file, mode))
                raise AssertionError(f"adapter 不应写文件: {file} {mode}")
            return real_open(file, mode, *a, **k)
        builtins.open = guard
        try:
            r = from_returns(days(5), [0.01, -0.01, 0.0, 0.02, 0.0], trades=[{"entry_date": "2020-01-01",
                             "exit_date": "2020-01-02", "ret": 0.01}])
            from_trades(r.trades)
            check_calendar(r, days(5))
            align_pair(r, r)
            run_grid(FakeDF(8), "f", {"a": [1, 2]}, MemLedger(), strategy_fn=fake_strategy, run_fn=fake_run)
            run_grid(FakeDF(8), "f", {"a": [1, 2]}, None, strategy_fn=fake_strategy, run_fn=fake_run)
        finally:
            builtins.open = real_open
        self.assertEqual(bad, [])

    def test_import_creates_nothing(self):
        # 在空临时目录里用新解释器导入模块,导入前后目录内容不变,且导入过程没有以写模式 open
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("import builtins,sys\n"
                "r=builtins.open\n"
                "def g(f,m='r',*a,**k):\n"
                "    assert not any(c in m for c in 'wax+'),(f,m)\n"
                "    return r(f,m,*a,**k)\n"
                "builtins.open=g\n"
                f"sys.path.insert(0,{root!r})\n"
                "import src.validation.adapter\n")
        with tempfile.TemporaryDirectory() as tmp:
            before = os.listdir(tmp)
            out = subprocess.run([sys.executable, "-B", "-c", code], cwd=tmp, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(os.listdir(tmp), before)

    @unittest.skipIf(pd is None, "需要 pandas")
    def test_real_paths_never_open_for_write(self):
        from src import data
        df = data.synthetic(n=300, seed=1)
        real_open = builtins.open
        bad = []

        def guard(file, mode="r", *a, **k):
            if any(c in mode for c in "wax+"):
                bad.append((file, mode))
                raise AssertionError(f"不应写文件: {file} {mode}")
            return real_open(file, mode, *a, **k)
        builtins.open = guard
        try:
            from_strategy(df, "trend_vol_target", {"sma_len": 50})
            held = pd.Series([0.0] + [1.0] * (len(df) - 1), index=df.index)
            rerun_with_held(df, held, cost=0.0)
        finally:
            builtins.open = real_open
        self.assertEqual(bad, [])


@unittest.skipIf(pd is None, "需要 pandas")
class TestRealPortfolioPath(unittest.TestCase):
    """主路径与直接调用 portfolio.run_weights 数值一致(合成数据,不读 data/)。"""

    @classmethod
    def setUpClass(cls):
        from src import data, portfolio, strategies
        cls.portfolio, cls.strategies = portfolio, strategies
        cls.df = data.synthetic(n=700, seed=3)

    def test_matches_direct_call(self):
        params = {"sma_len": 50}
        rec = from_strategy(self.df, "trend_vol_target", params, code="S", cost=0.001, band=0.02)
        direct = self.portfolio.run_weights({"S": self.df}, self.strategies.trend_vol_target(self.df, **params),
                                            cost=0.001, band=0.02)
        self.assertEqual(rec.meta["source"], "portfolio.run_weights")
        self.assertTrue(rec.meta["cost_included"])
        self.assertEqual(rec.base_cost if hasattr(rec, "base_cost") else rec.meta["base_cost"], 0.001)
        self.assertEqual(len(rec.returns), len(direct.returns))
        self.assertTrue(np.allclose(rec.returns, direct.returns.to_numpy(), rtol=0, atol=1e-15))
        self.assertTrue(np.allclose(rec.turnover, direct.turnover.to_numpy(), rtol=0, atol=1e-15))
        self.assertEqual(list(rec.dates), [t.date() for t in direct.returns.index])
        self.assertEqual(len(rec.trades), len(direct.trades))
        if len(direct.trades):
            self.assertTrue(np.allclose(rec.trade_returns(), direct.trades["ret"].to_numpy(), atol=1e-15))

    def test_limit_passed_through(self):
        rec = from_strategy(self.df, "donchian_s1", {}, code="S", limit=0.10)
        direct = self.portfolio.run_weights({"S": self.df}, self.strategies.donchian_s1(self.df),
                                            cost=0.0005, band=0.0, limits={"S": 0.10})
        self.assertTrue(np.allclose(rec.returns, direct.returns.to_numpy(), atol=1e-15))

    def test_unknown_strategy_rejected(self):
        with self.assertRaisesRegex(AdapterError, "未知策略"):
            from_strategy(self.df, "dual_momentum")
        with self.assertRaises(AdapterError):
            from_strategy(self.df, "no_such_strategy")

    def test_input_df_not_mutated(self):
        before = self.df.copy()
        from_strategy(self.df, "trend_vol_target", {"sma_len": 50})
        pd.testing.assert_frame_equal(before, self.df)

    def test_run_grid_real(self):
        out = run_grid(self.df, "trend_vol_target", {"sma_len": [30, 60]}, None)
        self.assertEqual(out["n_trials"], 2)
        self.assertTrue(out["untracked"])

    def test_rerun_with_held_weights_equal_held(self):
        """用 held 重跑后,run_weights 的持仓逐日等于 held(日期移位正确)。"""
        df = self.df.iloc[:12]
        held = pd.Series([0, 1, 1, 0, 0, 1, 0, 0, 1, 1, 1, 0], index=df.index, dtype=float)
        captured = {}

        def spy(prices, target, **k):
            res = self.portfolio.run_weights(prices, target, **k)
            captured["w"] = res.weights.iloc[:, 0]
            return res
        rec = rerun_with_held(df, held, code="S", cost=0.0, run_fn=spy)
        self.assertTrue(np.allclose(captured["w"].to_numpy(), held.to_numpy()))
        self.assertTrue(rec.meta["rerun_with_held"])
        # 默认(真实)run_weights 路径同样成立
        rec2 = rerun_with_held(df, held, code="S", cost=0.0)
        self.assertTrue(np.allclose(rec2.returns, rec.returns, atol=1e-15))
        self.assertTrue(rec2.meta["cost_included"])

    def test_rerun_with_held_rejects_misaligned(self):
        df = self.df.iloc[:5]
        with self.assertRaises(AdapterError):
            rerun_with_held(df, pd.Series([0, 1, 1], index=df.index[:3], dtype=float))
        with self.assertRaises(AdapterError):
            rerun_with_held(df, pd.Series([0, -1, 1, 0, 0], index=df.index, dtype=float))


if __name__ == "__main__":
    unittest.main()
