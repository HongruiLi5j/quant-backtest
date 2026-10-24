"""验证工具包端到端测试(T007 / S8):确定性合成数据 + 临时目录,不读写项目数据目录,不联网。
运行:python3 -B tests/test_validation_e2e.py

演示两个对照场景:
  A. 纯噪声策略被大量试验后挑出"最好的那个":原始 p 值看着显著,折扣后不再显著。
  B. 有真实漂移的合成序列:折扣后依然显著。
再串起 切分 -> 锁定段守卫 -> 滚动前进 -> 成本压力 -> 试验折扣 -> 模拟盘对比 -> 报告 -> 命令行。
"""
import datetime as dt
import os
import random
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from src.validation import adapter as ad
from src.validation import paper
from src.validation import report as rp
from src.validation import splits as sp
from src.validation import trials as tr

SCRIPT = os.path.join(ROOT, "scripts", "validate_strategy.py")
N_NOISE_TRIALS = 300
N_DAYS = 1000


def weekdays(n, start=dt.date(2018, 1, 1)):
    """从 start 起的 n 个工作日(不含节假日,合成用)。"""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def gauss_series(n, seed, mu=0.0, sd=0.01):
    rnd = random.Random(seed)
    return [mu + sd * rnd.gauss(0, 1) for _ in range(n)]


def pick_best_noise(log_path, n_trials=N_NOISE_TRIALS, n_days=N_DAYS):
    """跑 n_trials 个纯噪声'策略',全部登记进账本,返回夏普最高的那条收益序列。"""
    log = tr.TrialLog(log_path)
    best, best_sr = None, float("-inf")
    for k in range(n_trials):
        rets = gauss_series(n_days, seed=1000 + k)
        sr = tr.sharpe(rets)
        log.record("noise", {"k": k}, "sharpe_daily", sr, n_days)
        if sr > best_sr:
            best, best_sr = rets, sr
    return log, best, best_sr


class NoiseVsDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dates = weekdays(N_DAYS)

    def test_noise_best_of_many_not_significant_after_discount(self):
        log, best, best_sr = pick_best_noise(os.path.join(self.tmp.name, "trial_log.jsonl"))
        self.assertEqual(log.count("noise"), N_NOISE_TRIALS)
        text, s = rp.build_report_from_arrays(self.dates, best, params={"strategy": "noise"}, trial_log=log)
        t = s["trials"]
        self.assertEqual(t["n_trials"], N_NOISE_TRIALS)
        # 单看这一条,似乎显著(这正是挑最好者的陷阱)
        self.assertLess(t["p_raw"], 0.05)
        # 折扣之后不再显著
        self.assertGreater(t["p_bonferroni"], 0.05)
        self.assertGreater(t["p_sidak"], 0.05)
        self.assertIsNotNone(t["dsr"])
        self.assertLess(t["dsr"], 0.95)
        # 报告含试验次数警告,且不出现"策略有效"类结论
        self.assertTrue(any("试验次数" in w for w in s["warnings"]))
        for bad in ("通过成本检验", "可以实盘", "显著有效"):
            self.assertNotIn(bad, text)
        self.assertIn("不判定策略有效", text)
        self.assertIn(rp.DISCLAIMER, text)

    def test_noise_dsr_monotone_in_trial_count(self):
        _, best, _ = pick_best_noise(os.path.join(self.tmp.name, "trial_log.jsonl"), n_trials=60, n_days=500)
        mo = tr.sample_moments(best)
        sr = tr.sharpe(best)
        dsrs = [tr.deflated_sharpe(sr, len(best), mo["skew"], mo["kurt"], n, 1.0 / 500) for n in (2, 10, 100, 1000)]
        self.assertEqual(dsrs, sorted(dsrs, reverse=True))

    def test_real_drift_survives_discount(self):
        n = 1500
        dates = weekdays(n)
        rets = gauss_series(n, seed=7, mu=0.0012, sd=0.01)
        log = tr.TrialLog(os.path.join(self.tmp.name, "trial_log.jsonl"))
        # 诚实登记:这个漂移序列只试了 5 组参数,另外 4 组是噪声
        log.record("drift", {"k": 0}, "sharpe_daily", tr.sharpe(rets), n)
        for k in range(1, 5):
            log.record("drift", {"k": k}, "sharpe_daily", tr.sharpe(gauss_series(n, seed=50 + k)), n)
        text, s = rp.build_report_from_arrays(dates, rets, params={"strategy": "drift"}, trial_log=log)
        t = s["trials"]
        self.assertEqual(t["n_trials"], 5)
        self.assertLess(t["p_bonferroni"], 0.01)
        self.assertGreater(t["dsr"], 0.95)
        # 即便通过,报告也只说"没能证伪",且常驻警告仍在
        self.assertIn("没能证伪", text)
        self.assertIn("回测好看不等于有效", text)

    def test_report_deterministic(self):
        rets = gauss_series(800, seed=3, mu=0.0005)
        dates = weekdays(800)
        a, _ = rp.build_report_from_arrays(dates, rets)
        b, _ = rp.build_report_from_arrays(dates, rets)
        self.assertEqual(a, b)


class FullPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.n = 1200
        self.dates = weekdays(self.n)
        self.rets = gauss_series(self.n, seed=11, mu=0.0008)
        # 合成换手:每 10 天换一次仓,换手 1,其余 0
        self.turn = [1.0 if i % 10 == 0 else 0.0 for i in range(self.n)]
        self.base = 0.0005

    def test_split_guard_and_report(self):
        split = sp.three_way_split(self.dates, train=0.6, valid=0.2, embargo=5)
        s0, e0 = split.test
        fp = sp.locked_fingerprint(self.dates[s0:e0], self.rets[s0:e0])
        log_path = os.path.join(self.tmp.name, "locked_access_log.jsonl")
        guard = sp.LockedTestGuard(split, log_path, fp)
        self.assertEqual(guard.open_count(), 0)
        # 锁定段先不碰:报告自检应显示"未被访问"
        rec = ad.from_returns(self.dates, self.rets, turnover=self.turn, base_cost=self.base, cost_included=True)
        text, s = rp.build_report(rec, {"locked_open_count": guard.open_count()})
        self.assertEqual(s["segments"]["status"], "ok")
        self.assertEqual(s["walk_forward"]["status"], "ok")
        self.assertEqual(s["cost"]["status"], "ok")
        for sec in ("数据与切分", "样本内 / 验证 / 锁定测试", "滚动前进", "成本压力", "试验次数与折扣",
                    "模拟盘对比", "自检清单", "警告"):
            self.assertIn(sec, text)
        # 成本放大后收益不应更高(单调)
        sh = [r["sharpe"] for r in s["cost"]["rows"] if r["slip_bps"] == 0]
        self.assertEqual(sh, sorted(sh, reverse=True))
        # 看一次锁定段:留痕;同标签第二次被拒;改数据会被检出
        self.assertEqual(guard.open("final"), split.test)
        with self.assertRaises(sp.LockedTestError):
            sp.LockedTestGuard(split, log_path, fp).open("final")
        self.assertEqual(sp.LockedTestGuard(split, log_path, fp).open_count(), 1)
        tampered = list(self.rets)
        tampered[s0] += 0.01
        with self.assertRaises(sp.LockedTestError):
            sp.LockedTestGuard(split, log_path, sp.locked_fingerprint(self.dates[s0:e0], tampered[s0:e0]))
        # 访问后报告警告
        _, s2 = rp.build_report(rec, {"locked_open_count": 1})
        self.assertTrue(any("锁定测试段已被访问" in w for w in s2["warnings"]))

    def test_cost_declaration_matters(self):
        _, s_inc = rp.build_report_from_arrays(self.dates, self.rets, turnover=self.turn, base_cost=self.base,
                                               cost_included=True)
        _, s_gross = rp.build_report_from_arrays(self.dates, self.rets, turnover=self.turn, base_cost=self.base,
                                                 cost_included=False)
        _, s_none = rp.build_report_from_arrays(self.dates, self.rets, turnover=self.turn, base_cost=self.base)
        self.assertEqual(s_none["cost"]["status"], "unavailable")
        self.assertTrue(any("成本压力未检验" in w for w in s_none["warnings"]))
        self.assertNotEqual(s_inc["cost"]["rows"][0]["sharpe"], s_gross["cost"]["rows"][0]["sharpe"])

    def test_paper_journal_vs_backtest(self):
        cal = self.dates[:5]
        bt = {d: r for d, r in zip(cal, self.rets[:5])}
        db = os.path.join(self.tmp.name, "paper_journal.db")
        j = paper.PaperJournal(db)
        try:
            sid = j.add_signal(cal[0], "000001", "buy", 10.00, "e2e")
            j.add_fill(sid, 10.02, 1000, f"{cal[0]} 09:31:00", fee=5.0)
            for d, px in zip(cal, (10.02, 10.50, 10.20, 10.80, 10.70)):
                j.add_mark(d, "000001", px, "synthetic")
            rep = j.report(cal[0], cal[-1], 100000.0, bt, cal)
        finally:
            j.close()
        self.assertEqual(rep["n_filled"], 1)
        self.assertEqual(rep["n_days"], 5)
        self.assertAlmostEqual(rep["avg_slippage"], 0.002, places=9)
        # 无成交的第 2~5 天净值仍随估值价变化
        self.assertAlmostEqual(rep["equity"][str(cal[1])] - rep["equity"][str(cal[0])], 1000 * (10.50 - 10.02), places=6)
        self.assertEqual(rep["n_compared"], 5)
        self.assertIsNotNone(rep["tracking_error"])
        self.assertTrue(any("< 20" in w for w in rep["warnings"]))
        # 模拟盘天数写入报告警告
        rec = ad.from_returns(self.dates, self.rets)
        _, s = rp.build_report(rec, {"paper_days": rep["n_days"]})
        self.assertTrue(any("模拟盘覆盖仅 5 天" in w for w in s["warnings"]))


