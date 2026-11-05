"""D99（旧补丁 02 的 b、c）：复权价 ≤0 的坏数据行置 NaN；已实现波动率恰为 0 时波动率目标仓位给 0（与 backtrader / RQAlpha 一致）。
临时目录 + 合成数据，不读写 data/。三引擎一致性见 tests/test_engine_rules.py::test_zero_volatility_position_agrees。
运行：.venv/bin/python tests/test_d99_bad_rows_zero_vol.py"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import allmarket, strategies  # noqa: E402
from src.market_monitor import factors as fx  # noqa: E402

IDX = pd.bdate_range("2020-01-01", periods=8)


def frame():
    c = np.array([1.0, 0.5, -0.1, -0.3, 0.2, 1.0, 1.1, 1.2])
    df = pd.DataFrame({"Open": c, "High": c + 0.1, "Low": c - 0.05, "Close": c, "Volume": 1e5}, index=IDX)
    df.iloc[4, df.columns.get_loc("Low")] = -0.01                     # 第 4 行只有最低价 ≤0
    return df


class BadRows(unittest.TestCase):
    def test_mask_rows(self):
        df = frame()
        m = allmarket.mask_bad_rows(df)
        bad = (df[["Open", "High", "Low", "Close"]] <= 0).any(axis=1)     # 第 2、3 行收盘 ≤0，第 4 行只有最低价 ≤0
        self.assertEqual(list(bad), [False, False, True, True, True, False, False, False])
        self.assertTrue(m.loc[bad].isna().all().all())                       # 整行（含成交量）置空
        pd.testing.assert_frame_equal(m.loc[~bad], df.loc[~bad])             # 其余不变
        self.assertEqual(len(m), len(df))                                    # 不删行
        r = m["Close"].pct_change(fill_method=None)
        self.assertTrue(np.isnan(r.iloc[5]))                                 # 复牌第一天不会算出假收益
        self.assertIs(allmarket.mask_bad_rows(df.iloc[[0, 1, 5]]).__class__, pd.DataFrame)
        self.assertTrue(df.equals(frame()))                                  # 不改调用方的对象

    def test_load_masks_file_untouched(self):
        d = Path(tempfile.mkdtemp())
        frame().to_parquet(d / "999999.parquet")
        with mock.patch.object(allmarket, "ALL", d):
            m = allmarket.load("999999")
            raw = allmarket.load("999999", raw=True)                        # 更新路径（读出来再存回去）用原样数据
        pd.testing.assert_frame_equal(raw, pd.read_parquet(d / "999999.parquet"))
        self.assertEqual(int(m["Close"].isna().sum()), 3)
        self.assertEqual(int((pd.read_parquet(d / "999999.parquet")["Low"] <= 0).sum()), 3)   # 文件本身不改

    def test_panel(self):
        df = frame()
        w = lambda col: df[[col]].rename(columns={col: "600595"})
        p = fx.Panel(w("Open"), w("High"), w("Low"), w("Close"), w("Volume"))
        self.assertEqual(int(p.close["600595"].isna().sum()), 3)
        self.assertEqual(int(p.volume["600595"].isna().sum()), 3)


class ZeroVol(unittest.TestCase):
    def test_vol_scale(self):
        rng = np.random.default_rng(0)
        c = np.r_[40 + np.cumsum(rng.normal(0, 0.3, 60)), np.full(25, 0.0)]
        c[60:] = c[59]
        close = pd.Series(c, index=pd.bdate_range("2020-01-01", periods=len(c)))
        v = strategies.vol_scale(close, 0.15, 20)
        self.assertEqual(float(v.iloc[-1]), 0.0)                             # 价格完全不动：空仓（修复前是 1.0 满仓）
        self.assertTrue(v.iloc[:20].isna().all())                            # 预热期仍是 NaN
        rv = strategies.realized_vol(close, 20)
        live = rv.notna() & (rv > 0)
        np.testing.assert_allclose(v[live], (0.15 / rv[live]).clip(upper=1.0))   # 正常的天和原公式完全一样
        self.assertTrue((strategies.vol_scale(close, None) == 1.0).all())


if __name__ == "__main__":
    unittest.main()
