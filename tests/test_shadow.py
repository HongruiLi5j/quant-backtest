"""影子候选策略测试：python3 tests/test_shadow.py
重点：1) 没有未来函数（砍掉 t 之后的数据，t 当天的仓位不变）；2) 仓位在 [0,1]；3) 随机对照可重复。"""
import sys, unittest
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import shadow as SH
sys.path.insert(0, str(Path(__file__).resolve().parent))
from realdata_guard import ZRealDataUntouchedTests  # noqa: E402,F401  真实数据守卫


def fake(n=420, seed=1):
    rng = np.random.default_rng(seed)
    r = rng.normal(0.0004, 0.02, n)
    c = 10 * np.exp(np.cumsum(r))
    o = c * (1 + rng.normal(0, 0.003, n))
    h = np.maximum(o, c) * (1 + abs(rng.normal(0, 0.004, n)))
    l = np.minimum(o, c) * (1 - abs(rng.normal(0, 0.004, n)))
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": 1e6}, index=idx)


class T(unittest.TestCase):
    def test_no_lookahead(self):
        df = fake()
        for name, (_, _, fn) in SH.CANDIDATES.items():
            full = fn(df, "600000").fillna(0.0)
            for cut in (300, 350, 400):
                part = fn(df.iloc[:cut], "600000").fillna(0.0)
                self.assertAlmostEqual(float(full.iloc[cut - 1]), float(part.iloc[-1]), places=9, msg=f"{name} 在第 {cut} 天用到了未来数据")

    def test_weights_in_range(self):
        df = fake(seed=2)
        for name, (_, _, fn) in SH.CANDIDATES.items():
            w = fn(df, "000001").fillna(0.0)
            self.assertTrue(((w >= -1e-12) & (w <= 1 + 1e-12)).all(), name)

    def test_random_control_reproducible_and_calibrated(self):
        df = fake()
        a1, a2 = SH._random(df, "a", 0.3, "600000"), SH._random(df, "a", 0.3, "600000")
        self.assertTrue((a1 == a2).all())
        self.assertFalse((a1 == SH._random(df, "a", 0.3, "600001")).all())
        self.assertAlmostEqual(a1.mean(), 0.3, delta=0.08)

    def test_record_signals_idempotent(self):
        import tempfile
        from src import signal_ledger as L
        c = L.connect(Path(tempfile.mkdtemp()) / "s.db")
        c.execute("INSERT INTO days VALUES('2026-10-19',1,'x')")
        c.executemany("INSERT INTO state VALUES(?,?,?,?,?)", [("2026-10-19", "mom_6m", "600000", 0.0, 1.0), ("2026-10-19", "mom_6m", "600001", 1.0, 0.0),
                                                              ("2026-10-19", "mom_6m", "600002", 1.0, 1.0)])
        self.assertEqual(SH.record_signals(c, "2026-10-19"), 2)
        self.assertEqual(SH.record_signals(c, "2026-10-19"), 0)
        acts = sorted(r["action"] for r in c.execute("SELECT action FROM signals"))
        self.assertEqual(acts, ["买入", "卖出"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
