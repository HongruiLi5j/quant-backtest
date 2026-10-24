"""通宵分析任务跑手:试跑(--preflight)和正式跑(--run)共用同一份任务定义。

用法(项目根目录,Mac 上):
  caffeinate -i .venv/bin/python scripts/night_runner.py --preflight     试跑:每个任务用小参数跑一遍,全绿才允许正式跑
  caffeinate -i .venv/bin/python scripts/night_runner.py --run           正式跑:两条通道并行,直到全部任务结束,不自动停
  caffeinate -i .venv/bin/python scripts/night_runner.py --run --force-fundamental   T014 S5 没过也强行跑基本面
  .venv/bin/python scripts/night_runner.py --status                      看当前进度(随时可看)

吸取前两晚的教训(逐条对应):
  1. 不用系统 timeout 命令,全部用 subprocess.run(timeout=) 包装,每个任务独立时限,超时记 124 继续下一个。
  2. 慢任务(回放、因子检查)放各自通道最后;试跑用 1/10 规模先测时长。
  3. 每个任务独立 try,失败只记日志,后面的任务照跑。
  4. 队列里没有只换参数的重复项。
  5. 开跑前先检查每个任务需要的输入文件,缺了标 MISSING 直接跳过,不浪费时间。
  6. 不用接力器,没有任何会等人的环节。
  7. 两条通道:CPU 通道(回放→因子检验)和网络通道(基本面抓取→导出→排名)并行,互不等待。
  8. 每个任务开始/结束各写一行带时间和 rc 的日志;每个任务一份独立日志;PROGRESS.md 每次状态变化都重写。
  9. 断点续跑:state.json 记录已 OK 的任务,重新启动时跳过。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "bin" / "python")
TODAY = dt.date.today().isoformat()
# 上一个 A 股交易日(周六凌晨启动 → 周五);如果脚本在别的日子启动,按需改这一个值
EOD_DAY = (dt.date.today() - dt.timedelta(days={5: 1, 6: 2, 0: 3}.get(dt.date.today().weekday(), 1))).isoformat()
VALID_END = "2026-03-31"          # 封存段起点前一天,任何回放/检验的 end 都不能超过它
DEADLINE = dt.datetime.combine(dt.date.today(), dt.time(10, 30))  # 过了这个点不再启动新任务(已在跑的不打断),保证不会无限拖到下午
INDUSTRY = ROOT / "data" / "industry_cn.json"
DAILY = ROOT / "data" / "daily" / "cn_all"
CODES = ROOT / "data" / "daily" / "cn_all_list.csv"
S14_STATE = ROOT / "handoff" / "auto" / "state_T014.json"


def out_dir(mode: str) -> Path:
    d = ROOT / "reports" / (f"preflight_{TODAY}" if mode == "preflight" else f"overnight_{TODAY}")
    (d / "logs").mkdir(parents=True, exist_ok=True)
    return d


def build_tasks(mode: str, with_fund: bool):
    """返回任务列表。每个任务:name, lane, cmd, timeout, requires(输入文件), produces(说明)。
    mode=preflight 时用小参数、短时限。"""
    o = out_dir(mode)
    pre = mode == "preflight"
    T = []

    def add(name, lane, cmd, timeout, requires=(), produces="", clean_dirs=()):
        T.append(dict(name=name, lane=lane, cmd=[PY, "-B"] + [str(c) for c in cmd],
                      timeout=timeout, requires=[str(r) for r in requires], produces=produces,
                      clean_dirs=[str(c) for c in clean_dirs]))

    # ---------- 快任务(两条通道都排在最前,保证早上至少有东西) ----------
    add("EOD-收盘分析", "cpu",
        ["scripts/run_eod_analysis.py", "--day", EOD_DAY, "--output", o / f"eod_{EOD_DAY}.md"],
        300, [DAILY], f"{EOD_DAY} 收盘后的市场状态、建议敞口、监控名单重合度")
    add("信号回填-90d", "cpu",
        ["scripts/signal_backfill.py", "--days", 10 if pre else 90, "--output", o / "signal_90d.md"],
        600, [DAILY], "最近 90 天每个信号日的信号数与事后表现")
    add("数据质量-全市场", "net",
        ["scripts/data_quality_report.py", "--out-dir", o / "data_quality", "--max-seconds", 120 if pre else 900]
        + (["--sample", "100"] if pre else []),
        1200, [DAILY], "全部日线文件的缺口、异常值、停牌清单")
    add("周报", "net",
        ["scripts/weekly_summary.py", "--out-dir", o / "weekly"],
        120, [], "上一个完整周的账本周报")

    # ---------- CPU 通道:正式回放 → 因子检验(验证段) ----------
    if pre:
        rp = ["--universe", "100", "--n-boot", "200", "--null-k", "100",
              "--strategies", "mom_01,rev_01,ind_01"]
        rp_t = 1500
    else:
        rp = ["--universe", "500", "--top-n", "20", "--n-boot", "300", "--null-k", "1000"]
        rp_t = 5 * 3600
    add("回放-正式", "cpu",
        ["scripts/replay_run.py", "--start", "2024-01-01", "--end", VALID_END, "--out", o / "replay_500",
         "--industry-json", INDUSTRY, "--freeze-file", o / "freeze_replay.json", "--execute"] + rp,
        rp_t, [DAILY, INDUSTRY],
        "训练段淘汰 + 验证段只看:幸存策略、经验 p 值(Holm)、组合净值、报告 replay_500/",
        clean_dirs=[o / "replay_500"])  # 脚本拒绝覆盖非空输出目录,重跑前自己清空(上次失败的半成品也会被清掉)
    add("因子检验-验证段", "cpu",
        ["scripts/replay_factor_check.py", "--daily-dir", DAILY, "--output", o / "fc_valid.md",
         "--segment", "valid", "--industry-json", INDUSTRY,
         "--n-boot", "200" if pre else "300"] + (["--strategies", "mom_01"] if pre else []),   # 脚本要求 n-boot >= 200
        1500 if pre else int(3.5 * 3600), [DAILY, INDUSTRY],
        "验证段各因子分组单调性 + bootstrap 置信区间(train 段昨晚已有,这次是 valid)")
    if not pre:
        # 稳健性回放:换流动性门槛、换持仓集中度,看幸存策略是不是同一批(不是重复,是敏感性分析)
        for tag, extra in (("universe300", ["--universe", "300", "--top-n", "20"]),
                           ("top10", ["--universe", "500", "--top-n", "10"])):
            add(f"回放-稳健性-{tag}", "cpu",
                ["scripts/replay_run.py", "--start", "2024-01-01", "--end", VALID_END, "--out", o / f"replay_{tag}",
                 "--industry-json", INDUSTRY, "--freeze-file", o / f"freeze_{tag}.json", "--execute",
                 "--n-boot", "300", "--null-k", "1000"] + extra,
                5 * 3600, [DAILY, INDUSTRY], f"稳健性:{tag} 下的幸存策略与正式回放对比",
                clean_dirs=[o / f"replay_{tag}"])

    # ---------- 网络通道:基本面抓取 → 多个月末导出 → 排名 ----------
    if with_fund:
        add("基本面-抓取", "net",
            ["scripts/fetch_fundamental.py", "--asof", VALID_END] + (["--limit", "20"] if pre else []),
            600 if pre else 5 * 3600, [CODES],
            "全市场财报(利润/资产/现金流/分红)入库 data/fundamental.db,可断点续跑")
        # 先做最近 6 个月末,再往前补 15 个月末,凑成 21 个月的排名序列(以后检验基本面排名的预测力用)
        month_ends = ["2026-03-31"] if pre else (
            ["2025-10-31", "2025-11-30", "2025-12-31", "2026-01-31", "2026-02-28", "2026-03-31"]
            + ["2024-07-31", "2024-08-31", "2024-09-30", "2024-10-31", "2024-11-30", "2024-12-31",
               "2025-01-31", "2025-02-28", "2025-03-31", "2025-04-30", "2025-05-31", "2025-06-30",
               "2025-07-31", "2025-08-31", "2025-09-30"])
        for d in month_ends:
            add(f"基本面-导出-{d}", "net",
                ["scripts/fetch_fundamental.py", "--asof", d, "--skip-fetch",
                 "--export-fundamental", o / f"fund_{d}.csv", "--export-prices", o / f"prices_{d}.csv"]
                + (["--limit", "20"] if pre else []),
                600 if pre else 3600, [ROOT / "data" / "fundamental.db"],   # 导出要逐只取未复权价,全市场约 20-30 分钟
                f"{d} 时点可见的基本面快照 CSV")
            add(f"基本面-排名-{d}", "net",
                ["scripts/run_fundamental_rank.py", "--date", d, "--top", "50", "--out", o / f"fund_rank_{d}.md",
                 "--fundamental", o / f"fund_{d}.csv", "--prices", o / f"prices_{d}.csv", "--force"]
                + (["--min-industry-size", "2", "--min-factors", "1"] if pre else []),  # 脚本要求 min_industry_size >= 2
                600, [], f"{d} 基本面多因子排名:前 50 做多候选 / 后 50 避开")
    return T


# ---------- 运行与记录 ----------
class Runner:
    def __init__(self, mode, tasks):
        self.mode, self.tasks = mode, tasks
        self.o = out_dir(mode)
        self.state_f = self.o / "state.json"
        self.state = json.loads(self.state_f.read_text()) if self.state_f.exists() else {}
        self.lock = threading.Lock()
        self.main_log = self.o / "logs" / "main.log"

    def log(self, msg):
        line = f"[{dt.datetime.now():%m-%d %H:%M:%S}] {msg}"
        with self.lock:
            print(line, flush=True)
            with open(self.main_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def save(self):
        with self.lock:
            self.state_f.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))
            self.write_progress()

    ICON = {"OK": "✅", "FAIL": "❌", "TIMEOUT": "⏱️", "MISSING": "⛔", "RUNNING": "🔄",
            "SKIPPED_DEADLINE": "⏭️", "todo": "⬜"}

    def bar(self, done, total, width=24):
        n = int(width * done / total) if total else 0
        return "█" * n + "░" * (width - n)

    def count_output_files(self):
        exclude = {"logs", "state.json", "PROGRESS.md"}
        n = 0
        for p in self.o.iterdir():
            if p.name in exclude:
                continue
            n += 1 if p.is_file() else sum(1 for _ in p.rglob("*") if _.is_file())
        return n

    def write_progress(self):
        done = sum(1 for t in self.tasks if self.state.get(t["name"], {}).get("status") in
                   ("OK", "FAIL", "TIMEOUT", "MISSING", "SKIPPED_DEADLINE"))
        total = len(self.tasks)
        nfiles = self.count_output_files()
        rows = ["# 通宵进度(" + self.mode + ")", "", f"更新:{dt.datetime.now():%Y-%m-%d %H:%M:%S}", "",
                f"总进度 {self.bar(done, total)} {done}/{total}  ·  已产出 {nfiles} 个文件", ""]
        for lane in sorted({t["lane"] for t in self.tasks}):
            lt = [t for t in self.tasks if t["lane"] == lane]
            ldone = sum(1 for t in lt if self.state.get(t["name"], {}).get("status") in
                       ("OK", "FAIL", "TIMEOUT", "MISSING", "SKIPPED_DEADLINE"))
            rows.append(f"**通道 {lane}** {self.bar(ldone, len(lt), 16)} {ldone}/{len(lt)}")
            for t in lt:
                s = self.state.get(t["name"], {})
                status = s.get("status", "todo")
                icon = self.ICON.get(status, "⬜")
                secs = f"({s['secs']}s)" if "secs" in s else ""
                rows.append(f"- {icon} {t['name']} {secs}")
            rows.append("")
        n = {k: sum(1 for t in self.tasks if self.state.get(t["name"], {}).get("status") == k)
             for k in ("OK", "FAIL", "TIMEOUT", "MISSING", "RUNNING", "SKIPPED_DEADLINE")}
        rows += [f"统计:✅ {n['OK']} | ❌ {n['FAIL']} | ⏱️ {n['TIMEOUT']} | ⛔ {n['MISSING']} | 🔄 {n['RUNNING']} | ⏭️ {n['SKIPPED_DEADLINE']} | 共 {total}"]
        (self.o / "PROGRESS.md").write_text("\n".join(rows) + "\n", encoding="utf-8")

    def run_one(self, t):
        name = t["name"]
        if self.state.get(name, {}).get("status") == "OK":
            self.log(f"跳过(已完成) {name}")
            return
        if self.mode == "run" and dt.datetime.now() >= DEADLINE:
            self.state[name] = {"status": "SKIPPED_DEADLINE"}
            self.log(f"过了 {DEADLINE:%H:%M} 截止时间,不再启动新任务,跳过 {name}(下次 --run 会接着做)")
            self.save()
            return
        missing = [r for r in t["requires"] if not Path(r).exists()]
        if missing:
            self.state[name] = {"status": "MISSING", "detail": missing}
            self.log(f"缺输入 {name}: {missing}")
            self.save()
            return
        for d in t.get("clean_dirs", ()):
            dp = Path(d)
            if dp.exists():
                import shutil
                shutil.rmtree(dp)  # 上次失败留下的半成品目录:脚本拒绝覆盖非空目录,重跑前自己清空
        self.state[name] = {"status": "RUNNING", "start": dt.datetime.now().isoformat(timespec="seconds")}
        self.save()
        self.log(f">>> {name} (时限 {t['timeout']}s)")
        logf = self.o / "logs" / (name.replace("/", "_") + ".log")
        t0 = time.time()
        try:
            with open(logf, "a", encoding="utf-8") as lf:
                lf.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} cmd: {' '.join(t['cmd'])}\n")
                lf.flush()
                r = subprocess.run(t["cmd"], cwd=ROOT, stdout=lf, stderr=subprocess.STDOUT, timeout=t["timeout"])
            rc = r.returncode
            status = "OK" if rc == 0 else "FAIL"
        except subprocess.TimeoutExpired:
            rc, status = 124, "TIMEOUT"
        except Exception as e:  # 任何意外都只记录,不影响后面的任务
            rc, status = -1, "FAIL"
            with open(logf, "a", encoding="utf-8") as lf:
                lf.write(f"runner exception: {e!r}\n")
        secs = int(time.time() - t0)
        self.state[name] = {"status": status, "rc": rc, "secs": secs, "log": str(logf.relative_to(ROOT))}
        self.log(f"<<< {name} {status} rc={rc} 用时 {secs}s")
        self.save()

    def run_lane(self, lane):
        for t in self.tasks:
            if t["lane"] == lane:
                self.run_one(t)

    def run_all(self, parallel=True):
        self.log(f"开始 {self.mode}:{len(self.tasks)} 个任务,输出目录 {self.o.relative_to(ROOT)}")
        self.write_progress()
        lanes = sorted({t["lane"] for t in self.tasks})
        if parallel:
            th = [threading.Thread(target=self.run_lane, args=(l,), name=l) for l in lanes]
            for x in th:
                x.start()
            for x in th:
                x.join()
        else:
            for t in self.tasks:
                self.run_one(t)
        self.log("全部结束")
        self.summary()

    def summary(self):
        bad = [t["name"] for t in self.tasks if self.state.get(t["name"], {}).get("status") != "OK"]
        print("\n" + (self.o / "PROGRESS.md").read_text(encoding="utf-8"))
        if bad:
            print("未通过:", ", ".join(bad))
        return not bad


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--preflight", action="store_true", help="小参数试跑每个任务")
    g.add_argument("--run", action="store_true", help="正式通宵跑")
    g.add_argument("--status", action="store_true", help="只打印进度")
    ap.add_argument("--force-fundamental", action="store_true", help="T014 S5 未通过也跑基本面")
    ap.add_argument("--serial", action="store_true", help="不并行,按顺序跑(调试用)")
    a = ap.parse_args()
    mode = "preflight" if a.preflight else "run"
    if a.status:
        for m in ("run", "preflight"):
            p = ROOT / "reports" / f"{'overnight' if m == 'run' else 'preflight'}_{TODAY}" / "PROGRESS.md"
            if p.exists():
                print(p.read_text(encoding="utf-8"))
        return 0
    s5_done = S14_STATE.exists() and json.loads(S14_STATE.read_text()).get("S5") == "done"
    with_fund = s5_done or a.force_fundamental or mode == "preflight"
    tasks = build_tasks(mode, with_fund)
    r = Runner(mode, tasks)
    if not with_fund:
        r.log("T014 S5 未通过,基本面任务不排(加 --force-fundamental 可强行跑)")
    ok = r.run_all(parallel=not a.serial)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
