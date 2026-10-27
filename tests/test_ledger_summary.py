"""前向账本展示页(T040)的测试:纯函数 + 接线 + 静态检查。不 import app,不读写 data/ 真实文件。"""
import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import admin_settings, ledger_summary  # noqa: E402

UI = ROOT / "dashboard" / "ui"
JS = (UI / "pages" / "ledger.js").read_text(encoding="utf-8")
HTML = (UI / "w" / "ledger.html").read_text(encoding="utf-8")
ZH = json.loads((UI / "i18n" / "zh.json").read_text(encoding="utf-8"))
EN = json.loads((UI / "i18n" / "en.json").read_text(encoding="utf-8"))


def sample():
    return {"today": "2026-10-26", "signal_days": ["2026-10-01", "2026-10-02", "x"], "evaluated_days": ["2026-10-01"], "min_sample": 30, "main": "m",
            "precision": {"m": [
                {"action": "买入", "h": 1, "n": 10, "days": 1, "hit_excess": 0.5, "hit_excess_lo": 0.4, "hit_excess_hi": 0.6,
                 "mean_excess": 0.001, "net_excess": float("nan"), "enough": False, "verdict": "600000 买入 100 股", "code": "600000"},
                {"action": "乱写", "h": 1}, "not a dict"]},
            "promotion": {"any_passed": False, "complete_weeks": 1, "gate": {"min_weeks": 12}},
            "caveats": ["600001 卖出 200 股", 5, None]}


