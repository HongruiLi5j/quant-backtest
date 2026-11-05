"""B08 个股详情 /api/stock 结果缓存的单测：只用临时目录和合成数据，不读写 data/ 下任何真实文件，不联网，不 import app。
运行:.venv/bin/python tests/test_resultcache_b08.py

四部分:
  1. detail.input_stamps:build() 会读的每一个文件改了,指纹都要变;15:30 状态变了也要变。
  2. detail.cacheable:出错、A 股没取到真实价格的结果不缓存。
  3. app.Handler._stock_body(用 ast 取出方法源码在桩对象上跑,同 test_resultcache_b07 的做法):
     同参数第二次命中;输入文件变了失效;身份(远程/主人/本机)和个人模式之间不串数据;LRU 上限、过期。
  4. 守卫:哪些地方允许用结果缓存是白名单,带个人数据的接口(持仓、我的相关、会员、后台、AI、分享…)不能出现缓存;
     新增缓存用户必须改这个测试(强迫有人看一眼它会不会泄露个人数据)。
"""
import ast
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import MARKETS  # noqa: E402
from src import allmarket, datasource, detail, display_price, resultcache, usdata  # noqa: E402

APP = (ROOT / "app.py").read_text(encoding="utf-8")
BJ = ZoneInfo("Asia/Shanghai")


def _csv(path: Path, seed=0, rows=700):
    """合成日线 csv(随机游走),格式同 datasource.load_cached 读的那种。"""
    rng = np.random.default_rng(seed)
    close = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, rows))
    idx = pd.bdate_range("2023-01-02", periods=rows)
    df = pd.DataFrame({"Open": close * 0.999, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                       "Volume": 1e6}, index=idx)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, float_format="%.6f")


