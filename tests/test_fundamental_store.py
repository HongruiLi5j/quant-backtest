"""基本面存储测试(T014 S1):只用合成数据与临时目录,不联网,不读写真实 data/。"""
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src.fundamental import (FORBIDDEN_PATH_PREFIXES, FundamentalError, FundamentalStore, StorageError,
                             check_path_allowed, default_provider_fn, ingest_reports)
from src.fundamental import store as store_mod


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "fund.db"
        self.s = FundamentalStore(self.path)

    def up(self, code, rd, metric, value, source, ann=None, **kw):
        """测试用写入助手:公告日缺省为报告期后 30 天(合成数据)。"""
        if ann is None:
            ann = (date.fromisoformat(str(rd)) + timedelta(days=30)).isoformat()
        return self.s.upsert_report(code, rd, metric, value, source, announce_date=ann, **kw)

    def err(self, fn, *a, **k):
        with self.assertRaises(FundamentalError) as cm:
            fn(*a, **k)
        return cm.exception.code


class TestWriteRead(Base):
    def test_insert_and_query(self):
        self.up("600000", "2024-03-31", "roe", 0.05, "syn")
        self.up("600000", date(2024, 3, 31), "revenue", 1.5e9, "syn")
        self.assertEqual(self.s.get_metrics("600000", "2024-03-31"), {"roe": 0.05, "revenue": 1.5e9})
        rows = self.s.list_reports("600000")
        self.assertEqual([r["metric"] for r in rows], ["revenue", "roe"])
        self.assertEqual(rows[0]["fiscal_period"], "Q1")
        self.assertEqual(self.s.list_codes(), ["600000"])

    def test_fiscal_period_derived_and_checked(self):
        for md, fp in (("06-30", "H1"), ("09-30", "Q3"), ("12-31", "FY")):
            self.up("000001", "2023-" + md, "roe", 1, "syn")
            self.assertEqual(self.s.list_reports("000001", start="2023-" + md, end="2023-" + md)[0]["fiscal_period"], fp)
        self.assertEqual(self.err(self.s.upsert_report, "000001", "2023-03-31", "roe", 1, "syn", fiscal_period="FY"),
                         "bad_fiscal_period")

    def test_idempotent_and_restatement_keeps_history(self):
        for _ in range(3):
            self.up("600000", "2024-03-31", "roe", 0.05, "syn")
        self.assertEqual(self.s.count()["financial_reports"], 1)  # 重复写同一版本:幂等
        self.up("600000", "2024-03-31", "roe", 0.07, "syn", ann="2024-08-20")  # 重述:追加新版本,不覆盖
        self.assertEqual(self.s.count()["financial_reports"], 2)
        self.assertEqual(self.s.get_metrics("600000", "2024-03-31"), {"roe": 0.07})
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-05-01", "roe"), 0.05)

    def test_duplicate_in_batch_last_wins(self):
        n = self.s.upsert_reports([
            dict(code="600000", report_date="2024-03-31", metric="roe", value=1.0, source="syn", announce_date="2024-04-30"),
            dict(code="600000", report_date="2024-03-31", metric="roe", value=2.0, source="syn", announce_date="2024-04-30"),
        ])
        self.assertEqual(n, 2)
        self.assertEqual(self.s.get_metrics("600000", "2024-03-31"), {"roe": 2.0})

    def test_none_value_is_missing_not_zero(self):
        self.up("600000", "2024-03-31", "dividend", None, "syn")
        self.assertEqual(self.s.get_metrics("600000", "2024-03-31"), {"dividend": None})
        self.up("600000", "2024-03-31", "dividend", 0, "syn")
        self.assertEqual(self.s.get_metrics("600000", "2024-03-31"), {"dividend": 0.0})

    def test_missing_returns_empty(self):
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        self.assertEqual(self.s.get_metrics("600001", "2024-03-31"), {})
        self.assertEqual(self.s.get_metrics("600000", "2024-06-30"), {})
        self.assertEqual(self.s.list_reports("600001"), [])

    def test_two_sources_need_disambiguation(self):
        self.up("600000", "2024-03-31", "roe", 1, "a")
        self.up("600000", "2024-03-31", "roe", 2, "b")
        self.assertEqual(self.s.count()["financial_reports"], 2)
        self.assertEqual(self.err(self.s.get_metrics, "600000", "2024-03-31"), "ambiguous_source")
        self.assertEqual(self.s.get_metrics("600000", "2024-03-31", source="b"), {"roe": 2.0})

    def test_list_filters_and_order(self):
        for d in ("2023-12-31", "2024-03-31", "2024-06-30"):
            self.up("600000", d, "roe", 1, "syn")
            self.up("600000", d, "eps", 2, "syn")
        rows = self.s.list_reports("600000", start="2024-01-01", end="2024-03-31")
        self.assertEqual([r["report_date"] for r in rows], ["2024-03-31"] * 2)
        rows = self.s.list_reports("600000", metric="eps")
        self.assertEqual([r["report_date"] for r in rows], ["2023-12-31", "2024-03-31", "2024-06-30"])

    def test_stock_info(self):
        self.s.upsert_stock_info("600000", name="测试银行", industry="银行", listed_date="1999-11-10")
        info = self.s.get_stock_info("600000")
        self.assertEqual((info["name"], info["industry"], info["delisted_date"]), ("测试银行", "银行", None))
        self.s.upsert_stock_info("600000", name="新名", delisted_date=date(2025, 1, 2))
        info = self.s.get_stock_info("600000")
        self.assertEqual((info["name"], info["industry"], info["delisted_date"]), ("新名", None, "2025-01-02"))
        self.assertIsNone(self.s.get_stock_info("600001"))
        self.assertEqual(self.err(self.s.upsert_stock_info, "600000", listed_date="2020-01-01",
                                  delisted_date="2019-01-01"), "bad_date")

    def test_wipe(self):
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        self.s.upsert_stock_info("600000", name="x")
        self.assertEqual(self.err(self.s.wipe), "confirm_required")
        self.assertEqual(self.err(self.s.wipe, confirm="yes"), "confirm_required")
        self.assertEqual(self.s.count()["financial_reports"], 1)
        self.s.wipe(confirm=True)
        self.assertEqual(self.s.count(), {"financial_reports": 0, "stock_info": 0})
        self.up("600000", "2024-03-31", "roe", 1, "syn")  # 清空后仍可写
        self.assertEqual(self.s.count()["financial_reports"], 1)


