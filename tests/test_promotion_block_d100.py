"""D100：晋升检验改用区块抽样的随机对照（C02）。临时账本 / 合成数据，不读写 data/。
- promotion.block_side 与 C02 脚本 scripts/promotion_block_null._block_side 同一算法：同一输入、同一种子，结果逐位相同。
- 账本层面：连续多天持有同一批股票、但没有选股能力的策略，区块对照比每天独立抽样宽得多（C02 的机制）；
  每天换一批的策略两者差不多宽。
- 同一账本重算结果相同；promotion_status 标出 null_method="block"，旧口径的 p 作为 p_iid 一并给出。
运行：.venv/bin/python tests/test_promotion_block_d100.py"""
import importlib.util
import random
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import promotion as PR  # noqa: E402
from src import signal_ledger as L  # noqa: E402


def load_script():
    spec = importlib.util.spec_from_file_location("c02", ROOT / "scripts/promotion_block_null.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["c02"] = m                       # dataclass 需要模块已登记
    spec.loader.exec_module(m)
    return m


def ledger(n_weeks=6, stocks=400, per_day=20, hold=10, seed=3):
    """sticky：每 hold 天换一批股票、中间一直持有（没有选股能力）；fresh：每天随机换一批。
    5 日收益 = 之后 5 天日收益之和（相邻两天重叠 4/5），所以 sticky 连续几天的得分高度相关。"""
    c = L.connect(Path(tempfile.mkdtemp()) / "b.db")
    cal, d = [], date(2026, 1, 5)
    while len(cal) < n_weeks * 5:
        if d.weekday() < 5:
            cal.append(d.isoformat())
        d += timedelta(days=1)
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.03, (len(cal) + 6, stocks))
    cs = np.vstack([np.zeros(stocks), r.cumsum(0)])
    pr = random.Random(seed)
    sticky = []
    for i, day in enumerate(cal):
        ret5 = cs[i + 6] - cs[i + 1]
        c.execute("INSERT INTO days VALUES(?,?,?)", (day, 1, "x"))
        c.execute("INSERT INTO done VALUES(?,?,?)", (day, 5, stocks))
        c.executemany("INSERT INTO moves VALUES(?,?,?,?,?,?,?)", [(day, f"6{k:05d}", 5, float(ret5[k]), 0.0, 0, 0) for k in range(stocks)])
        if i % hold == 0:
            sticky = pr.sample(range(stocks), per_day)
        for st, ks in (("sticky", sticky), ("fresh", pr.sample(range(stocks), per_day))):
            c.executemany("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          [(day, st, f"6{k:05d}", "", "买入", 0, 0.5, 10, "正常", 0, "[]") for k in ks])
    c.commit()
    return c, (date.fromisoformat(cal[-1]) + timedelta(days=3)).isoformat()


class SameAsC02(unittest.TestCase):
    def test_block_side_bit_identical(self):
        S = load_script()
        rng0 = np.random.default_rng(7)
        book = S.synth_book(rng0, stocks=500, weeks=4, per_side=30, mean_run=5.0)
        lb = S.run_lengths(book.buy)
        np.testing.assert_array_equal(np.sort(lb), np.sort(PR.run_lengths(book.buy)))
        want, _, _ = S._block_side(book, "buy", lb, 200, np.random.default_rng(11))
        got, pos, _ = PR.block_side(list(book.ex), list(book.ok_buy), book.buy, lb, 200, np.random.default_rng(11))
        np.testing.assert_array_equal(want, got)
        self.assertTrue((pos >= 0).all())


class LedgerBlock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c, cls.today = ledger()
        PR.register(cls.c)
        PR.ensure_null(cls.c)

    def sd(self, strat, method):
        cw = PR.complete_weeks(self.c, self.today)
        wk = PR.weekly(self.c, strat, cw, method=method)
        _, null_c = PR._cum(wk)
        return float(null_c[-1].std(ddof=1)), wk

    def test_sticky_wider_fresh_same(self):
        s_blk, wb = self.sd("sticky", "block")
        s_iid, wi = self.sd("sticky", "iid")
        self.assertEqual(wb["weeks"], wi["weeks"])
        np.testing.assert_allclose(wb["own"], wi["own"], rtol=0, atol=1e-12)       # 策略自己的得分两种口径完全一样
        self.assertGreater(s_blk / s_iid, 1.5)                                       # 连续持有 10 天：独立抽样明显偏窄
        f_blk, _ = self.sd("fresh", "block")
        f_iid, _ = self.sd("fresh", "iid")
        self.assertLess(abs(f_blk / f_iid - 1), 0.15)                               # 每天换一批：两者差不多

    def test_status_fields_and_deterministic(self):
        PR._BLOCK_MEMO.clear()
        a = PR.promotion_status(self.c, self.today)
        PR._BLOCK_MEMO.clear()
        b = PR.promotion_status(self.c, self.today)
        self.assertEqual(a["null_method"], "block")
        for s in ("sticky", "fresh"):
            self.assertEqual(a["strategies"][s]["p"], b["strategies"][s]["p"])
            self.assertIn("p_iid", a["strategies"][s])
        old = PR.promotion_status(self.c, self.today, method="iid")
        self.assertEqual(old["null_method"], "iid")
        self.assertEqual(old["strategies"]["sticky"]["p"], a["strategies"]["sticky"]["p_iid"])


if __name__ == "__main__":
    unittest.main()
