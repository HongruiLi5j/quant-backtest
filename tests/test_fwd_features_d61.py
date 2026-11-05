"""D61：R2/R5 前向检验的当天值审计表 fwd_features。临时账本 + 假行情，不读写 data/。
- 冻结日（2026-11-09）之前的信号不记；对照策略不记；已记过的不重算。
- ratio 与 R2_forward_prereg.md §3（r2_forward_eval.tag）同一定义；max20、abvol5_60、yizi 按手算核对。
- 写这张表不改动账本里任何已有的行（signals / moves / days / limit_close 哈希不变）。
运行：.venv/bin/python tests/test_fwd_features_d61.py"""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import limits as LM  # noqa: E402
from src import signal_ledger as L  # noqa: E402

CAL = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-08-03", "2026-11-13")]


class FakeProvider:
    def __init__(self, bars, vols):
        self._b, self._v = bars, vols

    def bars(self, code):
        return self._b.get(code, {})

    def volume(self, code):
        return self._v.get(code, {})


def table_hash(c, t):
    return hashlib.sha256(repr([tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]).encode()).hexdigest()


class FwdFeatures(unittest.TestCase):
    def setUp(self):
        self.c = L.connect(Path(tempfile.mkdtemp()) / "t.db")
        closes = [10.0 * (1.01 ** i) for i in range(len(CAL))]
        closes[CAL.index("2026-11-10")] = closes[CAL.index("2026-11-09")] * 1.06       # 信号日涨 6%
        self.bars = {"600001": {d: (c * 0.99, c * 1.01, c * 0.98, c) for d, c in zip(CAL, closes)}}
        self.vols = {"600001": {d: (2e6 if i >= len(CAL) - 8 else 1e6) for i, d in enumerate(CAL)}}
        for day, strat in (("2026-11-06", "blend2"), ("2026-11-10", "blend2"), ("2026-11-10", "ctrl_random_a")):
            self.c.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)", (day, strat, "600001", "测试", "买入", 0, 1, 10.0, None, 0, "[]"))
        self.c.execute("INSERT INTO days VALUES(?,?,?)", ("2026-11-10", 0, "x"))
        self.c.execute("INSERT INTO moves VALUES(?,?,?,?,?,?,?)", ("2026-11-06", "600001", 1, 0.01, 0.0, 0, 0))
        self.c.commit()

    def test_values_and_scope(self):
        before = {t: table_hash(self.c, t) for t in ("signals", "moves", "days", "limit_close")}
        with mock.patch.object(LM, "listed_days", lambda code, day=None, *a, **k: 500):
            n = L.ensure_fwd_features(self.c, FakeProvider(self.bars, self.vols))
            self.assertEqual(n, 1)                                                     # 冻结日之前、对照策略都不记
            self.assertEqual(L.ensure_fwd_features(self.c, FakeProvider(self.bars, self.vols)), 0)
        after = {t: table_hash(self.c, t) for t in before}
        self.assertEqual(before, after)                                                # 已有的行一行都没动
        r = self.c.execute("SELECT * FROM fwd_features").fetchone()
        self.assertEqual((r["day"], r["code"], r["name"]), ("2026-11-10", "600001", "测试"))
        self.assertAlmostEqual(r["ratio"], 0.06 / 0.10, places=9)                     # §3：当天涨幅 ÷ 涨跌停幅度
        self.assertAlmostEqual(r["max20"], 0.06, places=9)                             # 20 日内最大单日涨幅
        bars, vols = self.bars["600001"], self.vols["600001"]
        i = CAL.index("2026-11-10")
        amt = [bars[d][3] * vols[d] for d in CAL[i - 59:i + 1]]
        self.assertAlmostEqual(r["abvol5_60"], (sum(amt[-5:]) / 5) / (sum(amt) / 60), places=9)
        self.assertEqual(r["yizi"], 0)

    def test_matches_prereg_tag(self):
        """ratio 与 revquant 冻结的评估脚本 r2_forward_eval.tag 完全相同。"""
        import importlib.util
        p = Path(__file__).resolve().parent.parent / "overnight/research/r2_forward_eval.py"
        if not p.exists():
            self.skipTest("评估脚本不在")
        spec = importlib.util.spec_from_file_location("r2", p)
        R2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(R2)
        df = pd.DataFrame(self.bars["600001"], index=["Open", "High", "Low", "Close"]).T
        with mock.patch.object(LM, "listed_days", lambda code, day=None, *a, **k: 500):
            L.ensure_fwd_features(self.c, FakeProvider(self.bars, self.vols))
            want = R2.tag("600001", "测试", "2026-11-10", df, CAL)
        got = self.c.execute("SELECT ratio FROM fwd_features").fetchone()[0]
        self.assertAlmostEqual(got, want, places=12)


if __name__ == "__main__":
    unittest.main()
