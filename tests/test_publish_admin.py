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
    return patch.object(admin_settings, "get_all", return_value={"publish_check_on": flag})


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
