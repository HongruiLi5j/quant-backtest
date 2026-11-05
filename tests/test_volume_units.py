"""成交量单位（股/手）读取层换算 + 检测脚本的测试。全部用合成数据和临时目录，不读写 data/ 真实文件。"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from src import volume_units as vu  # noqa: E402
import volume_unit_check as chk  # noqa: E402


def make_daily(n=120, seed=0, start="2025-01-02"):
    """合成一只普通股票：收盘价 10 元附近随机游走，成交量 1e7 股附近（log10 噪声 0.1），最高≠最低。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    close = 10 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    vol = 10 ** (7 + rng.normal(0, 0.1, n))
    return pd.DataFrame({"Open": close, "High": close * 1.02, "Low": close * 0.98,
                         "Close": close, "Volume": vol}, index=idx)


def turnover_rows(daily, dates, code="600000"):
    """按"真实股数 = 合成时的股数"造旁表：成交额 = 股数 × 收盘价。dates 上的 Volume 必须是换算前的真值。"""
    true_shares = daily["_true"] if "_true" in daily.columns else daily["Volume"]
    d = pd.DatetimeIndex(dates)
    return pd.DataFrame({"date": d.strftime("%Y-%m-%d"), "code": code,
                         "turnover_yuan": (true_shares[d] * daily["Close"][d]).to_numpy(),
                         "close_raw": daily["Close"][d].to_numpy(), "source": "测试"})


class ClassifyTests(unittest.TestCase):
    def test_ratio(self):
        out = vu.classify_ratio([100, 1, 30, 0, 100], [100, 100, 100, 100, np.nan])
        self.assertEqual(out[0], 0)                 # 比值 1 → 股
        self.assertEqual(out[1], 1)                 # 比值 0.01 → 手
        self.assertTrue(np.isnan(out[2:]).all())    # 0.3 对不上、0 成交量、缺旁表都不判断
        self.assertEqual(vu.classify_ratio(1.0, 100.0), 1)

    def test_verified_flags(self):
        d = make_daily(10)
        d["_true"] = d["Volume"]
        d.loc[d.index[3], "Volume"] /= 100          # 第 4 天文件里是"手"
        rows = turnover_rows(d, d.index[2:5])
        v = vu.verified_lot_flags(d, rows)
        self.assertEqual(v.iloc[2], 0)
        self.assertEqual(v.iloc[3], 1)
        self.assertEqual(v.iloc[4], 0)
        self.assertTrue(v.iloc[[0, 1, 5]].isna().all())


