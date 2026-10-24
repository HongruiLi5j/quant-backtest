"""src/dataquality/status_table.py 的测试:合成清单 + 临时目录,不读写 data/,不联网。

运行:python3 -B tests/test_dataquality_status.py
"""
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.dataquality import (  # noqa: E402
    StockInfo, build_range, build_status, infos_from_frame, parse_date, summarize, write_table)


def weekdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# 2023-01-02 ~ 2024-12-31 的工作日,去掉 2024-02-09~02-16 当作春节假期
CAL = [d for d in weekdays(date(2023, 1, 2), date(2024, 12, 31))
       if not (date(2024, 2, 9) <= d <= date(2024, 2, 16))]

OLD = StockInfo("600001", date(2023, 1, 2), None)             # 日历起点即上市
NEW = StockInfo("300002", date(2024, 6, 3), None)             # 2024 年中上市
DEAD = StockInfo("000003", date(2023, 1, 2), date(2024, 3, 1))  # 2024-03-01 退市
NOIPO = StockInfo("000004", None, None)                       # 上市日缺失
INFOS = [OLD, NEW, DEAD, NOIPO]


class TestParse(unittest.TestCase):
    def test_parse_date(self):
        self.assertEqual(parse_date("2024-06-03"), date(2024, 6, 3))
        self.assertEqual(parse_date("20240603"), date(2024, 6, 3))
        self.assertEqual(parse_date(pd.Timestamp("2024-06-03")), date(2024, 6, 3))
        for bad in (None, "", "  ", float("nan"), "abc", pd.NaT):
            self.assertIsNone(parse_date(bad))

    def test_infos_from_frame(self):
        df = pd.DataFrame({"code": ["600001", "300002"], "ipo": ["2020-01-01", ""],
                           "board": [None, "创业板"]})
        a, b = infos_from_frame(df)
        self.assertEqual(a.ipo, date(2020, 1, 1))
        self.assertEqual(a.board, "主板")   # 缺省按前缀推断
        self.assertIsNone(b.ipo)            # 空串 -> None
        self.assertIsNone(b.delist)         # 没有 delist 列


class TestListing(unittest.TestCase):
    def test_not_listed_yet_and_first_day(self):
        before = build_status(date(2024, 5, 31), [NEW], CAL)["300002"]
        self.assertIs(before.listed, False)
        self.assertIs(before.tradable, False)
        self.assertIn("not_listed", before.reasons)
        first = build_status(date(2024, 6, 3), [NEW], CAL)["300002"]
        self.assertIs(first.listed, True)
        self.assertEqual(first.listed_days, 1)   # 上市当天算第 1 天

    def test_less_than_one_year(self):
        r = build_status(date(2024, 12, 31), [NEW], CAL)["300002"]
        self.assertTrue(0 < r.listed_days < 250)
        self.assertIn("listed_lt_min_days", r.reasons)
        # 不满一年不并入 tradable(两个概念分开)
        s = build_status(date(2024, 12, 31), [NEW], CAL, delist_info_complete=True,
                         suspended={date(2024, 12, 31): set()})["300002"]
        self.assertIs(s.tradable, True)
        # 阈值可调
        r2 = build_status(date(2024, 12, 31), [NEW], CAL, min_listed_days=10)["300002"]
        self.assertNotIn("listed_lt_min_days", r2.reasons)

    def test_listed_days_counts_calendar_not_weekdays(self):
        # 2024-02-08 与 2024-02-19 之间隔着假期:19 日应是 8 日之后的下一个交易日
        info = StockInfo("600009", date(2024, 2, 8))
        r8 = build_status(date(2024, 2, 8), [info], CAL)["600009"]
        r19 = build_status(date(2024, 2, 19), [info], CAL)["600009"]
        self.assertEqual((r8.listed_days, r19.listed_days), (1, 2))

    def test_ipo_on_non_trading_day(self):
        info = StockInfo("600010", date(2024, 2, 10))   # 假期里
        r = build_status(date(2024, 2, 19), [info], CAL)["600010"]
        self.assertEqual(r.listed_days, 1)

    def test_old_ipo_before_calendar_is_unknown(self):
        old = StockInfo("600011", date(2010, 1, 1))
        r = build_status(date(2024, 5, 6), [old], CAL)["600011"]
        self.assertIs(r.listed, True)
        self.assertIsNone(r.listed_days)   # 日历不够长,不伪造天数

    def test_ipo_missing(self):
        r = build_status(date(2024, 5, 6), [NOIPO], CAL)["000004"]
        self.assertIsNone(r.listed)
        self.assertIsNone(r.listed_days)
        self.assertIn("ipo_unknown", r.reasons)
        self.assertIsNone(r.tradable)