class TestValidation(Base):
    def test_bad_code(self):
        for c in ("60000", "6000000", "abcdef", "６００００００", "６０００００","", None, 600000, "60000 "):
            self.assertEqual(self.err(self.s.upsert_report, c, "2024-03-31", "roe", 1, "syn"), "bad_code")
            self.assertEqual(self.err(self.s.get_metrics, c, "2024-03-31"), "bad_code")

    def test_bad_report_date(self):
        for d in ("2024-02-30", "2024-13-01", "2024-03-30", "20240331", "2024-3-31", None, 20240331,
                  "1980-12-31", date(2024, 4, 30)):
            self.assertEqual(self.err(self.s.upsert_report, "600000", d, "roe", 1, "syn"), "bad_report_date")

    def test_bad_value(self):
        for v in (float("nan"), float("inf"), -float("inf"), "1.5", True, [1]):
            self.assertEqual(self.err(self.s.upsert_report, "600000", "2024-03-31", "roe", v, "syn"), "bad_value")

    def test_bad_metric_and_source(self):
        for m in ("", "ROE", "a b", "a;drop", "1abc", None, "x" * 65):
            self.assertEqual(self.err(self.s.upsert_report, "600000", "2024-03-31", m, 1, "syn"), "bad_metric")
        for s in ("", "a b", "a'b", None, "x" * 33):
            self.assertEqual(self.err(self.s.upsert_report, "600000", "2024-03-31", "roe", 1, s), "bad_source")

    def test_batch_is_atomic(self):
        good = dict(code="600000", report_date="2024-03-31", metric="roe", value=1, source="syn",
                    announce_date="2024-04-30")
        bad = dict(code="bad", report_date="2024-03-31", metric="roe", value=1, source="syn")
        self.assertEqual(self.err(self.s.upsert_reports, [good, bad]), "bad_code")
        self.assertFalse(self.path.exists())  # 校验失败连库文件都不创建
        self.assertEqual(self.err(self.s.upsert_reports, [good, "oops"]), "bad_row")

    def test_sql_injection_is_inert(self):
        evil = "x'; DROP TABLE financial_reports; --"
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        self.assertEqual(self.err(self.s.upsert_report, "600000", "2024-03-31", evil, 1, "syn"), "bad_metric")
        self.assertEqual(self.err(self.s.upsert_stock_info, "600000", name="a\x00b"), "bad_text")
        self.s.upsert_stock_info("600000", name=evil)  # 合法文本进参数,按字面值存储
        self.assertEqual(self.s.get_stock_info("600000")["name"], evil)
        self.assertEqual(self.s.count()["financial_reports"], 1)

    def test_error_does_not_echo_input(self):
        secret = "SECRET-INPUT-123"
        try:
            self.up(secret, "2024-03-31", "roe", 1, "syn")
        except FundamentalError as e:
            self.assertNotIn(secret, str(e))
            self.assertNotIn(secret, repr(e))


