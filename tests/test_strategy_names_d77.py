"""D77：src/registry.py 是两站策略名的唯一来源。每个策略有一个中文名（label）和一个英文名（label_en）、英文说明（desc_en），
两种语言里名字都不重复；app.meta() 把两种名字都带给前端。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import registry  # noqa: E402


class StrategyNameTests(unittest.TestCase):
    def test_every_strategy_has_both_names(self):
        self.assertEqual(set(registry.STRATEGY_EN), set(registry.STRATEGIES))
        for k, v in registry.STRATEGIES.items():
            self.assertTrue(v["label"].strip(), k)
            self.assertTrue(v["label_en"].strip(), k)
            self.assertTrue(v["desc_en"].strip(), k)
            self.assertFalse(any("一" <= ch <= "鿿" for ch in v["label_en"] + v["desc_en"]), f"{k} 英文名里有中文")

    def test_every_param_has_english_label(self):
        """D89：回测参数名也有英文（registry.PARAM_EN），英文界面不再露出「趋势集成权重」之类"""
        for k, v in registry.STRATEGIES.items():
            for p in v["params"]:
                self.assertTrue(p.get("label_en"), (k, p["key"]))
                self.assertFalse(any("\u4e00" <= ch <= "\u9fff" for ch in p["label_en"] + p.get("help_en", "")), (k, p["key"]))
                if p["help"]:
                    self.assertTrue(p.get("help_en"), (k, p["key"]))

    def test_names_unique_per_language(self):
        zh = [v["label"] for v in registry.STRATEGIES.values()]
        en = [v["label_en"] for v in registry.STRATEGIES.values()]
        self.assertEqual(len(zh), len(set(zh)))
        self.assertEqual(len(en), len(set(en)))

    def test_meta_exposes_both(self):
        # 在独立的子进程里 import app：在本进程 import 会污染其他测试（test_upload_* 检查「没有 import app」）
        import json
        import subprocess
        root = Path(__file__).resolve().parents[1]
        code = ("import json, app; print(json.dumps({k: [v['label'], v['label_en']] "
                "for k, v in app.meta()['strategies'].items()}, ensure_ascii=False))")
        r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        m = json.loads(r.stdout.strip().splitlines()[-1])
        for k in registry.STRATEGIES:
            self.assertEqual(m[k], [registry.STRATEGIES[k]["label"], registry.STRATEGIES[k]["label_en"]])


class PerformanceNoteTests(unittest.TestCase):
    """信号页说明文字从字典取（c78.perf_note），中文必须和后端常量一致，否则两边会再分叉"""
    def test_zh_note_equals_backend_constant(self):
        import json
        from src import trade_list
        zh = json.loads((Path(__file__).resolve().parents[1] / "dashboard/ui/i18n/zh.json").read_text(encoding="utf-8"))
        self.assertEqual(zh["c78.perf_note"], trade_list.PERFORMANCE_NOTE)
        self.assertNotIn("样本外：", zh["c78.perf_note"])
        self.assertEqual([zh[f"c78.lim.{i}"] for i in range(1, len(trade_list.LIMITATIONS) + 1)], list(trade_list.LIMITATIONS))
        self.assertEqual(zh["c78.disclaimer"], trade_list.DISCLAIMER)
        self.assertEqual(len(trade_list.LIMITATIONS), 5)   # 前端按 1–5 取词条；改条数时要同步 signals.js


if __name__ == "__main__":
    unittest.main()