class TestDelist(unittest.TestCase):
    def test_delist_boundaries(self):
        before = build_status(date(2024, 2, 29), [DEAD], CAL)["000003"]
        on = build_status(date(2024, 3, 1), [DEAD], CAL)["000003"]
        after = build_status(date(2024, 3, 4), [DEAD], CAL)["000003"]
        self.assertIs(before.delisted, False)
        self.assertIs(on.delisted, True)
        self.assertIs(after.delisted, True)
        for r in (on, after):
            self.assertIs(r.tradable, False)
            self.assertIn("delisted", r.reasons)

    def test_delist_info_missing_is_none_not_false(self):
        r = build_status(date(2024, 5, 6), [OLD], CAL)["600001"]
        self.assertIsNone(r.delisted)
        self.assertIn("delist_unknown", r.reasons)

    def test_delist_info_complete_flag(self):
        r = build_status(date(2024, 5, 6), [OLD], CAL, delist_info_complete=True)["600001"]
        self.assertIs(r.delisted, False)


class TestSuspension(unittest.TestCase):
    D = date(2024, 5, 6)

    def test_recorded(self):
        susp = {self.D: {"600001"}}
        out = build_status(self.D, [OLD, NEW], CAL, suspended=susp, delist_info_complete=True)
        r = out["600001"]
        self.assertIs(r.suspended, True)
        self.assertEqual(r.susp_source, "recorded")
        self.assertIs(r.tradable, False)
        # 同一天不在名单里的股票:记录在案的"未停牌"
        self.assertIs(out["300002"].suspended, False)
        self.assertEqual(out["300002"].susp_source, "recorded")

    def test_recorded_beats_bars(self):
        r = build_status(self.D, [OLD], CAL, suspended={self.D: set()},
                         bars_present={self.D: set()}, delist_info_complete=True)["600001"]
        self.assertEqual(r.susp_source, "recorded")
        self.assertIs(r.suspended, False)

    def test_inferred_from_missing_bar(self):
        r = build_status(self.D, [OLD], CAL, bars_present={self.D: {"999999"}},
                         delist_info_complete=True)["600001"]
        self.assertIs(r.suspended, True)
        self.assertEqual(r.susp_source, "inferred")
        self.assertIn("suspension_inferred", r.reasons)
        self.assertIs(r.tradable, False)

    def test_inferred_not_suspended_when_bar_exists(self):
        r = build_status(self.D, [OLD], CAL, bars_present={self.D: {"600001"}},
                         delist_info_complete=True)["600001"]
        self.assertIs(r.suspended, False)
        self.assertIs(r.tradable, True)

    def test_missing_bar_not_inferred_if_delist_unknown(self):
        # 退市信息未知时,缺 K 线可能是退市,不能推断为停牌
        r = build_status(self.D, [OLD], CAL, bars_present={self.D: set()})["600001"]
        self.assertIsNone(r.suspended)
        self.assertEqual(r.susp_source, "unknown")

    def test_unknown_when_no_info(self):
        r = build_status(self.D, [OLD], CAL, delist_info_complete=True)["600001"]
        self.assertIsNone(r.suspended)
        self.assertEqual(r.susp_source, "unknown")
        self.assertIsNone(r.tradable)   # 其余为真、停牌未知 -> None
        self.assertIn("suspension_unknown", r.reasons)

    def test_untrusted_calendar_downgrades_inference(self):
        r = build_status(self.D, [OLD], CAL, bars_present={self.D: set()},
                         delist_info_complete=True, calendar_trusted=False)["600001"]
        self.assertIsNone(r.suspended)
        self.assertEqual(r.susp_source, "unknown")
        # 记录在案的停牌不受影响
        r2 = build_status(self.D, [OLD], CAL, suspended={self.D: {"600001"}},
                          calendar_trusted=False)["600001"]
        self.assertIs(r2.suspended, True)