class TestFileSafety(Base):
    def test_empty_db_reads_create_nothing(self):
        self.assertEqual(self.s.count(), {"financial_reports": 0, "stock_info": 0})
        self.assertEqual(self.s.get_metrics("600000", "2024-03-31"), {})
        self.assertEqual(self.s.list_reports("600000"), [])
        self.assertEqual(self.s.list_codes(), [])
        self.assertIsNone(self.s.get_stock_info("600000"))
        self.s.wipe(confirm=True)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_empty_but_existing_db(self):
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        self.s.wipe(confirm=True)
        self.assertEqual(self.s.list_reports("600000"), [])
        self.assertEqual(self.s.list_codes(), [])

    def test_created_with_0600(self):
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_loose_permission_tightened(self):
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        os.chmod(self.path, 0o666)
        self.up("600000", "2024-03-31", "roe", 2, "syn")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_symlink_rejected_everywhere(self):
        target = self.dir / "real.db"
        FundamentalStore(target).upsert_report("600000", "2024-03-31", "roe", 1, "syn", announce_date="2024-04-30")
        link = self.dir / "link.db"
        os.symlink(target, link)
        s = FundamentalStore(link)
        before = target.read_bytes()
        for fn in (lambda: s.upsert_report("600000", "2024-03-31", "roe", 9, "syn", announce_date="2024-04-30"),
                   lambda: s.get_metrics("600000", "2024-03-31"),
                   lambda: s.list_reports("600000"),
                   lambda: s.count(),
                   lambda: s.wipe(confirm=True),
                   lambda: s.upsert_stock_info("600000", name="x")):
            self.assertEqual(self.err(fn), "bad_path")
        self.assertEqual(target.read_bytes(), before)

    def test_dangling_symlink_not_followed(self):
        link = self.dir / "dangling.db"
        victim = self.dir / "victim.db"
        os.symlink(victim, link)
        self.assertEqual(self.err(FundamentalStore(link).upsert_report, "600000", "2024-03-31", "roe", 1, "syn",
                                  announce_date="2024-04-30"),
                         "bad_path")
        self.assertFalse(victim.exists())

    def test_directory_and_missing_parent_rejected(self):
        d = self.dir / "adir"
        d.mkdir()
        self.assertEqual(self.err(FundamentalStore(d).count), "bad_path")
        self.assertEqual(self.err(FundamentalStore(self.dir / "nope" / "f.db").upsert_report,
                                  "600000", "2024-03-31", "roe", 1, "syn", announce_date="2024-04-30"), "bad_path")
        self.assertFalse((self.dir / "nope").exists())

    def test_garbage_file_gives_db_error(self):
        self.path.write_bytes(b"this is not sqlite" * 100)
        os.chmod(self.path, 0o600)
        self.assertEqual(self.err(self.s.count), "db_error")
        self.assertEqual(self.err(self.up, "600000", "2024-03-31", "roe", 1, "syn"), "db_error")

    def test_default_path_is_data_fundamental_db(self):
        self.assertEqual(store_mod.DEFAULT_PATH, ROOT / "data" / "fundamental.db")
        self.assertEqual(FundamentalStore().path, store_mod.DEFAULT_PATH)


