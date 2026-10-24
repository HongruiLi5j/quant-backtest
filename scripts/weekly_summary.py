"""生成纸面实盘周报(T012 / S2)。默认只读真实账本,输出到 handoff/reports/weekly/<周>.md,拒绝覆盖。

用法:
    .venv/bin/python scripts/weekly_summary.py --week 2026-W44
可选:--ledger 账本路径  --journal 模拟盘日志路径  --initial-cash 本金  --benchmark-csv date,ret
      --bt-csv date,ret  --calendar-csv 每行一个日期  --reflection-jsonl 路径  --out-dir 输出目录  --main 主策略
安全:账本用 sqlite mode=ro 打开,不存在就报错(不会新建);不弹通知、不联网;已有同名周报退出码 2。
说明:--journal 的库由 PaperJournal 打开(表已存在时不会改动数据);不传则跳过日志相关章节并标注"数据不足"。
"""
import argparse
import csv
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import promotion as PR  # noqa: E402
from src import signal_ledger as L  # noqa: E402
from src.paper_review import weekly as W  # noqa: E402

DEFAULT_OUT = ROOT / "handoff" / "reports" / "weekly"


def _read_returns(path):
    """两列 CSV(date,ret,可带表头)-> {日期: 日收益}。"""
    out = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            try:
                out[row[0].strip()] = float(row[1])
            except ValueError:
                continue                    # 表头或坏行
    return out


def _read_lines(path):
    with open(path, encoding="utf-8") as f:
        return [x.strip() for x in f if x.strip() and x.strip()[0].isdigit()]


def _read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _last_complete_week(today=None):
    d = (today or date.today()) - timedelta(days=7)
    return PR.week_key(d.isoformat())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--week", help="ISO 周,如 2026-W44;默认上一个完整周")
    ap.add_argument("--ledger", default=str(L.DB))
    ap.add_argument("--journal")
    ap.add_argument("--initial-cash", type=float)
    ap.add_argument("--benchmark-csv")
    ap.add_argument("--bt-csv")
    ap.add_argument("--calendar-csv")
    ap.add_argument("--reflection-jsonl")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--main", default="blend2")
    a = ap.parse_args(argv)

    week = a.week or _last_complete_week()
    try:
        W.week_range(week)
    except ValueError as exc:
        print(f"错误:{exc}", file=sys.stderr)
        return 1
    target = Path(a.out_dir) / f"{week}.md"
    if target.exists():                      # 先检查,避免白算一遍
        print(f"拒绝覆盖已存在的周报:{target}", file=sys.stderr)
        return 2
    ledger = Path(a.ledger)
    if not ledger.is_file():
        print(f"账本不存在:{ledger}(只读模式不会新建)", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    journal = None
    try:
        if a.journal:
            jp = Path(a.journal)
            if not jp.is_file():
                print(f"模拟盘日志不存在:{jp}(不会新建)", file=sys.stderr)
                return 1
            from src.validation.paper import PaperJournal
            journal = PaperJournal(str(jp))
        bench = _read_returns(a.benchmark_csv) if a.benchmark_csv else None
        bt = _read_returns(a.bt_csv) if a.bt_csv else None
        cal = _read_lines(a.calendar_csv) if a.calendar_csv else None
        refl = _read_jsonl(a.reflection_jsonl) if a.reflection_jsonl else None
        rep = W.build(conn, week, main_strategy=a.main, journal=journal, calendar=cal, bt_returns=bt,
                      benchmark_daily=bench, reflections=refl, initial_cash=a.initial_cash)
    finally:
        conn.close()
        if journal is not None:
            journal.close()
    try:
        path = W.write_report(W.render_markdown(rep), Path(a.out_dir), week)
    except FileExistsError:
        print(f"拒绝覆盖已存在的周报:{target}", file=sys.stderr)
        return 2
    print(f"周报已写入 {path}")
    for al in rep["alerts"]:
        print(f"[{al['level']}] {al['title']}:{al['body']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
