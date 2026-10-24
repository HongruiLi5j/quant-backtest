"""report.py 与 scripts/validate_strategy.py 单测(T007 / S7):合成 csv + 临时目录,不读写项目数据目录,不联网。
运行:python3 -B tests/test_validation_report.py
"""
import contextlib
import datetime as dt
import io
import math
import os
import random
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.validation import adapter as ad
from src.validation import report as rp
from src.validation.trials import TrialLog

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import validate_strategy as cli  # noqa: E402

D = dt.date
SCRIPT = os.path.join(ROOT, "scripts", "validate_strategy.py")


def synth(n=900, seed=1, mu=0.0006, sd=0.01):
    rnd = random.Random(seed)
    start = D(2018, 1, 1)
    dates, d = [], start
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d += dt.timedelta(days=1)
    rets = [rnd.gauss(mu, sd) for _ in range(n)]
    turn = [abs(rnd.gauss(0.1, 0.05)) for _ in range(n)]
    return dates, rets, turn


def synth_trades(dates, k, seed=2):
    rnd = random.Random(seed)
    out = []
    for i in range(k):
        a = dates[i * 2]
        out.append({"entry_date": a, "exit_date": dates[i * 2 + 1], "ret": rnd.gauss(0.005, 0.03)})
    return out


def write_csv(path, dates, rets, turn=None, header=None):
    with open(path, "w", encoding="utf-8") as f:
        f.write(header or ("date,ret" + (",turnover" if turn else "")) + "\n")
        for i, (d, r) in enumerate(zip(dates, rets)):
            f.write(f"{d},{r!r}" + (f",{turn[i]!r}" if turn else "") + "\n")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.dates, self.rets, self.turn = synth()

    def p(self, name):
        return os.path.join(self.dir, name)

    # ---- 完整流程
    def test_full_report_sections(self):
        log = TrialLog(self.p("trial_log.jsonl"))
        rnd = random.Random(5)
        for i in range(25):
            log.record("s", {"a": i}, "sharpe_daily", rnd.gauss(0.03, 0.03), 900)
        trades = synth_trades(self.dates, 40)
        text, s = rp.build_report_from_arrays(self.dates, self.rets, trades=trades, turnover=self.turn,
                                              base_cost=0.0005, cost_included=True,
                                              params={"strategy": "s", "locked_open_count": 0}, trial_log=log)
        for h in ("数据与切分", "样本内 / 验证 / 锁定测试", "滚动前进", "成本压力", "试验次数与折扣",
                  "模拟盘对比", "自检清单", "警告"):
            self.assertIn(h, text)
        self.assertEqual(s["cost"]["status"], "ok")
        self.assertEqual(s["trials"]["n_trials"], 25)
        self.assertIsNotNone(s["trials"]["dsr"])
        self.assertGreater(s["walk_forward"]["n_folds"], 0)
        self.assertTrue(any("试验次数 25" in w for w in s["warnings"]))
        self.assertNotIn("未登记试验", "".join(s["warnings"]))
        self.assertIn("Deflated Sharpe", text)

    def test_disclaimer_present(self):
        text, s = rp.build_report_from_arrays(self.dates, self.rets)
        self.assertIn("仅供研究,不构成投资建议,回测与模拟结果不代表未来", text)
        self.assertIn(rp.DISCLAIMER, text.strip().splitlines()[-1])

    # ---- 仅收益
    def test_returns_only(self):
        text, s = rp.build_report_from_arrays(self.dates, self.rets)
        self.assertEqual(s["cost"]["status"], "unavailable")
        self.assertIn("无法检验", text)
        self.assertIn("成本压力未检验", "".join(s["warnings"]))
        self.assertNotIn("通过成本检验", text)
        self.assertIsNone(s["trades"]["count"])
        self.assertTrue(any("未登记试验" in w for w in s["warnings"]))
        self.assertEqual(s["segments"]["status"], "ok")

    def test_cost_three_tiers(self):
        _, a = rp.build_report_from_arrays(self.dates, self.rets, turnover=self.turn, base_cost=0.0005)
        self.assertEqual(a["cost"]["status"], "unavailable")
        self.assertIn("未声明收益是否已含费", a["cost"]["reason"])
        _, t = rp.build_report_from_arrays(self.dates, self.rets, turnover=self.turn, base_cost=0.0005,
                                           cost_included=True)
        _, f = rp.build_report_from_arrays(self.dates, self.rets, turnover=self.turn, base_cost=0.0005,
                                           cost_included=False)
        self.assertEqual(t["cost"]["status"], "ok")
        st = {(r["multiplier"], r["slip_bps"]): r["sharpe"] for r in t["cost"]["rows"]}
        sf = {(r["multiplier"], r["slip_bps"]): r["sharpe"] for r in f["cost"]["rows"]}
        self.assertNotAlmostEqual(st[(2, 0)], sf[(2, 0)], places=6)
        # cost_included=True 时 1x 且无额外滑点应还原为原收益
        base = rp._st.perf_stats(self.rets)["sharpe"]
        self.assertAlmostEqual(st[(1, 0)], base, places=9)
        # cost_inputs 覆盖 与 直接放进 record 一致
        rec0 = ad.from_returns(self.dates, self.rets)
        _, c = rp.build_report(rec0, cost_inputs={"turnover": self.turn, "base_cost": 0.0005, "cost_included": True})
        self.assertEqual(c["cost"]["rows"], t["cost"]["rows"])

    def test_cost_stress_hand_value(self):
        r = rp.cost_stress([0.01] * 3, [1.0] * 3, 0.001, False, multipliers=(2,), slip_bps=(0,))
        # (1.01)(1-0.002)-1 = 0.00798;常数序列 -> 累计收益 (1.00798)^3 - 1
        self.assertAlmostEqual(r["rows"][0]["cum_return"], (1.01 * 0.998) ** 3 - 1, places=12)
        r2 = rp.cost_stress([0.01] * 3, [1.0] * 3, 0.001, True, multipliers=(2,), slip_bps=(0,))
        self.assertAlmostEqual(r2["rows"][0]["cum_return"], (1.01 * 0.998 / 0.999) ** 3 - 1, places=12)
        with self.assertRaises(ValueError):
            rp.cost_stress([0.01], [1.0, 1.0], 0.001, True)
        with self.assertRaises(ValueError):
            rp.cost_stress([0.01], [1.0], -0.1, True)

    def test_cost_2x_sharpe_negative_warns(self):
        d, _, t = synth(400)
        rets = [0.0004] * 400
        rets[::7] = [0.0003] * len(rets[::7])
        tt = [1.0] * 400
        _, s = rp.build_report_from_arrays(d, rets, turnover=tt, base_cost=0.0005, cost_included=False)
        self.assertTrue(any("2 倍后夏普转负" in w for w in s["warnings"]))

    # ---- 交易笔数
    def test_few_trades_warning_and_checklist(self):
        _, s = rp.build_report_from_arrays(self.dates, self.rets, trades=synth_trades(self.dates, 10))
        self.assertTrue(any("交易笔数只有 10" in w for w in s["warnings"]))
        self.assertIn(("交易笔数 ≥ 30", "否", "10 笔"), s["checklist"])
        _, s2 = rp.build_report_from_arrays(self.dates, self.rets, trades=synth_trades(self.dates, 40))
        self.assertFalse(any("交易笔数只有" in w for w in s2["warnings"]))

    # ---- 其他警告与自检
    def test_short_sample_and_embargo_zero(self):
        d, r, _ = synth(120)
        _, s = rp.build_report_from_arrays(d, r, params={"embargo": 0})
        self.assertTrue(any("样本只有" in w for w in s["warnings"]))
        self.assertIn(("切分留了 embargo 隔离带", "否", "embargo=0;持仓/标签跨天时段间会泄漏信息"), s["checklist"])
        self.assertEqual(s["walk_forward"]["status"], "error")     # 120 根放不下默认窗口

    def test_locked_test_accessed(self):
        _, s = rp.build_report_from_arrays(self.dates, self.rets, params={"locked_open_count": 2})
        self.assertTrue(any("锁定测试段已被访问 2 次" in w for w in s["warnings"]))
        _, u = rp.build_report_from_arrays(self.dates, self.rets)
        self.assertTrue(any(c[0].startswith("锁定测试段") and c[1] == "未知" for c in u["checklist"]))

    def test_decay_warning(self):
        n = 900
        d, _, _ = synth(n)
        rnd = random.Random(3)
        rets = [rnd.gauss(0.003, 0.01) for _ in range(n // 2)] + [rnd.gauss(-0.001, 0.01) for _ in range(n - n // 2)]
        _, s = rp.build_report_from_arrays(d, rets)
        self.assertTrue(any("衰减" in w for w in s["warnings"]))

    def test_paper_days_warning(self):
        _, s = rp.build_report_from_arrays(self.dates, self.rets, params={"paper_days": 5})
        self.assertTrue(any("模拟盘覆盖仅 5 天" in w for w in s["warnings"]))

    def test_trial_log_empty_and_missing_var(self):
        log = TrialLog(self.p("trial_log.jsonl"))
        _, s = rp.build_report_from_arrays(self.dates, self.rets, trial_log=log)
        self.assertEqual(s["trials"]["n_trials"], 0)
        self.assertFalse(os.path.exists(log.path))                 # 读账本不会创建文件
        log.record("s", {"a": 1}, "sharpe_daily", 0.05, 900)
        text, s2 = rp.build_report_from_arrays(self.dates, self.rets, trial_log=log)
        self.assertIsNone(s2["trials"]["dsr"])
        self.assertIn("无法计算", text)

    # ---- 非法输入
    def test_invalid_inputs(self):
        with self.assertRaises(rp.ReportError):
            rp.build_report_from_arrays(self.dates, self.rets, params={"bogus": 1})
        with self.assertRaises(ad.AdapterError):
            rp.build_report_from_arrays(self.dates[:-1], self.rets)
        with self.assertRaises(ad.AdapterError):
            rp.build_report_from_arrays(self.dates, [-1.5] + self.rets[1:])
        with self.assertRaises(ad.AdapterError):
            rp.build_report(ad.from_trades(synth_trades(self.dates, 5)))     # 仅交易列表无逐日收益
        rec = ad.from_returns(self.dates, self.rets)
        with self.assertRaises(rp.ReportError):
            rp.build_report(rec, cost_inputs={"x": 1})

    # ---- write_report
    def test_write_report_refuses_overwrite(self):
        out = self.p("r.md")
        rp.write_report(out, "第一版\n")
        with self.assertRaises(rp.ReportError):
            rp.write_report(out, "第二版\n")
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "第一版\n")
        rp.write_report(out, "第二版\n", force=True)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), "第二版\n")
        with self.assertRaises(rp.ReportError):
            rp.write_report(self.p("nodir/x.md"), "x")
        with self.assertRaises(rp.ReportError):
            rp.write_report(self.dir, "x")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.dates, self.rets, self.turn = synth()
        self.csv = os.path.join(self.dir, "ret.csv")
        write_csv(self.csv, self.dates, self.rets, self.turn)

    def run_cli(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(list(args))
        return code, err.getvalue()

    def test_full_flow_and_refuse_overwrite(self):
        tr = os.path.join(self.dir, "trades.csv")
        with open(tr, "w", encoding="utf-8") as f:
            f.write("entry_date,exit_date,ret\n")
            for t in synth_trades(self.dates, 35):
                f.write(f"{t['entry_date']},{t['exit_date']},{t['ret']!r}\n")
        out = os.path.join(self.dir, "report.md")
        code, _ = self.run_cli("--returns", self.csv, "--trades", tr, "--turnover-col", "turnover",
                               "--base-cost", "0.0005", "--cost-included", "--out", out)
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as f:
            text = f.read()
        self.assertIn(rp.DISCLAIMER, text)
        self.assertIn("2x", text)
        self.assertNotIn("无法检验:缺少", text.split("## 4.")[1].split("## 5.")[0])
        code, err = self.run_cli("--returns", self.csv, "--out", out)
        self.assertEqual(code, 2)
        self.assertIn("拒绝覆盖", err)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)
        code, _ = self.run_cli("--returns", self.csv, "--out", out, "--force")
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as f:
            self.assertIn("缺少换手", f.read())                     # 缺 trades/成本输入时降级

    def test_force_cannot_overwrite_input(self):
        code, err = self.run_cli("--returns", self.csv, "--out", self.csv, "--force")
        self.assertEqual(code, 2)
        self.assertIn("不能与输入文件相同", err)

    def test_bad_inputs(self):
        out = os.path.join(self.dir, "x.md")
        code, err = self.run_cli("--returns", os.path.join(self.dir, "nope.csv"), "--out", out)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out))
        bad = os.path.join(self.dir, "bad.csv")
        write_csv(bad, self.dates[:5], [0.01, "abc", 0.0, 0.0, 0.0])
        self.assertEqual(self.run_cli("--returns", bad, "--out", out)[0], 2)
        pct = os.path.join(self.dir, "pct.csv")
        write_csv(pct, self.dates[:50], [150.0] * 50)
        out2 = os.path.join(self.dir, "pct.md")
        self.assertEqual(self.run_cli("--returns", pct, "--out", out2)[0], 0)     # 不拒绝,但必须警告
        with open(out2, encoding="utf-8") as f:
            self.assertIn("请确认收益是小数", f.read())
        self.assertEqual(self.run_cli("--returns", self.csv, "--out", out, "--turnover-col", "zzz")[0], 2)
        self.assertEqual(self.run_cli("--returns", self.csv, "--out", out, "--trial-log",
                                      os.path.join(self.dir, "none.jsonl"))[0], 2)
        self.assertFalse(os.path.exists(out))
        with self.assertRaises(SystemExit):                       # --cost-included 与 --gross 互斥
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main(["--returns", self.csv, "--out", out, "--cost-included", "--gross"])

    def test_subprocess_entry(self):
        out = os.path.join(self.dir, "sub.md")
        r = subprocess.run([sys.executable, "-B", SCRIPT, "--returns", self.csv, "--out", out],
                           capture_output=True, text=True, cwd=self.dir, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(out))
        r2 = subprocess.run([sys.executable, "-B", SCRIPT, "--returns", self.csv, "--out", out],
                            capture_output=True, text=True, cwd=self.dir, timeout=120)
        self.assertEqual(r2.returncode, 2)


if __name__ == "__main__":
    unittest.main()
