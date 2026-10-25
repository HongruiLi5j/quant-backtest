"""T021:持仓页"最新价"必须是不复权的真实价，不能是本地日线里的后复权价。

- display_price.cached_scale:只读缓存、绝不联网。
- app.py 的 _holdings_price:用 AST 摘出来测(不导入 app.py——导入会读 .secret)，数据全是合成的，
  缓存文件在临时目录，不读真实 data/。

python -m pytest tests/test_holdings_price_cache.py
"""
import ast
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import display_price  # noqa: E402

TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
METHOD = next(n for n in HANDLER.body if isinstance(n, ast.FunctionDef) and n.name == "_holdings_price")


def frame(day="2026-10-23", close=75.694):
    return pd.DataFrame({"Close": [70.0, close]}, index=pd.to_datetime(["2026-10-22", day]))


class _NoNetwork:
    """测试期间：记录每一次联网尝试（connect / connect_ex / create_connection / DNS 查询），同时让它们失败。
    只靠"抛错"不够——生产代码里有 except Exception，会把抛错吞成"无缓存"，即使真的尝试联网了测试也照样绿。
    所以每个测试结束时（tearDown）要独立断言 attempts 为空。"""

    def __init__(self):
        self.attempts = []

    def __enter__(self):
        def boom(name):
            def f(*a, **k):
                self.attempts.append(name)
                raise OSError(f"持仓价格不应联网（{name}）")
            return f
        self._ps = [patch.object(socket.socket, "connect", boom("connect")),
                    patch.object(socket.socket, "connect_ex", boom("connect_ex")),
                    patch.object(socket, "create_connection", boom("create_connection")),
                    patch.object(socket, "getaddrinfo", boom("getaddrinfo"))]
        for p in self._ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._ps:
            p.stop()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "engine_price_scale.json"
        p = patch.object(display_price, "_SCALE_FILE", self.cache)
        p.start()
        self.addCleanup(p.stop)
        self.net = _NoNetwork()
        self.net.__enter__()
        self.addCleanup(self.net.__exit__)

    def tearDown(self):
        self.assertEqual(self.net.attempts, [], "测试期间有联网尝试（即使被生产代码吞掉也算）")

    def write(self, obj):
        self.cache.write_text(json.dumps(obj), encoding="utf-8")

    def write_raw(self, raw_json_value, day="2026-10-23"):
        """直接写原始 JSON 文本（可以是 true / Infinity / NaN / "10.1" 这些 json.dumps 不会按这样写的值）。"""
        self.cache.write_text('{"600531": {"day": "%s", "raw": %s}}' % (day, raw_json_value), encoding="utf-8")