class TestST(unittest.TestCase):
    SPANS = {"600001": [(date(2024, 3, 1), date(2024, 3, 29)), (date(2024, 9, 2), None)]}

    def test_none_without_spans(self):
        self.assertIsNone(build_status(date(2024, 3, 5), [OLD], CAL)["600001"].is_st)

    def test_boundaries_inclusive(self):
        def st(d):
            return build_status(d, INFOS, CAL, st_spans=self.SPANS)["600001"].is_st
        self.assertIs(st(date(2024, 2, 29)), False)
        self.assertIs(st(date(2024, 3, 1)), True)    # 起始当天
        self.assertIs(st(date(2024, 3, 29)), True)   # 结束当天
        self.assertIs(st(date(2024, 4, 1)), False)
        self.assertIs(st(date(2024, 12, 2)), True)   # 开区间至今

    def test_code_not_in_spans_is_false(self):
        r = build_status(date(2024, 3, 5), INFOS, CAL, st_spans=self.SPANS)
        self.assertIs(r["300002"].is_st, False)

    def test_st_does_not_affect_tradable(self):
        r = build_status(date(2024, 3, 5), [OLD], CAL, st_spans=self.SPANS,
                         suspended={date(2024, 3, 5): set()}, delist_info_complete=True)["600001"]
        self.assertIs(r.is_st, True)
        self.assertIs(r.tradable, True)


class TestCalendarAndRange(unittest.TestCase):
    def test_non_trading_day_empty(self):
        self.assertEqual(build_status(date(2024, 5, 4), INFOS, CAL), {})   # 周六
        self.assertEqual(build_status(date(2024, 2, 12), INFOS, CAL), {})  # 假期
        self.assertEqual(build_status("bad", INFOS, CAL), {})

    def test_build_range_shape_and_consistency(self):
        s, e = date(2024, 2, 7), date(2024, 2, 20)
        df = build_range(s, e, INFOS, CAL, delist_info_complete=True)
        days = [d for d in CAL if s <= d <= e]
        self.assertEqual(len(df), len(days) * len(INFOS))
        self.assertEqual(sorted(df["day"].unique()), days)
        self.assertNotIn(date(2024, 2, 12), set(df["day"]))
        # 与逐日 build_status 一致
        d = date(2024, 2, 19)
        one = build_status(d, INFOS, CAL, delist_info_complete=True)
        for _, r in df[df["day"] == d].iterrows():
            self.assertEqual(r["listed_days"] if not pd.isna(r["listed_days"]) else None,
                             one[r["code"]].listed_days)
            exp = one[r["code"]].tradable
            self.assertEqual(None if pd.isna(r["tradable"]) else bool(r["tradable"]), exp)

    def test_build_range_preserves_none(self):
        df = build_range(date(2024, 5, 6), date(2024, 5, 7), [OLD], CAL)
        self.assertTrue(df["delisted"].isna().all())
        self.assertTrue(df["is_st"].isna().all())
        self.assertTrue(df["tradable"].isna().all())

    def test_build_range_empty(self):
        df = build_range(date(2030, 1, 1), date(2030, 1, 5), INFOS, CAL)
        self.assertEqual(len(df), 0)
        self.assertEqual(summarize(df)["rows"], 0)


class TestSummarize(unittest.TestCase):
    def test_missing_rate(self):
        s, e = date(2024, 5, 6), date(2024, 5, 10)   # 5 个交易日
        df = build_range(s, e, INFOS, CAL, suspended={date(2024, 5, 6): {"600001"}})
        m = summarize(df)
        self.assertEqual(m["rows"], 5 * 4)
        # is_st 没提供 -> 缺失率 100%;ipo 缺失 1/4 只 -> listed 缺失率 25%
        self.assertEqual(m["missing_rate"]["is_st"], 1.0)
        self.assertAlmostEqual(m["missing_rate"]["listed"], 0.25)
        # DEAD 有退市日 -> delisted 已知;其余 3 只未知
        self.assertAlmostEqual(m["missing_rate"]["delisted"], 0.75)
        self.assertEqual(m["reason_counts"]["ipo_unknown"], 5)
        self.assertEqual(m["susp_source_counts"]["recorded"], 4)
        self.assertEqual(sum(m["tradable_counts"].values()), 20)
        # 无停牌/ST 历史,但有记录日:600001 当日停牌 -> False
        self.assertGreaterEqual(m["tradable_counts"]["false"], 1)


