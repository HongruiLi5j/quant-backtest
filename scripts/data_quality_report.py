#!/usr/bin/env python3
"""每日数据质量日报(命令行):只读扫描日线 parquet,写 data_quality.md / .json。

不联网;对数据目录只读;任何失败只打印并以退出码 0 返回(运行状态看报告里的“运行状态”)。
示例:.venv/bin/python scripts/data_quality_report.py --sample 200
"""
import argparse
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main(argv=None) -> int:
    try:
        ap = argparse.ArgumentParser(description="数据质量日报(只读)")
        ap.add_argument("--data-dir", default=os.path.join(ROOT, "data", "daily", "cn_all"),
                        help="parquet 所在目录(只读)")
        ap.add_argument("--out-dir", default=os.path.join(ROOT, "reports", "data_quality"),
                        help="报告输出目录,不存在则创建")
        ap.add_argument("--sample", type=int, default=None, help="只抽查 N 只股票")
        ap.add_argument("--seed", type=int, default=0, help="抽样随机种子")
        ap.add_argument("--top", type=int, default=20, help="异常清单显示前 N 只")
        ap.add_argument("--date", default=None, help="报告日期 YYYY-MM-DD,默认北京时间今天")
        ap.add_argument("--max-seconds", type=float, default=None, help="扫描耗时上限(秒)")
        a = ap.parse_args(argv)

        from src.dataquality.report import run
        today = date.fromisoformat(a.date) if a.date else None
        out = run(a.data_dir, a.out_dir, sample=a.sample, seed=a.seed, top_n=a.top,
                  today=today, max_seconds=a.max_seconds)
        w = out["write"]
        if w["ok"]:
            print(f"状态={out['status']} 已检查={out['scanned']} 报告={w['md']}")
        else:
            print(f"状态={out['status']} 报告写入失败:{w['error']}")
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"数据质量日报失败(已忽略):{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