class TempData(unittest.TestCase):
    """把 datasource / allmarket / display_price / usdata 的路径都指到临时目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cache = base / "daily"
        self.us_root = base / "usdata"
        (self.us_root / "data").mkdir(parents=True)
        self.patches = [
            patch.object(datasource, "CACHE", self.cache),
            patch.object(allmarket, "ALL", self.cache / "cn_all"),
            patch.object(allmarket, "LIST_FILE", self.cache / "cn_all_list.csv"),
            patch.object(display_price, "_SCALE_FILE", self.cache / "engine_price_scale.json"),
            patch.object(usdata, "root", lambda: self.us_root),
            patch.object(allmarket, "_now_bj", lambda: datetime(2026, 3, 2, 16, 0, tzinfo=BJ)),
        ]
        for p in self.patches:
            p.start()
        self.mcfg = MARKETS["us"]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def stamps(self, market="us", code="SPY", mcfg=None):
        return detail.input_stamps(market, code, mcfg or self.mcfg)


class TestInputStamps(TempData):
    def test_every_input_file_changes_the_stamp(self):
        """build() 读的每个文件:不存在 → 出现 → 内容变,指纹每一步都要不同。"""
        files = {
            "ETF/自选股行情 csv": datasource.cache_path("us", "SPY"),
            "现金 csv": datasource.cache_path("us", self.mcfg["cash"]),
            "自选股名单": datasource._watch_path("us"),
            "全市场股票清单": allmarket.LIST_FILE,
            "A 股全市场 parquet": allmarket.path("SPY"),
            "复权系数": display_price._SCALE_FILE,
            "美股全市场 parquet": self.us_root / "data" / "SPY.parquet",
            "美股名称表": self.us_root / "universe.csv",
        }
        for label, f in files.items():
            with self.subTest(label):
                f.parent.mkdir(parents=True, exist_ok=True)
                s0 = self.stamps()
                f.write_bytes(b"1234")
                s1 = self.stamps()
                self.assertNotEqual(s0, s1, f"{label} 出现后指纹没变")
                tmp = f.with_suffix(".swap")
                tmp.write_bytes(b"abcd")                 # 同样 4 字节、内容不同;原子替换
                tmp.replace(f)
                s2 = self.stamps()
                self.assertNotEqual(s1, s2, f"{label} 内容变了指纹没变")

    def test_unchanged_files_give_equal_stamps(self):
        _csv(datasource.cache_path("us", "SPY"))
        self.assertEqual(self.stamps(), self.stamps())

    def test_clock_state_is_part_of_the_stamp(self):
        before = self.stamps()
        with patch.object(allmarket, "_now_bj", lambda: datetime(2026, 3, 2, 15, 29, tzinfo=BJ)):
            b1530 = self.stamps()                         # 同一天,但还没到 15:30
        with patch.object(allmarket, "_now_bj", lambda: datetime(2026, 3, 3, 16, 0, tzinfo=BJ)):
            nextday = self.stamps()                       # 第二天
        self.assertNotEqual(before, b1530)
        self.assertNotEqual(before, nextday)
        with patch.object(allmarket, "_now_bj", lambda: datetime(2026, 3, 2, 17, 0, tzinfo=BJ)):
            self.assertEqual(before, self.stamps())       # 同一天、都已过 15:30:指纹相同,缓存可以继续用

    def test_no_us_data_dir_is_fine(self):
        with patch.object(usdata, "root", lambda: None):
            self.assertEqual(len(self.stamps()[0]), 6)    # 少了两个美股文件,其余照常


class TestBjClockState(unittest.TestCase):
    def test_boundary(self):
        f = lambda h, m: resultcache.bj_clock_state(datetime(2026, 3, 2, h, m, tzinfo=BJ))
        self.assertEqual(f(15, 29), ("2026-03-02", False))
        self.assertEqual(f(15, 30), ("2026-03-02", True))
        self.assertEqual(f(0, 0), ("2026-03-02", False))


class TestCacheable(TempData):
    def test_error_and_degraded_results_are_not_cacheable(self):
        self.assertFalse(detail.cacheable({"error": "x 没有数据"}))
        self.assertFalse(detail.cacheable({"info": {"price_basis": "未取到真实价格，显示为后复权"}}))
        self.assertFalse(detail.cacheable(None))
        self.assertTrue(detail.cacheable({"info": {"price_basis": "前复权"}}))
        self.assertTrue(detail.cacheable({"info": {"price_basis": ""}}))

    def test_real_build_on_synthetic_data_is_cacheable_and_deterministic(self):
        _csv(datasource.cache_path("us", "SPY"), seed=1)
        _csv(datasource.cache_path("us", self.mcfg["cash"]), seed=2)
        a = detail.build("us", "SPY", self.mcfg, "blend2", "1y")
        b = detail.build("us", "SPY", self.mcfg, "blend2", "1y")
        self.assertNotIn("error", a)
        self.assertTrue(detail.cacheable(a))
        self.assertEqual(json.dumps(a, default=float), json.dumps(b, default=float))   # 纯函数:同输入同输出,缓存才安全

    def test_real_build_degraded_cn_price_is_not_cacheable(self):
        _csv(datasource.cache_path("cn", "510300"), seed=3)
        _csv(datasource.cache_path("cn", "511880"), seed=4)
        cn = dict(MARKETS["cn"], universe={"510300": "沪深300ETF"}, cash="511880")
        with patch.object(display_price, "scale_for", lambda m, c, df: (1.0, "未取到真实价格，显示为后复权")):
            r = detail.build("cn", "510300", cn, "blend2", "1y")
        self.assertNotIn("error", r)
        self.assertFalse(detail.cacheable(r))


# ---------------------------------------------------------------- app.Handler._stock_body / _cache_scope
def load_handler_methods(names, env):
    tree = ast.parse(APP)
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Handler":
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name in names:
                    exec(compile(ast.Module(body=[f], type_ignores=[]), "app.py", "exec"), env)
                    out[f.name] = env[f.name]
    missing = set(names) - set(out)
    if missing:
        raise AssertionError(f"找不到 Handler 方法: {missing}")
    return out


class FakeClock:
    t = 0.0

    def __call__(self):
        return self.t


class FakeSelf:
    """只提供 _stock_body / _cache_scope 用到的东西。"""

    def __init__(self, outer, who="local", personal=False):
        self.outer, self.who, self._personal_mode = outer, who, personal

    def _remote(self):
        return self.who == "remote"

    def _owner(self):
        return self.who == "owner"


class TestStockBody(unittest.TestCase):
    def setUp(self):
        self.builds = []
        self.stamp = ("S1",)
        self.clock = FakeClock()
        self.memo = resultcache.Memo(maxsize=24, ttl=600, clock=self.clock)
        outer = self

        class FakeDetail:
            SINGLE = ["blend2", "trend_ens"]

            @staticmethod
            def build(m, code, mcfg, strategy="blend2", period="1y"):
                outer.builds.append((m, code, strategy, period, outer.viewer))
                if code == "BAD":
                    return {"error": "BAD 没有数据"}
                basis = "未取到真实价格，显示为后复权" if code == "DEGRADED" else "前复权"
                # 故意把「当前是谁在看」写进结果:如果缓存串了身份,另一个身份会拿到别人的 viewer
                return {"info": {"price_basis": basis}, "code": code, "strategy": strategy, "period": period,
                        "viewer": outer.viewer, "n": len(outer.builds)}

            @staticmethod
            def input_stamps(m, code, mcfg):
                return outer.stamp

            cacheable = staticmethod(detail.cacheable)

        self.viewer = "local"
        env = {"MARKETS": {"cn": {}, "us": {}}, "detail": FakeDetail, "_stock_memo": self.memo, "json": json}
        self.m = load_handler_methods({"_stock_body", "_cache_scope"}, env)
        FakeSelf._cache_scope = self.m["_cache_scope"]      # 桩对象用 app.py 里真正的 _cache_scope

    def body(self, who="local", personal=False, code="510300", strategy="blend2", period="1y", market="cn"):
        self.viewer = who
        s = FakeSelf(self, who, personal)
        return json.loads(self.m["_stock_body"](s, market, code, strategy, period))

    # ---- 命中 / 失效
    def test_second_call_hits_cache(self):
        a = self.body()
        b = self.body()
        self.assertEqual(len(self.builds), 1)
        self.assertEqual(a, b)

    def test_cached_value_is_immutable_bytes(self):
        s = FakeSelf(self)
        a = self.m["_stock_body"](s, "cn", "510300", "blend2", "1y")
        self.assertIsInstance(a, bytes)
        self.assertIs(self.m["_stock_body"](s, "cn", "510300", "blend2", "1y"), a)

    def test_input_file_change_invalidates(self):
        a = self.body()
        self.stamp = ("S2",)                               # 行情 / 自选股 / 复权系数 / 15:30 状态任一项变了
        b = self.body()
        self.assertEqual(len(self.builds), 2)
        self.assertNotEqual(a["n"], b["n"])
        self.body()
        self.assertEqual(len(self.builds), 2)              # 新指纹下又能命中

    def test_params_are_part_of_the_key(self):
        self.body()
        self.body(code="600531")
        self.body(period="all")
        self.body(strategy="trend_ens")
        self.body(market="us")
        self.assertEqual(len(self.builds), 5)

    def test_unknown_strategy_shares_entry_with_blend2(self):
        """build() 把不认识的策略当 blend2,所以它们是同一个结果,只存一份。"""
        self.body(strategy="blend2")
        self.body(strategy="no_such_strategy")
        self.assertEqual(len(self.builds), 1)

    def test_errors_and_degraded_prices_are_not_cached(self):
        self.assertEqual(self.body(code="BAD")["error"], "BAD 没有数据")
        self.body(code="BAD")
        self.body(code="DEGRADED")
        self.body(code="DEGRADED")
        self.assertEqual(len(self.builds), 4)              # 4 次请求,4 次都真算了
        self.assertEqual(len(self.memo), 0)

    def test_ttl_is_a_backstop(self):
        self.body()
        self.clock.t = 601
        self.body()
        self.assertEqual(len(self.builds), 2)

    def test_memory_is_bounded(self):
        for i in range(60):
            self.body(code=f"{600000 + i}")
        self.assertLessEqual(len(self.memo), 24)

    # ---- 身份 / 模式之间不串数据
    def test_identities_do_not_share_entries(self):
        seen = {}
        for who in ("remote", "owner", "local"):
            seen[who] = self.body(who=who)["viewer"]
        self.assertEqual(seen, {"remote": "remote", "owner": "owner", "local": "local"})
        self.assertEqual(len(self.builds), 3)              # 三种身份各算一次
        for who in ("remote", "owner", "local"):           # 之后各自命中自己那份,看到的还是自己的
            self.assertEqual(self.body(who=who)["viewer"], who)
        self.assertEqual(len(self.builds), 3)

    def test_personal_mode_does_not_share_entries(self):
        self.body(personal=False)
        self.body(personal=True)
        self.assertEqual(len(self.builds), 2)
        self.body(personal=False)
        self.body(personal=True)
        self.assertEqual(len(self.builds), 2)

    def test_scope_values(self):
        sc = lambda who, p: self.m["_cache_scope"](FakeSelf(self, who, p))
        self.assertEqual(sc("remote", False), ("remote", False))
        self.assertEqual(sc("owner", True), ("owner", True))
        self.assertEqual(sc("local", False), ("local", False))


# ---------------------------------------------------------------- 守卫:缓存只能出现在白名单里
class TestCacheGuard(unittest.TestCase):
    """带个人数据的接口不能加缓存。做法:白名单。谁用了结果缓存是明确列出来的,多出来的测试直接失败。"""

    # app.py 里允许碰这些缓存对象的 Handler 方法(三个都只读公开行情;回测和逐只表的结果与身份无关,见 B07 报告)
    APP_ALLOWED = {"_backtest": {"_backtest_memo"}, "_table": {"_table_memo"}, "_stock_body": {"_stock_memo"}}
    MEMO_NAMES = {"_backtest_memo", "_table_memo", "_stock_memo"}
    # src/ 里允许 import resultcache 的模块
    SRC_ALLOWED = {"resultcache.py", "trade_signals.py", "detail.py"}
    # 这些关键词出现在被缓存的函数里就说明它在碰个人数据
    PERSONAL_WORDS = ("members.", "_member_user", "trade_private", "holdings", "simtrade", "_trade_local_sessions",
                      "owner_access", "admin.", "sharepass", "ledger_summary", "upload_api", "notifications")

    def functions_using_memos(self):
        tree = ast.parse(APP)
        found = {}
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)] + [tree]:
            for f in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
                names = {n.id for n in ast.walk(f) if isinstance(n, ast.Name) and n.id in self.MEMO_NAMES}
                if names:
                    found[f.name] = names
        return found

    def test_only_whitelisted_handler_methods_touch_the_memos(self):
        self.assertEqual(self.functions_using_memos(), self.APP_ALLOWED)

    def test_cached_methods_do_not_touch_personal_data(self):
        tree = ast.parse(APP)
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler"]:
            for f in cls.body:
                if isinstance(f, ast.FunctionDef) and f.name in self.APP_ALLOWED:
                    src = ast.get_source_segment(APP, f)
                    for w in self.PERSONAL_WORDS:
                        self.assertNotIn(w, src, f"{f.name} 里出现了 {w}:带个人数据的接口不能缓存")

    def test_personal_routes_never_reach_a_cached_method(self):
        """do_GET 里分派给缓存方法的只有 /api/backtest、/api/table、/api/stock、/api/trade_list_tracking;
        个人路径(holdings / trade_list_mine / member / admin / ai / ledger / sim)的分支里不能出现缓存方法。"""
        tree = ast.parse(APP)
        do_get = next(f for c in tree.body if isinstance(c, ast.ClassDef) and c.name == "Handler"
                      for f in c.body if isinstance(f, ast.FunctionDef) and f.name == "do_GET")
        cached_calls = {"_backtest", "_table", "_stock_body"}
        personal = ("/api/holdings", "/api/trade_list_mine", "/api/member", "/api/admin", "/api/ai", "/api/sim", "/api/ledger")
        for node in ast.walk(do_get):
            if isinstance(node, ast.If):
                test_src = ast.get_source_segment(APP, node.test) or ""
                if any(p in test_src for p in personal):
                    body_src = "\n".join(ast.get_source_segment(APP, s) or "" for s in node.body)
                    for c in cached_calls:
                        self.assertNotIn(c, body_src, f"个人路径分支 {test_src[:50]} 调用了缓存方法 {c}")

    def test_every_memo_has_a_size_cap(self):
        """所有 Memo(...) 都必须显式给 maxsize,防止内存膨胀。"""
        sources = [("app.py", APP)] + [(p.name, p.read_text(encoding="utf-8")) for p in sorted((ROOT / "src").rglob("*.py"))]
        n = 0
        for fname, text in sources:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Memo"
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "resultcache"):
                    n += 1
                    cap = [k for k in node.keywords if k.arg == "maxsize"] or node.args
                    self.assertTrue(cap, f"{fname}:{node.lineno} 的 Memo 没有设 maxsize")
        self.assertGreaterEqual(n, 4)       # _backtest / _table / _stock(app.py) + trade_signals

    def test_only_whitelisted_src_modules_import_resultcache(self):
        users = set()
        for p in (ROOT / "src").rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            if "resultcache" in text:
                users.add(p.name)
        self.assertEqual(users, self.SRC_ALLOWED)

    def test_personal_modules_do_not_use_the_cache(self):
        for rel in ("src/holdings", "src/trade_private.py", "src/members.py", "src/admin.py", "src/simtrade.py",
                    "src/ai_translator", "src/paper_review", "src/upload_api.py"):
            p = ROOT / rel
            files = list(p.rglob("*.py")) if p.is_dir() else [p]
            for f in files:
                if f.exists():
                    self.assertNotIn("resultcache", f.read_text(encoding="utf-8"), f"{rel} 不应使用结果缓存")


if __name__ == "__main__":
    unittest.main(verbosity=2)
