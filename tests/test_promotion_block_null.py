"""C02 区块抽样零分布对比脚本的测试：.venv/bin/python tests/test_promotion_block_null.py

全部用临时目录里的合成账本，不读写 data/，不联网。
  persist —— 同一批股票连续多天出信号（买入/加仓/卖出/减仓都有，含开盘一字板的信号）
  noise   —— 每天随机挑股票
"""
import hashlib
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from src import promotion as PR  # noqa: E402
from src import signal_ledger as L  # noqa: E402
import promotion_block_null as B  # noqa: E402

K = 200


def weekdays(start, n):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def build_db(start="2026-01-05", weeks=3, stocks=200, seed=3):
    """返回 (账本路径, 信号日)。另外多记 5 个未走满的记录日，让最后一个信号日之后有第 5 个记录日。"""
    path = Path(tempfile.mkdtemp(prefix="c02_test_")) / "ledger.db"
    c = L.connect(path)
    days = weekdays(start, weeks * 5 + 5)
    sig_days = days[:weeks * 5]
    rng = np.random.default_rng(seed)
    held = rng.choice(stocks, 15, replace=False)
    for i, d in enumerate(days):
        c.execute("INSERT INTO days VALUES(?,?,?)", (d, 1, "x"))
        if d not in sig_days:
            continue
        c.execute("INSERT INTO done VALUES(?,?,?)", (d, 5, stocks))
        ret = rng.normal(0.002, 0.05, stocks)
        lim = rng.choice([0, 0, 0, 0, 0, 0, 0, 0, 1, -1], stocks)
        order = rng.permutation(stocks)                                # 乱序插入：抽样池顺序不能依赖插入顺序
        c.executemany("INSERT INTO moves VALUES(?,?,?,?,?,?,?)",
                      [(d, f"6{k:05d}", 5, float(ret[k]), 0.0, int(lim[k]), 0) for k in order])
        if i % 4 == 3:                                                 # 每 4 天换掉一部分持仓
            held[:5] = rng.choice(stocks, 5, replace=False)
        rows = []
        for j, k in enumerate(sorted(set(held.tolist()))):
            rows.append((d, "persist", f"6{k:05d}", "", ("买入", "加仓", "卖出", "减仓")[j % 4], 0, 0.5, 10, "正常", 0, "[]"))
        for k in rng.choice(stocks, 12, replace=False):
            rows.append((d, "noise", f"6{k:05d}", "", "买入" if k % 2 else "卖出", 0, 0.5, 10, "正常", 0, "[]"))
        c.executemany("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    PR.register(c)
    PR.ensure_null(c, k=K)
    c.close()
    return path, sig_days


def toy_book(T=4, U=6, n=2, ok=None, ex=None):
    """手工小账本：每天 n 条买入信号（代码 0..n-1），没有卖出。"""
    ok = np.ones((T, U), bool) if ok is None else ok
    ex = np.ones((T, U)) if ex is None else ex
    buy = [np.arange(n) for _ in range(T)]
    return B.Book("toy", weekdays("2025-03-03", T), [str(i) for i in range(U)], ex, ok, ok.copy(),
                  buy, [np.zeros(0, int) for _ in range(T)], np.zeros(T))


class ReproduceProductionNull(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path, cls.days = build_db()
        cls.c = B.open_ro(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.c.close()

    def test_iid_null_bitwise_equal_to_ensure_null(self):
        for s in ("persist", "noise"):
            book = B.load_book(self.c, s, self.days)
            z = B.iid_null(book, K, seed_of=B.promo_seed(s))
            stored = {r["day"]: r for r in self.c.execute("SELECT * FROM null_draws WHERE strategy=?", (s,))}
            self.assertEqual(sorted(stored), book.days)
            for t, d in enumerate(book.days):
                self.assertTrue(np.array_equal(z[t], np.frombuffer(stored[d]["sums"], dtype="<f8")), (s, d))
                self.assertEqual(int(book.n[t]), stored[d]["n"])
                self.assertAlmostEqual(book.own[t], stored[d]["own_sum"], places=12)

    def test_window_matches_promotion_cum(self):
        _, ok = B.usable_days(self.c, "2026-03-31")
        today = (date.fromisoformat(self.days[-1]) + timedelta(days=3)).isoformat()
        cw = PR.complete_weeks(self.c, today)
        self.assertEqual(len(cw), 3)
        book = B.load_book(self.c, "persist", self.days)
        stat, null, weeks = B.window(book, book.own, B.iid_null(book, K, seed_of=B.promo_seed("persist")), cw)
        own_c, null_c = PR._cum(PR.load_weekly(self.c, "persist", cw, k=K))
        self.assertEqual(weeks, cw)
        self.assertAlmostEqual(stat, float(own_c[-1]), places=12)
        self.assertTrue(np.allclose(null, null_c[-1], rtol=0, atol=1e-12))

    def test_lim_blocked_signals_dropped(self):
        book = B.load_book(self.c, "persist", self.days)
        for t in range(len(book.days)):
            self.assertTrue(book.ok_buy[t][book.buy[t]].all())
            self.assertTrue(book.ok_sell[t][book.sell[t]].all())

    def test_ledger_file_untouched_by_script(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        out = Path(tempfile.mkdtemp()) / "o"
        self.assertEqual(B.main(["--ledger", str(self.path), "--out-dir", str(out), "--strategies", "persist,noise",
                                 "--k", str(K), "--holding"]), 0)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["ledger.db"])     # 没有 -journal / -wal
        self.assertTrue((out / "real_compare.md").exists())
        with self.assertRaises(FileExistsError):                       # 不覆盖已有结果
            B.main(["--ledger", str(self.path), "--out-dir", str(out), "--strategies", "persist", "--k", str(K)])


class Cutoff(unittest.TestCase):
    def test_refuse_end_after_cutoff_without_flag(self):
        path, _ = build_db(start="2026-10-05", weeks=2)
        out = Path(tempfile.mkdtemp()) / "o"
        self.assertEqual(B.main(["--ledger", str(path), "--out-dir", str(out), "--end", "2026-10-29"]), 2)
        self.assertFalse((out / "real_compare.json").exists())

    def test_default_cutoff_excludes_later_days(self):
        path, days = build_db(start="2026-03-02", weeks=6)
        c = B.open_ro(path)
        all_days, ok = B.usable_days(c, B.CUTOFF)
        self.assertTrue(ok and max(ok) <= B.CUTOFF)
        for d in ok:                                                    # 第 5 个后续记录日也不晚于截止日
            self.assertLessEqual(all_days[all_days.index(d) + 5], B.CUTOFF)
        self.assertEqual(max(ok), "2026-03-24")
        r = B.compare_real(c, "persist", B.CUTOFF, "2026-04-01", K, 1, False)
        self.assertTrue(all(w <= PR.week_key("2026-03-24") for w in r["weeks"]))
        self.assertNotIn(PR.week_key("2026-03-24"), r["weeks"])        # 这一周的 25~27 日不可用 → 不是完整周
        c.close()


class BlockNullMechanics(unittest.TestCase):
    def test_counts_matched_and_only_eligible_codes(self):
        ok = np.ones((5, 8), bool)
        ok[:, 7] = False
        ex = np.ones((5, 8))
        ex[:, 7] = 1e9                                                   # 抽到不可抽的股票就会暴露
        book = toy_book(T=5, U=8, n=3, ok=ok, ex=ex)
        z, _ = B.block_null(book, 50, [3], [1], np.random.default_rng(0))
        self.assertTrue(np.allclose(z, 3 - 3 * B.COST))                 # 每天正好 3 条、都来自可抽的股票

    def test_long_blocks_persist_and_unit_blocks_do_not(self):
        book = toy_book(T=6, U=500, n=4)
        _, cont = B.block_null(book, 100, [100], [1], np.random.default_rng(1))
        self.assertEqual(cont, 1.0)
        _, cont = B.block_null(book, 100, [1], [1], np.random.default_rng(1))
        self.assertEqual(cont, 0.0)

    def test_block_ends_when_stock_not_tradable(self):
        U = 10
        ok = np.ones((3, U), bool)
        ok[0, 1:] = False                                                # 第 1 天只能抽到 0 号
        ok[1, 0] = False                                                 # 第 2 天 0 号开盘涨停
        ex = np.zeros((3, U))
        ex[1, 0] = 1e9
        book = toy_book(T=3, U=U, n=1, ok=ok, ex=ex)
        z, cont = B.block_null(book, 50, [10], [1], np.random.default_rng(2))
        self.assertLess(np.abs(z[1]).max(), 1)
        self.assertGreater(cont, 0)                                      # 第 3 天接着第 2 天新开的块

    def test_more_alive_blocks_than_needed_are_trimmed(self):
        book = toy_book(T=2, U=300, n=5)
        book.buy[1] = np.arange(2)                                       # 第 2 天只要 2 条
        _, cont = B.block_null(book, 100, [10], [1], np.random.default_rng(3))
        self.assertEqual(cont, 1.0)

    def test_run_lengths_and_continuation(self):
        seq = [np.array([1, 2]), np.array([1]), np.array([1, 3]), np.array([], int)]
        self.assertEqual(sorted(B.run_lengths(seq).tolist()), [1, 1, 3])
        self.assertAlmostEqual(B.continuation_share(seq), 2 / 3)
        self.assertEqual(sorted(B.run_lengths([{"a", "b"}, {"b"}]).tolist()), [1, 2])


class SyntheticCalibration(unittest.TestCase):
    def test_no_persistence_both_nulls_agree(self):
        rows = B.calibrate([1.0], reps=3, k=300, seed=7, stocks=800, weeks=4, per_side=30, log=lambda *_: None)
        r = rows[0]
        self.assertLess(r["cont_real"], 0.1)
        self.assertLess(abs(r["block_sd"] / r["iid_sd"] - 1), 0.1)

    def test_persistence_widens_block_null(self):
        rows = B.calibrate([10.0], reps=3, k=300, seed=7, stocks=800, weeks=4, per_side=30, log=lambda *_: None)
        r = rows[0]
        self.assertGreater(r["cont_real"], 0.7)
        self.assertGreater(r["block_sd"] / r["iid_sd"], 1.5)

    def test_synth_dates_before_cutoff(self):
        book = B.synth_book(np.random.default_rng(0), stocks=100, weeks=12, per_side=5, mean_run=3)
        self.assertLessEqual(max(book.days), B.CUTOFF)
        self.assertEqual(len(book.days), 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
