"""B04:scripts/check_etf_adjust.py 的判定逻辑。数据全是合成的，放在临时目录，不读写真实 data/。

python .venv/bin/python tests/test_check_etf_adjust.py
"""
import contextlib
import io
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
import check_etf_adjust as C  # noqa: E402
from src import display_price  # noqa: E402

LAST_DAY = "2026-03-27"


def make_df(n=400, last_close=1.2, tail=0, k=1.0, seed=0):
    """合成日线：3 位小数报价；最后 tail 行乘比例 k（模拟 _sina_fallback 缩放的尾段），最后一根 Close 强制等于 last_close。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=LAST_DAY, periods=n)
    close = np.round(1.0 + np.cumsum(rng.normal(0, 0.002, n)), 3)
    df = pd.DataFrame({"Open": np.r_[close[0], close[:-1]], "High": close + 0.004, "Low": close - 0.004,
                       "Close": close, "Volume": rng.integers(1_000, 2_000, n).astype(float)}, index=idx)
    df["High"] = df[["High", "Open"]].max(axis=1)
    df["Low"] = df[["Low", "Open"]].min(axis=1)
    if tail:
        df.iloc[-tail:, :4] = df.iloc[-tail:, :4] * k
        df.iloc[-tail:, 4] *= 100
    df.iloc[-1, df.columns.get_loc("Close")] = last_close
    return df


class CheckEtfAdjust(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "cn").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, code, df, raw=None, day=LAST_DAY):
        df.to_csv(self.dir / "cn" / f"{code}.csv", float_format="%.6f")
        if raw is not None:
            p = self.dir / "engine_price_scale.json"
            cache = json.loads(p.read_text()) if p.exists() else {}
            cache[code] = {"day": day, "k": raw / float(df["Close"].iloc[-1]), "raw": raw}
            p.write_text(json.dumps(cache), encoding="utf-8")

    def run_one(self, code):
        return C.analyze(self.dir, [code], {code: "测试"}, "2026-03-31")[0]

    def test_back_adjusted_when_local_above_real(self):
        self.put("AAA", make_df(last_close=1.2), raw=1.0)
        r = self.run_one("AAA")
        self.assertAlmostEqual(r["scale"]["f"], 1.2)
        self.assertIn("后复权", r["scale"]["verdict"])
        self.assertEqual(r["holdings"]["shows"], 1.0)        # 持仓页显示的是缓存里的真实价，不是 1.2

    def test_equal_cannot_distinguish(self):
        self.put("AAA", make_df(last_close=1.0), raw=1.0)
        r = self.run_one("AAA")
        self.assertIn("无法区分", r["scale"]["verdict"])
        self.assertEqual(r["holdings"]["shows"], 1.0)

    def test_local_below_real_is_not_plain_hfq(self):
        self.put("AAA", make_df(last_close=0.5), raw=1.5)
        r = self.run_one("AAA")
        self.assertLess(r["scale"]["f"], 1)
        self.assertIn("不是单纯的分红后复权", r["scale"]["verdict"])
        self.assertEqual(r["holdings"]["shows"], 1.5)

    def test_cache_day_mismatch_or_missing_gives_no_price(self):
        self.put("AAA", make_df(), raw=1.0, day="2026-03-26")      # 日期不是最后一根 K 线
        r = self.run_one("AAA")
        self.assertIsNone(r["scale"]["f"])
        self.assertIsNone(r["holdings"]["shows"])
        self.put("BBB", make_df())                                 # 完全没有缓存条目
        r = self.run_one("BBB")
        self.assertIsNone(r["scale"]["f"])
        self.assertIsNone(r["holdings"]["shows"])

    def test_no_local_file(self):
        r = self.run_one("ZZZ")
        self.assertEqual(r["scale"]["verdict"], "无本地数据")
        self.assertIsNone(r["holdings"]["shows"])

    def test_holdings_check_restores_scale_file(self):
        before = display_price._SCALE_FILE
        self.put("AAA", make_df(), raw=1.0)
        self.run_one("AAA")
        self.assertEqual(display_price._SCALE_FILE, before)

    def test_seam_finds_scaled_tail_only(self):
        s = C.seam(make_df(tail=12, k=0.83371))
        self.assertEqual(s["tail_rows"], 12)
        self.assertGreater(s["volume_ratio_at_seam"], 50)          # 尾段成交量被放大 100 倍
        self.assertIsNone(C.seam(make_df())["tail_start"])         # 全是 3 位小数报价：没有尾段

    def test_gap_stats_ignores_data_after_end(self):
        df = make_df(n=300)
        df.iloc[-1, df.columns.get_loc("Open")] = df["Close"].iloc[-2] * 0.5     # 截止日之后的巨大负跳空
        g = C.gap_stats(df, "2026-03-20")
        self.assertGreater(g["worst_gap"], -0.2)
        self.assertLess(C.gap_stats(df, "2026-03-31")["worst_gap"], -0.4)

    def test_bad_open_day_detected(self):
        df = make_df(n=300)
        day = df.index[150]
        df.loc[day, "Open"] = df["Close"].iloc[149] * 0.9
        df.loc[day, "Low"] = df.loc[day, "Open"]
        self.assertEqual(C.bad_open_days(df, "2026-03-31"), [day.strftime("%Y-%m-%d")])

    def test_main_writes_only_to_out_dir(self):
        self.put("AAA", make_df(last_close=1.2), raw=1.0)
        before = sorted(p.name for p in self.dir.rglob("*"))
        with tempfile.TemporaryDirectory() as out_tmp:
            out = Path(out_tmp) / "out"
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                C.main(["--daily-dir", str(self.dir), "--codes", "AAA", "--out", str(out)])
            self.assertEqual(sorted(p.name for p in self.dir.rglob("*")), before)     # 输入目录一个文件都没多
            data = json.loads((out / "check_etf_adjust.json").read_text(encoding="utf-8"))
            self.assertEqual(data[0]["code"], "AAA")
            self.assertIn("后复权", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
