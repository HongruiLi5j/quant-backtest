"""BaoStock 基本面接入测试(T014 S5):全部离线——注入假的 baostock 模块,临时目录,不碰 data/。"""
import contextlib
import io
import math
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from src.fundamental import FundamentalStore, ingest_reports  # noqa: E402
from src.fundamental import fetch_baostock as fb  # noqa: E402
import fetch_fundamental as cli  # noqa: E402

BJT = timezone(timedelta(hours=8))
NOW = datetime(2024, 9, 30, 18, 0, tzinfo=BJT)   # 测试里的"现在"(已过 15:30)
CODE = "600001"


# ---------- 假 baostock ----------

class _RS:
    def __init__(self, fields, rows, error_code="0", error_msg=""):
        self.fields, self._rows, self.error_code, self.error_msg = fields, list(rows), error_code, error_msg
        self._i = -1

    def next(self):
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self):
        return self._rows[self._i]


class _Login:
    error_code, error_msg = "0", ""


class FakeBS:
    """按 (代码, 年, 季) 返回预设数据的假 baostock 模块。fail_next=N 让接下来 N 次请求报错。"""
    PROFIT = ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM",
              "MBRevenue", "totalShare", "liqaShare"]
    BALANCE = ["code", "pubDate", "statDate", "currentRatio", "quickRatio", "cashRatio", "YOYLiability",
               "liabilityToAsset", "assetToEquity"]
    CASH = ["code", "pubDate", "statDate", "CAToAsset", "NCAToAsset", "tangibleAssetToAsset", "ebitToInterest",
            "CFOToOR", "CFOToNP", "CFOToGr"]
    DIV = ["code", "dividPreNoticeDate", "dividAgmPumDate", "dividPlanAnnounceDate", "dividPlanDate",
           "dividRegistDate", "dividOperateDate", "dividPayDate", "dividStockMarketDate", "dividCashPsBeforeTax",
           "dividCashPsAfterTax", "dividStocksPs", "dividCashStock", "dividReserveToStockPs"]

    def __init__(self):
        self.reports = {}     # (bs_code, year, quarter) -> dict(pub=..., np=..., npm=..., gpm=..., share=..., cfo_np=..., la=...)
        self.dividends = {}   # (bs_code, year) -> [(ex_date, cash)]
        self.closes = {}      # (bs_code, date_str) -> close
        self.calls = []
        self.fail_next = 0
        self.logins = 0

    def login(self):
        self.logins += 1
        return _Login()

    def logout(self):
        return _Login()

    def _maybe_fail(self, name):
        self.calls.append(name)
        if self.fail_next > 0:
            self.fail_next -= 1
            return _RS([], [], error_code="10002007", error_msg="network error")
        return None

    def _report(self, name, fields, code, year, quarter, build):
        bad = self._maybe_fail(name)
        if bad:
            return bad
        rep = self.reports.get((code, year, quarter))
        if rep is None:
            return _RS(fields, [])
        stat = fb.quarter_end(year, quarter).isoformat()
        row = dict.fromkeys(fields, "")
        row.update(code=code, pubDate=rep.get("pub", ""), statDate=rep.get("stat", stat))
        row.update({k: ("" if v is None else str(v)) for k, v in build(rep).items()})
        return _RS(fields, [[row[f] for f in fields]])

    def query_profit_data(self, code, year, quarter):
        return self._report("profit", self.PROFIT, code, year, quarter,
                            lambda r: dict(netProfit=r.get("np"), npMargin=r.get("npm"), gpMargin=r.get("gpm"),
                                           totalShare=r.get("share")))

    def query_balance_data(self, code, year, quarter):
        return self._report("balance", self.BALANCE, code, year, quarter,
                            lambda r: dict(liabilityToAsset=r.get("la"), assetToEquity=r.get("ae")))

    def query_cash_flow_data(self, code, year, quarter):
        return self._report("cash", self.CASH, code, year, quarter, lambda r: dict(CFOToNP=r.get("cfo_np")))

    def query_dividend_data(self, code, year, yearType):
        bad = self._maybe_fail("div")
        if bad:
            return bad
        rows = []
        for ex, cash in self.dividends.get((code, int(year)), []):
            row = dict.fromkeys(self.DIV, "")
            row.update(code=code, dividOperateDate=ex, dividCashPsBeforeTax=cash)
            rows.append([row[f] for f in self.DIV])
        return _RS(self.DIV, rows)

    def query_history_k_data_plus(self, code, fields, start_date, end_date, frequency, adjustflag):
        bad = self._maybe_fail("k")
        if bad:
            return bad
        assert adjustflag == "3", "必须取未复权价"
        rows = [[d, str(c), "1"] for (cd, d), c in sorted(self.closes.items())
                if cd == code and start_date <= d <= end_date]
        return _RS(["date", "close", "tradestatus"], rows)


