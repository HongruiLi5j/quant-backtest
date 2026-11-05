"""D110：数据质量标记。合成数据 + 临时标记文件，不读写 data/。
运行：.venv/bin/python tests/test_d110_quality_flags.py"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from src import allmarket  # noqa: E402
import build_quality_flags as B  # noqa: E402

IDX = pd.bdate_range("2020-01-01", periods=40)


def frame(close):
    c = np.array(close, float)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1e5}, index=IDX)


class Quality(unittest.TestCase):
    def test_scan_mask_vs_flag(self):
        c = np.full(40, 10.0)
        c[20] = 30.0                                  # 单日 +200% 回落：只标记
        self.assertEqual(B.scan_one(frame(c))[0]["kind"], "flag")
        c = np.full(40, 10.0)
        c[10:16] = [0.05, 0.03, 0.04, 0.02, 0.06, 10.0]   # 近零价一段：置空
        segs = B.scan_one(frame(c))
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["kind"], "mask")

    def test_clean_has_no_flags(self):
        self.assertEqual(B.scan_one(frame(10 + np.arange(40) * 0.01)), [])

    def test_mask_flagged_only_masks(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "q.json"
            f.write_text(json.dumps({"codes": {"AAA": [{"from": "2020-01-10", "to": "2020-01-14", "kind": "mask"}],
                                                 "BBB": [{"from": "2020-01-10", "to": "2020-01-14", "kind": "flag"}]}}))
            old = allmarket.QUALITY_FILE
            allmarket.QUALITY_FILE = f
            try:
                df = frame(np.full(40, 10.0))
                m = allmarket.mask_flagged(df, "AAA")
                self.assertTrue(m.loc["2020-01-10":"2020-01-14"].isna().all().all())
                self.assertEqual(len(m), 40)                                  # 不删行
                self.assertTrue(df.equals(frame(np.full(40, 10.0))))          # 不改调用方对象
                self.assertIs(allmarket.mask_flagged(df, "BBB"), df)          # flag 不动数据
                self.assertIs(allmarket.mask_flagged(df, "CCC"), df)
            finally:
                allmarket.QUALITY_FILE = old

    def test_missing_file_is_harmless(self):
        old = allmarket.QUALITY_FILE
        allmarket.QUALITY_FILE = Path("/nonexistent/q.json")
        try:
            self.assertEqual(allmarket.quality_flags("600595"), [])
        finally:
            allmarket.QUALITY_FILE = old

    def test_real_file_marks_600595(self):
        self.assertTrue(any(g["kind"] == "mask" for g in allmarket.quality_flags("600595")))


if __name__ == "__main__":
    unittest.main()
