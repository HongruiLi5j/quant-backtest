"""src/dataquality/report.py 与 scripts/data_quality_report.py 的测试:临时目录 + 合成 parquet。

运行:.venv/bin/python -B tests/test_dataquality_report.py
"""
import hashlib
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

from src.dataquality.report import BEIJING, render_markdown, run, scan_dir, write_report  # noqa: E402

TODAY = date(2026, 10, 22)
AFTER_CLOSE = datetime(2026, 10, 22, 16, 0, tzinfo=BEIJING)
BEFORE_CLOSE = datetime(2026, 10, 22, 10, 0, tzinfo=BEIJING)


def weekdays(end, n):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def make_df(end=date(2026, 10, 21), n=60, bad=False, seed=1):
    days = weekdays(end, n)
    rng = np.random.default_rng(seed)
    c = 10 * np.cumprod(1 + rng.normal(0, 0.005, n))
    df = pd.DataFrame({"Open": c, "High": c * 1.005, "Low": c * 0.995, "Close": c,
                       "Volume": np.full(n, 1e6)}, index=pd.to_datetime(days))
    if bad:
        df.iloc[30, df.columns.get_loc("High")] = df.iloc[30]["Low"] * 0.5  # 高<低
        df.iloc[40, df.columns.get_loc("Close")] = df.iloc[39]["Close"] * 1.8  # 超涨停
    return df