def make_client(fake, today=date(2024, 9, 30)):
    """真的 BaostockClient + 假模块 + 不真睡。"""
    return fb.BaostockClient(fake, sleep=lambda s: None, today=lambda: today)


def standard_fake():
    """一只股票的合成累计数据(净利润单位:元)。

    报告期     公告日       净利润累计  净利率  毛利率  CFO/NP
    2022H1   2022-08-20      40      0.1    0.3    1.0
    2022FY   2023-03-25     100      0.1    0.3    1.0
    2023Q1   2023-04-28      25      0.1    0.3    1.0
    2023H1   2023-08-20      50      0.1    0.3    1.0
    2023FY   2024-03-25     120      0.1    0.3    1.0
    2024Q1   2024-04-25      30      0.1    0.3    1.0
    2024H1   2024-08-20      70      0.1    0.3    1.2
    """
    fake = FakeBS()
    bc = fb.bs_code(CODE)
    data = [((2022, 2), "2022-08-20", 40), ((2022, 4), "2023-03-25", 100), ((2023, 1), "2023-04-28", 25),
            ((2023, 2), "2023-08-20", 50), ((2023, 4), "2024-03-25", 120), ((2024, 1), "2024-04-25", 30),
            ((2024, 2), "2024-08-20", 70)]
    for (y, q), pub, npv in data:
        fake.reports[(bc, y, q)] = dict(pub=pub, np=npv, npm=0.1, gpm=0.3, share=1000, la=0.4, ae=2.0,
                                        cfo_np=1.2 if (y, q) == (2024, 2) else 1.0)
    fake.dividends[(bc, 2023)] = [("2023-09-28", 0.5)]
    fake.dividends[(bc, 2024)] = [("2024-07-10", 0.3), ("2024-07-10", 0.1)]   # 同一除权日两条 -> 合并 0.4
    fake.closes[(bc, "2024-09-27")] = 10.0
    return fake


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "fund.db"
        self.store = FundamentalStore(self.db)
        self.daily = self.dir / "cn_all"
        self.daily.mkdir()
        self.codes = self.dir / "codes.csv"
        pd.DataFrame({"code": [CODE], "name": ["测试股"], "industry": ["C01测试业"]}).to_csv(self.codes, index=False)
        days = pd.to_datetime(["2024-09-20", "2024-09-23", "2024-09-24", "2024-09-25", "2024-09-26", "2024-09-27"])
        pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": [80.0, 81, 82, 83, 84, 85], "Volume": 1.0},
                     index=days).to_parquet(self.daily / ("%s.parquet" % CODE))

    def run_cli(self, fake, asof="2024-09-27", extra=(), start="2022-01-01"):
        argv = ["--codes-file", str(self.codes), "--db", str(self.db), "--start", start, "--asof", asof,
                "--export-fundamental", str(self.dir / "out" / "fund.csv"),
                "--export-prices", str(self.dir / "out" / "prices.csv")] + list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv, client=make_client(fake), now=NOW, daily_dir=self.daily)
        return rc, out.getvalue(), err.getvalue()

    def fund_csv(self):
        return pd.read_csv(self.dir / "out" / "fund.csv", dtype={"code": str})