class CachedScaleTests(Base):
    def test_hit_returns_factor_that_restores_real_price(self):
        self.write({"600531": {"day": "2026-10-23", "k": 0.1334, "raw": 10.1}})
        k, note = display_price.cached_scale("cn", "600531", frame())
        self.assertEqual(note, "前复权")
        self.assertAlmostEqual(75.694 * k, 10.1, places=6)

    def test_miss_cases_return_no_cache(self):
        df = frame()
        self.assertEqual(display_price.cached_scale("cn", "600531", df), (1.0, "无缓存"))      # 文件不存在
        self.write({"600531": {"day": "2026-10-22", "k": 0.1, "raw": 10.1}})
        self.assertEqual(display_price.cached_scale("cn", "600531", df), (1.0, "无缓存"))      # 日期不是这根 K 线
        self.write({"000001": {"day": "2026-10-23", "k": 0.1, "raw": 10.1}})
        self.assertEqual(display_price.cached_scale("cn", "600531", df), (1.0, "无缓存"))      # 没有这只股票
        self.write({"600531": {"day": "2026-10-23", "k": 0.1, "raw": 0}})
        self.assertEqual(display_price.cached_scale("cn", "600531", df), (1.0, "无缓存"))      # 真实价无效
        self.write({"600531": {"day": "2026-10-23", "raw": "x"}})
        self.assertEqual(display_price.cached_scale("cn", "600531", df), (1.0, "无缓存"))
        self.cache.write_text("{not json", encoding="utf-8")
        self.assertEqual(display_price.cached_scale("cn", "600531", df), (1.0, "无缓存"))      # 缓存损坏
        self.assertEqual(display_price.cached_scale("us", "AAPL", df), (1.0, "无缓存"))
        self.assertEqual(display_price.cached_scale("cn", "600531", frame().iloc[0:0]), (1.0, "无缓存"))
        self.assertEqual(display_price.cached_scale("cn", "600531", None), (1.0, "无缓存"))

    def test_bad_cached_raw_values_are_misses(self):
        """JSON 的 true 会被 float() 变成 1.0；Infinity/NaN/字符串/负数/0 等都不能当成真实价。"""
        bad = ["true", "false", "null", '"10.1"', "[10.1]", '{"a": 1}', "Infinity", "-Infinity", "NaN", "0", "0.0", "-5", "1e999", "1e-999", '""']
        for text in bad:
            with self.subTest(raw=text):
                self.write_raw(text)
                self.assertEqual(display_price.cached_scale("cn", "600531", frame()), (1.0, "无缓存"), text)
        self.write_raw("10")                                        # 整数是合法的价格
        self.assertEqual(display_price.cached_scale("cn", "600531", frame())[1], "前复权")

    def test_cache_entry_must_be_an_object(self):
        for entry in ("10.1", "[10.1]", '"x"', "true", "null"):
            with self.subTest(entry=entry):
                self.cache.write_text('{"600531": %s}' % entry, encoding="utf-8")
                self.assertEqual(display_price.cached_scale("cn", "600531", frame()), (1.0, "无缓存"))

    def test_bad_last_close_is_a_miss(self):
        self.write_raw("10.1")
        for bad in (float("nan"), 0.0, -1.0, float("inf"), float("-inf")):
            with self.subTest(close=bad):
                self.assertEqual(display_price.cached_scale("cn", "600531", frame(close=bad)), (1.0, "无缓存"))

    def test_non_numeric_last_close_types_are_misses(self):
        """Close 的原始类型也要校验：float(True)=1.0、float("75.694") 都会把坏数据变成"好价格"（Codex r2 R1）。"""
        from decimal import Decimal
        self.write_raw("10.1")
        for name, bad in (("bool", True), ("str", "75.694"), ("Decimal", Decimal("75.694")), ("None", None),
                          ("list", [75.694]), ("np.bool_", np.bool_(True)), ("huge int", 10 ** 400)):
            with self.subTest(close=name):
                df = pd.DataFrame({"Close": pd.Series([70.0, bad], dtype=object).values},
                                  index=pd.to_datetime(["2026-10-22", "2026-10-23"])) if name != "huge int" else \
                    pd.DataFrame({"Close": [70.0, 1.0]}, index=pd.to_datetime(["2026-10-22", "2026-10-23"]), dtype=object)
                if name == "huge int":
                    df.iloc[-1, 0] = bad
                self.assertEqual(display_price.cached_scale("cn", "600531", df), (1.0, "无缓存"))

    def test_numpy_real_scalar_close_is_accepted(self):
        self.write_raw("10.1")
        for dt in ("float64", "float32"):
            with self.subTest(dtype=dt):
                df = pd.DataFrame({"Close": np.array([70.0, 75.694], dtype=dt)}, index=pd.to_datetime(["2026-10-22", "2026-10-23"]))
                k, note = display_price.cached_scale("cn", "600531", df)
                self.assertEqual(note, "前复权")
                self.assertAlmostEqual(float(df["Close"].iloc[-1]) * k, 10.1, places=3)

    def test_overflow_and_underflow_of_the_factor_are_misses(self):
        self.write_raw("1e308")
        self.assertEqual(display_price.cached_scale("cn", "600531", frame(close=1e-300)), (1.0, "无缓存"))     # k = inf
        self.write_raw("1e-300")
        self.assertEqual(display_price.cached_scale("cn", "600531", frame(close=1e300)), (1.0, "无缓存"))      # k 下溢为 0

    def test_never_calls_network_helper(self):
        """用 mock 记录调用次数并在结束后独立断言（side_effect 抛错会被生产代码的 except 吞掉，不能当证据）。"""
        with patch("engines.common._raw_close_tencent", side_effect=AssertionError("不该联网取价")) as raw_helper:
            self.assertEqual(display_price.cached_scale("cn", "600531", frame()), (1.0, "无缓存"))       # 缓存为空 = 会诱使代码去联网
            self.write_raw("10.1")
            display_price.cached_scale("cn", "600531", frame())
        raw_helper.assert_not_called()


