"""令牌登录失败限流（src/login_throttle.py，E07 / D01 第 18 条）的测试。
只用标准库；不导入 app（用 AST 从 app.py 摘出 do_GET 和 _login_source 跑接线测试）；不读写 data/。
运行：.venv/bin/python tests/test_login_throttle_e07.py"""
import ast
import hmac
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import admin, login_throttle  # noqa: E402
from src.login_throttle import LoginThrottle, source_key  # noqa: E402

HOSTS = {"127.0.0.1:8765", "localhost:8765"}


class SourceKeyTests(unittest.TestCase):
    def test_local_is_one_bucket(self):
        self.assertEqual(source_key({"Host": "127.0.0.1:8765"}, True), "local")

    def test_remote_uses_cf_ip(self):
        h = {"CF-Connecting-IP": "203.0.113.7"}
        self.assertEqual(source_key(h, False), "ip:203.0.113.7")
        self.assertNotEqual(source_key(h, False), source_key({"CF-Connecting-IP": "203.0.113.8"}, False))

    def test_x_forwarded_for_is_ignored(self):
        # 客户端可以随便写 X-Forwarded-For，不能用它换来源
        self.assertEqual(source_key({"X-Forwarded-For": "1.2.3.4"}, False), login_throttle.UNKNOWN_KEY)

    def test_missing_or_garbage_ip_shares_one_bucket(self):
        for h in ({}, {"CF-Connecting-IP": ""}, {"CF-Connecting-IP": "not-an-ip"}, {"CF-Connecting-IP": "999.1.1.1"}):
            self.assertEqual(source_key(h, False), login_throttle.UNKNOWN_KEY)

    def test_ipv6_grouped_by_64(self):
        a = source_key({"CF-Connecting-IP": "2001:db8:1:2::1"}, False)
        b = source_key({"CF-Connecting-IP": "2001:db8:1:2:ffff:ffff:ffff:ffff"}, False)
        c = source_key({"CF-Connecting-IP": "2001:db8:1:3::1"}, False)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_ipv4_mapped_ipv6_folds_to_ipv4(self):
        self.assertEqual(source_key({"CF-Connecting-IP": "::ffff:203.0.113.7"}, False), "ip:203.0.113.7")

    def test_comma_list_takes_first(self):
        self.assertEqual(source_key({"CF-Connecting-IP": "203.0.113.7, 10.0.0.1"}, False), "ip:203.0.113.7")


def make(**kw):
    """人造时间戳的测试里，时钟也固定在 1000，否则「上次清理时间」用的是真实时间，清理永远不触发。"""
    kw.setdefault("clock", lambda: 1000.0)
    return LoginThrottle(**kw)


class ThrottleTests(unittest.TestCase):
    def test_same_source_limit_and_window_unchanged(self):
        # 和旧版同一来源的规则一致：窗口 600 秒，满 10 次才锁，第 10 次失败后立即锁
        t = make()
        self.assertEqual((t.limit, t.window), (10, 600.0))
        for i in range(9):
            t.record("ip:a", now=1000 + i)
        self.assertFalse(t.blocked("ip:a", now=1010))
        t.record("ip:a", now=1009)
        self.assertTrue(t.blocked("ip:a", now=1010))

    def test_boundary_matches_old_rule(self):
        # 旧规则：t > now - 600 才算窗口内。第一次在 1000，now=1600 时 1000 > 1000 不成立 → 已过期
        t = make()
        for i in range(10):
            t.record("ip:a", now=1000 + i)
        self.assertTrue(t.blocked("ip:a", now=1599.9))
        self.assertFalse(t.blocked("ip:a", now=1600))

    def test_one_source_does_not_lock_others(self):
        t = make()
        for i in range(50):
            t.record("ip:attacker", now=1000 + i)
        self.assertTrue(t.blocked("ip:attacker", now=1100))
        self.assertFalse(t.blocked("local", now=1100))
        self.assertFalse(t.blocked("ip:other", now=1100))

    def test_blocked_does_not_create_entries(self):
        t = make()
        for i in range(100):
            t.blocked(f"ip:{i}", now=1000)
        self.assertEqual(t.size(), 0)

    def test_blocked_requests_are_not_extended(self):
        # 被拒绝的请求不再计数（旧版也是这样），否则攻击者可以让站主永远等不到解锁
        t = make()
        for i in range(10):
            t.record("ip:a", now=1000 + i)
        for i in range(100):
            if not t.blocked("ip:a", now=1100 + i):
                t.record("ip:a", now=1100 + i)
        self.assertFalse(t.blocked("ip:a", now=1700))

    def test_expired_entries_are_swept(self):
        t = make()
        for i in range(200):
            t.record(f"ip:{i}", now=1000)
        self.assertEqual(t.size(), 200)
        t.blocked("ip:new", now=1000 + 700)        # 超过扫描间隔，顺手清理
        t.record("ip:new", now=1000 + 700)
        self.assertEqual(t.size(), 1)

    def test_table_size_is_capped(self):
        t = make(max_sources=20)
        for i in range(500):
            t.record(f"ip:{i}", now=1000 + i * 0.001)
        self.assertLessEqual(t.size(), 21)               # 20 个来源 + 1 个共用的溢出桶

    def test_overflow_bucket_still_limits_guessing(self):
        # 表满后，新来源共用一个桶，仍然会在 10 次后被锁；不会因为"换新来源"而获得无限次猜测
        t = make(max_sources=5)
        for i in range(5):
            t.record(f"ip:{i}", now=1000)
        for i in range(10):
            t.record(f"ip:new{i}", now=1001)
        self.assertTrue(t.blocked("ip:new_x", now=1002))
        self.assertTrue(t.blocked(login_throttle.OVERFLOW_KEY, now=1002))

    def test_full_table_does_not_evict_existing_counts(self):
        t = make(max_sources=5)
        for i in range(10):
            t.record("ip:0", now=1000 + i)
        for i in range(1, 5):
            t.record(f"ip:{i}", now=1000)
        for i in range(300):
            t.record(f"ip:flood{i}", now=1001)
        self.assertTrue(t.blocked("ip:0", now=1010))     # 灌入大量假来源洗不掉已有来源的失败次数

    def test_local_always_has_room(self):
        t = make(max_sources=3)
        for i in range(3):
            t.record(f"ip:{i}", now=1000)
        self.assertFalse(t.blocked("local", now=1001))
        t.record("local", now=1001)
        self.assertEqual(t.size(), 4)
        self.assertFalse(t.blocked("local", now=1002))     # 本机只被自己的失败次数影响，不被溢出桶牵连

    def test_clear(self):
        t = make()
        for i in range(10):
            t.record("ip:a", now=1000)
        t.clear()
        self.assertFalse(t.blocked("ip:a", now=1001))
        self.assertEqual(t.size(), 0)

    def test_per_source_memory_is_bounded(self):
        t = make()
        for i in range(1000):
            t.record("ip:a", now=1000)
        self.assertLessEqual(len(t._hits["ip:a"]), t.limit)

    def test_threads(self):
        t = make()

        def work(n):
            for i in range(200):
                t.record(f"ip:{n}", now=1000 + i * 0.01)
                t.blocked(f"ip:{n}", now=1000 + i * 0.01)

        ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for th in ts:
            th.start()
        for th in ts:
            th.join()
        self.assertEqual(t.size(), 8)