# ---------- 1. pubDate 缺失必须丢弃 ----------

class TestPubDate(Base):
    def test_missing_pubdate_dropped_and_counted(self):
        fake = standard_fake()
        fake.reports[(fb.bs_code(CODE), 2024, 2)]["pub"] = ""
        client = make_client(fake)
        recs = client.fetch_quarterly(CODE, 2024, 2)
        self.assertEqual(recs, [])
        self.assertEqual(client.stats["dropped_no_pubdate"], 3)   # 三个接口各一行
        recs = client.fetch_quarterly(CODE, 2024, 1)
        self.assertTrue(recs)
        for r in recs:   # 公告日 = pubDate,报告期 = statDate,绝不互相冒充
            self.assertEqual(r["announce_date"], "2024-04-25")
            self.assertEqual(r["report_date"], "2024-03-31")
            self.assertEqual(r["source"], "baostock")

    def test_bad_pubdate_and_stat_mismatch_dropped(self):
        fake = standard_fake()
        bc = fb.bs_code(CODE)
        fake.reports[(bc, 2024, 1)]["pub"] = "2024-03-01"       # 早于报告期末
        fake.reports[(bc, 2023, 4)]["pub"] = "2099-01-01"       # 晚于今天
        fake.reports[(bc, 2023, 2)]["stat"] = "2023-03-31"      # 报告期与请求不符
        client = make_client(fake)
        self.assertEqual(client.fetch_quarterly(CODE, 2024, 1), [])
        self.assertEqual(client.fetch_quarterly(CODE, 2023, 4), [])
        self.assertEqual(client.fetch_quarterly(CODE, 2023, 2), [])
        self.assertEqual(client.stats["dropped_bad_pubdate"], 6)
        self.assertEqual(client.stats["dropped_stat_mismatch"], 3)

    def test_missing_pubdate_never_reaches_store(self):
        fake = standard_fake()
        fake.reports[(fb.bs_code(CODE), 2024, 2)]["pub"] = ""
        rc, out, _ = self.run_cli(fake)
        self.assertEqual(rc, 0)
        self.assertEqual(self.store.list_reports(CODE, start="2024-06-30", end="2024-06-30", metric="net_profit"), [])
        self.assertIn("缺公告日 3", out)
        row = self.fund_csv().iloc[0]
        self.assertEqual(row["report_date"], "2024-03-31")   # 退回上一期,不拿报告期当公告日

    def test_dividend_without_exdate_dropped(self):
        fake = standard_fake()
        fake.dividends[(fb.bs_code(CODE), 2024)].append(("", 9.9))
        client = make_client(fake)
        recs = client.fetch_dividends(CODE, 2024)
        cash = [r for r in recs if r["metric"] == fb.DIVIDEND_METRIC]
        self.assertEqual(len(cash), 1)
        self.assertAlmostEqual(cash[0]["value"], 0.4)
        self.assertEqual(cash[0]["announce_date"], "2024-07-10")
        self.assertEqual(cash[0]["report_date"], "2024-06-30")
        self.assertEqual(client.stats["dropped_no_exdate"], 1)


# ---------- 2. TTM 差分 ----------

