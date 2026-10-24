"""src/dataquality/consistency.py 的测试:全部用合成序列,不读写 data/,不联网。

运行:.venv/bin/python -B tests/test_dataquality_consistency.py
"""
import json
import os
import subprocess
import sys
import unittest
from datetime import date, timedelta

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.dataquality.consistency import (  # noqa: E402
    Issue, Thresholds, check_adjustment, check_calendar_gaps, check_jumps, check_ohlc,
    check_series, check_zero_volume, infer_ex_days, summarize_issues)


def weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = weekdays(date(2023, 1, 2), 30)


def make(closes=None, days=DAYS, vol=1000.0):
    """平稳合成序列:H/L 正确包络 O=C。"""
    closes = closes or [10.0 + 0.01 * i for i in range(len(days))]
    rows = [(c, c * 1.005, c * 0.995, c, vol) for c in closes]
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.DatetimeIndex(days))


def kinds(issues):
    return [i.kind for i in issues]


def setv(df, row, col, val):
    df.iloc[row, df.columns.get_loc(col)] = val


class TestOhlc(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(check_ohlc(make(), "600001"), [])

    def test_high_below_low(self):
        df = make()
        setv(df, 3, "High", 9.0)
        setv(df, 3, "Low", 11.0)
        r = check_ohlc(df, "600001")
        self.assertIn("high_below_low", kinds(r))
        self.assertEqual(r[0].severity, "error")
        self.assertEqual(r[0].day, DAYS[3])

    def test_nonpositive_and_nan(self):
        df = make()
        setv(df, 1, "Close", 0.0)
        setv(df, 2, "Open", float("nan"))
        k = kinds(check_ohlc(df, "x"))
        self.assertIn("nonpositive_price", k)
        self.assertIn("nan_price", k)

    def test_envelope(self):
        df = make()
        setv(df, 4, "High", df.iloc[4]["Close"] * 0.997)
        setv(df, 5, "Low", df.iloc[5]["Close"] * 1.003)
        k = kinds(check_ohlc(df, "x"))
        self.assertIn("high_not_max", k)
        self.assertIn("low_not_min", k)

    def test_negative_volume_duplicate_unsorted(self):
        df = make()
        setv(df, 2, "Volume", -1)
        self.assertIn("negative_volume", kinds(check_ohlc(df, "x")))
        dup = pd.concat([df, df.iloc[[0]]])
        self.assertIn("duplicate_date", kinds(check_ohlc(dup, "x")))
        self.assertIn("unsorted_index", kinds(check_ohlc(dup, "x")))

    def test_missing_column_and_bad_input(self):
        self.assertIn("missing_column", kinds(check_ohlc(make()[["Open", "Close"]], "x")))
        self.assertIn("bad_input", kinds(check_ohlc(None, "x")))


class TestGaps(unittest.TestCase):
    def test_no_gap(self):
        self.assertEqual(check_calendar_gaps(make(), DAYS, "x"), [])

    def test_missing_middle_days_grouped(self):
        df = make().drop(index=[pd.Timestamp(DAYS[5]), pd.Timestamp(DAYS[6]), pd.Timestamp(DAYS[10])])
        r = check_calendar_gaps(df, DAYS, "x")
        self.assertEqual(kinds(r), ["missing_trading_days"] * 2)
        self.assertEqual((r[0].day, r[0].value), (DAYS[5], 2.0))
        self.assertEqual((r[1].day, r[1].value), (DAYS[10], 1.0))

    def test_suspended_downgraded_to_info(self):
        df = make().drop(index=[pd.Timestamp(DAYS[5])])
        r = check_calendar_gaps(df, DAYS, "600001", suspended={DAYS[5]: {"600001"}})
        self.assertEqual([(i.kind, i.severity) for i in r], [("gap_suspended", "info")])
        # 名单里是别的股票:仍然报
        r = check_calendar_gaps(df, DAYS, "600001", suspended={DAYS[5].isoformat(): ["000002"]})
        self.assertEqual(kinds(r), ["missing_trading_days"])

    def test_tail_gap_not_reported(self):
        df = make().iloc[:-3]
        self.assertEqual(check_calendar_gaps(df, DAYS, "x"), [])

    def test_max_gap_days(self):
        df = make().drop(index=[pd.Timestamp(DAYS[5]), pd.Timestamp(DAYS[6])])
        self.assertEqual(check_calendar_gaps(df, DAYS, "x", th=Thresholds(max_gap_days=2)), [])
        self.assertEqual(len(check_calendar_gaps(df, DAYS, "x", th=Thresholds(max_gap_days=1))), 1)

    def test_holiday_not_a_gap(self):
        cal = [d for d in DAYS if d != DAYS[5]]   # 日历里 DAYS[5] 是假日,K 线也没有
        df = make().drop(index=[pd.Timestamp(DAYS[5])])
        self.assertEqual(check_calendar_gaps(df, cal, "x"), [])

    def test_bar_not_in_calendar(self):
        cal = [d for d in DAYS if d != DAYS[5]]
        self.assertEqual(kinds(check_calendar_gaps(make(), cal, "x")), ["bar_not_in_calendar"])

    def test_untrusted_calendar(self):
        df = make().drop(index=[pd.Timestamp(DAYS[5])])
        r = check_calendar_gaps(df, DAYS, "x", calendar_trusted=False)
        self.assertEqual([(i.kind, i.severity) for i in r], [("calendar_untrusted", "info")])

    def test_empty_calendar(self):
        self.assertEqual(check_calendar_gaps(make(), None, "x"), [])


class TestJumps(unittest.TestCase):
    def closes_with(self, i, mult):
        c = [10.0] * len(DAYS)
        for j in range(i, len(DAYS)):
            c[j] = 10.0 * mult
        return make(c)

    def test_within_limit_not_reported(self):
        self.assertEqual(check_jumps(self.closes_with(10, 1.10), "600001"), [])  # 恰好涨停

    def test_over_limit_reported(self):
        r = check_jumps(self.closes_with(10, 1.20), "600001")
        self.assertEqual(kinds(r), ["price_jump"])
        self.assertEqual(r[0].day, DAYS[10])
        self.assertAlmostEqual(r[0].value, 0.20)

    def test_board_limits(self):
        df = self.closes_with(10, 1.20)       # +20%
        self.assertEqual(len(check_jumps(df, "600001")), 1)                    # 主板 10%
        self.assertEqual(check_jumps(df, "688001"), [])                        # 科创板 20%
        self.assertEqual(check_jumps(df, "300001"), [])                        # 创业板 2023 年 20%
        self.assertEqual(len(check_jumps(df, "300001", board="主板")), 1)      # 显式板块优先

    def test_chinext_before_2020_is_10pct(self):
        days = weekdays(date(2019, 1, 2), 30)
        c = [10.0] * 30
        for j in range(10, 30):
            c[j] = 12.0
        self.assertEqual(len(check_jumps(make(c, days), "300001")), 1)

    def test_ex_day_exempt(self):
        df = self.closes_with(10, 0.6)
        self.assertEqual(len(check_jumps(df, "600001")), 1)
        self.assertEqual(check_jumps(df, "600001", ex_days=[DAYS[10]]), [])

    def test_st_uses_5pct_on_main_board(self):
        df = self.closes_with(10, 1.09)
        self.assertEqual(check_jumps(df, "600001"), [])
        self.assertEqual(len(check_jumps(df, "600001", st_spans=[(DAYS[0], None)])), 1)
        # 区间外不用 ST 阈值
        self.assertEqual(check_jumps(df, "600001", st_spans=[(DAYS[0], DAYS[5])]), [])

    def test_new_listing_free_days(self):
        df = self.closes_with(2, 1.8)          # 第 3 根 K 线 +80%
        self.assertEqual(len(check_jumps(df, "600001")), 1)
        self.assertEqual(check_jumps(df, "600001", ipo=DAYS[0]), [])   # 上市第 3 天,在前 5 天内
        r = check_jumps(self.closes_with(8, 1.8), "600001", ipo=DAYS[0])
        self.assertEqual(len(r), 1)

    def test_slack_configurable(self):
        df = self.closes_with(10, 1.115)
        self.assertEqual(check_jumps(df, "600001"), [])
        self.assertEqual(len(check_jumps(df, "600001", th=Thresholds(slack=0.0))), 1)

    def test_custom_rules(self):
        th = Thresholds(limit_rules=(("主板", date(2000, 1, 1), 0.30),))
        self.assertEqual(check_jumps(self.closes_with(10, 1.25), "600001", th=th), [])

    def test_bad_prices_skipped(self):
        df = self.closes_with(10, 1.0)
        setv(df, 5, "Close", 0.0)
        self.assertEqual(check_jumps(df, "600001"), [])


class TestZeroVolume(unittest.TestCase):
    def flat(self):
        df = make([10.0] * len(DAYS))
        df["High"] = df["Low"] = df["Open"] = df["Close"]
        return df

    def test_zero_volume_flat_ok(self):
        df = self.flat()
        setv(df, 5, "Volume", 0)
        self.assertEqual(check_zero_volume(df, "x"), [])

    def test_zero_volume_with_move(self):
        df = make()
        setv(df, 5, "Volume", 0)
        r = check_zero_volume(df, "x")
        self.assertEqual(kinds(r), ["zero_volume_price_move"])
        self.assertEqual(r[0].day, DAYS[5])

    def test_zero_volume_close_gap_vs_prev(self):
        df = self.flat()
        for c in ("Open", "High", "Low", "Close"):
            df.iloc[5:, df.columns.get_loc(c)] = 10.5
        setv(df, 5, "Volume", 0)
        self.assertEqual(len(check_zero_volume(df, "x")), 1)

    def test_no_volume_column(self):
        self.assertEqual(check_zero_volume(make().drop(columns=["Volume"]), "x"), [])


class TestAdjustment(unittest.TestCase):
    def setUp(self):
        n = len(DAYS)
        self.idx = pd.DatetimeIndex(DAYS)
        # 第 10 天除权:后复权因子从 1 升到 1.1,原始价同日下降 1/1.1,后复权价连续
        self.factor = pd.Series([1.0] * 10 + [1.1] * (n - 10), index=self.idx)
        self.raw = pd.Series([10.0] * 10 + [10.0 / 1.1] * (n - 10), index=self.idx)
        self.hfq = self.raw * self.factor
        self.qfq = self.raw * self.factor / self.factor.iloc[-1]

    def test_consistent(self):
        r = check_adjustment(self.hfq, "x", raw_close=self.raw, factors=self.factor, qfq_close=self.qfq)
        self.assertEqual(r, [])

    def test_infer_ex_days(self):
        self.assertEqual(infer_ex_days(self.hfq, self.raw), [DAYS[10]])

    def test_factor_decrease(self):
        hfq = self.hfq.copy()
        hfq.iloc[15:] = hfq.iloc[15:] * 0.9          # 后复权价相对原始价掉了 10%
        r = check_adjustment(hfq, "x", raw_close=self.raw)
        self.assertEqual(kinds(r), ["factor_decrease"])
        self.assertEqual(r[0].day, DAYS[15])
        self.assertEqual(r[0].severity, "error")

    def test_factor_series_checks(self):
        f = self.factor.copy()
        f.iloc[20] = 0.5
        f.iloc[22] = -1
        k = kinds(check_adjustment(self.hfq, "x", factors=f))
        self.assertIn("factor_series_decrease", k)
        self.assertIn("factor_invalid", k)

    def test_factor_mismatch(self):
        f = self.factor.copy()
        f.iloc[15:] = f.iloc[15:] * 1.05
        r = check_adjustment(self.hfq, "x", raw_close=self.raw, factors=f)
        self.assertIn("factor_mismatch", kinds(r))

    def test_conversion_mismatch(self):
        q = self.qfq.copy()
        q.iloc[12] = q.iloc[12] * 1.02
        r = check_adjustment(self.hfq, "x", qfq_close=q)
        self.assertEqual([(i.kind, i.day) for i in r], [("adj_conversion_mismatch", DAYS[12])])

    def test_constant_scale_between_sources_ok(self):
        # 不同来源整体差一个常数 k:因子水平变了但比值不变
        self.assertEqual(check_adjustment(self.hfq * 2.37, "x", raw_close=self.raw,
                                          qfq_close=self.qfq), [])

    def test_unverifiable_is_info(self):
        r = check_adjustment(self.hfq, "x")
        self.assertEqual([(i.kind, i.severity) for i in r], [("adjustment_unverifiable", "info")])

    def test_partial_overlap_raw(self):
        r = check_adjustment(self.hfq, "x", raw_close=self.raw.iloc[15:])
        self.assertEqual(r, [])


class TestThresholdsAndIssue(unittest.TestCase):
    def test_invalid_thresholds_rejected(self):
        for kw in ({"slack": -0.01}, {"max_reports_per_kind": 0}, {"min_factor_ratio": 0},
                   {"min_factor_ratio": 1.5}, {"default_limit": float("nan")},
                   {"max_gap_days": -1}, {"new_listing_free_days": 1.5}):
            with self.assertRaises(ValueError, msg=str(kw)):
                Thresholds(**kw)
        Thresholds(slack=0.0)

    def test_to_dict_nan_becomes_none(self):
        for v in (float("nan"), float("inf")):
            d = Issue("x", None, "k", "info", "d", v).to_dict()
            self.assertIsNone(d["value"])
            json.dumps(d, allow_nan=False)


class TestSeries(unittest.TestCase):
    def build_bad(self):
        df = make()
        df = df.drop(index=[pd.Timestamp(DAYS[7])])                     # 缺失交易日
        setv(df, 3, "High", 1.0)                                        # 坏数据
        setv(df, 3, "Low", 20.0)
        df.iloc[12:, df.columns.get_loc("Close")] = 50.0                # 跳变
        setv(df, 20, "Volume", 0)                                       # 零量
        return df

    def test_all_kinds_and_order(self):
        r = check_series("600001", self.build_bad(), calendar=DAYS)
        k = set(kinds(r))
        self.assertTrue({"high_below_low", "missing_trading_days", "price_jump",
                         "zero_volume_price_move"} <= k, k)
        order = {"error": 0, "warn": 1, "info": 2}
        sev = [order[i.severity] for i in r]
        self.assertEqual(sev, sorted(sev))

    def test_json_serialisable(self):
        r = check_series("600001", self.build_bad(), calendar=DAYS)
        s = json.dumps([i.to_dict() for i in r], ensure_ascii=False)
        self.assertEqual(len(json.loads(s)), len(r))
        self.assertIsInstance(r[0], Issue)

    def test_scale_invariance(self):
        a = check_series("600001", self.build_bad(), calendar=DAYS)
        df2 = self.build_bad()
        df2[["Open", "High", "Low", "Close"]] *= 2.37
        b = check_series("600001", df2, calendar=DAYS)

        def strip(rs):
            return [(i.kind, i.severity, i.day) for i in rs]
        self.assertEqual(strip(a), strip(b))

    def test_ex_day_from_raw_close(self):
        n = len(DAYS)
        idx = pd.DatetimeIndex(DAYS)
        raw = pd.Series([10.0] * 15 + [6.0] * (n - 15), index=idx)
        factor = pd.Series([1.0] * 15 + [1 / 0.6] * (n - 15), index=idx)
        # 正确的后复权价连续,不应有任何问题
        df = make((raw * factor).tolist())
        self.assertEqual(check_series("600001", df, raw_close=raw), [])
        # 把原始价当后复权价(模拟复权错误):当天 -40%,应报跳变
        r = check_series("600001", make(raw.tolist()), raw_close=raw)
        self.assertIn("price_jump", kinds(r))

    def test_inferred_ex_day_really_exempts_jump(self):
        # 原始价平稳、后复权价在第 15 天跳 +40%:隐含因子同日变化 -> 推出除权日 -> 豁免
        n = len(DAYS)
        idx = pd.DatetimeIndex(DAYS)
        raw = pd.Series([10.0] * n, index=idx)
        hfq = pd.Series([10.0] * 15 + [14.0] * (n - 15), index=idx)
        df = make(hfq.tolist())
        self.assertEqual(infer_ex_days(hfq, raw), [DAYS[15]])
        self.assertIn("price_jump", kinds(check_series("600001", df)))   # 不给 raw_close 就会报
        self.assertNotIn("price_jump", kinds(check_series("600001", df, raw_close=raw)))

    def test_price_rounding_does_not_create_ex_days(self):
        # 价格按两位小数舍入,只有第 30 天一个真实除权日
        import random
        rnd = random.Random(7)
        n, ex = 60, 30
        days = weekdays(date(2023, 1, 2), n)
        idx = pd.DatetimeIndex(days)
        raw_v, hfq_v, p = [], [], 6.0
        for i in range(n):
            p *= 1 + rnd.uniform(-0.02, 0.02)
            f = 1.0 if i < ex else 1.1
            raw_v.append(round(p / f, 2))
            hfq_v.append(round(p, 2))
        raw, hfq = pd.Series(raw_v, index=idx), pd.Series(hfq_v, index=idx)
        self.assertEqual(infer_ex_days(hfq, raw), [days[ex]])
        self.assertNotIn("factor_decrease", kinds(check_adjustment(hfq, "x", raw_close=raw)))

    def test_empty_single_nan_do_not_raise(self):
        empty = pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        one = make().iloc[:1]
        nan = make()
        nan.iloc[:, :] = float("nan")
        for df in (empty, one, nan, None, pd.DataFrame()):
            check_series("600001", df, calendar=DAYS)
        self.assertEqual(check_series("600001", empty, calendar=DAYS), [])
        self.assertEqual(check_series("600001", one, calendar=DAYS), [])

    def test_string_index_and_cap(self):
        df = make()
        df.index = [d.isoformat() for d in DAYS]
        self.assertEqual(check_series("600001", df, calendar=DAYS), [])
        bad = make([10.0 if i % 2 else 30.0 for i in range(len(DAYS))])
        r = check_jumps(bad, "600001", th=Thresholds(max_reports_per_kind=3))
        self.assertEqual(kinds(r).count("price_jump"), 3)
        self.assertIn("price_jump_truncated", kinds(r))

    def test_summarize(self):
        s = summarize_issues(check_series("600001", self.build_bad(), calendar=DAYS))
        self.assertGreater(s["total"], 0)
        self.assertIn("error", s["by_severity"])

    def test_no_network_imports(self):
        code = ("import sys; sys.path.insert(0, %r); import src.dataquality.consistency; "
                "names = ('akshare','baostock','requests','urllib3','allmarket','datasource','scan'); "
                "print([m for m in sys.modules if m.split('.')[-1] in names])") % ROOT
        out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(out.stdout.strip(), "[]", out.stderr)


if __name__ == "__main__":
    unittest.main()
