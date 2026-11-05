"""B07 接口结果缓存的单测：只用临时目录和假数据，不读写 data/ 下任何真实文件，不联网，不 import app。
运行:.venv/bin/python tests/test_resultcache_b07.py

三部分:
  1. src/resultcache.py:LRU、过期、命中只算一次、异常不缓存、文件指纹。
  2. trade_signals.evaluate:信号表 / 行情文件 / 15:30 状态任何一项变了都要重算;自定义 prices_loader 不缓存;改返回值不会污染缓存。
  3. app.Handler._table / _backtest:用 ast 取出函数源码在桩对象上跑(和 test_http_desync 一样),
     检查「同样输入只算一次;行情对象、自选股、参数、成本、起点任何一项变了都重算」。
"""
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import resultcache, trade_signals, tracking  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TestMemo(unittest.TestCase):
    def test_get_put_and_miss(self):
        m = resultcache.Memo(maxsize=4)
        self.assertIsNone(m.get("a"))
        self.assertEqual(m.get("a", 7), 7)
        m.put("a", 1)
        self.assertEqual(m.get("a"), 1)
        self.assertEqual(len(m), 1)

    def test_lru_evicts_least_recently_used(self):
        m = resultcache.Memo(maxsize=2)
        m.put("a", 1)
        m.put("b", 2)
        m.get("a")              # a 变成最近用过的
        m.put("c", 3)           # 超容量：应该丢 b
        self.assertEqual(m.get("a"), 1)
        self.assertIsNone(m.get("b"))
        self.assertEqual(m.get("c"), 3)

    def test_ttl_expires(self):
        clock = FakeClock()
        m = resultcache.Memo(maxsize=4, ttl=10, clock=clock)
        m.put("a", 1)
        clock.t = 10
        self.assertEqual(m.get("a"), 1)          # 刚好 10 秒：还没过期
        clock.t = 10.5
        self.assertIsNone(m.get("a"))            # 超过 ttl：丢掉
        self.assertEqual(len(m), 0)

    def test_get_or_compute_computes_once(self):
        m, calls = resultcache.Memo(), []

        def compute():
            calls.append(1)
            return {"x": len(calls)}
        self.assertEqual(m.get_or_compute("k", compute), {"x": 1})
        self.assertEqual(m.get_or_compute("k", compute), {"x": 1})
        self.assertEqual(len(calls), 1)
        self.assertEqual(m.get_or_compute("k2", compute), {"x": 2})

    def test_falsy_and_none_results_are_cached_correctly(self):
        m, calls = resultcache.Memo(), []
        for value in ([], 0, ""):
            m.get_or_compute(("k", repr(value)), lambda v=value: calls.append(1) or v)
            m.get_or_compute(("k", repr(value)), lambda v=value: calls.append(1) or v)
        self.assertEqual(len(calls), 3)          # 空列表 / 0 / 空串也算命中，每个只算一次

    def test_exception_is_not_cached(self):
        m, calls = resultcache.Memo(), []

        def boom():
            calls.append(1)
            raise ValueError("x")
        for _ in range(2):
            with self.assertRaises(ValueError):
                m.get_or_compute("k", boom)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(m), 0)

    def test_keepalive_object_stays_referenced(self):
        import weakref

        class Obj:
            pass
        m, o = resultcache.Memo(), Obj()
        ref = weakref.ref(o)
        m.put(id(o), "v", keepalive=o)
        del o
        self.assertIsNotNone(ref())              # 缓存还拿着它，id 不会被别的对象复用

    def test_file_stamp_changes_on_atomic_replace(self):
        with tempfile.TemporaryDirectory() as d:
            p, tmp = Path(d) / "f.bin", Path(d) / "f.tmp"
            self.assertIsNone(resultcache.file_stamp(p))
            p.write_bytes(b"1234")
            s1 = resultcache.file_stamp(p)
            tmp.write_bytes(b"abcd")             # 同样 4 字节，内容不同
            tmp.replace(p)
            self.assertNotEqual(s1, resultcache.file_stamp(p))


def _prices_frame():
    idx = pd.bdate_range("2026-01-01", periods=40)
    return pd.DataFrame({"Open": range(100, 140), "Close": range(101, 141)}, index=idx, dtype=float)