class TestTTM(Base):
    def test_ttm_differencing(self):
        rc, _, err = self.run_cli(standard_fake())
        self.assertEqual(rc, 0, err)
        r = self.fund_csv().iloc[0]
        self.assertEqual(r["report_date"], "2024-06-30")
        self.assertAlmostEqual(r["net_profit_ttm"], 70 + 120 - 50)         # 140
        self.assertAlmostEqual(r["net_profit_ttm_prev"], 50 + 100 - 40)    # 110
        self.assertAlmostEqual(r["revenue_ttm"], (70 + 120 - 50) / 0.1)    # 营收 = 净利润 / 净利率
        self.assertAlmostEqual(r["revenue_ttm_prev"], 110 / 0.1)
        self.assertAlmostEqual(r["operating_cost_ttm"], 1400 * 0.7)        # 营收 × (1 − 毛利率)
        self.assertAlmostEqual(r["cfo_ttm"], 70 * 1.2 + 120 - 50)          # 各期 CFO 累计再差分
        self.assertAlmostEqual(r["close_raw"], 10.0)                       # 未复权价,不是本地后复权的 85
        self.assertAlmostEqual(r["market_cap"], 10.0 * 1000)
        self.assertAlmostEqual(r["dps_ttm"], 0.4 + 0.5)                    # 2023-09-28 与 2024-07-10 都在 (2023-09-27, 2024-09-27] 内
        for c in ("operating_profit_ttm", "equity_parent", "total_assets", "total_liabilities"):
            self.assertTrue(math.isnan(r[c]), c)                            # baostock 不给绝对值:NaN,不猜

    def test_fy_is_its_own_ttm_and_missing_quarter_is_nan(self):
        fake = standard_fake()
        del fake.reports[(fb.bs_code(CODE), 2022, 2)]     # 去掉 2022H1:上年同期 TTM 凑不齐
        self.assertEqual(self.run_cli(fake)[0], 0)
        r = self.fund_csv().iloc[0]
        self.assertAlmostEqual(r["net_profit_ttm"], 140)
        self.assertTrue(math.isnan(r["net_profit_ttm_prev"]))
        # 直接测 _ttm:年报本身就是 TTM;缺任一期 -> NaN
        cum = {date(2023, 12, 31): 120.0}
        self.assertEqual(fb._ttm(cum, date(2023, 12, 31)), 120.0)
        self.assertTrue(math.isnan(fb._ttm(cum, date(2024, 3, 31))))


# ---------- 3. asof 早于公告日 -> 不可见 ----------

class TestPointInTime(Base):
    def test_asof_before_announce_not_visible(self):
        client = make_client(standard_fake())
        for y, q in cli.quarters_between(date(2022, 1, 1), date(2024, 9, 27)):
            ingest_reports(self.store, [CODE], [fb.quarter_end(y, q)],
                           provider_fn=lambda c, r, q=q: client.fetch_quarterly(c, r.year, q))
        # 2024H1 公告日 2024-08-20:前一天看不到它,最新一期是 2024Q1
        row = fb.assemble_asof(self.store, CODE, date(2024, 8, 19), close_raw=10.0)
        self.assertEqual(row["report_date"], "2024-03-31")
        self.assertAlmostEqual(row["net_profit_ttm"], 30 + 120 - 25)
        self.assertLessEqual(row["available_date"], "2024-08-19")
        row = fb.assemble_asof(self.store, CODE, date(2024, 8, 20), close_raw=10.0)
        self.assertEqual(row["report_date"], "2024-06-30")
        self.assertEqual(row["available_date"], "2024-08-20")
        # 一个报告都没公告之前:没有行
        self.assertIsNone(fb.assemble_asof(self.store, CODE, date(2022, 8, 19)))

    def test_restatement_after_asof_invisible(self):
        client = make_client(standard_fake())
        for y, q in [(2022, 2), (2022, 4), (2023, 2), (2023, 4), (2024, 2)]:
            ingest_reports(self.store, [CODE], [fb.quarter_end(y, q)],
                           provider_fn=lambda c, r, q=q: client.fetch_quarterly(c, r.year, q))
        # 2023FY 净利润 2024-09-25 重述为 999:9 月 24 日不可见,9 月 25 日可见
        self.store.upsert_report(CODE, "2023-12-31", "net_profit", 999.0, "baostock", announce_date="2024-09-25")
        self.assertAlmostEqual(fb.assemble_asof(self.store, CODE, date(2024, 9, 24))["net_profit_ttm"], 140)
        self.assertAlmostEqual(fb.assemble_asof(self.store, CODE, date(2024, 9, 25))["net_profit_ttm"], 70 + 999 - 50)

    def test_dividend_window_and_unqueried_is_nan(self):
        client = make_client(standard_fake())
        recs = client.fetch_dividends(CODE, 2024)
        groups = {}
        for r in recs:
            groups.setdefault(date.fromisoformat(r["report_date"]), []).append(r)
        ingest_reports(self.store, [CODE], sorted(groups), provider_fn=lambda c, r: groups[r])
        self.assertTrue(math.isnan(fb.dps_ttm_asof(self.store, CODE, date(2024, 9, 27))))  # 2023 年没查过 -> NaN
        recs = client.fetch_dividends(CODE, 2023)
        groups = {}
        for r in recs:
            groups.setdefault(date.fromisoformat(r["report_date"]), []).append(r)
        ingest_reports(self.store, [CODE], sorted(groups), provider_fn=lambda c, r: groups[r])
        self.assertAlmostEqual(fb.dps_ttm_asof(self.store, CODE, date(2024, 7, 9)), 0.5)    # 2024 那次还没除权
        self.assertAlmostEqual(fb.dps_ttm_asof(self.store, CODE, date(2024, 7, 10)), 0.9)
        self.assertAlmostEqual(fb.dps_ttm_asof(self.store, CODE, date(2024, 9, 28)), 0.4)   # 正好满一年:2023-09-28 滑出窗口(左开右闭,防止年度分红重复计入)


