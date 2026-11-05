"""因子检验页（N24）的测试：导出函数用造的小数据，不读真实 reports/、不写项目文件；前端只做静态检查。"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import export_factor_view as X  # noqa: E402

UI = ROOT / "dashboard" / "ui"
CJK = re.compile(r"[㐀-鿿　-〿＀-￯]")


def mono(results):
    return {"horizon": 20, "n_groups": 5, "results": results}


def train(decisions, segment="train"):
    return {"segment": segment, "segment_range": ["2024-01-01", "2025-06-30"], "survivors": [], "rules": {"r2_max_monthly_turnover": 1.0},
            "record_sha256": "ab" * 32, "catalog_hash": "cd" * 32, "timestamp": "2026-10-24T19:22:50+00:00", "decisions": decisions}


def dec(key, reasons, p=0.5, to=2.0, gross=0.1, net=-0.05):
    return {"key": key, "decision": "eliminated", "reasons": reasons, "rule1": {"p_value": p},
            "rule2": {"monthly_turnover": to, "gross_annual_excess": gross, "net_annual_excess": net}}


CAT = {"strategies": [{"id": "mom_01", "name": "N日收益率", "category": "动量"}]}


class BuildViewTests(unittest.TestCase):
    def test_joins_by_key_and_converts_units(self):
        m = mono([{"id": "mom_01", "params": [{"params": {"n": 5, "w": 0.5}, "passed": True, "ic_mean": 0.0123456, "rho": 0.9,
                                               "spread_mean": 0.0123, "group_mean": [0.01, 0.02, 0.03, 0.04, 0.05]}]}])
        v = X.build_view(m, train([dec("mom_01|n=5,w=0.5", ["规则1:…", "规则2:月换手…"])]), CAT)
        r = v["rows"][0]
        self.assertEqual(v["schema"], "factor_view/1")
        self.assertEqual((v["total"], v["survivors"]), (1, 0))
        self.assertEqual(r["key"], "mom_01|n=5,w=0.5")
        self.assertEqual(r["name"], "N日收益率")
        self.assertEqual(r["rules_hit"], [1, 2])
        self.assertEqual(r["groups_pct"], [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual((r["gross_pct"], r["net_pct"], r["spread_pct"], r["ic"]), (10.0, -5.0, 1.23, 0.0123))
        self.assertTrue(r["mono_passed"])

    def test_bad_numbers_become_none(self):
        m = mono([{"id": "x", "params": [{"params": {"n": 1}, "passed": False, "ic_mean": float("nan"), "rho": None,
                                         "spread_mean": None, "group_mean": [0.01, float("inf"), 0.0, 0.0, 0.0]}]}])
        d = dec("x|n=1", ["规则4:…"], p=True, to="2", gross=None, net=float("-inf"))
        r = X.build_view(m, train([d]), CAT)["rows"][0]
        self.assertEqual([r[k] for k in ("ic", "rho", "spread_pct", "p_value", "turnover", "gross_pct", "net_pct")], [None] * 7)
        self.assertEqual(r["groups_pct"], [])
        self.assertIsNone(r["name"])

    def test_refuses_mismatched_inputs(self):
        m = mono([{"id": "x", "params": [{"params": {"n": 1}, "passed": False}]}])
        with self.assertRaises(X.ExportError):
            X.build_view(m, train([]), CAT)                                    # 判定里没有这个组合
        with self.assertRaises(X.ExportError):
            X.build_view(m, train([dec("x|n=1", []), dec("y|n=2", [])]), CAT)  # 判定多出组合
        with self.assertRaises(X.ExportError):
            X.build_view(m, train([dec("x|n=1", [])], segment="valid"), CAT)   # 不是训练段
        dup = mono([{"id": "x", "params": [{"params": {"n": 1}}, {"params": {"n": 1}}]}])
        with self.assertRaises(X.ExportError):
            X.build_view(dup, train([dec("x|n=1", [])]), CAT)

    def test_main_writes_only_the_out_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            run, out = Path(tmp) / "run", Path(tmp) / "out" / "fv.json"
            run.mkdir()
            m = mono([{"id": "mom_01", "params": [{"params": {"n": 5}, "passed": False}]}])
            (run / "monotonic_report.json").write_text(json.dumps(m), encoding="utf-8")
            (run / "train_record.json").write_text(json.dumps(train([dec("mom_01|n=5", [])])), encoding="utf-8")
            before = sorted(p.name for p in run.iterdir())
            self.assertEqual(X.main(["--run", str(run), "--out", str(out)]), 0)
            self.assertEqual(sorted(p.name for p in run.iterdir()), before)
            self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ["fv.json"])
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["total"], 1)
            self.assertEqual(X.main(["--run", str(Path(tmp) / "missing"), "--out", str(out)]), 1)


class FrontendStaticTests(unittest.TestCase):
    JS = (UI / "pages" / "research-factor.js").read_text(encoding="utf-8")
    ZH = json.loads((UI / "i18n" / "zh.json").read_text(encoding="utf-8"))
    EN = json.loads((UI / "i18n" / "en.json").read_text(encoding="utf-8"))

    def test_no_api_calls_and_reads_static_file(self):
        code = re.sub(r"/\*.*?\*/", "", self.JS, flags=re.S)             # 去掉文件头注释（里面写着「不走 /api」）
        self.assertNotIn("/api", code)
        self.assertIn("../research-data/factor_view.json", self.JS)
        self.assertNotIn("innerHTML", self.JS)

    def test_wired_into_research_page(self):
        research = (UI / "pages" / "research.js").read_text(encoding="utf-8")
        html = (UI / "w" / "research.html").read_text(encoding="utf-8")
        self.assertIn("import { factorView } from './research-factor.js';", research)
        self.assertIn("tab === 'factor' ? factorView()", research)
        self.assertIn('href="../pages/research-factor.css"', html)

    def test_every_key_used_exists_in_both_languages(self):
        used = set(re.findall(r"t\(\s*'(n24\.[\w.]+)'", self.JS))
        used |= {f"n24.fv.mode_{m}" for m in ("gross", "net")} | {f"n24.fv.rule{n}" for n in range(1, 5)}
        for k in used:
            self.assertIn(k, self.ZH, k)
            self.assertIn(k, self.EN, k)
        parts_zh = json.loads((UI / "i18n" / "parts" / "N24.zh.json").read_text(encoding="utf-8"))
        parts_en = json.loads((UI / "i18n" / "parts" / "N24.en.json").read_text(encoding="utf-8"))
        self.assertEqual(set(parts_zh), set(parts_en))
        for k in parts_zh:
            self.assertEqual(self.ZH[k], parts_zh[k])
            self.assertEqual(self.EN[k], parts_en[k])
            self.assertEqual(set(re.findall(r"\{(\w+)\}", parts_zh[k])), set(re.findall(r"\{(\w+)\}", parts_en[k])), k)
            self.assertFalse(CJK.search(parts_en[k]), k)


if __name__ == "__main__":
    unittest.main()
