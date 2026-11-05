"""D16：信号核对账本的「诚实区间」测试。
- 区间覆盖率：同一天的信号共享一个当天冲击（真实情况），逐条 Wilson 覆盖率不够，按「期」的 t 区间要 ≥ 92%。
- 4 个相互重叠的信号日不能得出「有效/反向」。
- 随机基线（当天跑赢等权平均的股票占比）按手算核对。
- 卖出信号：股票跑输全市场等权平均 = 方向正确。
运行：.venv/bin/python tests/test_ledger_honest_ci.py
"""
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import signal_ledger as L  # noqa: E402

CAL = [f"2026-{m:02d}-{d:02d}" for m in (1, 2, 3, 4, 5, 6) for d in range(1, 29) if (d % 7) not in (5, 6)]   # 合成交易日（每周 5 天）


def _db():
    return L.connect(Path(tempfile.mkdtemp()) / "t.db")


def _add_day(c, day, market, signals, h=5):
    """market = {code: ret}；signals = [(code, action)]。lim 全为 0。"""
    c.execute("INSERT OR IGNORE INTO days VALUES(?,?,?)", (day, 0, "x"))
    for code, ret in market.items():
        c.execute("INSERT OR REPLACE INTO moves VALUES(?,?,?,?,?,?,?)", (day, code, h, ret, 0.0, 0, 0))
    for code, act in signals:
        c.execute("INSERT OR REPLACE INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (day, "s", code, None, act, 0, 1, 10.0, None, 0, "[]"))
    c.commit()


class CoverageTest(unittest.TestCase):
    def test_period_t_interval_covers_wilson_does_not(self):
        """真实命中率 0.5，每天有共享冲击 p_d ~ N(0.5, 0.06)，每天 30–200 条，13 个信号日（h=1，不重叠）。"""
        reps, cov_t, cov_w = 300, 0, 0
        pos = {d: i for i, d in enumerate(CAL)}
        for seed in range(reps):
            rng = random.Random(seed)
            day_hit, k_all, n_all = {}, 0, 0
            for d in CAL[:13]:
                p = min(max(rng.gauss(0.5, 0.06), 0.0), 1.0)
                n = rng.randint(30, 200)
                k = sum(rng.random() < p for _ in range(n))
                day_hit[d] = k / n
                k_all += k
                n_all += n
            lo, hi, g, _ = L._period_ci(day_hit, k_all / n_all, 1, pos)
            self.assertEqual(g, 13)
            cov_t += lo <= 0.5 <= hi
            wl, wh = L._wilson(k_all, n_all)
            cov_w += wl <= 0.5 <= wh
        self.assertGreaterEqual(cov_t / reps, 0.92, f"t 区间覆盖率 {cov_t / reps:.2%}")
        self.assertLess(cov_w / reps, 0.92, f"逐条 Wilson 覆盖率 {cov_w / reps:.2%} 本应不够")

    def test_thin_spacing(self):
        pos = {d: i for i, d in enumerate(CAL)}
        sel = L._thin(CAL[:12], pos, 5)
        self.assertEqual(sel, [CAL[0], CAL[5], CAL[10]])
        self.assertEqual(L._thin(CAL[:4], pos, 1), CAL[:4])
        self.assertEqual(L._t975(1), 12.706)
        self.assertAlmostEqual(L._t975(60), 2.0, places=2)


class LedgerFixtureTest(unittest.TestCase):
    def test_four_overlapping_days_no_verdict(self):
        """减仓 h=10 的那种情况：4 个连续信号日，超额很一致，但只能是「样本不足/积累中」。"""
        c = _db()
        for i, d in enumerate(CAL[:4]):
            mk = {f"x{j}": 0.01 * (j % 5) for j in range(40)}
            mk.update({f"s{j}": -0.05 for j in range(12)})
            _add_day(c, d, mk, [(f"s{j}", "减仓") for j in range(12)], h=10)
        p = [r for r in L.precision(c, "s", CAL[30]) if r["action"] == "减仓" and r["h"] == 10][0]
        self.assertNotIn(p["verdict"], ("有效", "反向"))
        self.assertEqual(p["periods"], 1)                   # 4 天都在同一个 10 日窗口里，只算 1 期
        self.assertIsNone(p["hit_excess_lo"])               # 1 期无法给区间，而不是给一个窄得吓人的区间

    def test_random_share_and_sell_direction(self):
        c = _db()
        d = CAL[0]
        mk = {"a": 0.10, "b": 0.0, "c": -0.01, "d": -0.02, "e": -0.02}     # 平均 0.01：跑赢 1 只，跑输 4 只
        _add_day(c, d, mk, [("a", "买入"), ("c", "卖出")])
        up, dn = L.random_share(c)[(d, 5)]
        self.assertAlmostEqual(up, 0.2)
        self.assertAlmostEqual(dn, 0.8)
        rows = {r["action"]: r for r in L.precision(c, "s", CAL[10]) if r["h"] == 5}
        self.assertEqual(rows["买入"]["hit_excess"], 1.0)
        self.assertAlmostEqual(rows["买入"]["hit_random"], 0.2)
        self.assertAlmostEqual(rows["买入"]["edge"], 0.8)
        self.assertEqual(rows["卖出"]["hit_excess"], 1.0)          # c 跑输平均 → 卖出方向正确
        self.assertAlmostEqual(rows["卖出"]["hit_random"], 0.8)    # 卖出的随机基线是「跑输平均」的占比
        self.assertEqual(rows["卖出"]["benchmark"], "全市场等权平均")


if __name__ == "__main__":
    unittest.main()
