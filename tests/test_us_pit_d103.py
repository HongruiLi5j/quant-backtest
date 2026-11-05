"""D103：美股样本按历史时点筛选 + 美股扫描进每日任务。合成数据 / 临时目录，不读写 data/、不读外部美股目录。
- eligible_pit：只用前一个交易日为止的数据；按月调仓（月内不变）；门槛和 screen 相同。
- 没有未来函数：截断未来数据不改变过去任何一天的结果。
- 早年成交清淡、后来活跃的股票：旧口径整只入选并持有全程；新口径只在成交额够的月份持有。
- 不按股价筛（历史价已按之后的拆股调整）。
- 每日任务：外部目录有比上次 meta 新的文件才重扫；目录不存在不跑；扫描出错只记状态。
运行：.venv/bin/python tests/test_us_pit_d103.py"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import usdata  # noqa: E402

IDX = pd.bdate_range("2018-01-01", periods=1000)


def raw(px, vol=1e6):
    px = np.asarray(px, dtype=float)
    return pd.DataFrame({"Open": px, "High": px * 1.01, "Low": px * 0.99, "Close": px, "Adj Close": px,
                         "Volume": vol if np.ndim(vol) else np.full(len(px), vol)}, index=IDX[:len(px)])


class Eligible(unittest.TestCase):
    def test_split_adjusted_low_price_still_eligible(self):
        """revquant 复核：历史 Close 已按之后的拆股调整（NVDA 2012 年 0.35，当时真实约 14 美元），不能拿来判断“< 1 美元”。
        成交额足够时照样在样本内。"""
        r = raw(np.full(600, 0.35), vol=5e7)                                       # 0.35 × 5000 万股 = 1750 万美元/天
        e = usdata.eligible_pit(r, r.index)
        self.assertTrue(e.iloc[300:].all())

    def test_thin_then_liquid(self):
        vol = np.r_[np.full(500, 1e4), np.full(500, 1e6)]                          # 前 500 天成交额 5 万美元/天，之后 500 万
        r = raw(np.full(1000, 5.0), vol=vol)
        e = usdata.eligible_pit(r, r.index)
        self.assertEqual(usdata.screen(r, usdata.adjust(r)), "")                  # 旧口径：今天流动性够，整只全程入选
        self.assertFalse(e.iloc[:500].any())                                       # 新口径：成交清淡那段不持有
        first_ok = e.idxmax()
        k = e.index.get_loc(first_ok)
        self.assertGreater(k, 500)                                                 # 60 日中位数过线后，等到下一个月初才持有
        self.assertNotEqual(first_ok.month, e.index[k - 1].month)
        self.assertTrue(e.iloc[-100:].all())

    def test_monthly_and_no_lookahead(self):
        rng = np.random.default_rng(0)
        px = np.exp(np.cumsum(rng.normal(0, 0.05, 1000))) * 2                     # 在 1 美元上下来回
        vol = rng.choice([0, 1e6], 1000, p=[0.1, 0.9])
        r = raw(px, vol)
        e = usdata.eligible_pit(r, r.index)
        m = r.index.to_period("M")
        for per in set(m):
            self.assertEqual(e[m == per].nunique(), 1)                             # 月内不变
        for cut in (400, 650, 900):
            e2 = usdata.eligible_pit(r.iloc[:cut], r.index[:cut])
            pd.testing.assert_series_equal(e.iloc[:cut], e2, check_names=False)    # 截掉未来不改变过去
        self.assertTrue(e.any() and not e.all())

    def test_thresholds_match_screen(self):
        r = raw(np.full(400, 5.0), vol=50_000)                                     # 成交额 25 万 < 50 万
        self.assertIn("流动性", usdata.screen(r, usdata.adjust(r)))
        self.assertFalse(usdata.eligible_pit(r, r.index).any())
        self.assertEqual(usdata.hard_screen(r, usdata.adjust(r)), "")              # 不再整只排除
        bad = raw(np.r_[np.full(300, 1.0), [5.0], np.full(99, 5.0)])
        self.assertIn("300%", usdata.hard_screen(bad, usdata.adjust(bad)))


class ScanOne(unittest.TestCase):
    def test_masked_weights(self):
        from src import scan_us
        from config import MARKETS
        px = 5 * np.exp(np.cumsum(np.full(1000, 0.002)))
        r = raw(px, vol=np.r_[np.full(500, 1e4), np.full(500, 1e6)])
        scan_us._init(None, MARKETS["us"], {})
        with mock.patch.object(usdata, "load_raw", lambda code: r):
            rows, why = scan_us.scan_one("TEST")
        self.assertEqual(why, "")
        self.assertTrue(rows and all("eligible_share" in x for x in rows))
        self.assertLess(rows[0]["eligible_share"], 0.8)
        self.assertTrue(rows[0]["eligible_now"])


class DailyStep(unittest.TestCase):
    def test_due_logic(self):
        import daily
        from src import scan_us
        d = Path(tempfile.mkdtemp())
        (d / "data").mkdir()
        meta = d / "meta.json"
        with mock.patch.object(usdata, "root", lambda: d), mock.patch.object(scan_us, "META", meta):
            self.assertFalse(daily._us_scan_due())                                  # 目录里没有 parquet、也没 meta：没数据可扫
            (d / "data" / "AAA.parquet").write_bytes(b"x")
            self.assertTrue(daily._us_scan_due())                                   # 没有 meta：要扫
            meta.write_text("{}")
            os.utime(meta, (time.time() + 10, time.time() + 10))
            self.assertFalse(daily._us_scan_due())                                  # meta 比数据新：跳过
            os.utime(d / "data" / "AAA.parquet", (time.time() + 20, time.time() + 20))
            self.assertTrue(daily._us_scan_due())
        with mock.patch.object(usdata, "root", lambda: None):
            self.assertFalse(daily._us_scan_due())

    def test_failure_only_noted(self):
        import daily
        from src import scan_us
        notes = []
        with mock.patch.object(daily, "_us_scan_due", lambda: True), \
             mock.patch.object(scan_us, "run", mock.Mock(side_effect=RuntimeError("boom"))), \
             mock.patch.object(daily.daily_status, "note", lambda m, msg: notes.append((m, msg))):
            daily._us_scan()
        self.assertEqual(notes[0][0], "us")


if __name__ == "__main__":
    unittest.main()
