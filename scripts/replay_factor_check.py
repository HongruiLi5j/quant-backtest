"""T008 S2:单因子分组单调性检验的命令行(设计文档第 6 节)。

用法:
  python3 -B scripts/replay_factor_check.py --daily-dir data/daily/cn_all --output /新目录 [--segment train] [--groups 5]
      [--strategies mom_01,rev_02] [--industry-json 行业映射.json] [--n-boot 1000]

- 只读 --daily-dir 下的 <代码>.parquet(列 Open/High/Low/Close/Volume,日期索引),不联网,不写该目录;
- 输出到 --output(必须不存在或为空目录,且不能在 --daily-dir 里面),拒绝覆盖;写 monotonic_report.json 和 monotonic_summary.csv;
- 北京时间 15:30 前的当天 K 线丢弃(盘中半天数据);数据再截到检验段末,段后数据不参与;
- 封存段(holdout)不能在这里检验;
- 结果好不代表策略有效:通过只是入场券;约 490 个网格组合每个都是一次试验,通过者里有运气成分;没扣成本、没过滤涨跌停/停牌、
  股票池若是现存股票则有幸存者偏差。
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.replay import monotonic  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
CLOSE_READY = (15, 30)
CAVEATS = [
    "通过单调性只是进入回放的入场券,不是策略有效的证据",
    "网格里每个参数组合都是一次试验(m=网格总数),通过者里混有纯运气,多重检验留到 S4 之后处理",
    "没有扣交易成本,没有处理涨跌停/停牌,时点状态未过滤;股票池若来自现存股票则有幸存者偏差",
    "20 日标签相邻日重叠 19 天,显著性用块长≥20 的 bootstrap,仍只是近似",
]


def load_panel(daily_dir, end_ts, now=None):
    """读 parquet 面板 {code: DataFrame(open/high/low/close/volume)},截到 end_ts,丢弃盘中当天 K 线。"""
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    drop_today = (now.hour, now.minute) < CLOSE_READY
    root = Path(daily_dir)
    if not root.is_dir():
        raise FileNotFoundError("daily_dir 不存在")
    cols = ["Open", "High", "Low", "Close", "Volume"]
    panel = {}
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if not entry.name.endswith(".parquet") or not entry.is_file(follow_symlinks=False):
            continue
        try:
            df = pd.read_parquet(entry.path, columns=cols)
            df.index = pd.to_datetime(df.index)
        except Exception:
            continue
        df = df[df.index <= end_ts]
        if drop_today:
            df = df[df.index.date < now.date()]
        if df.empty:
            continue
        df.columns = [c.lower() for c in df.columns]
        panel[entry.name[:-len(".parquet")]] = df.astype(float)
    if not panel:
        raise FileNotFoundError("没有可用的日线数据")
    return panel


def _safe_output(out: Path, daily_dir: Path):
    out_r, daily_r = out.resolve(), daily_dir.resolve()
    if out_r == daily_r or daily_r in out_r.parents:
        raise ValueError("输出目录不能在数据目录里面")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError("输出目录已有内容,拒绝覆盖")


def write_report(report: dict, out: Path):
    """写 JSON(完整)和 CSV(每个参数组合一行)。"""
    out.mkdir(parents=True, exist_ok=True)
    report = dict(report, caveats=CAVEATS)
    rep = out / "monotonic_report.json"
    rep.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    csv_path = out / "monotonic_summary.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["strategy", "params", "status", "strategy_eliminated", "passed", "n_days", "rho",
                    "spread_mean", "ci_lower", "ic_mean", "group_means", "reasons"])
        for s in report["results"]:
            if not s["params"]:
                w.writerow([s["id"], "", s["status"], s.get("eliminated"), "", "", "", "", "", "", "",
                            s.get("error", "")])
            for r in s["params"]:
                w.writerow([s["id"], json.dumps(r["params"], sort_keys=True), s["status"], s["eliminated"],
                            r["passed"], r["n_days"], r["rho"], r["spread_mean"], r["spread_ci_lower"],
                            r["ic_mean"], json.dumps(r["group_mean"]), ";".join(r["reasons"])])
    return {"report": str(rep), "summary": str(csv_path)}


def main(argv=None, now=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--daily-dir", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--segment", default="train", choices=list(monotonic.SEG_CHECKABLE))
    ap.add_argument("--groups", type=int, default=monotonic.N_GROUPS)
    ap.add_argument("--strategies", default="")
    ap.add_argument("--industry-json", default="")
    ap.add_argument("--n-boot", type=int, default=monotonic.N_BOOT)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    if not (monotonic.MIN_GROUPS <= args.groups <= monotonic.MAX_GROUPS) or args.n_boot < monotonic.MIN_N_BOOT:
        print(f"失败:--groups 必须在 5..10,--n-boot 必须 >= {monotonic.MIN_N_BOOT}")
        return 1
    out = Path(args.output)
    try:
        _safe_output(out, Path(args.daily_dir))
    except FileExistsError as e:
        print(f"拒绝覆盖:{e}")
        return 2
    except ValueError as e:
        print(f"失败:{e}")
        return 1
    industry = None
    if args.industry_json:
        industry = json.loads(Path(args.industry_json).read_text(encoding="utf-8"))
    from src.replay import registry
    cat, _ = registry.load_validated()
    end = pd.Timestamp(cat["segments"][args.segment][1])
    try:
        panel = load_panel(args.daily_dir, end, now)
    except FileNotFoundError as e:
        print(f"失败:{e}")
        return 1
    only = [s for s in args.strategies.split(",") if s] or None
    report = monotonic.check_all(panel, args.segment, args.groups, industry, args.n_boot, args.seed, only)
    files = write_report(report, out)
    checked = [r for r in report["results"] if r["status"] == "checked"]
    elim = [r["id"] for r in checked if r["eliminated"]]
    print(f"完成:检验 {len(checked)} 个策略,整体淘汰 {len(elim)} 个;"
          f"通过的参数组合 {sum(r['n_passed'] for r in checked)}/{sum(r['n_params'] for r in checked)}")
    print("提醒:" + ";".join(CAVEATS[:2]))
    print(f"报告 {files['report']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
