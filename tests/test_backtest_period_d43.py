"""D43：回测区间预设（all / is / recent）。截到 end 再算，end 之前每一天的净值与不截断完全相同（没有未来函数时理应如此）。
合成行情，不读 data/。运行：.venv/bin/python tests/test_backtest_period_d43.py"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import MARKETS  # noqa: E402
from src import registry  # noqa: E402

MCFG = MARKETS["cn"]
CODE = "510300"


def synth(n=2000, seed=5):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2012-01-04", periods=n)
    c = 10 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, n)))
    o = c * np.exp(rng.normal(0, 0.003, n))
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.004, "Low": np.minimum(o, c) * 0.996, "Close": c, "Volume": 1e6}, index=idx)


class Period(unittest.TestCase):
    def test_bounds_whitelist(self):
        self.assertEqual(registry.period_bounds("is"), ("2013-01-01", "2019-12-31"))
        self.assertEqual(registry.period_bounds("recent"), ("2020-01-01", "2026-03-31"))
        self.assertEqual(registry.period_bounds("all"), (None, None))
        self.assertEqual(registry.period_bounds("2030-01-01"), (None, None))      # 不认识的值不当日期用

    def test_end_truncation_matches_full_run(self):
        prices = {CODE: synth()}
        for name in ("blend2", "trend_vol", "rsi2", "donchian_s1", "buy_hold"):
            full = registry.run(name, prices, CODE, {}, MCFG, market="cn")
            cut = registry.run(name, prices, CODE, {}, MCFG, market="cn", end="2016-12-30", start="2013-01-01")
            self.assertLessEqual(cut["end"], "2016-12-30", name)
            f = pd.Series(full["equity"], index=full["dates"])
            c = pd.Series(cut["equity"], index=cut["dates"])
            f = f.loc[c.index[0]:c.index[-1]]
            # 净值输出保留 5 位小数，且各自从自己的统计起点累乘：比较逐日收益
            np.testing.assert_allclose(f.pct_change().iloc[1:].to_numpy(), c.pct_change().iloc[1:].to_numpy(), atol=3e-5, err_msg=name)

    def test_detail_uses_truncated_data(self):
        prices = {CODE: synth()}
        r = registry.run("blend2", prices, CODE, {}, MCFG, market="cn", end="2016-12-30", detail=True)
        self.assertLessEqual(max(r["dates"]), "2016-12-30")
        self.assertIsNotNone(r["analysis"])


if __name__ == "__main__":
    unittest.main()
