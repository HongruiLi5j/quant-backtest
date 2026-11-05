"""scripts/data_freshness.py 的测试:临时目录 + 合成 parquet,不读写 data/ 下的真实文件。

运行:.venv/bin/python -B tests/test_data_freshness.py
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.dataquality.consistency import Thresholds, check_calendar_gaps  # noqa: E402
from src.dataquality.report import BEIJING  # noqa: E402

SCRIPT = os.path.join(ROOT, "scripts", "data_freshness.py")
_spec = importlib.util.spec_from_file_location("data_freshness", SCRIPT)
df_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(df_mod)

END = date(2026, 10, 21)
AFTER_CLOSE = datetime(2026, 10, 22, 16, 0, tzinfo=BEIJING)
BEFORE_CLOSE = datetime(2026, 10, 22, 10, 0, tzinfo=BEIJING)
TODAY = date(2026, 10, 22)


def weekdays(end, n):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


CAL = weekdays(END, 80)           # 合成的「全市场交易日」,最后一天是 END


def write_stock(d, code, days):
    df = pd.DataFrame({"Open": 10.0, "High": 10.1, "Low": 9.9, "Close": 10.0, "Volume": 1e6},
                      index=pd.to_datetime(sorted(days)))
    df.to_parquet(Path(d) / f"{code}.parquet")


def snapshot(d):
    out = {}
    for p in sorted(Path(d).rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def d64(days):
    return np.array(sorted(days), dtype="datetime64[D]")


class PureTest(unittest.TestCase):
    def test_up_to_date_has_no_gaps(self):
        f = df_mod.freshness_of(d64(CAL), d64(CAL))
        self.assertEqual((f["tail_missing"], f["mid_missing"], f["mid_longest"], f["longest"]), (0, 0, 0, 0))
        self.assertEqual(f["n_bars"], 80)

    def test_tail_gap(self):
        f = df_mod.freshness_of(d64(CAL[:-12]), d64(CAL))
        self.assertEqual(f["tail_missing"], 12)
        self.assertEqual(f["last"], CAL[-13].isoformat())
        self.assertEqual(f["longest"], 12)

    def test_mid_gap_length_and_start(self):
        have = CAL[:20] + CAL[35:]          # 缺 CAL[20:35] 共 15 个交易日
        f = df_mod.freshness_of(d64(have), d64(CAL))
        self.assertEqual(f["mid_longest"], 15)
        self.assertEqual(f["mid_longest_start"], CAL[20].isoformat())
        self.assertEqual(f["mid_missing"], 15)
        self.assertEqual(f["tail_missing"], 0)

    def test_leading_missing_not_counted(self):
        """新上市:首根 K 线之前不算缺失。"""
        f = df_mod.freshness_of(d64(CAL[50:]), d64(CAL))
        self.assertEqual((f["mid_missing"], f["tail_missing"]), (0, 0))

    def test_lookback_separates_old_gap(self):
        have = CAL[:10] + CAL[30:]          # 缺口 20 日,在日历最前面,K 线于 CAL[30] 恢复
        cal = d64(CAL)
        old = df_mod.freshness_of(d64(have), cal, lookback=30)    # 窗口只含 CAL[50:],缺口在窗口前
        self.assertEqual(old["mid_longest"], 20)
        self.assertEqual(old["mid_recent_longest"], 0)
        self.assertEqual(df_mod.classify(old, False, 10), df_mod.CAT_HIST)
        new = df_mod.freshness_of(d64(have), cal, lookback=60)    # 窗口含 CAL[20:],K 线恢复在窗口内
        self.assertEqual(new["mid_recent_longest"], 20)
        self.assertEqual(df_mod.classify(new, False, 10), df_mod.CAT_MID)
        whole = df_mod.freshness_of(d64(have), cal, lookback=0)
        self.assertEqual(whole["mid_recent_longest"], 20)

    def test_classify_threshold_is_strictly_greater(self):
        f10 = df_mod.freshness_of(d64(CAL[:-10]), d64(CAL))
        f11 = df_mod.freshness_of(d64(CAL[:-11]), d64(CAL))
        self.assertEqual(df_mod.classify(f10, False, 10), df_mod.CAT_LAG)
        self.assertEqual(df_mod.classify(f11, False, 10), df_mod.CAT_TAIL_OUT)
        self.assertEqual(df_mod.classify(f11, True, 10), df_mod.CAT_TAIL_SUSP)

    def test_agrees_with_dataquality_calendar_gaps(self):
        """中间缺口长度与 src/dataquality 的 check_calendar_gaps 一致(同一日历、同一段缺失)。"""
        have = CAL[:20] + CAL[35:]
        df = pd.DataFrame({"Open": 10.0, "High": 10.1, "Low": 9.9, "Close": 10.0, "Volume": 1e6},
                          index=pd.to_datetime(have))
        issues = check_calendar_gaps(df, CAL, "X", th=Thresholds(max_gap_days=0))
        runs = [int(i.value) for i in issues if i.kind == "missing_trading_days"]
        self.assertEqual(runs, [df_mod.freshness_of(d64(have), d64(CAL))["mid_longest"]])

    def test_union_calendar(self):
        u = df_mod.union_calendar([d64(CAL[:10]), d64(CAL[5:20]), np.array([], dtype="datetime64[D]")])
        self.assertEqual(list(u.astype(str)), [d.isoformat() for d in CAL[:20]])
        self.assertEqual(len(df_mod.union_calendar([])), 0)


class ScanTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.daily = self.tmp / "daily"
        self.cn = self.daily / "cn_all"
        self.cn.mkdir(parents=True)
        write_stock(self.cn, "000001", CAL)                         # 正常
        write_stock(self.cn, "000002", CAL[:-3])                    # 短期滞后 3 日
        write_stock(self.cn, "000003", CAL[:-15])                   # 末尾断更 15 日,不在停牌名单
        write_stock(self.cn, "000004", CAL[:-20])                   # 末尾断更 20 日,在停牌名单
        write_stock(self.cn, "000005", CAL[:30] + CAL[50:])         # 中间缺口 20 日
        write_stock(self.cn, "000006", CAL[40:])                    # 新上市,正常
        (self.daily / "cn_all_suspended.json").write_text(
            json.dumps({CAL[-1].isoformat(): ["000004", "000002"]}), encoding="utf-8")
        (self.daily / "cn_all_list.csv").write_text(
            "code,name,ipo,industry,board\n000001,甲,2000-01-01,x,深市主板\n000002,乙,2000-01-01,x,深市主板\n",
            encoding="utf-8")
        (self.daily / "cn_trade_calendar.csv").write_text(
            "trade_date\n" + "\n".join(d.isoformat() for d in CAL) + "\n", encoding="utf-8")

    def tearDown(self):
        self._t.cleanup()

    def scan(self, **kw):
        kw.setdefault("now", AFTER_CLOSE)
        kw.setdefault("today", TODAY)
        return df_mod.scan(self.cn, **kw)

    def test_categories(self):
        r = self.scan(lookback=0)
        cat = {c: s["category"] for c, s in r["stocks"].items()}
        self.assertEqual(cat, {"000001": "ok", "000002": "lag_short", "000003": df_mod.CAT_TAIL_OUT,
                               "000004": df_mod.CAT_TAIL_SUSP, "000005": df_mod.CAT_MID, "000006": "ok"})
        self.assertEqual(r["latest_day"], END.isoformat())
        self.assertEqual(r["at_latest"], 3)
        self.assertEqual(r["last_date_dist"][CAL[-4].isoformat()], 1)
        self.assertEqual(r["stocks"]["000004"]["tail_in_suspend_days"], 1)    # 名单只覆盖最新交易日
        self.assertEqual(r["stocks"]["000005"]["mid_recent_longest"], 20)
        self.assertEqual(r["stocks"]["000003"]["name"], "")                   # 不在 list.csv:名称为空

    def test_suspend_check_and_calendar_check(self):
        r = self.scan()
        sc = r["suspend_check"]
        self.assertEqual(sc["missing_latest"], 3)                 # 000002/3/4 最新交易日没 K 线
        self.assertEqual(sc["missing_in_list"], 2)                # 000002 和 000004 在名单
        self.assertEqual(sc["missing_not_in_list"], ["000003"])
        self.assertEqual(sc["list_has_bar"], [])
        self.assertTrue(r["calendar_check"]["official_available"])
        self.assertEqual(r["calendar_check"]["union_only_n"], 0)
        self.assertEqual(r["calendar_check"]["official_only_n"], 0)
        lv = r["list_vs_files"]
        self.assertEqual((lv["listed"], lv["files"], lv["file_not_listed"]), (2, 6, 4))

    def test_problems_sorted_by_longest(self):
        r = self.scan(lookback=0)
        self.assertEqual([c for c, _ in df_mod.problems(r)], ["000004", "000005", "000003"])

    def test_intraday_bar_dropped_before_1530(self):
        """北京时间 15:30 前,当天的 K 线不用:000001 多出的当天 K 线应被忽略,不能把日历推到今天。"""
        write_stock(self.cn, "000001", CAL + [TODAY])
        early = self.scan(now=BEFORE_CLOSE)
        self.assertEqual(early["latest_day"], END.isoformat())
        self.assertEqual(early["intraday_dropped_count"], 1)
        late = self.scan(now=AFTER_CLOSE)
        self.assertEqual(late["latest_day"], TODAY.isoformat())
        self.assertEqual(late["at_latest"], 1)

    def test_as_of_truncates(self):
        cut = CAL[-30]
        r = self.scan(as_of=cut)
        self.assertEqual(r["latest_day"], cut.isoformat())
        self.assertTrue(all(s["last"] <= cut.isoformat() for s in r["stocks"].values()))

    def test_unreadable_file_does_not_abort(self):
        (self.cn / "999999.parquet").write_bytes(b"not a parquet")
        r = self.scan()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["scanned"], 6)
        self.assertEqual([n for n, _ in r["unreadable"]], ["999999.parquet"])

    def test_empty_dir_is_no_data(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        r = df_mod.scan(empty, now=AFTER_CLOSE, today=TODAY)
        self.assertEqual(r["status"], "no_data")

    def test_scan_is_read_only(self):
        before = snapshot(self.tmp)
        self.scan()
        self.assertEqual(snapshot(self.tmp), before)

    def test_write_outputs_and_refuse_inside_data_dir(self):
        r = self.scan(lookback=0)
        out = self.tmp / "out"
        w = df_mod.write_outputs(r, out, top_n=2)
        self.assertTrue(w["ok"], w)
        md = (out / "data_freshness.md").read_text(encoding="utf-8")
        self.assertIn("汇总表", md)
        self.assertIn("000004", md)
        self.assertNotIn("| 000003 |", md)                       # top_n=2:第 3 名不在表里
        js = json.loads((out / "data_freshness.json").read_text(encoding="utf-8"))
        self.assertEqual([p["code"] for p in js["problems"]], ["000004", "000005", "000003"])
        self.assertTrue((out / "data_freshness_problems.csv").is_file())
        bad = df_mod.write_outputs(r, self.daily / "reports")
        self.assertFalse(bad["ok"])
        self.assertFalse((self.daily / "reports").exists())

    def test_cli_end_to_end(self):
        out = self.tmp / "cli_out"
        before = snapshot(self.cn)
        p = subprocess.run([sys.executable, "-B", SCRIPT, "--data-dir", str(self.cn), "--out-dir", str(out),
                            "--date", TODAY.isoformat(), "--lookback", "0"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("状态=ok", p.stdout)
        self.assertTrue((out / "data_freshness.md").is_file())
        self.assertEqual(snapshot(self.cn), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