class HoldingsPriceTests(Base):
    def build(self, df, drop=None):
        cls = ast.ClassDef(name="Handler", bases=[], keywords=[], decorator_list=[], body=[METHOD])
        ns = dict(MARKETS={"cn": {"tz": "Asia/Shanghai", "close_ready": "15:30"}}, display_price=display_price,
                  datasource=SimpleNamespace(load_cached=lambda m, c: df, drop_incomplete_bar=drop or (lambda d, tz, cr: d)))
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<handler>", "exec"), ns)
        return ns["Handler"]()

    def test_returns_real_unadjusted_price_not_hfq(self):
        self.write({"600531": {"day": "2026-10-23", "k": 0.1334, "raw": 10.1}})
        px, day = self.build(frame())._holdings_price("600531")
        self.assertEqual(day, "2026-10-23")
        self.assertAlmostEqual(px, 10.1, places=4)
        self.assertNotAlmostEqual(px, 75.694, places=1)         # 绝不能是后复权价
        self.assertEqual(round(px, 2), px)                        # 个股真实价是两位小数

    def test_missing_real_price_gives_none_not_hfq(self):
        """缓存里没有这一天的真实价：返回 None(页面显示"无现价")，而不是把后复权价当现价。"""
        for cache in ({}, {"600531": {"day": "2026-10-22", "k": 0.1, "raw": 10.1}}):
            self.write(cache)
            self.assertIsNone(self.build(frame())._holdings_price("600531"), cache)

    def test_no_data_or_broken_cache_gives_none(self):
        self.assertIsNone(self.build(frame().iloc[0:0])._holdings_price("600531"))
        self.assertIsNone(self.build(None)._holdings_price("600531"))
        self.cache.write_text("[]", encoding="utf-8")
        self.assertIsNone(self.build(frame())._holdings_price("600531"))

    def test_handler_returns_none_for_every_bad_cached_price(self):
        for text in ("true", "Infinity", "NaN", '"10.1"', "0", "-5", "1e999", "null"):
            with self.subTest(raw=text):
                self.write_raw(text)
                self.assertIsNone(self.build(frame())._holdings_price("600531"), text)

    def test_handler_rejects_a_non_finite_or_non_positive_final_price(self):
        self.write_raw("1e-9")                                       # 还原后四舍五入到 4 位 = 0.0
        self.assertIsNone(self.build(frame(close=1.0))._holdings_price("600531"))

    def test_handler_never_touches_the_network_or_the_networked_helpers(self):
        self.write_raw("10.1")
        with patch.object(display_price, "scale_for", side_effect=AssertionError("scale_for 会联网")) as sf, \
                patch.object(display_price, "apply", side_effect=AssertionError("apply 会联网")) as ap, \
                patch("engines.common._raw_close_tencent", side_effect=AssertionError("不该联网取价")) as raw_helper:
            self.assertIsNotNone(self.build(frame())._holdings_price("600531"))                  # 命中
            self.write({})                                                                       # 缓存为空，会诱使代码联网
            self.assertIsNone(self.build(frame())._holdings_price("600531"))
        for m in (sf, ap, raw_helper):
            m.assert_not_called()                                                                # 独立断言：一次都没调用

    def test_half_bar_filtering_changes_which_cached_day_must_match(self):
        """盘中过滤会去掉今天那根半截 K 线：缓存必须匹配过滤后的最后一天，不能拿今天的真实价配昨天的 K 线。"""
        drop_last = lambda d, tz, cr: d.iloc[:-1]                 # 模拟 drop_incomplete_bar 去掉盘中半根
        df = frame()                                              # 10-22 收 70.0；10-23 是今天（半截）
        self.write_raw("10.1", day="2026-10-23")                 # 只有今天的真实价
        self.assertIsNone(self.build(df, drop=drop_last)._holdings_price("600531"))
        self.write_raw("9.8", day="2026-10-22")                  # 有昨天的真实价
        px, day = self.build(df, drop=drop_last)._holdings_price("600531")
        self.assertEqual((px, day), (9.8, "2026-10-22"))

    def test_source_does_not_use_network_scale(self):
        src = ast.get_source_segment((ROOT / "app.py").read_text(encoding="utf-8"), METHOD)
        self.assertIn("cached_scale", src)
        self.assertNotIn("scale_for", src)                        # scale_for 缓存没命中会联网，持仓页不能用
        self.assertNotIn("display_price.apply", src)


if __name__ == "__main__":
    unittest.main()
