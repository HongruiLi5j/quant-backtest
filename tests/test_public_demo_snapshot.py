"""D26：公开站演示数据生成器 + 构建时一致性检查。

  - 磁盘上的 pub-today/market/signals 必须正好等于生成器输出（不许手改）；
  - 同一种子生成两次结果相同（可复现）；
  - 一致性检查对生成的数据报 0 个错，对故意改坏的数据能报出来。
运行：.venv/bin/python -m pytest -q tests/test_public_demo_snapshot.py
"""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_public_site as bps          # noqa: E402
import public_demo_snapshot as gen       # noqa: E402

MOCK = ROOT / "dashboard" / "mockups" / "mock-data"
FILES = ("pub-today", "pub-market", "pub-signals", "pub-strategies")


class DemoSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = gen.build()

    def test_files_on_disk_equal_generator_output(self):
        for name, obj in self.docs.items():
            self.assertEqual((MOCK / f"{name}.json").read_text(encoding="utf-8"), gen.dump(obj), name)

    def test_deterministic(self):
        self.assertEqual(gen.dump(self.docs["pub-today"]), gen.dump(gen.build()["pub-today"]))

    def _site(self, docs):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        (d / "mock-data").mkdir()
        for name in FILES:
            obj = docs.get(name)
            if obj is None:
                shutil.copy(MOCK / f"{name}.json", d / "mock-data" / f"{name}.json")
            else:
                (d / "mock-data" / f"{name}.json").write_text(gen.dump(obj), encoding="utf-8")
        return d

    def test_generated_data_is_consistent(self):
        self.assertEqual(bps.demo_consistency_errors(self._site(self.docs)), [])

    def test_check_catches_sector_contradiction(self):
        bad = copy.deepcopy(self.docs)
        bad["pub-today"]["today"]["sectors_top"][0][2] = -9.99       # 今日页领涨行业改成负数
        errs = bps.demo_consistency_errors(self._site(bad))
        self.assertTrue(any("行业" in e for e in errs), errs)

    def test_check_catches_real_stock_and_breadth_and_trigger(self):
        bad = copy.deepcopy(self.docs)
        bad["pub-market"]["rows"][0].update(code="600519", name="贵州茅台", name_en="Kweichow Moutai")
        bad["pub-today"]["yesterday"]["regime"]["breadth"] = 0.99
        s = bad["pub-today"]["today"]["signals"][0]
        s["trigger_en"] = s["trigger_en"] + " 7"
        bad["pub-signals"]["days"][0]["signals"][0] = copy.deepcopy(s)
        errs = bps.demo_consistency_errors(self._site(bad))
        self.assertTrue(any("不像虚构" in e for e in errs), errs)
        self.assertTrue(any("宽度" in e for e in errs), errs)
        self.assertTrue(any("中英文数字" in e for e in errs), errs)

    def test_check_catches_previous_day_mismatch(self):
        bad = copy.deepcopy(self.docs)
        bad["pub-market"]["previous"]["summary"]["up"] += 1           # 全市场页的上一交易日上涨家数改掉
        errs = bps.demo_consistency_errors(self._site(bad))
        self.assertTrue(any("previous" in e for e in errs), errs)

    def test_landing_market_block_on_disk_matches(self):
        lp = MOCK / "exports-landing.json"
        text = lp.read_text(encoding="utf-8")
        self.assertEqual(text, gen.splice_landing(text, gen.landing_market_text(self.docs["pub-today"])))

    def test_check_catches_real_code_in_any_mock(self):
        d = self._site(self.docs)
        c = json.loads((MOCK / "community.json").read_text(encoding="utf-8"))
        c["portfolio"]["positions"][0]["code"] = "600519"            # 放回一个真实代码
        (d / "mock-data" / "community.json").write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        errs = bps.demo_consistency_errors(d)
        self.assertTrue(any("community.json" in e and "600519" in e for e in errs), errs)

    def test_encyclopedia_only_lists_registry_strategies(self):
        self.assertEqual(bps.encyclopedia_errors(self._site(self.docs)), [])
        d = self._site(self.docs)
        st = json.loads((MOCK / "pub-strategies.json").read_text(encoding="utf-8"))
        st["list"].append({"id": "macd_trend", "family": "trend"})          # 代码里没有的策略
        (d / "mock-data" / "pub-strategies.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        errs = bps.encyclopedia_errors(d)
        self.assertTrue(any("macd_trend" in e for e in errs) and any("count_total" in e for e in errs), errs)

    def test_encyclopedia_names_come_from_registry(self):
        labels = gen.registry_labels()
        for k, v in labels.items():                    # 英文名不能是 id（D77 第一版就是这样错的）
            self.assertNotEqual(v["name_en"], k, k)
        for x in json.loads((MOCK / "pub-strategies.json").read_text(encoding="utf-8"))["list"]:
            self.assertEqual((x["name"], x["name_en"]), (labels[x["id"]]["name"], labels[x["id"]]["name_en"]), x["id"])
        d = self._site(self.docs)
        st = json.loads((MOCK / "pub-strategies.json").read_text(encoding="utf-8"))
        st["list"][0]["name"] = "改过的名字"
        (d / "mock-data" / "pub-strategies.json").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        self.assertTrue(any("名字" in e for e in bps.encyclopedia_errors(d)))

    def test_signal_strategies_exist_in_encyclopedia(self):
        ids = {x["id"] for x in json.loads((MOCK / "pub-strategies.json").read_text(encoding="utf-8"))["list"]}
        for d in self.docs["pub-signals"]["days"]:
            self.assertLessEqual({s["strategy"] for s in d["signals"]}, ids)


if __name__ == "__main__":
    unittest.main()
