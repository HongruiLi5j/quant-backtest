"""D18：个股页的「所有策略对比」与回测实验室/今日页（registry.run）在同一份数据上必须给出同一个年化和超额 Sharpe。
以前个股页从第 250 根 K 线起算、registry 从第 260 根起算，510300 正好多算进 2013 年 6 月的大跌：买入持有 5.1% vs 6.3%。
用合成行情（不读 data/），替换 detail.lookup 和现金曲线。运行：.venv/bin/python tests/test_detail_registry_same_d18.py"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import MARKETS  # noqa: E402
from src import datasource, detail, display_price, registry  # noqa: E402

MCFG = MARKETS["cn"]
CODE = "510300"


def synth(n=900, seed=3, drift=0.0003, vol=0.015, start=10.0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2013-01-04", periods=n)
    close = start * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    open_ = close * np.exp(rng.normal(0, 0.003, n))
    hi, lo = np.maximum(open_, close) * 1.004, np.minimum(open_, close) * 0.996
    return pd.DataFrame({"Open": open_, "High": hi, "Low": lo, "Close": close, "Volume": 1e6}, index=idx)


class SameNumbers(unittest.TestCase):
    def test_compare_equals_registry(self):
        df = synth()
        cash = synth(seed=9, drift=0.0001, vol=0.0002, start=100.0)
        info = {"code": CODE, "name": "测试ETF", "kind": "ETF", "board": "", "industry": ""}
        with mock.patch.object(detail, "lookup", return_value=(df, info)), \
             mock.patch.object(datasource, "load_cached", return_value=cash), \
             mock.patch.object(display_price, "apply", lambda m, c, d: (d, 1.0, "测试")):
            out = detail.build("cn", CODE, MCFG, "blend2", "1y")
        prices = {CODE: df, MCFG["cash"]: cash}
        got = {c["key"]: c for c in out["compare"]}
        self.assertEqual(out["compare_start"], registry.stats_start(df.index).strftime("%Y-%m-%d"))
        for name in detail.SINGLE:
            r = registry.run(name, prices, CODE, {}, MCFG, market="cn")
            self.assertEqual(r["start"], out["compare_start"], name)
            self.assertLess(abs(r["stats"]["CAGR%"] - got[name]["cagr"]), 0.01, name)
            self.assertLess(abs(r["stats"]["ExcessSharpe"] - got[name]["sharpe"]), 0.01, name)

    def test_stats_start(self):
        idx = pd.bdate_range("2020-01-01", periods=400)
        self.assertEqual(registry.stats_start(idx), idx[registry.WARMUP_BARS])
        self.assertEqual(registry.stats_start(idx, "2021-06-01"), pd.Timestamp("2021-06-01"))
        self.assertEqual(registry.stats_start(idx[:100]), idx[99])          # 数据不够预热：取最后一根


if __name__ == "__main__":
    unittest.main()
