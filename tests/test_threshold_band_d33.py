"""D33（revquant P5a）：清单分类阈值 TRADE_LIST_THRESHOLD 必须等于回测的调仓带 band。
扫描里 next_w 是没经过 band 的原始目标、now_w 是经过 band 的实际仓位；两者差在 band 以内时回测不调仓，
清单也用同一个阈值判断「要不要动」，所以两边一致。只要有人改了其中一个，这个测试就会失败。
band 目前在三处写成字面量（scan.scan_one、trade_list.replay、registry.run），这里直接读源码核对，不改产品代码。
运行：.venv/bin/python tests/test_threshold_band_d33.py"""
import inspect
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import TRADE_LIST_THRESHOLD  # noqa: E402
from src import registry, scan, trade_list  # noqa: E402

PAT = re.compile(r"band\s*=\s*([0-9.]+)")


class ThresholdEqualsBand(unittest.TestCase):
    def bands(self, fn):
        vals = [float(x) for x in PAT.findall(inspect.getsource(fn))]
        self.assertTrue(vals, f"{fn.__qualname__} 里找不到 band= 字面量（改成常量后请同步改这个测试）")
        return vals

    def test_scan_band(self):
        self.assertEqual(self.bands(scan.scan_one), [TRADE_LIST_THRESHOLD])

    def test_trade_list_replay_band(self):
        self.assertEqual(self.bands(trade_list.replay), [TRADE_LIST_THRESHOLD])

    def test_registry_band(self):
        nonzero = [b for b in self.bands(registry.run) if b != 0.0]       # buy_hold / dual_momentum 用 0
        self.assertEqual(nonzero, [TRADE_LIST_THRESHOLD])


if __name__ == "__main__":
    unittest.main()