# ---------- 4. 断点续跑 ----------

class TestResume(Base):
    def test_second_run_skips_stored_periods(self):
        fake = standard_fake()
        self.assertEqual(self.run_cli(fake)[0], 0)
        n1 = self.store.count()["financial_reports"]
        q_calls_1 = sum(1 for c in fake.calls if c in ("profit", "balance", "cash"))
        fake.calls.clear()
        rc, out, _ = self.run_cli(fake)
        self.assertEqual(rc, 0)
        self.assertEqual(self.store.count()["financial_reports"], n1)        # 不重复写
        q_calls_2 = sum(1 for c in fake.calls if c in ("profit", "balance", "cash"))
        # 第二次只重试第一次没数据的报告期(2022Q1/2022Q3/2023Q3 合成数据里没有:3 个季度 × 3 接口)
        self.assertEqual(q_calls_2, 3 * 3)
        self.assertLess(q_calls_2, q_calls_1)
        self.assertIn("跳过 7", out)

    def test_failed_quarter_retried_next_run_and_stop_after_5(self):
        fake = standard_fake()
        fake.fail_next = 5
        rc, _, err = self.run_cli(fake)
        self.assertEqual(rc, 3)                                   # 连续 5 次失败:停下报错
        self.assertIn("连续失败", err)
        self.assertFalse((self.dir / "out" / "fund.csv").exists())
        rc, _, err = self.run_cli(fake)                           # 恢复后续跑补齐
        self.assertEqual(rc, 0, err)
        self.assertAlmostEqual(self.fund_csv().iloc[0]["net_profit_ttm"], 140)

    def test_four_failures_then_success_resets_counter(self):
        fake = standard_fake()
        fake.fail_next = 4
        client = make_client(fake)
        for _ in range(4):
            with self.assertRaises(fb.FetchError):
                client.query("query_profit_data", code="sh.600001", year=2024, quarter=1)
        client.query("query_profit_data", code="sh.600001", year=2024, quarter=1)
        self.assertEqual(client.consecutive_failures, 0)

    def test_rate_limit_sleep_between_requests(self):
        slept, t = [], [0.0]
        client = fb.BaostockClient(standard_fake(), sleep=lambda s: slept.append(s) or t.__setitem__(0, t[0] + s),
                                   clock=lambda: t[0], today=lambda: date(2024, 9, 30))
        client.fetch_quarterly(CODE, 2024, 2)                      # 3 次请求 -> 2 次间隔
        self.assertEqual(len(slept), 2)
        self.assertTrue(all(abs(s - 0.2) < 1e-9 for s in slept))


