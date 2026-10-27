"""publish_admin 测试:全部用替身 runner,不真正构建、不读 data/、不联网、不导入 app。"""
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import admin, admin_settings, publish_admin as pa  # noqa: E402


def on(flag=True):
    return patch.object(admin_settings, "get_all", return_value={"publish_check_on": flag})            # 替身接受 strict 参数


def fake(stdout="ok", stderr="", code=0):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)
    run.calls = calls
    return run


class RunCheckTests(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(admin_settings.DEFAULTS["publish_check_on"])
        for v in (False, None, "yes", 1):
            with on(v), self.assertRaises(pa.PublishCheckError):
                pa.run_check(runner=fake())
        with patch.object(admin_settings, "get_all", side_effect=RuntimeError("boom")):
            self.assertFalse(pa.enabled())                                           # 读失败按关闭

    def test_db_failure_does_not_fall_back_to_cached_true(self):
        # 真实 get_all：缓存里是 True，但这一次读库失败 → strict 读取必须抛出，enabled() 返回 False，runner 不被调用
        with patch.object(admin_settings, "_last_good", {"publish_check_on": True}), \
                patch.object(admin_settings.members, "_conn", side_effect=RuntimeError("db down")):
            self.assertFalse(pa.enabled())
            r = fake()
            with self.assertRaises(pa.PublishCheckError):
                pa.run_check(runner=r)
            self.assertEqual(r.calls, [])
            self.assertFalse(admin_settings.upload_check_on())                              # 上传体检同样按关闭
            self.assertTrue(admin_settings.get_all()["publish_check_on"])                    # 非 strict 仍退回缓存（其他功能的既有行为不变）

    def test_output_is_redacted(self):
        raw = f"构建 通过：{pa.ROOT}/build/2026/site\nfile {Path.home()}/Documents/x\n  /private/var/folders/ab/cd/T/x.tmp\n命令 .venv/bin/python scripts/build_public_site.py"
        with on():
            out = pa.run_check(runner=fake(raw, f"Traceback {pa.ROOT}/src/y.py"))["output"]
        self.assertNotIn(str(pa.ROOT), out)
        self.assertNotIn(str(Path.home()), out)
        self.assertNotIn("/private/var", out)
        self.assertIn("<项目>/build/2026/site", out)
        self.assertIn(".venv/bin/python scripts/build_public_site.py", out)                   # 相对路径的命令保持可复制

    def test_redact_bypass_attempts(self):
        home = Path.home()
        leaks = ["C:/Users/alice/private.txt", "error:/etc/hosts", "FILE:///etc/hosts", "%2Fetc%2Fhosts", "..\\..\\etc\\hosts", "PATH=/usr/bin", f"file://{pa.ROOT}/x", "file:///Users/someone/secret.txt", r"C:\Users\x\y.txt", "../../etc/passwd", "/etc/hosts", "/Library/Foo/bar",
                 "/Applications/App", "/mnt/x/y", "/root/.x", "/data/daily/x", "/System/Volumes/Data/x", f"user {home.name} here", f"'{pa.ROOT}/a b'"]
        for c in leaks:
            out = pa.redact(c)
            for bad in ("/Users", "/etc", "/Library", "/Applications", "/mnt", "/root", "/data/", "C:\\", "C:/", "..\\", "../", "%2F", home.name, str(pa.ROOT)):
                self.assertNotIn(bad, out, (c, out))
        keep = ["上传这份快照：<项目>/build/2026-aa/site", ".venv/bin/python scripts/build_public_site.py --verify 2026-ab --release --expect-manifest-sha256 abc",
                "构建/检查 and/or 1/2 https://example.com/a/b", "错误：a/b.json 缺失"]
        for c in keep:
            self.assertEqual(pa.redact(c), c)                                              # 命令、相对路径、URL、普通斜杠文字不被误伤

    def test_fixed_argv_env_cwd(self):
        r = fake("构建 通过")
        with on():
            out = pa.run_check(runner=r)
        argv, kw = r.calls[0]
        self.assertEqual(argv, [sys.executable, "-B", str(pa.SCRIPT)])                # 固定参数，没有任何用户输入
        self.assertEqual(kw["cwd"], str(pa.ROOT))
        self.assertEqual(set(kw["env"]), {"PATH", "LANG", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING"})
        self.assertEqual(kw["timeout"], pa.TIMEOUT_SECONDS)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(out, {"ok": True, "exit_code": 0, "output": "构建 通过"})

    def test_nonzero_exit_is_not_ok(self):
        with on():
            out = pa.run_check(runner=fake("错误：x", "traceback?", 1))
        self.assertFalse(out["ok"])
        self.assertEqual(out["exit_code"], 1)

    def test_output_truncated(self):
        with on():
            out = pa.run_check(runner=fake("x" * (pa.MAX_OUTPUT_CHARS * 3)))
        self.assertLessEqual(len(out["output"]), pa.MAX_OUTPUT_CHARS + 40)
        self.assertIn("已截断", out["output"])

    def test_timeout_and_oserror_fixed_messages_and_lock_released(self):
        def boom_t(*a, **k):
            raise subprocess.TimeoutExpired("x", 1)

        def boom_o(*a, **k):
            raise OSError("/Users/secret/path")
        for fn, word in ((boom_t, "中止"), (boom_o, "无法启动")):
            with on(), self.assertRaises(pa.PublishCheckError) as cm:
                pa.run_check(runner=fn)
            self.assertIn(word, str(cm.exception))
            self.assertNotIn("secret", str(cm.exception))
        with on():
            self.assertTrue(pa.run_check(runner=fake())["ok"])                         # 出错后锁已释放

    def test_busy(self):
        self.assertTrue(pa._BUSY.acquire(blocking=False))
        try:
            with on(), self.assertRaises(pa.PublishCheckError) as cm:
                pa.run_check(runner=fake())
            self.assertIn("已经有一个", str(cm.exception))
        finally:
            pa._BUSY.release()


def _build_admin_post():
    import ast
    tree = ast.parse((Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8"))
    handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    fn = next(n for n in handler.body if isinstance(n, ast.FunctionDef) and n.name == "_admin_post")
    cls = ast.ClassDef(name="H", bases=[], keywords=[], decorator_list=[], body=[fn])
    ns = dict(admin=admin, ADMIN_CTX={})
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<h>", "exec"), ns)
    return ns["H"]


class OriginTests(unittest.TestCase):
    def mk(self, origin):
        H = _build_admin_post()

        class F(H):
            client_address = ("127.0.0.1", 1)
            headers = {"X-Requested-With": "dashboard", **({"Origin": "http://127.0.0.1:8765"} if origin else {})}
            out = None
            def _local_or_owner(self): return True
            def _admin_authed(self): return True
            def _origin_ok(self): return True                                      # 真实实现在缺头时也返回 True
            def _body(self): return {"action": "publish_check"}
            def _json(self, obj, code=200): self.out = (code, obj)
            def _send(self, code, body, ctype="text/plain"): self.out = (code, body)
        return F()

    def test_missing_origin_rejected_for_publish_check(self):
        h = self.mk(origin=False)
        with patch.object(admin, "dispatch", return_value={"ok": True}) as d, patch.object(admin, "set_actor_from_request"):
            h._admin_post(SimpleNamespace(path="/api/admin/action"))
        self.assertEqual(h.out[0], 403)
        d.assert_not_called()

    def test_with_origin_goes_to_dispatch(self):
        h = self.mk(origin=True)
        with patch.object(admin, "dispatch", return_value={"ok": True}) as d, patch.object(admin, "set_actor_from_request"):
            h._admin_post(SimpleNamespace(path="/api/admin/action"))
        self.assertEqual(h.out, (200, {"ok": True}))
        d.assert_called_once()


class AdminRouteTests(unittest.TestCase):
    def test_do_routes_to_publish_check(self):
        with on(), patch.object(pa, "run_check", return_value={"ok": True, "exit_code": 0, "output": "z"}) as m:
            self.assertEqual(admin._do("publish_check", {}, {}, None, ""), {"ok": True, "exit_code": 0, "output": "z"})
            m.assert_called_once_with()

    def test_disabled_becomes_admin_error(self):
        with on(False), self.assertRaises(admin.AdminError) as cm:
            admin._do("publish_check", {}, {}, None, "")
        self.assertIn("未开启", str(cm.exception))

    def test_validate_accepts_bool_only(self):
        with patch.object(admin_settings, "get_all", return_value=dict(admin_settings.DEFAULTS)):
            self.assertEqual(admin_settings.validate({"publish_check_on": True}), {"publish_check_on": True})
            with self.assertRaises(admin_settings.SettingError):
                admin_settings.validate({"publish_check_on": "yes"})

    def test_no_app_import(self):
        self.assertNotIn("app", sys.modules)


if __name__ == "__main__":
    unittest.main()
