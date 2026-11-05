"""D93：信号核对报告里 baseline / random_share 只算一次，报告内容与「每个策略各自重算」完全相同。
在同一份当前代码里比较：共用 tables 的 build_report vs 把 precision 的 tables 参数去掉（=改动前的重算方式）的 build_report。
不和 git 里的旧版本比，免得以后别的提交改了文字也让这个测试失败（revquant 建议）。python tests/test_ledger_hoist_d93.py"""
import contextlib
import json
import random
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import signal_ledger as L  # noqa: E402


@contextlib.contextmanager
def unhoisted():
    """让 build_report 里每次 precision 都自己重算三张表（改动前的行为）。"""
    real = L.precision
    L.precision = lambda c, strategy, today=None, *, tables=None: real(c, strategy, today)
    try:
        yield
    finally:
        L.precision = real


def synthetic_db(path):
    """几个策略、几十个交易日、随机收益的小账本。"""
    c = L.connect(path)
    rng = random.Random(93)
    days = [f"2026-09-{d:02d}" for d in range(1, 29)]
    codes = [f"{600000 + i}" for i in range(60)]
    for d in days:
        c.execute("INSERT OR IGNORE INTO days VALUES (?,?,?)", (d, 1, d))
        for code in codes:
            for h in L.HORIZONS:
                c.execute("INSERT OR IGNORE INTO moves VALUES (?,?,?,?,?,?,?)",
                          (d, code, h, rng.gauss(0, 0.03), 0.0, rng.choice([0, 0, 0, 1, -1]), 0))
        for st in ("blend2", "trend_ens", "rsi2"):
            for code in rng.sample(codes, 12):
                c.execute("INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (d, st, code, "x", rng.choice(L.ACTIONS), 0.0, 1.0, 10.0, "ok", 0, ""))
        for h in L.HORIZONS:
            c.execute("INSERT OR IGNORE INTO done VALUES (?,?,?)", (d, h, 1))
    c.commit()
    return c


def dump(report):
    return json.dumps(report, ensure_ascii=False, sort_keys=True, default=str)


def test_synthetic_identical_to_unhoisted():
    with tempfile.TemporaryDirectory() as t:
        c = synthetic_db(Path(t) / "l.db")
        new = dump(L.build_report(c, "2026-09-28"))
        # 不传 tables 时 precision 自己算，结果必须和共用一份时相同
        for st in ("blend2", "trend_ens", "rsi2"):
            a = L.precision(c, st, "2026-09-28")
            b = L.precision(c, st, "2026-09-28", tables=(L.baseline(c), L.random_share(c), L._cal_pos(c)))
            assert a == b, st
        with unhoisted():
            assert dump(L.build_report(c, "2026-09-28")) == new
        c.close()


def test_real_ledger_copy_identical():
    db = ROOT / "data" / "signal_ledger.db"
    if not db.exists():
        print("跳过：没有沙盒账本")
        return
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "copy.db"
        shutil.copy(db, p)                                   # 只读副本，真实账本不打开写
        c = L.connect(p)
        today = c.execute("SELECT MAX(day) d FROM days").fetchone()["d"]
        new = dump(L.build_report(c, today))
        with unhoisted():
            assert dump(L.build_report(c, today)) == new
        c.close()


if __name__ == "__main__":
    test_synthetic_identical_to_unhoisted()
    test_real_ledger_copy_identical()
    print("ok: 2 项通过")