class SummarizeTests(unittest.TestCase):
    def test_basic_and_no_codes(self):
        s = ledger_summary.summarize(sample())
        self.assertEqual((s["signal_days"], s["evaluated_days"], s["strategy"]), (2, 1, "m"))      # "x" 不是日期,不计
        bad = sample(); bad["main"] = "600000 买入"; bad["today"] = "买入600000"
        sb = ledger_summary.summarize(bad)
        self.assertIsNone(sb["strategy"]); self.assertIsNone(sb["today"])
        self.assertEqual(len(s["rows"]), 1)                       # 非法动作、非字典行被丢弃
        self.assertNotIn("600000", json.dumps(s, ensure_ascii=False))   # 不带任何股票代码字段
        self.assertIsNone(s["rows"][0]["net_excess"])             # NaN 变 None
        self.assertFalse(s["any_enough"])
        self.assertEqual(s["caveats"], list(ledger_summary.CAVEATS))           # 固定文案,不转发账本里的自由文本
        self.assertIsNone(s["rows"][0]["verdict"])                             # 非枚举的结论文字被丢弃
        self.assertNotIn("600001", json.dumps(s, ensure_ascii=False))
        self.assertNotIn("股", json.dumps(s["rows"], ensure_ascii=False))
        json.dumps(s, allow_nan=False)                            # 结果必须是合法 JSON

    def test_garbage_degrades(self):
        for bad in (None, [], "x", 3):
            self.assertEqual(ledger_summary.summarize(bad)["status"], "na")
        s = ledger_summary.summarize({"signal_days": "x", "precision": [], "main": 5, "promotion": 1})
        self.assertEqual((s["status"], s["rows"], s["signal_days"]), ("ok", [], 0))

    def test_load_missing_corrupt_big(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "latest.json"
            self.assertEqual(ledger_summary.load(p)["reason"], "missing")
            p.write_text("{bad", encoding="utf-8")
            self.assertEqual(ledger_summary.load(p)["reason"], "unreadable")
            p.write_text(json.dumps(sample()), encoding="utf-8")
            self.assertEqual(ledger_summary.load(p)["status"], "ok")
            with patch.object(ledger_summary, "MAX_FILE_BYTES", 5):
                self.assertEqual(ledger_summary.load(p)["reason"], "missing")


class WiringTests(unittest.TestCase):
    def test_setting_default_off_and_strict(self):
        self.assertFalse(admin_settings.DEFAULTS["ledger_page_on"])
        with patch.object(admin_settings, "get_all", side_effect=RuntimeError):
            self.assertFalse(admin_settings.ledger_page_on())
        with patch.object(admin_settings, "get_all", return_value={"ledger_page_on": "yes"}):
            self.assertFalse(admin_settings.ledger_page_on())
        with patch.object(admin_settings, "get_all", return_value={"ledger_page_on": True}):
            self.assertTrue(admin_settings.ledger_page_on())
        with patch.object(admin_settings, "get_all", return_value=dict(admin_settings.DEFAULTS)):     # 不连真实数据库(Codex N1027_5)
            self.assertEqual(admin_settings.validate({"ledger_page_on": True}), {"ledger_page_on": True})

    def test_route_registered_and_gated(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        ast.parse(src)
        self.assertIn('u.path == "/api/ledger/summary"', src)
        body = src[src.index("def _ledger_summary"):src.index("# ---------- 实时盯盘(T006 S3)")]
        for need in ("ledger_page_on()", "_local_or_owner()", "not self._remote()", "_authed()", '"dashboard"', "members.hit"):
            self.assertIn(need, body)
        self.assertLess(body.index("ledger_page_on()"), body.index("_authed()"))   # 关闭时先 404,不暴露鉴权差异


class HandlerBehaviourTests(unittest.TestCase):
    """用 ast 取出真实的 Handler._ledger_summary，在桩对象上执行(不 import app)：关闭 404、未登录 401、缺头 403、非本机 404、限频 429、成功 200。"""

    def run_handler(self, *, on=True, local=True, remote=False, authed=True, header="dashboard", hits=False):
        from src import members
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_ledger_summary")
        ns = {"members": members}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "<ledger>", "exec"), ns)
        out = []
        h = SimpleNamespace(headers={"X-Requested-With": header}, _local_or_owner=lambda: local, _remote=lambda: remote, _authed=lambda: authed,
                            _json=lambda obj, code=200: out.append(code), _send=lambda code, *a: out.append(code))
        with patch.object(admin_settings, "ledger_page_on", return_value=on), patch.object(ledger_summary, "load", return_value={"status": "ok"}) as load, \
                patch.object(members, "hit", return_value=hits):
            ns["_ledger_summary"](h)
        return out, load

    def test_matrix(self):
        out, load = self.run_handler(on=False);                    self.assertEqual(out, [404]); load.assert_not_called()
        out, load = self.run_handler(local=False);                 self.assertEqual(out, [404]); load.assert_not_called()
        out, load = self.run_handler(remote=True);                 self.assertEqual(out, [404]); load.assert_not_called()
        out, load = self.run_handler(authed=False);                self.assertEqual(out, [401]); load.assert_not_called()
        out, load = self.run_handler(header="");                   self.assertEqual(out, [403]); load.assert_not_called()
        out, load = self.run_handler(hits=True);                   self.assertEqual(out, [429]); load.assert_not_called()
        out, load = self.run_handler();                            self.assertEqual(out, [200]); load.assert_called_once()


class StaticTests(unittest.TestCase):
    def test_keys_and_safety(self):
        keys = set(re.findall(r"t\('([a-zA-Z_.0-9]+)'", JS)) | set(re.findall(r'data-i18n="([a-z_.0-9]+)"', HTML))
        keys |= {f"led.act.{a}" for a in ("买入", "卖出", "加仓", "减仓")} | {"adm.row.ledger_page_on", "adm.row.ledger_page_on.hint"}
        keys = {k for k in keys if k.startswith(("led.", "adm.row.ledger"))}
        self.assertGreater(len(keys), 15)
        for k in sorted(keys):
            self.assertIn(k, ZH, k)
            self.assertIn(k, EN, k)
        self.assertNotIn("innerHTML", JS)
        self.assertEqual(set(re.findall(r"/api/[a-z_/]+", JS)), {"/api/ledger/summary"})
        self.assertIn('data-page="ledger"', HTML)
        self.assertIn("live('ledger_page_on'", (UI / "pages" / "admin-views.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