class TestPointInTime(Base):
    """财报版本控制:T 日只能看到 announce_date <= T 的报告(T014 S1 修复 #1)。"""

    def test_announce_date_cutoff(self):
        # 同一指标两个报告期:Q4 年报 4-30 公告,次一期 Q1 一季报 5-15 公告
        self.s.upsert_report("600000", "2023-12-31", "roe", 0.10, "syn", announce_date="2024-04-30")
        self.s.upsert_report("600000", "2024-03-31", "roe", 0.20, "syn", announce_date="2024-05-15")
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-05-01", "roe"), 0.10)  # 5-15 的那条还不可见
        self.assertEqual(self.s.get_report_asof("600000", "2024-05-01", "roe")["announce_date"], "2024-04-30")
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-05-14", "roe"), 0.10)
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-05-15", "roe"), 0.20)  # 公告当天含
        self.assertEqual(self.s.get_metrics_asof("600000", date(2024, 12, 31), "roe"), 0.20)
        self.assertIsNone(self.s.get_metrics_asof("600000", "2024-04-29", "roe"))  # 公告前什么都没有
        self.assertIsNone(self.s.get_report_asof("600000", "2024-04-29", "roe"))

    def test_history_preserved(self):
        self.s.upsert_report("600000", "2024-03-31", "roe", 10, "syn", announce_date="2024-04-30")
        self.s.upsert_report("600000", "2024-03-31", "roe", 12, "syn", announce_date="2024-04-30")  # 同日更正
        hist = self.s.get_history("600000", "2024-03-31", "roe")
        self.assertEqual([h["value"] for h in hist], [10.0, 12.0])
        self.assertLess(hist[0]["inserted_at"], hist[1]["inserted_at"])
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-06-01", "roe"), 12.0)  # 取最新一版
        self.assertEqual(self.s.get_metrics("600000", "2024-03-31"), {"roe": 12.0})
        self.assertEqual(self.s.count()["financial_reports"], 2)

    def test_restatement_visible_only_after_its_announce_date(self):
        self.s.upsert_report("600000", "2024-03-31", "roe", 10, "syn", announce_date="2024-04-30")
        self.s.upsert_report("600000", "2024-03-31", "roe", 12, "syn", announce_date="2024-08-20")  # 半年报时重述
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-08-19", "roe"), 10.0)  # 重述前看到旧版
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-08-20", "roe"), 12.0)
        self.assertEqual([h["announce_date"] for h in self.s.get_history("600000", "2024-03-31", "roe")],
                         ["2024-04-30", "2024-08-20"])

    def test_late_restatement_of_old_period_does_not_beat_newer_period(self):
        self.s.upsert_report("600000", "2023-12-31", "roe", 1, "syn", announce_date="2024-04-20")
        self.s.upsert_report("600000", "2024-03-31", "roe", 2, "syn", announce_date="2024-04-28")
        self.s.upsert_report("600000", "2023-12-31", "roe", 1.5, "syn", announce_date="2024-05-10")  # 旧期迟到重述
        self.assertEqual(self.s.get_report_asof("600000", "2024-06-01", "roe")["report_date"], "2024-03-31")
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-06-01", "roe"), 2.0)

    def test_reinsert_same_version_is_idempotent(self):
        written = [self.s.upsert_report("600000", "2024-03-31", "roe", 0.05, "syn", announce_date="2024-04-30")
                   for _ in range(3)]
        self.assertEqual(written, [1, 0, 0])
        self.assertEqual(self.s.count()["financial_reports"], 1)
        # 不同公告日 = 新版本(例如补抓到了更早的真实公告日)
        self.s.upsert_report("600000", "2024-03-31", "roe", 0.05, "syn", announce_date="2024-04-25")
        self.assertEqual(self.s.count()["financial_reports"], 2)
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-04-26", "roe"), 0.05)

    def test_weekend_announce(self):
        sat = date(2024, 5, 4)
        self.assertEqual(sat.weekday(), 5)
        self.s.upsert_report("600000", "2024-03-31", "roe", 0.3, "syn", announce_date=sat)
        self.assertIsNone(self.s.get_metrics_asof("600000", "2024-05-03", "roe"))  # 周五还看不到
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-05-04", "roe"), 0.3)  # 周六当天
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-05-05", "roe"), 0.3)  # 周日
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-05-06", "roe"), 0.3)
        # 查询日本身落在周末也照常
        self.assertEqual(self.s.get_metrics_asof("600000", "2024-05-04", "roe", source="syn"), 0.3)

    def test_missing_announce_date_raises(self):
        for kw in ({}, {"announce_date": None}):
            self.assertEqual(self.err(self.s.upsert_report, "600000", "2024-03-31", "roe", 1, "syn", **kw),
                             "missing_announce_date")
        self.assertEqual(self.err(self.s.upsert_reports, [dict(code="600000", report_date="2024-03-31",
                                                               metric="roe", value=1, source="syn")]),
                         "missing_announce_date")
        self.assertFalse(self.path.exists())  # 校验失败不落库、不建文件

    def test_bad_announce_date(self):
        for ad in ("2024-03-30", "2024-02-30", "20240430", 20240430, datetime(2024, 4, 30, 1, 0)):
            self.assertEqual(self.err(self.s.upsert_report, "600000", "2024-03-31", "roe", 1, "syn",
                                      announce_date=ad), "bad_announce_date")
        self.assertEqual(self.err(self.s.get_metrics_asof, "600000", "2024-4-30", "roe"), "bad_as_of_date")
        self.assertEqual(self.err(self.s.get_metrics_asof, "600000", "2024-04-30", "ROE"), "bad_metric")
        self.assertEqual(self.err(self.s.get_metrics_asof, "60000", "2024-04-30", "roe"), "bad_code")

    def test_legacy_schema_without_announce_date_is_flagged(self):
        """旧结构的库(没有公告日列)明确报错,不静默迁移、不被当成 point-in-time 数据。"""
        con = sqlite3.connect(self.path)
        con.execute("CREATE TABLE financial_reports (code TEXT, report_date TEXT, fiscal_period TEXT, "
                    "metric TEXT, value REAL, source TEXT, inserted_at TEXT)")
        con.execute("INSERT INTO financial_reports VALUES ('600000','2024-03-31','Q1','roe',0.1,'old','t')")
        con.commit()
        con.close()
        os.chmod(self.path, 0o600)
        self.assertEqual(self.err(self.s.get_metrics_asof, "600000", "2024-12-31", "roe"), "legacy_schema")
        self.assertEqual(self.err(self.s.count), "legacy_schema")
        self.assertEqual(self.err(self.s.upsert_report, "600000", "2024-06-30", "roe", 1, "syn",
                                  announce_date="2024-08-30"), "legacy_schema")
        con = sqlite3.connect(self.path)
        cols = [r[1] for r in con.execute("PRAGMA table_info(financial_reports)")]
        con.close()
        self.assertNotIn("announce_date", cols)  # 原库未被改动

    def test_schema_matches_spec(self):
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        con = sqlite3.connect(self.path)
        cols = [r[1] for r in con.execute("PRAGMA table_info(financial_reports)")]
        pk = [r[1] for r in sorted(con.execute("PRAGMA table_info(financial_reports)"), key=lambda r: r[5]) if r[5]]
        idx = {r[1]: [c[2] for c in con.execute("PRAGMA index_info(%s)" % r[1])]
               for r in con.execute("PRAGMA index_list(financial_reports)")}
        con.close()
        self.assertEqual(cols, ["code", "report_date", "fiscal_period", "metric", "value", "announce_date",
                                "source", "inserted_at"])
        self.assertEqual(pk, ["code", "report_date", "metric", "inserted_at"])
        self.assertEqual(idx["idx_asof"], ["code", "announce_date"])