class InferTests(unittest.TestCase):
    def test_step_history_in_lots(self):
        """前 60 天是腾讯历史（手），后 60 天新浪（股），旁表只覆盖最后 5 天。"""
        d = make_daily(120)
        d["_true"] = d["Volume"]
        d.loc[d.index[:60], "Volume"] /= 100
        rows = turnover_rows(d, d.index[-5:])
        original = d.copy()
        out, info = vu.to_shares(d.drop(columns="_true"), rows)
        self.assertTrue(info["is_lot"].iloc[:60].all())
        self.assertFalse(info["is_lot"].iloc[60:].any())
        np.testing.assert_allclose(out["Volume"].to_numpy(), d["_true"].to_numpy(), rtol=1e-9)
        self.assertEqual((info["basis"] == vu.BASIS_VERIFIED).sum(), 5)
        self.assertEqual((info["basis"] == vu.BASIS_INFERRED).sum(), 115)
        pd.testing.assert_frame_equal(d, original)          # 不改传入的数据
        s = vu.summarize(info)
        self.assertEqual((s["lot_rows"], s["switches"], s["lot_inferred"]), (60, 1, 60))

    def test_isolated_snapshot_day(self):
        """中间某天用了东方财富快照（手），旁表没覆盖那天 → 推断成手。"""
        d = make_daily(60, seed=1)
        d["_true"] = d["Volume"]
        d.loc[d.index[40], "Volume"] /= 100
        rows = turnover_rows(d, d.index[-3:])
        out, info = vu.to_shares(d.drop(columns="_true"), rows)
        self.assertEqual(int(info["is_lot"].sum()), 1)
        self.assertTrue(info["is_lot"].iloc[40])
        self.assertAlmostEqual(out["Volume"].iloc[40], d["_true"].iloc[40], delta=1e-3)

    def test_one_price_streak_not_flipped(self):
        """新股连续一字板：成交量真实地只有正常的 1/100，不能当成"手"放大。"""
        d = make_daily(80, seed=2)
        streak = d.index[:10]
        d.loc[streak, "High"] = d.loc[streak, "Close"]
        d.loc[streak, "Low"] = d.loc[streak, "Close"]
        d.loc[streak, "Volume"] /= 100
        rows = turnover_rows(d, d.index[-3:])
        out, info = vu.to_shares(d, rows)
        self.assertFalse(info["is_lot"].any())
        np.testing.assert_allclose(out["Volume"].to_numpy(), d["Volume"].to_numpy())

    def test_limit_close_dip_not_flipped(self):
        """涨停收盘（收在最高、涨幅 ≥4.5%）当天极度缩量也不翻转。"""
        d = make_daily(60, seed=3)
        i = 30
        d.loc[d.index[i], "Close"] = d["Close"].iloc[i - 1] * 1.10
        d.loc[d.index[i], "High"] = d["Close"].iloc[i]
        d.loc[d.index[i], "Low"] = d["Close"].iloc[i] * 0.99
        d.loc[d.index[i], "Volume"] /= 100
        _, info = vu.to_shares(d, turnover_rows(d, d.index[-3:]))
        self.assertFalse(info["is_lot"].any())

    def _limit_close(self, d, i, up=True):
        d.loc[d.index[i], "Close"] = d["Close"].iloc[i - 1] * (1.10 if up else 0.90)
        edge = d["Close"].iloc[i]
        d.loc[d.index[i], "High"] = edge if up else edge * 1.03
        d.loc[d.index[i], "Low"] = edge * 0.97 if up else edge

    def test_lot_day_next_to_limit_close(self):
        """东方财富"手"日的第二天是跌停收盘：软罚分下仍能识别出这一天（硬禁止时会漏）。"""
        d = make_daily(60, seed=9)
        d["_true"] = d["Volume"]
        self._limit_close(d, 41, up=False)
        d.loc[d.index[40], "Volume"] /= 100
        out, info = vu.to_shares(d.drop(columns="_true"), turnover_rows(d, d.index[-1:]))
        self.assertTrue(info["is_lot"].iloc[40])
        self.assertEqual(int(info["is_lot"].sum()), 1)

    def test_limit_streak_no_island(self):
        """连续 4 天涨停收盘，中间一天是"手"：最多漏掉这一天，不能把前面整段历史翻错。"""
        d = make_daily(80, seed=10)
        for i in range(40, 44):
            self._limit_close(d, i, up=True)
        d.loc[d.index[42], "Volume"] /= 100
        _, info = vu.to_shares(d, turnover_rows(d, d.index[-1:]))
        others = info["is_lot"].drop(d.index[42])
        self.assertFalse(others.any())

    def test_ipo_first_day_not_flipped(self):
        """上市首日（第一行，没有昨收）成交量只有之后的 1/60：不当成"手"。"""
        d = make_daily(60, seed=11)
        d.loc[d.index[0], "Volume"] /= 60
        _, info = vu.to_shares(d, turnover_rows(d, d.index[-1:]))
        self.assertFalse(info["is_lot"].any())

    def test_moderate_dip_not_flipped(self):
        """普通的 10 倍缩量（远小于 100 倍）不翻转。"""
        d = make_daily(60, seed=4)
        d.loc[d.index[30], "Volume"] /= 10
        _, info = vu.to_shares(d, turnover_rows(d, d.index[-3:]))
        self.assertFalse(info["is_lot"].any())

    def test_no_anchor_unchanged(self):
        d = make_daily(50, seed=5)
        d.loc[d.index[:25], "Volume"] /= 100
        out, info = vu.to_shares(d, None)
        self.assertFalse(info["is_lot"].any())
        self.assertTrue((info["basis"] == vu.BASIS_NO_ANCHOR).all())
        pd.testing.assert_frame_equal(out, d)

    def test_verified_overrides_and_zero_volume(self):
        """旁表核实的日子即使看起来不平滑也服从旁表；停牌 0 成交量不参与、换算后仍是 0。"""
        d = make_daily(40, seed=6)
        d["_true"] = d["Volume"]
        d.loc[d.index[10], "Volume"] = 0.0
        d.loc[d.index[20], "Volume"] /= 100
        rows = turnover_rows(d, [d.index[20], d.index[-1]])
        out, info = vu.to_shares(d.drop(columns="_true"), rows)
        self.assertTrue(info["is_lot"].iloc[20])
        self.assertEqual(info["basis"].iloc[20], vu.BASIS_VERIFIED)
        self.assertEqual(out["Volume"].iloc[10], 0.0)
        self.assertEqual(int(info["is_lot"].sum()), 1)

    def test_empty(self):
        out, info = vu.to_shares(pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"]), None)
        self.assertEqual(len(out), 0)
        self.assertEqual(len(info), 0)


class ScriptTests(unittest.TestCase):
    def test_scan_temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cn = tmp / "cn_all"
            cn.mkdir()
            a = make_daily(120, seed=7)
            a["_true"] = a["Volume"]
            a.loc[a.index[:60], "Volume"] /= 100           # 历史是手
            a.loc[a.index[100], "Volume"] /= 100            # 有一天快照是手
            b = make_daily(120, seed=8)
            b["_true"] = b["Volume"]                         # 全是股
            rows = pd.concat([turnover_rows(a, a.index[95:], "600000"), turnover_rows(b, b.index[95:], "000001")])
            a.drop(columns="_true").to_parquet(cn / "600000.parquet")
            b.drop(columns="_true").to_parquet(cn / "000001.parquet")
            rows.to_parquet(tmp / "turnover_cn.parquet", index=False)
            before = {p.name: p.read_bytes() for p in cn.iterdir()}
            out = tmp / "out"
            res = chk.main(["--cn-all", str(cn), "--turnover", str(tmp / "turnover_cn.parquet"), "--out", str(out)])
            self.assertEqual(before, {p.name: p.read_bytes() for p in cn.iterdir()})     # 只读
            s = res["summary"]
            self.assertEqual(s["stocks_scanned"], 2)
            self.assertEqual(s["change_points"]["stocks_unlocked"], 1)
            self.assertEqual(s["turnover_check"]["by_unit"], {"股": 49, "手": 1})
            self.assertEqual(s["inference"]["stocks_with_lot_rows"], 1)
            self.assertEqual(s["inference"]["lot_rows_total"], 61)
            h = s["holdout_validation"]
            self.assertEqual((h["tp"], h["fn"], h["fp"]), (1, 0, 0))
            per = res["per_code"].set_index("code")
            self.assertEqual(per.loc["000001", "lot_rows"], 0)
            for name in ("summary.json", "change_points.csv", "isolated_days.csv", "turnover_check.csv", "per_code.csv"):
                self.assertTrue((out / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