class TestWrite(unittest.TestCase):
    def setUp(self):
        self.df = build_range(date(2024, 5, 6), date(2024, 5, 8), INFOS, CAL)

    def test_roundtrip_parquet_csv(self):
        with tempfile.TemporaryDirectory() as d:
            p = write_table(self.df, os.path.join(d, "sub", "s.parquet"))
            back = pd.read_parquet(p)
            self.assertEqual(len(back), len(self.df))
            self.assertTrue(back["is_st"].isna().all())   # None 落盘后仍是缺失
            c = write_table(self.df, os.path.join(d, "s.csv"))
            self.assertEqual(len(pd.read_csv(c)), len(self.df))
            self.assertFalse([f for f in os.listdir(d) if f.endswith(".tmp")])

    def test_refuse_overwrite_and_bad_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.csv")
            write_table(self.df, p)
            with self.assertRaises(FileExistsError):
                write_table(self.df, p)
            write_table(self.df, p, overwrite=True)
            with self.assertRaises(ValueError):
                write_table(self.df, os.path.join(d, "s.txt"))

    def test_refuse_data_dir(self):
        # 路径只用于被拒绝的判断,拒绝发生在任何写入之前
        target = os.path.join(ROOT, "data", "daily", "status_should_not_exist.csv")
        with self.assertRaises(ValueError):
            write_table(self.df, target)
        self.assertFalse(os.path.exists(target))


class TestBadInputs(unittest.TestCase):
    """返工:输入写错时必须降级为 None,不能被写成 True/False。"""
    DAY = date(2024, 5, 6)

    def test_st_bad_end_is_none_not_open_ended(self):
        r = build_status(self.DAY, [OLD], CAL, st_spans={"600001": [("2023-01-01", "2023-13-01")]})
        self.assertIsNone(r["600001"].is_st)
        self.assertIn("st_span_invalid", r["600001"].reasons)

    def test_st_blank_end_still_open_ended(self):
        for blank in (None, "", float("nan")):
            r = build_status(self.DAY, [OLD], CAL, st_spans={"600001": [("2023-01-01", blank)]})
            self.assertIs(r["600001"].is_st, True)

    def test_st_bad_start_is_none_not_false(self):
        r = build_status(self.DAY, [OLD], CAL, st_spans={"600001": [("bad", "2025-01-01")]})
        self.assertIsNone(r["600001"].is_st)

    def test_st_good_span_wins_over_bad_span(self):
        spans = {"600001": [("bad", None), (date(2024, 5, 1), date(2024, 5, 31))]}
        self.assertIs(build_status(self.DAY, [OLD], CAL, st_spans=spans)["600001"].is_st, True)
        # 好区间没覆盖当天时,坏区间让结果变成未知
        self.assertIsNone(build_status(date(2024, 7, 1), [OLD], CAL, st_spans=spans)["600001"].is_st)

    def test_invalid_delist_date_is_none_even_if_list_complete(self):
        df = pd.DataFrame({"code": ["600001"], "ipo": ["2023-01-02"], "delist": ["2024/13/01"]})
        info = infos_from_frame(df)[0]
        self.assertTrue(info.delist_invalid)
        r = build_status(self.DAY, [info], CAL, delist_info_complete=True)["600001"]
        self.assertIsNone(r.delisted)
        self.assertIsNone(r.tradable)
        self.assertIn("delist_invalid", r.reasons)

    def test_blank_delist_is_not_invalid(self):
        df = pd.DataFrame({"code": ["600001", "600002"], "delist": [None, " "]})
        for i in infos_from_frame(df):
            self.assertFalse(i.delist_invalid)

    def test_untrusted_calendar_listed_days_none(self):
        r = build_status(self.DAY, [OLD], CAL, calendar_trusted=False)["600001"]
        self.assertIsNone(r.listed_days)
        self.assertIs(r.listed, True)
        self.assertIn("listed_days_unknown_calendar_untrusted", r.reasons)

    def test_write_refuses_data_dir_case_variants(self):
        for name in ("DATA", "Data"):
            target = os.path.join(ROOT, name, "daily", "status_should_not_exist.csv")
            with self.assertRaises(ValueError):
                write_table(build_range(self.DAY, self.DAY, INFOS, CAL), target)
            self.assertFalse(os.path.exists(target))

    def test_build_range_rejects_unknown_kwargs(self):
        with self.assertRaises(TypeError):
            build_range(self.DAY, self.DAY, INFOS, CAL, suspend={self.DAY: set()})

    def test_infos_from_frame_rejects_non_six_digit_code(self):
        # 读清单没加 dtype=str:000001 -> 1
        with self.assertRaises(ValueError):
            infos_from_frame(pd.DataFrame({"code": [1]}))
        with self.assertRaises(ValueError):
            infos_from_frame(pd.DataFrame({"code": ["60000a"]}))


class TestNoNetwork(unittest.TestCase):
    def test_no_network_modules_imported(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "import src.dataquality;"
                "bad=[m for m in ('akshare','baostock','requests','urllib3',"
                "'src.allmarket','src.datasource','src.scan') if m in sys.modules];"
                "print(bad)")
        out = subprocess.run([sys.executable, "-B", "-c", code, ROOT],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