class TestPathGuard(Base):
    """路径黑名单(T014 S1 修复 #2):守卫先于任何文件操作,被拒的路径不会被创建或打开。"""

    def _forbidden_paths(self):
        out = []
        for pre in FORBIDDEN_PATH_PREFIXES:
            out.append(ROOT / (pre + "x_guard_test.db") if pre.endswith("/") else ROOT / (pre + ".db"))
        out.append(ROOT / "data" / "daily" / "sub" / "x_guard_test.db")
        out.append(ROOT / "DATA" / "Daily" / "x_guard_test.db")  # macOS 大小写不敏感
        out.append(ROOT / "data" / "fundamental_x" / ".." / "daily" / "x_guard_test.db")  # ../ 绕行
        return out

    @staticmethod
    def _fingerprint(p):
        try:
            st = os.lstat(os.path.normpath(p))
        except FileNotFoundError:
            return None
        return (st.st_size, st.st_mtime_ns, st.st_mode)

    def test_forbidden_paths(self):
        paths = self._forbidden_paths()
        self.assertGreaterEqual(len(paths), len(FORBIDDEN_PATH_PREFIXES) + 3)
        for p in paths:
            before = self._fingerprint(p)  # 真实文件可能存在(如 data/signal_ledger.db):只 lstat 元数据,不读内容
            with self.assertRaises(StorageError, msg=str(p)) as cm:
                check_path_allowed(p)
            self.assertEqual(cm.exception.code, "forbidden_path")
            s = FundamentalStore(p)
            for fn in (lambda: s.upsert_report("600000", "2024-03-31", "roe", 1, "syn", announce_date="2024-04-30"),
                       lambda: s.upsert_stock_info("600000", name="x"),
                       lambda: s.get_metrics_asof("600000", "2024-12-31", "roe"),
                       lambda: s.count(),
                       lambda: s.wipe(confirm=True)):
                self.assertEqual(self.err(fn), "forbidden_path")
            self.assertEqual(self._fingerprint(p), before, p)  # 被拒后:不存在的仍不存在,存在的原封不动

    def test_error_is_fundamental_error_without_path(self):
        with self.assertRaises(FundamentalError) as cm:
            FundamentalStore(ROOT / "data" / "daily" / "x_guard_test.db").count()
        self.assertNotIn("x_guard_test", str(cm.exception))

    def test_symlinked_parent_into_protected_dir_rejected(self):
        protected = ROOT / "data" / "daily"
        if not protected.is_dir():
            self.skipTest("本机没有 data/daily 目录")
        link = self.dir / "sneaky"
        os.symlink(protected, link)
        target = link / "x_guard_test.db"
        self.assertEqual(self.err(FundamentalStore(target).count), "forbidden_path")
        self.assertFalse(os.path.lexists(protected / "x_guard_test.db"))

    def test_allowed_paths(self):
        check_path_allowed(ROOT / "data" / "fundamental.db")  # 默认路径放行(只做路径检查,不创建)
        check_path_allowed(store_mod.DEFAULT_PATH)
        check_path_allowed(self.path)  # 临时目录放行
        check_path_allowed("/tmp/fund_guard_test.db")
        check_path_allowed(ROOT / "data" / "fundamental_other.db")  # 前缀必须精确匹配
        self.up("600000", "2024-03-31", "roe", 1, "syn")  # 临时目录里真的能写
        self.assertTrue(self.path.exists())


