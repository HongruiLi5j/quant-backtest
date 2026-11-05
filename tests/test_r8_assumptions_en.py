"""R8：回测页「假设与局限」的英文版 assumptions_en 与中文版条数一致、不含汉字（A 股 / 美股、个股 / ETF、两种引擎口径）。"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import analytics as A  # noqa: E402

CJK = re.compile(r"[一-鿿]")


class AssumptionsEn(unittest.TestCase):
    def test_same_shape_no_cjk(self):
        for m in ("cn", "us"):
            for eng in ("lab", "other"):
                for stock in (True, False):
                    zh, en = A.assumptions(m, eng, 5, stock), A.assumptions_en(m, eng, 5, stock)
                    self.assertEqual(len(zh), len(en), (m, eng, stock))
                    self.assertFalse(any(CJK.search(x) for row in en for x in row), (m, eng, stock))


if __name__ == "__main__":
    unittest.main()
