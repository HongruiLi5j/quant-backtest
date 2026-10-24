"""命令行:由收益 CSV(可选交易 CSV)生成统一验证报告(T007 / S7)。

用法:
  python3 -B scripts/validate_strategy.py --returns FILE.csv [--trades FILE.csv] --out REPORT.md
  可选:--turnover-col NAME --base-cost 0.0005 (--cost-included | --gross) --trial-log FILE.jsonl --force

只读你给的文件、只写你指定的输出路径;不访问项目数据目录、不联网。已存在的输出文件默认拒绝覆盖。
退出码:0 成功;2 输入/参数/输出路径有问题。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.validation import adapter, report, trials  # noqa: E402


def _parser():
    ap = argparse.ArgumentParser(description="生成统一验证报告(仅供研究,不构成投资建议)")
    ap.add_argument("--returns", required=True, help="逐日收益 CSV(列: date, ret)")
    ap.add_argument("--trades", help="逐笔交易 CSV(列: entry_date, exit_date, ret)")
    ap.add_argument("--out", required=True, help="输出的 Markdown 路径")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的输出文件")
    ap.add_argument("--date-col", default="date")
    ap.add_argument("--ret-col", default="ret")
    ap.add_argument("--turnover-col", help="换手列名(成本压力需要)")
    ap.add_argument("--base-cost", type=float, help="1x 单边成本率,如 0.0005")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--cost-included", action="store_true", help="声明收益已扣除 base-cost 的费用")
    g.add_argument("--gross", action="store_true", help="声明收益是税前/未扣费")
    ap.add_argument("--trial-log", help="试验账本 JSONL(只读)")
    ap.add_argument("--strategy", help="按策略名统计试验次数")
    ap.add_argument("--train", type=float, default=0.6)
    ap.add_argument("--valid", type=float, default=0.2)
    ap.add_argument("--embargo", type=int, default=5)
    ap.add_argument("--wf-train", type=int, help="滚动前进训练窗(根),默认 252")
    ap.add_argument("--wf-test", type=int, help="滚动前进测试窗(根),默认 63")
    ap.add_argument("--locked-open-count", type=int, help="锁定测试段已被访问的次数")
    return ap


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if os.path.exists(args.out) and not args.force:
            raise report.ReportError(f"输出文件已存在,拒绝覆盖: {args.out}(确认要覆盖请加 --force)")
        real_out = os.path.realpath(args.out)
        for src_path in (args.returns, args.trades, args.trial_log):
            if src_path and os.path.realpath(src_path) == real_out:
                raise report.ReportError("输出路径不能与输入文件相同")
        cost_included = True if args.cost_included else (False if args.gross else None)
        tr = adapter.trades_from_csv(args.trades) if args.trades else None
        rec = adapter.from_csv(args.returns, date_col=args.date_col, ret_col=args.ret_col,
                               turnover_col=args.turnover_col, trades=tr, base_cost=args.base_cost,
                               cost_included=cost_included)
        params = {"train": args.train, "valid": args.valid, "embargo": args.embargo}
        if args.strategy:
            params["strategy"] = args.strategy
        if args.wf_train is not None:
            params["wf_train_len"] = args.wf_train
        if args.wf_test is not None:
            params["wf_test_len"] = args.wf_test
        if args.locked_open_count is not None:
            params["locked_open_count"] = args.locked_open_count
        if args.trial_log and not os.path.isfile(args.trial_log):
            raise report.ReportError(f"试验账本不存在: {args.trial_log}")
        log = trials.TrialLog(args.trial_log) if args.trial_log else None
        text, _ = report.build_report(rec, params, log)
        report.write_report(args.out, text, force=args.force)
    except (adapter.AdapterError, report.ReportError, trials.TrialError, ValueError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2
    print(f"报告已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