# ---------------------------------------------------------------- 接线：app.py 的 do_GET 真的按来源分开
TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
METHODS = {n.name: n for n in HANDLER.body if isinstance(n, ast.FunctionDef)}
TOKEN = "synthetic-token"


def build_handler():
    cls = ast.ClassDef(name="H", bases=[], keywords=[], decorator_list=[],
                       body=[METHODS["do_GET"], METHODS["_login_source"]])
    ns = dict(urlparse=urlparse, parse_qs=parse_qs, hmac=hmac, admin=admin, login_throttle=login_throttle,
              ALLOWED_HOSTS=HOSTS, TOKEN=TOKEN, COOKIE="qb", _bad_login=LoginThrottle(), _alive={}, time=time)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<handler>", "exec"), ns)
    return ns


class Stubs:
    """只造 do_GET 走到令牌登录分支需要的东西；其余统统短路。"""

    def __init__(self, headers, path):
        self.headers = headers
        self.path = path
        self.sent = []

    def _guard(self):
        return True

    def _personal_blocked(self):
        return False

    def _maintenance_blocked(self):
        return False

    def _share_gate(self, u):
        return False

    def _remote(self):
        return self.headers.get("Host", "").endswith(".trycloudflare.com")

    def _https(self):
        return self._remote()

    def _log_visit(self, path):     # 真实实现会写 data/share_visits.jsonl，测试里不碰
        pass

    def _authed(self):
        return False

    def _send(self, code, body, ctype, headers=None, **kw):
        self.sent.append(code)


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.ns = build_handler()
        self.req = type("R", (Stubs, self.ns["H"]), {})

    def login(self, headers, token):
        r = self.req(headers, f"/?t={token}")
        r.do_GET()
        return r.sent[-1]

    def test_share_visitor_cannot_lock_local_owner(self):
        visitor = {"Host": "abc.trycloudflare.com", "CF-Connecting-IP": "203.0.113.7"}
        local = {"Host": "127.0.0.1:8765"}
        codes = [self.login(visitor, "wrong") for _ in range(10)]
        self.assertEqual(codes, [401] * 10)
        self.assertEqual(self.login(visitor, "wrong"), 429)         # 访客自己被锁
        self.assertEqual(self.login(visitor, TOKEN), 429)           # 锁住期间连对的令牌也不收（和旧版一致）
        self.assertEqual(self.login(local, TOKEN), 303)             # 站主本机不受影响
        self.assertEqual(self.login(local, "wrong"), 401)

    def test_two_visitors_are_independent(self):
        a = {"Host": "abc.trycloudflare.com", "CF-Connecting-IP": "203.0.113.7"}
        b = {"Host": "abc.trycloudflare.com", "CF-Connecting-IP": "198.51.100.9"}
        for _ in range(10):
            self.login(a, "wrong")
        self.assertEqual(self.login(a, "wrong"), 429)
        self.assertEqual(self.login(b, TOKEN), 303)

    def test_local_still_locks_itself_after_10(self):
        local = {"Host": "127.0.0.1:8765"}
        for _ in range(10):
            self.assertEqual(self.login(local, "wrong"), 401)
        self.assertEqual(self.login(local, "wrong"), 429)

    def test_proxy_header_on_local_host_is_not_local(self):
        # Host 是本机但带了代理头：不算本机直连，按头里的 IP 计
        h = {"Host": "127.0.0.1:8765", "CF-Connecting-IP": "203.0.113.7"}
        self.assertEqual(self.req(h, "/")._login_source(), "ip:203.0.113.7")

    def test_app_uses_new_throttle(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("_bad_login = login_throttle.LoginThrottle()", src)
        self.assertNotIn("_bad_login.append", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
