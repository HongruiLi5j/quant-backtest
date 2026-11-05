"""D101：账本 signals 表加 (strategy, action) 索引。只加索引不改数据；有没有索引，报告、榜单、晋升结果逐字节相同。
用临时目录里的合成账本，不碰 data/。python tests/test_ledger_index_d101.py"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import promotion as PR  # noqa: E402
from src import signal_ledger as L  # noqa: E402
from tests.test_ledger_hoist_d93 import synthetic_db  # noqa: E402

TODAY = "2026-09-28"


def _outputs(c):
    dump = lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True, default=str)  # noqa: E731
    return (dump(L.build_report(c, TODAY)), dump(L.strategy_board(c)),
            dump(PR.jsonable(PR.promotion_status(c, TODAY))))


def _index_names(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='signals'")}


def test_connect_creates_index_without_touching_data():
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "l.db"
        synthetic_db(p).close()
        c = L.connect(p)
        assert "signals_sa" in _index_names(c)
        plan = " ".join(r[3] for r in c.execute(
            "EXPLAIN QUERY PLAN SELECT s.day FROM signals s JOIN moves m ON m.day=s.day AND m.code=s.code AND m.h=? "
            "WHERE s.strategy=? AND s.action=?", (5, "blend2", "买入")))
        assert "signals_sa" in plan, plan                                    # precision 的查询真的用上了
        rows = c.execute("SELECT * FROM signals ORDER BY day, strategy, code").fetchall()
        c.close()
        c = L.connect(p)                                                     # 再连一次：已存在就跳过，不报错
        assert c.execute("SELECT * FROM signals ORDER BY day, strategy, code").fetchall() == rows


def test_outputs_identical_with_and_without_index():
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "l.db"
        synthetic_db(p).close()
        c = L.connect(p)
        with_idx = _outputs(c)
        c.execute("DROP INDEX signals_sa")                                    # 只在临时副本上去掉，模拟改动前
        assert "signals_sa" not in _index_names(c)
        assert _outputs(c) == with_idx


def test_old_db_gets_index_on_connect():
    """真站账本是改动前建的：没有索引。connect 时补上，数据行数不变。"""
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "old.db"
        synthetic_db(p).close()
        raw = sqlite3.connect(p)
        raw.execute("DROP INDEX signals_sa")
        n = raw.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        raw.commit()
        raw.close()
        c = L.connect(p)
        assert "signals_sa" in _index_names(c)
        assert c.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == n


if __name__ == "__main__":
    test_connect_creates_index_without_touching_data()
    test_outputs_identical_with_and_without_index()
    test_old_db_gets_index_on_connect()
    print("ok: 3 项通过")