class TestPermissionTightened(Base):
    """修复 #3:打开写连接时无条件 chmod 0600(包括 0700 这种旧代码不会收紧的权限)。"""

    def test_permission_tightened(self):
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        for mode in (0o700, 0o644, 0o666, 0o400 | 0o200, 0o640):
            os.chmod(self.path, mode)
            self.up("600000", "2024-03-31", "roe", mode, "syn")
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600, oct(mode))

    def test_preexisting_empty_0700_file(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        os.close(fd)
        os.chmod(self.path, 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o700)
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(self.s.get_metrics("600000", "2024-03-31"), {"roe": 1.0})

    def test_wipe_and_stock_info_also_tighten(self):
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        os.chmod(self.path, 0o700)
        self.s.upsert_stock_info("600000", name="x")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        os.chmod(self.path, 0o755)
        self.s.wipe(confirm=True)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)


class _FailingCon:
    """sqlite 连接代理:事务内第 fail_at 条 SELECT value / INSERT / DELETE 语句抛 OperationalError。"""

    def __init__(self, real, fail_at):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_fail_at", fail_at)
        object.__setattr__(self, "_n", 0)

    def execute(self, sql, *a):
        if sql.lstrip().upper().startswith(("SELECT VALUE", "INSERT", "DELETE")):
            object.__setattr__(self, "_n", self._n + 1)
            if self._n == self._fail_at:
                raise sqlite3.OperationalError("injected failure")
        return self._real.execute(sql, *a)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        setattr(self._real, name, value)