def snapshot(d):
    out = {}
    for p in sorted(Path(d).rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


class ReportTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.data = self.tmp / "daily" / "cn_all"
        self.data.mkdir(parents=True)
        self.out = self.tmp / "out" / "dq"

    def tearDown(self):
        self._t.cleanup()

    def put(self, code, df):
        df.to_parquet(self.data / f"{code}.parquet")

    def fill(self, n_good=4):
        for i in range(n_good):
            self.put(f"60000{i}", make_df(seed=i))
        self.put("300001", make_df(bad=True, seed=9))

    def test_generate_report(self):
        self.fill()
        before = snapshot(self.tmp / "daily")
        res = scan_dir(self.data, today=TODAY, now=AFTER_CLOSE)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.files_total, 5)
        self.assertEqual(res.scanned, 5)
        self.assertEqual(res.latest_day, "2026-10-21")
        self.assertIn("300001", res.stocks)
        self.assertGreater(res.stocks["300001"]["error"], 0)
        self.assertNotIn("600000", [c for c, s in res.stocks.items() if s["error"]])
        w = write_report(res, self.out)
        self.assertTrue(w["ok"], w)
        md = (self.out / "data_quality.md").read_text(encoding="utf-8")
        self.assertIn("数据质量日报 2026-10-22", md)
        self.assertIn("300001", md)
        self.assertIn("没有找到更早的日报", md)
        js = json.loads((self.out / "data_quality.json").read_text(encoding="utf-8"))
        self.assertIn("300001", js["flagged"])
        self.assertTrue((self.out / "history" / "2026-10-22.json").exists())
        # 数据目录一个字节都没变,报告也没写进数据目录
        self.assertEqual(before, snapshot(self.tmp / "daily"))

    def test_empty_dir(self):
        res = scan_dir(self.data, today=TODAY, now=AFTER_CLOSE)
        self.assertEqual(res.status, "no_data")
        w = write_report(res, self.out)
        self.assertTrue(w["ok"])
        md = (self.out / "data_quality.md").read_text(encoding="utf-8")
        self.assertIn("无数据", md)

    def test_missing_dir(self):
        out = run(self.tmp / "nope", self.out, today=TODAY, now=AFTER_CLOSE)
        self.assertEqual(out["status"], "no_data")
        self.assertTrue(out["write"]["ok"])

    def test_corrupt_file_does_not_stop(self):
        self.fill(3)
        (self.data / "600099.parquet").write_bytes(b"this is not parquet")
        res = scan_dir(self.data, today=TODAY, now=AFTER_CLOSE)
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.scanned, 4)
        self.assertEqual([n for n, _ in res.unreadable], ["600099.parquet"])
        w = write_report(res, self.out)
        self.assertTrue(w["ok"])
        self.assertIn("600099.parquet", (self.out / "data_quality.md").read_text(encoding="utf-8"))

    def test_sample_limits_and_reproducible(self):
        for i in range(12):
            self.put(f"60{i:04d}", make_df(seed=i))
        a = scan_dir(self.data, sample=5, seed=3, today=TODAY, now=AFTER_CLOSE)
        b = scan_dir(self.data, sample=5, seed=3, today=TODAY, now=AFTER_CLOSE)
        c = scan_dir(self.data, sample=5, seed=4, today=TODAY, now=AFTER_CLOSE)
        self.assertEqual(a.scanned, 5)
        self.assertEqual(a.files_total, 12)
        self.assertEqual(a.last_date_dist, b.last_date_dist)
        md = render_markdown(a)
        self.assertIn("抽样", md)
        self.assertIn("5/12", md)
        self.assertEqual(c.scanned, 5)

    def test_previous_day_comparison(self):
        self.fill()
        r1 = scan_dir(self.data, today=date(2026, 10, 21), now=AFTER_CLOSE - timedelta(days=1))
        write_report(r1, self.out)
        # 次日:300001 修好,600000 变坏
        self.put("300001", make_df(seed=9))
        self.put("600000", make_df(bad=True, seed=0))
        r2 = scan_dir(self.data, today=TODAY, now=AFTER_CLOSE)
        write_report(r2, self.out)
        md = (self.out / "data_quality.md").read_text(encoding="utf-8")
        self.assertIn("对比对象:2026-10-21", md)
        self.assertIn("新增异常股票 1 只:600000", md)
        self.assertIn("消失异常股票 1 只:300001", md)

    def test_intraday_bar_ignored_before_1530(self):
        df = make_df(end=TODAY, n=40)
        self.put("600000", df)
        early = scan_dir(self.data, today=TODAY, now=BEFORE_CLOSE)
        late = scan_dir(self.data, today=TODAY, now=AFTER_CLOSE)
        self.assertTrue(early.intraday_dropped)
        self.assertEqual(early.intraday_dropped_count, 1)
        self.assertEqual(early.latest_day, "2026-10-21")
        self.assertEqual(late.latest_day, "2026-10-22")
        self.assertIn("盘中", render_markdown(early))

    def test_stale_stocks_counted(self):
        self.put("600000", make_df())
        self.put("600001", make_df(end=date(2026, 10, 19)))
        res = scan_dir(self.data, today=TODAY, now=AFTER_CLOSE)
        self.assertEqual(res.latest_day, "2026-10-21")
        self.assertEqual(res.stale, 1)

    def test_refuse_output_inside_data_dir(self):
        self.fill(1)
        res = scan_dir(self.data, today=TODAY, now=AFTER_CLOSE)
        before = snapshot(self.data)
        w = write_report(res, self.data / "reports")
        self.assertFalse(w["ok"])
        self.assertEqual(before, snapshot(self.data))

    def test_parent_dir_accepted(self):
        self.fill(2)
        res = scan_dir(self.tmp / "daily", today=TODAY, now=AFTER_CLOSE)
        self.assertEqual(res.scanned, 3)

    def test_aux_files_used(self):
        self.fill(1)
        daily = self.tmp / "daily"
        (daily / "cn_all_list.csv").write_text("code,name,ipo,industry,board\n600000,A,2000-01-01,x,主板\n",
                                               encoding="utf-8")
        cal = [d.isoformat() for d in weekdays(date(2026, 10, 21), 60)]
        (daily / "cn_trade_calendar.csv").write_text("trade_date\n" + "\n".join(cal) + "\n", encoding="utf-8")
        res = scan_dir(self.data, today=TODAY, now=AFTER_CLOSE)
        self.assertEqual(res.list_count, 1)
        self.assertTrue(res.calendar_used)

    def test_max_seconds_marks_timeout(self):
        self.fill()
        res = scan_dir(self.data, today=TODAY, now=AFTER_CLOSE, max_seconds=-1)
        self.assertTrue(res.timed_out)
        w = write_report(res, self.out)
        self.assertTrue(w["ok"])
        self.assertTrue((self.out / "history").is_dir())
        self.assertEqual(list((self.out / "history").glob("*.json")), [])

    def test_cli_returns_zero_even_on_failure(self):
        self.fill(1)
        script = os.path.join(ROOT, "scripts", "data_quality_report.py")
        p = subprocess.run([sys.executable, "-B", script, "--data-dir", str(self.data),
                            "--out-dir", str(self.out), "--date", "2026-10-22", "--sample", "1"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((self.out / "data_quality.md").exists())
        # 输出目录非法(位于数据目录内)也只记录、退出码 0
        p2 = subprocess.run([sys.executable, "-B", script, "--data-dir", str(self.data),
                             "--out-dir", str(self.data / "x")],
                            capture_output=True, text=True, timeout=120)
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertIn("失败", p2.stdout)
        # 完全不存在的数据目录
        p3 = subprocess.run([sys.executable, "-B", script, "--data-dir", str(self.tmp / "zzz"),
                             "--out-dir", str(self.out)], capture_output=True, text=True, timeout=120)
        self.assertEqual(p3.returncode, 0, p3.stderr)

    def test_no_network_imports(self):
        code = ("import sys; sys.path.insert(0, %r); import src.dataquality.report; "
                "bad=[m for m in ('akshare','baostock','requests','urllib3','src.allmarket','src.datasource','src.scan') "
                "if m in sys.modules]; print(bad)" % ROOT)
        p = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.stdout.strip(), "[]", p.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=0)
