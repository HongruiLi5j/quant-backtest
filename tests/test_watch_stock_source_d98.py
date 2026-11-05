"""D98：A 股自选个股优先读全市场文件（比例复权），不读加法复权的 csv。
用临时目录造两份行情，不碰 data/。另外对真实的 600531 做一条只读检查：读到的行情里没有主板不可能的单日涨跌（>10.2%）。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import allmarket, datasource  # noqa: E402


def _frame(dates, close):
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    return pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": [100.0] * len(close)}, index=idx)


class WatchStockSourceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        (root / "cn").mkdir()
        (root / "cn_all").mkdir()
        self.patches = [mock.patch.object(datasource, "CACHE", root),
                        mock.patch.object(allmarket, "ALL", root / "cn_all"),
                        mock.patch.object(allmarket, "_drop_unfinished", side_effect=lambda df: df)]
        for p in self.patches:
            p.start()
        self.root = root

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def _write(self, csv=None, full=None, code="600531"):
        if csv is not None:
            csv.to_csv(self.root / "cn" / f"{code}.csv")
        if full is not None:
            full.to_parquet(self.root / "cn_all" / f"{code}.parquet")

    def test_prefers_full_market_file(self):
        self._write(_frame(["2026-11-03", "2026-11-04"], [1.0, 2.0]), _frame(["2026-11-03", "2026-11-04"], [1.0, 1.1]))
        df = datasource.load_stock_prices("cn", "600531")
        self.assertEqual(list(df["Close"]), [1.0, 1.1])

    def test_falls_back_when_full_file_is_older(self):
        """全市场更新失败、文件比 csv 旧：用 csv，不拿旧数据算今天的信号。"""
        self._write(_frame(["2026-11-03", "2026-11-04"], [1.0, 2.0]), _frame(["2026-11-03"], [1.0]))
        self.assertEqual(datasource.load_stock_prices("cn", "600531").index[-1], pd.Timestamp("2026-11-04"))

    def test_falls_back_without_full_file(self):
        self._write(_frame(["2026-11-04"], [3.0]))
        self.assertEqual(list(datasource.load_stock_prices("cn", "600531")["Close"]), [3.0])

    def test_etf_and_us_untouched(self):
        self._write(_frame(["2026-11-04"], [3.0]), _frame(["2026-11-04"], [9.0]), code="510300")
        self.assertIsNone(datasource.stock_price_path("cn", "510300"))
        self.assertIsNone(datasource.stock_price_path("us", "AAPL"))

    def test_load_market_uses_it_for_watchlist_only(self):
        self._write(_frame(["2026-11-04"], [2.0]), _frame(["2026-11-04"], [1.1]))
        self._write(_frame(["2026-11-04"], [5.0]), code="510300")
        cfg = {"universe": {"510300": "沪深300ETF"}}
        with mock.patch.object(datasource, "load_watchlist", return_value={"600531": ""}):
            out = datasource.load_market("cn", cfg)
        self.assertEqual(float(out["600531"]["Close"].iloc[-1]), 1.1)
        self.assertEqual(float(out["510300"]["Close"].iloc[-1]), 5.0)


class Real600531Tests(unittest.TestCase):
    """只读真实沙盒数据：没有这两个文件就跳过。"""

    def test_no_impossible_daily_moves(self):
        if datasource.stock_price_path("cn", "600531") is None:
            self.skipTest("沙盒里没有 600531 的全市场文件")
        df = datasource.load_stock_prices("cn", "600531")
        moves = df["Close"].pct_change().abs()
        self.assertEqual(int((moves > 0.102).sum()), 0)


if __name__ == "__main__":
    unittest.main()
