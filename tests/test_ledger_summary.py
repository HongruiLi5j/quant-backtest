"""前向账本展示页(T040)的测试:纯函数 + 接线 + 静态检查。不 import app,不读写 data/ 真实文件。"""
import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
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
    return {"today": "2026-10-26", "signal_days": ["a", "b"], "evaluated_days": ["a"], "min_sample": 30, "main": "m",
            "precision": {"m": [
                {"action": "买入", "h": 1, "n": 10, "days": 1, "hit_excess": 0.5, "hit_excess_lo": 0.4, "hit_excess_hi": 0.6,
                 "mean_excess": 0.001, "net_excess": float("nan"), "enough": False, "verdict": "样本不足", "code": "600000"},
                {"action": "乱写", "h": 1}, "not a dict"]},
            "promotion": {"any_passed": False, "complete_weeks": 1, "gate": {"min_weeks": 12}},
            "caveats": ["局限一", 5, None]}


class SummarizeTests(unittest.TestCase):
    def test_basic_and_no_codes(self):
        s = ledger_summary.summarize(sample())
        self.assertEqual((s["signal_days"], s["evaluated_days"], s["strategy"]), (2, 1, "m"))
        self.assertEqual(len(s["rows"]), 1)                       # 非法动作、非字典行被丢弃
        self.assertNotIn("600000", json.dumps(s, ensure_ascii=False))   # 不带任何股票代码字段
        self.assertIsNone(s["rows"][0]["net_excess"])             # NaN 变 None
        self.assertFalse(s["any_enough"])
        self.assertEqual(s["caveats"], ["局限一"])
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
        self.assertEqual(admin_settings.validate({"ledger_page_on": True}), {"ledger_page_on": True})

    def test_route_registered_and_gated(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        ast.parse(src)
        self.assertIn('u.path == "/api/ledger/summary"', src)
        body = src[src.index("def _ledger_summary"):src.index("# ---------- 实时盯盘(T006 S3)")]
        for need in ("ledger_page_on()", "_local_or_owner()", "not self._remote()", "_authed()", '"dashboard"', "members.hit"):
            self.assertIn(need, body)
        self.assertLess(body.index("ledger_page_on()"), body.index("_authed()"))   # 关闭时先 404,不暴露鉴权差异


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