class TestAtomicRollback(Base):
    """修复 #4:SQL 执行到一半抛异常,整个事务回滚,表状态与开始时完全相同。"""

    def _dump(self):
        con = sqlite3.connect(self.path)
        try:
            return (con.execute("SELECT * FROM financial_reports ORDER BY inserted_at").fetchall(),
                    con.execute("SELECT * FROM stock_info ORDER BY code").fetchall())
        finally:
            con.close()

    def _patched(self, fail_at):
        real_connect = sqlite3.connect
        return mock.patch.object(store_mod.sqlite3, "connect",
                                 side_effect=lambda *a, **k: _FailingCon(real_connect(*a, **k), fail_at))

    def test_batch_rolls_back_at_every_statement(self):
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        self.s.upsert_stock_info("600000", name="x")
        before = self._dump()
        batch = [dict(code="600001", report_date="2024-03-31", metric=m, value=i, source="syn",
                      announce_date="2024-04-30") for i, m in enumerate(("roe", "eps", "bps"))]
        # 3 行 × (SELECT + INSERT) = 6 条语句;在每一条上注入失败,包括"前一行已 INSERT 之后"
        for k in range(1, 7):
            with self._patched(k):
                self.assertEqual(self.err(self.s.upsert_reports, batch), "db_error", k)
            self.assertEqual(self._dump(), before, "fail_at=%d 后表状态变了" % k)
        self.assertEqual(self.s.upsert_reports(batch), 3)  # 失败没有留下残渣,正常批次照常写入
        self.assertEqual(self.s.count()["financial_reports"], 4)

    def test_wipe_rolls_back_when_second_delete_fails(self):
        self.up("600000", "2024-03-31", "roe", 1, "syn")
        self.s.upsert_stock_info("600000", name="x")
        before = self._dump()
        with self._patched(2):  # 第 1 条 DELETE financial_reports 已执行,第 2 条 DELETE stock_info 失败
            self.assertEqual(self.err(self.s.wipe, confirm=True), "db_error")
        self.assertEqual(self._dump(), before)
        self.assertEqual(self.s.count(), {"financial_reports": 1, "stock_info": 1})

    def test_stock_info_rolls_back(self):
        self.s.upsert_stock_info("600000", name="old")
        before = self._dump()
        with self._patched(1):
            self.assertEqual(self.err(self.s.upsert_stock_info, "600000", name="new"), "db_error")
        self.assertEqual(self._dump(), before)
        self.assertEqual(self.s.get_stock_info("600000")["name"], "old")


class TestProvider(Base):
    def test_default_provider_raises(self):
        with self.assertRaises(NotImplementedError):
            default_provider_fn("600000", date(2024, 3, 31))
        with self.assertRaises(NotImplementedError):
            ingest_reports(self.s, ["600000"], [date(2024, 3, 31)])
        self.assertFalse(self.path.exists())

    def test_injected_provider(self):
        calls = []

        def fake(code, rd):
            calls.append((code, rd))
            return [dict(metric="roe", value=0.1, source="fake", announce_date=(rd + timedelta(days=45)).isoformat()),
                    dict(metric="eps", value=None, source="fake", announce_date=(rd + timedelta(days=45)).isoformat())]

        n = ingest_reports(self.s, ["600000", "000001"], ["2024-06-30", date(2024, 9, 30)], provider_fn=fake)
        self.assertEqual(n, 8)
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.s.get_metrics("000001", "2024-09-30"), {"roe": 0.1, "eps": None})
        # 重跑幂等
        ingest_reports(self.s, ["600000", "000001"], ["2024-06-30", date(2024, 9, 30)], provider_fn=fake)
        self.assertEqual(self.s.count()["financial_reports"], 8)

    def test_provider_empty_and_bad_rows(self):
        self.assertEqual(ingest_reports(self.s, ["600000"], ["2024-03-31"], provider_fn=lambda c, d: None), 0)
        bad = lambda c, d: [dict(metric="roe", value=float("nan"), source="fake", announce_date="2024-04-30")]
        self.assertEqual(self.err(ingest_reports, self.s, ["600000"], ["2024-03-31"], provider_fn=bad), "bad_value")
        self.assertEqual(self.err(ingest_reports, self.s, ["abc"], ["2024-03-31"], provider_fn=bad), "bad_code")


class TestImportSideEffects(unittest.TestCase):
    def test_import_and_construct_have_no_side_effects(self):
        """子进程里把 socket 打坏、cwd 设为空临时目录,导入并构造后不得联网、不得创建任何文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            code = (
                "import socket, sys\n"
                "def boom(*a, **k): raise RuntimeError('network used')\n"
                "socket.socket = boom\n"
                "socket.create_connection = boom\n"
                "sys.path.insert(0, %r)\n"
                "import src.fundamental as f\n"
                "s = f.FundamentalStore(%r)\n"
                "s.count(); s.list_reports('600000'); s.get_metrics('600000', '2024-03-31')\n"
                % (str(ROOT), os.path.join(tmp, "x.db"))
            )
            r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                               cwd=tmp, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout + r.stderr, "")
            self.assertEqual(os.listdir(tmp), [])

    def test_module_has_no_network_imports(self):
        src = (ROOT / "src" / "fundamental" / "store.py").read_text(encoding="utf-8")
        for word in ("import requests", "import socket", "urllib", "http.client", "import akshare",
                     "import baostock", "import tushare", "print("):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
