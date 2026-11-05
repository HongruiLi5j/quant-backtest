"""D87（revquant 美股审计）：U3 外部美股日线去掉美东 16:30 前保存的当天 K 线（只在内存里）；U2 排除原因的说明；U4 元数据；U6 美股跟踪只显示条数。
合成数据，不读写 data/ 和外部目录。运行：.venv/bin/python tests/test_us_d87.py"""
import inspect
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import MARKETS  # noqa: E402
from src import scan_us, tracking, usdata  # noqa: E402

NY = ZoneInfo("America/New_York")
IDX = pd.DatetimeIndex(["2026-09-30", "2026-10-01", "2026-10-02"])
DF = pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=IDX)


def ts(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=NY).timestamp()


class PartialBar(unittest.TestCase):
    def test_saved_before_close_drops_that_day(self):
        self.assertEqual(list(usdata.drop_partial(DF, ts(2026, 10, 2, 15, 0)).index), list(IDX[:2]))

    def test_saved_after_close_keeps(self):
        self.assertEqual(len(usdata.drop_partial(DF, ts(2026, 10, 2, 16, 45))), 3)

    def test_boundary_and_future_bars(self):
        self.assertEqual(len(usdata.drop_partial(DF, ts(2026, 10, 2, 16, 30))), 3)          # 16:30 整算已收盘
        self.assertEqual(len(usdata.drop_partial(DF, ts(2026, 10, 1, 20, 0))), 2)           # 文件日期之后的 K 线不可能是完整的

    def test_tz_aware_index(self):
        d = DF.copy()
        d.index = d.index.tz_localize("America/New_York")
        self.assertEqual(len(usdata.drop_partial(d, ts(2026, 10, 2, 9, 45))), 2)


class MetaAndLabels(unittest.TestCase):
    def test_u2_reason_label(self):
        c = np.r_[np.full(300, 10.0), 50.0]
        raw = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Adj Close": c, "Volume": 1e7},
                           index=pd.bdate_range("2025-01-01", periods=301))
        why = usdata.screen(raw, usdata.adjust(raw))
        self.assertIn("也可能是真实暴跌", why)

    def test_u4_meta_source(self):
        src = inspect.getsource(scan_us)
        self.assertIn('"listed": None', src)
        self.assertIn("delisted_note", src)

    def test_u6_counts_only_flag(self):
        days = pd.bdate_range("2026-09-01", periods=40)
        f = pd.DataFrame({"Open": 10.0, "High": 10.0, "Low": 10.0, "Close": np.linspace(10, 11, 40), "Volume": 1e6}, index=days)
        hist = pd.DataFrame([{"market": "us", "signal_date": days[3].strftime("%Y-%m-%d"), "code": "AAPL", "name": "a",
                              "strategy": "blend2", "action": "买入"}])
        with mock.patch.object(tracking, "_prices", lambda m, code, cfg: f):
            ev = tracking.evaluate("us", MARKETS["us"], hist=hist)
        self.assertTrue(ev["counts_only"])
        self.assertEqual(ev["summary"][0]["ew_n5"], 0)


if __name__ == "__main__":
    unittest.main()