# ---------- 5. 导出 CSV ----------

class TestExport(Base):
    def test_columns_complete(self):
        rc, _, err = self.run_cli(standard_fake())
        self.assertEqual(rc, 0, err)
        fdf = self.fund_csv()
        need = ["market_cap", "close_raw", "net_profit_ttm", "net_profit_ttm_prev", "revenue_ttm", "revenue_ttm_prev",
                "operating_cost_ttm", "operating_profit_ttm", "equity_parent", "total_assets", "total_liabilities",
                "cfo_ttm", "dps_ttm", "available_date"]
        for c in ["code", "name", "industry"] + need:
            self.assertIn(c, fdf.columns)
        self.assertEqual(fdf.iloc[0]["code"], CODE)
        pdf = pd.read_csv(self.dir / "out" / "prices.csv", dtype={"code": str})
        self.assertEqual(list(pdf.columns), ["code", "date", "close"])
        self.assertEqual(list(pdf["date"]), ["2024-09-23", "2024-09-24", "2024-09-25", "2024-09-26", "2024-09-27"])

    def test_export_feeds_ranker(self):
        """导出的两份 CSV 能直接被排名脚本读进去(只校验格式,不验证排名好坏)。"""
        from src.fundamental.ranker import build_ranking
        self.assertEqual(self.run_cli(standard_fake())[0], 0)
        fdf = self.fund_csv()
        pdf = pd.read_csv(self.dir / "out" / "prices.csv", dtype={"code": str})
        res = build_ranking(fdf, pdf, "2024-09-27", top_n=5)
        self.assertEqual(res.n_input, 1)

    def test_asof_rules(self):
        rc, _, err = self.run_cli(standard_fake(), asof="2024-10-01")
        self.assertEqual(rc, 2)
        self.assertIn("晚于今天", err)
        early = datetime(2024, 9, 27, 10, 0, tzinfo=BJT)
        with contextlib.redirect_stderr(io.StringIO()) as e:
            rc = cli.main(["--codes-file", str(self.codes), "--db", str(self.db), "--asof", "2024-09-27",
                           "--export-fundamental", str(self.dir / "f.csv"), "--export-prices", str(self.dir / "p.csv")],
                          client=make_client(standard_fake()), now=early, daily_dir=self.daily)
        self.assertEqual(rc, 2)
        self.assertIn("15:30", e.getvalue())

    def test_refuses_protected_dir(self):
        rc, _, err = self.run_cli(standard_fake(), extra=["--export-prices", str(ROOT / "data" / "daily" / "x.csv")])
        self.assertEqual(rc, 2)
        self.assertIn("受保护", err)

    def test_limit(self):
        pd.DataFrame({"code": ["600001", "000002", "abc", "300003"]}).to_csv(self.codes, index=False)
        self.assertEqual(list(cli.load_codes(self.codes, 2)["code"]), ["600001", "000002"])


class TestHygiene(unittest.TestCase):
    def test_forbidden_strings_absent(self):
        words = ["User-" + "Agent", "Coo" + "kie", "Ref" + "erer", "add_" + "header", "best" + "ip",
                 "sea" + "led", "te" + "st2", "trials" + ".jsonl"]
        for p in (ROOT / "src" / "fundamental" / "fetch_baostock.py", ROOT / "scripts" / "fetch_fundamental.py"):
            text = p.read_text(encoding="utf-8")
            for w in words:
                self.assertNotIn(w, text, "%s in %s" % (w, p.name))

    def test_import_does_not_import_baostock(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, %r); import src.fundamental.fetch_baostock;"
                "print('baostock' in sys.modules)" % str(ROOT))
        r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout.strip(), "False", r.stderr)


if __name__ == "__main__":
    unittest.main()