def _history(path: Path, codes=("600001",), extra_date="2026-02-02"):
    rows = []
    for c in codes:
        rows.append({"market": "cn", "signal_date": extra_date, "code": c, "name": "测试", "strategy": "blend2",
                     "action": "买入", "next": 1.0, "recorded_at": "2026-02-02T16:00:00+08:00", "source": "trade_list",
                     "now": 0.0, "close": 10.0, "reason": "r", "snapshot_json": "{}"})
    pd.DataFrame(rows, columns=trade_signals.COLUMNS).to_parquet(path, index=False)


class TestEvaluateMemo(unittest.TestCase):
    def setUp(self):
        trade_signals._EVAL_MEMO.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        _history(self.base / "trade_signals_history.parquet")
        self.loads = []
        self.stamp = ("A",)
        self.clock = ("2026-03-02", True)
        frame = _prices_frame()

        def fake_prices(market, code, cfg):
            self.loads.append(code)
            return frame
        self.patches = [patch.object(tracking, "_prices", fake_prices),
                        patch.object(trade_signals, "_price_stamps", lambda codes: (tuple(sorted(codes)), self.stamp)),
                        patch.object(trade_signals, "_clock_state", lambda: self.clock)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        trade_signals._EVAL_MEMO.clear()
        self.tmp.cleanup()

    def ev(self):
        return trade_signals.evaluate("blend2", cache=self.base)

    def test_second_call_hits_memo_and_equals_first(self):
        a = self.ev()
        n = len(self.loads)
        self.assertGreater(n, 0)
        b = self.ev()
        self.assertEqual(len(self.loads), n)     # 没有再读行情
        self.assertEqual(a, b)
        self.assertFalse(a["empty"])

    def test_mutating_returned_dict_does_not_poison_cache(self):
        a = self.ev()
        a["groups"].clear()
        a["note"] = "被改了"
        b = self.ev()
        self.assertNotEqual(b["groups"], [])
        self.assertNotEqual(b["note"], "被改了")

    def test_price_file_change_invalidates(self):
        self.ev()
        n = len(self.loads)
        self.stamp = ("B",)                       # 行情文件被更新（每日任务写了新数据）
        self.ev()
        self.assertGreater(len(self.loads), n)

    def test_clock_state_change_invalidates(self):
        self.ev()
        n = len(self.loads)
        self.clock = ("2026-03-02", False)        # 同一天，但回到 15:30 之前的状态
        self.ev()
        self.assertGreater(len(self.loads), n)
        n = len(self.loads)
        self.clock = ("2026-03-03", False)        # 跨天
        self.ev()
        self.assertGreater(len(self.loads), n)

    def test_history_change_invalidates_and_new_signal_is_counted(self):
        a = self.ev()
        _history(self.base / "trade_signals_history.parquet", codes=("600001", "600002"))
        b = self.ev()
        self.assertEqual(a["recorded_signals"], 1)
        self.assertEqual(b["recorded_signals"], 2)

    def test_custom_loader_bypasses_memo(self):
        calls = []

        def loader(market, code, cfg):
            calls.append(code)
            return _prices_frame()
        trade_signals.evaluate("blend2", cache=self.base, prices_loader=loader)
        n = len(calls)
        trade_signals.evaluate("blend2", cache=self.base, prices_loader=loader)
        self.assertEqual(len(calls), 2 * n)       # 传了自定义 loader：每次都老老实实重算
        self.assertEqual(len(trade_signals._EVAL_MEMO), 0)

    def test_empty_results_are_not_affected(self):
        with tempfile.TemporaryDirectory() as d:
            r = trade_signals.evaluate("blend2", cache=d)
            self.assertTrue(r["empty"])
        self.assertTrue(trade_signals.evaluate("rsi2", cache=self.base)["empty"])     # 没有这个策略的信号


# ---------------------------------------------------------------- app.Handler._table / _backtest
APP = (ROOT / "app.py").read_text(encoding="utf-8")


def load_handler_methods(names, env):
    tree = ast.parse(APP)
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Handler":
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name in names:
                    mod = ast.Module(body=[f], type_ignores=[])
                    exec(compile(mod, "app.py", "exec"), env)
                    out[f.name] = env[f.name]
    missing = set(names) - set(out)
    if missing:
        raise AssertionError(f"找不到 Handler 方法: {missing}")
    return out


class TestAppMemo(unittest.TestCase):
    def setUp(self):
        self.prices = {"510300": _prices_frame(), "511880": _prices_frame()}
        self.watch = {"600531": "豫光金铅"}
        self.table_calls, self.run_calls = [], []

        class FakeDaily:
            @staticmethod
            def table_rows(m, mcfg, prices, strategy, start=None):
                outer.table_calls.append((m, strategy, start))
                return [{"symbol": "510300", "n": len(outer.table_calls)}]

        class FakeRegistry:
            STRATEGIES = {"blend2": {}, "trend_ens": {}}

            @staticmethod
            def run(name, prices, symbol, params, mcfg, cost_bp=None, start=None, market=None, detail=False):
                outer.run_calls.append((name, symbol, json.dumps(params, sort_keys=True), cost_bp, start))
                return {"n": len(outer.run_calls)}

        class FakeDatasource:
            @staticmethod
            def load_watchlist(m):
                return dict(outer.watch)

            @staticmethod
            def is_valid_stock(code):
                return False

        outer = self
        self.cur_prices = self.prices
        self.memos = {"_backtest_memo": resultcache.Memo(16), "_table_memo": resultcache.Memo(16)}
        env = {"MARKETS": {"cn": {"main": "510300", "cash": "511880", "universe": {"510300": "沪深300ETF"}}},
               "registry": FakeRegistry, "daily_report": FakeDaily, "datasource": FakeDatasource,
               "resultcache": resultcache, "prices_for": lambda m: outer.cur_prices, "pd": pd, "json": json,
               "_pending": set(), "allmarket": None, "usdata": None, **self.memos}
        m = load_handler_methods({"_table", "_backtest"}, env)
        self.table, self.backtest = (lambda q: m["_table"](None, q)), (lambda q: m["_backtest"](None, q))

    def test_table_cached_until_inputs_change(self):
        q = {"market": "cn", "strategy": "blend2"}
        a = self.table(q)
        self.assertEqual(self.table(q), a)
        self.assertEqual(len(self.table_calls), 1)
        self.table({**q, "start": "2020-01-01"})                 # 起点变了
        self.assertEqual(len(self.table_calls), 2)
        self.table({**q, "strategy": "trend_ens"})               # 策略变了
        self.assertEqual(len(self.table_calls), 3)
        self.watch = {"600531": "豫光金铅", "000001": "平安银行"}   # 自选股变了
        self.table(q)
        self.assertEqual(len(self.table_calls), 4)
        self.cur_prices = dict(self.prices)                      # 行情缓存换了新对象（文件更新）
        self.table(q)
        self.assertEqual(len(self.table_calls), 5)
        self.table(q)
        self.assertEqual(len(self.table_calls), 5)

    def test_table_bad_inputs_unchanged(self):
        self.assertEqual(self.table({"market": "xx", "strategy": "blend2"}), {"error": "参数不对"})
        self.assertEqual(self.table({"market": "cn", "strategy": "nope"}), {"error": "参数不对"})
        self.assertEqual(self.table_calls, [])

    def test_backtest_cached_until_inputs_change(self):
        q = {"market": "cn", "strategy": "blend2", "symbol": "510300"}
        a = self.backtest(q)
        self.assertEqual(self.backtest(q), a)
        self.assertEqual(len(self.run_calls), 1)
        self.backtest({**q, "params": json.dumps({"w_trend": 0.5})})
        self.assertEqual(len(self.run_calls), 2)
        self.backtest({**q, "params": json.dumps({"w_trend": 0.5})})           # 同参数：命中
        self.assertEqual(len(self.run_calls), 2)
        self.backtest({**q, "params": json.dumps({"b": 1, "a": 2})})
        self.backtest({**q, "params": json.dumps({"a": 2, "b": 1})})           # 键顺序不同、内容相同：命中
        self.assertEqual(len(self.run_calls), 3)
        self.backtest({**q, "cost": "20"})
        self.assertEqual(len(self.run_calls), 4)
        self.backtest({**q, "start": "2025-01-01"})
        self.assertEqual(len(self.run_calls), 5)
        self.watch = {}                                           # 自选股变了（A 股涨跌停判断会变）
        self.backtest(q)
        self.assertEqual(len(self.run_calls), 6)
        self.cur_prices = dict(self.prices)                       # 行情文件更新
        self.backtest(q)
        self.assertEqual(len(self.run_calls), 7)

    def test_backtest_errors_not_cached_and_unchanged(self):
        self.assertEqual(self.backtest({"market": "cn", "strategy": "nope"}), {"error": "参数不对"})
        self.assertEqual(self.backtest({"market": "cn", "strategy": "blend2", "symbol": "999999"}), {"error": "品种不在列表里"})
        self.assertEqual(self.run_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