class CommandLine(unittest.TestCase):
    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            n = 900
            dates = weekdays(n)
            rets = gauss_series(n, seed=5, mu=0.0007)
            csv = os.path.join(tmp, "ret.csv")
            with open(csv, "w", encoding="utf-8") as f:
                f.write("date,ret,turnover\n")
                for i, (d, r) in enumerate(zip(dates, rets)):
                    f.write(f"{d},{r:.8f},{1.0 if i % 10 == 0 else 0.0}\n")
            out = os.path.join(tmp, "report.md")
            cmd = [sys.executable, "-B", SCRIPT, "--returns", csv, "--out", out, "--turnover-col", "turnover",
                   "--base-cost", "0.0005", "--gross"]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=tmp)
            self.assertEqual(p.returncode, 0, p.stderr)
            with open(out, encoding="utf-8") as f:
                text = f.read()
            self.assertIn("策略验证报告", text)
            self.assertIn(rp.DISCLAIMER, text)
            # 第二次不加 --force 拒绝覆盖,退出码 2,原文件不变
            p2 = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=tmp)
            self.assertEqual(p2.returncode, 2)
            with open(out, encoding="utf-8") as f:
                self.assertEqual(f.read(), text)
            # 只给收益、不给含费声明 -> 成本压力"无法检验"
            out2 = os.path.join(tmp, "report2.md")
            p3 = subprocess.run([sys.executable, "-B", SCRIPT, "--returns", csv, "--out", out2],
                                capture_output=True, text=True, timeout=120, cwd=tmp)
            self.assertEqual(p3.returncode, 0, p3.stderr)
            with open(out2, encoding="utf-8") as f:
                self.assertIn("无法检验", f.read())
            # 仓库 data/ 目录未被脚本创建文件:临时目录只含我们自己的文件
            self.assertEqual(sorted(os.listdir(tmp)), ["report.md", "report2.md", "ret.csv"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
