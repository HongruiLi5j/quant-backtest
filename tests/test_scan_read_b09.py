"""B09：全市场扫描读 parquet 的提速，要求结果和改前逐字节一致。
运行：.venv/bin/python tests/test_scan_read_b09.py

做法：
  1. 在临时目录造 20 只合成股票的 parquet（不读写 data/ 下任何真实文件，不联网），
     其中 5 只故意带一根"盘中半天 K 线"（日期晚于文件保存时的已收盘日）。
  2. 目录复制成两份：一份跑改前的 strip_intraday（本文件里原样保留，当参照），一份跑现在 src 里的版本。
  3. 对两份目录里的 20 只股票各跑 scan 同款回测（7 个策略的目标仓位 + run_weights），
     净值序列（equity）、日收益、持仓必须完全相等（不是近似），parquet 文件字节也必须相等。
  4. 没有半天 K 线的文件不应该被整份读出来（只读索引），用计数器验证。
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import MARKETS  # noqa: E402
from src import allmarket, costs, data, registry  # noqa: E402
from src.portfolio import run_weights  # noqa: E402

STRATS = ["blend2", "trend_ens", "trend_vol", "donchian_s1", "rsi2", "ma_adx_original", "ma_adx_plus"]
N_STOCKS = 20
BAD = {2, 5, 9, 14, 18}                       # 这几只最后一根是盘中半天 K 线
BJ = ZoneInfo("Asia/Shanghai")
SAVED_AT = datetime(2026, 3, 10, 10, 0, tzinfo=BJ)   # 文件在 3/10 10:00（15:30 前）保存 → 3/10 的 K 线是半天数据


def old_strip_intraday() -> int:
    """改前的 strip_intraday（原样保留，只当参照）：每个文件都整份读出来。"""
    bj = ZoneInfo("Asia/Shanghai")
    fixed = 0
    for code in allmarket.cached_codes():
        p = allmarket.path(code)
        saved = datetime.fromtimestamp(p.stat().st_mtime, bj)
        df = allmarket.load(code)
        if df.empty:
            continue
        ready = pd.Timestamp(saved.date()) if saved.hour * 60 + saved.minute >= 15 * 60 + 30 \
            else pd.Timestamp(saved.date()) - pd.Timedelta(days=1)
        bad = df.index > ready
        if bad.any():
            allmarket.save(code, df[~bad])
            fixed += 1
    return fixed


def make_stock(i: int) -> pd.DataFrame:
    """第 i 只合成股票；BAD 里的最后一根日期是 2026-03-10，其余最后一根是 2026-03-09。"""
    last = pd.Timestamp("2026-03-10") if i in BAD else pd.Timestamp("2026-03-09")
    n = 900 + 7 * i
    start = pd.bdate_range(end=last, periods=n)[0]
    df = data.synthetic(n=n, seed=100 + i, start=start.strftime("%Y-%m-%d"))
    df.index.name = None
    return df


def fill_dir(d: Path):
    """用 allmarket.save 写 20 个文件，再把修改时间统一设成 SAVED_AT。"""
    for i in range(N_STOCKS):
        allmarket.save(f"{600000 + i}", make_stock(i))
    ts = SAVED_AT.timestamp()
    for p in d.glob("*.parquet"):
        os.utime(p, (ts, ts))


def backtest_all() -> dict:
    """和 scan.scan_one 同款流程：读文件 → 7 个策略 → run_weights。返回 {(代码, 策略): PortResult}。"""
    out = {}
    for code in allmarket.cached_codes():
        df = allmarket._drop_unfinished(allmarket.load(code))
        prices = {code: df}
        lim = {code: 0.10}
        cash = pd.Series(0.0, index=df.index)
        for s in STRATS:
            W = registry.target_weights(s, prices, code, registry.clean_params(s, {}), MARKETS["cn"])
            out[(code, s)] = run_weights(prices, W, cost=costs.CN_STOCK_ONE_WAY, band=0.10, cash_ret=cash, limits=lim)
    return out


class TestStripIntradayEquivalent(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="b09_"))
        self.old_all = allmarket.ALL
        # 先在 A 目录造数据，再原样复制成 B（copy2 保留修改时间）
        self.dir_a, self.dir_b = self.tmp / "a", self.tmp / "b"
        allmarket.ALL = self.dir_a
        fill_dir(self.dir_a)
        shutil.copytree(self.dir_a, self.dir_b, copy_function=shutil.copy2)

    def tearDown(self):
        allmarket.ALL = self.old_all
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_same_files_and_same_equity(self):
        allmarket.ALL = self.dir_a
        fixed_old = old_strip_intraday()
        res_old = backtest_all()

        allmarket.ALL = self.dir_b
        fixed_new = allmarket.strip_intraday()
        res_new = backtest_all()

        self.assertEqual(fixed_old, len(BAD))
        self.assertEqual(fixed_new, fixed_old)
        # 1) 文件字节相等
        names = sorted(p.name for p in self.dir_a.glob("*.parquet"))
        self.assertEqual(names, sorted(p.name for p in self.dir_b.glob("*.parquet")))
        self.assertEqual(len(names), N_STOCKS)
        for n in names:
            self.assertEqual((self.dir_a / n).read_bytes(), (self.dir_b / n).read_bytes(), n)
        # 2) 20 只 × 7 个策略的净值/收益/持仓完全相等
        self.assertEqual(res_old.keys(), res_new.keys())
        self.assertEqual(len(res_old), N_STOCKS * len(STRATS))
        for k in res_old:
            a, b = res_old[k], res_new[k]
            pd.testing.assert_series_equal(a.equity, b.equity, check_exact=True)
            pd.testing.assert_series_equal(a.returns, b.returns, check_exact=True)
            pd.testing.assert_frame_equal(a.weights, b.weights, check_exact=True)
            self.assertEqual(a.equity.to_numpy().tobytes(), b.equity.to_numpy().tobytes(), k)
        # 半天 K 线确实被删掉了（防止这个测试变成两边都没做事）
        for i in BAD:
            self.assertEqual(allmarket.load(f"{600000 + i}").index[-1], pd.Timestamp("2026-03-09"))

    def test_only_index_read_when_clean(self):
        """没有半天 K 线的文件不应该被 allmarket.load 整份读出来。"""
        allmarket.ALL = self.dir_b
        calls = []
        real_load = allmarket.load
        allmarket.load = lambda code, delisted=False: (calls.append(code), real_load(code, delisted))[1]
        try:
            fixed = allmarket.strip_intraday()
        finally:
            allmarket.load = real_load
        self.assertEqual(fixed, len(BAD))
        self.assertEqual(sorted(calls), sorted(f"{600000 + i}" for i in BAD))

    def test_second_run_is_noop(self):
        """修复过一次后，再跑一遍应该什么都不改（幂等），文件字节不变。"""
        allmarket.ALL = self.dir_b
        allmarket.strip_intraday()
        before = {p.name: p.read_bytes() for p in self.dir_b.glob("*.parquet")}
        self.assertEqual(allmarket.strip_intraday(), 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.dir_b.glob("*.parquet")})

    def test_empty_file_skipped(self):
        """0 行的 parquet 不报错、不计入修复数（和改前的 df.empty → continue 一致）。"""
        allmarket.ALL = self.dir_b
        empty = pd.DataFrame({c: pd.Series(dtype="float64") for c in allmarket.COLS},
                             index=pd.DatetimeIndex([], name=None))
        empty.to_parquet(self.dir_b / "600999.parquet")
        self.assertEqual(allmarket.strip_intraday(), len(BAD))


if __name__ == "__main__":
    unittest.main(verbosity=2)
