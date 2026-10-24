"""收盘后分析入口（T006 S3c）。

用法：python3 -B scripts/run_eod_analysis.py --day 2026-10-23 --output DIR
默认读 data/market_monitor_intraday/<day>.jsonl 与 data/daily/cn_all；输出目录默认 data/market_monitor_intraday/<day>_eod/，已有内容则拒绝覆盖。
只读本地文件，不联网。可选 --ledger 账户流水 JSON（字段见 docs/eod_analysis_howto.md）用于成本会计恒等式。
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor.eod_analysis import EodError, run_eod_analysis
from src.market_monitor.intraday import INTRADAY_DIR

MESSAGES = {
    'no_daily_data': '日线目录为空或不存在', 'no_bar_for_day': '日线里没有该日的 K 线（15:30 前当天半天数据不可用）',
    'future_day': '日期在未来', 'locked_segment': '分析日落在封存段（2026-04-01 起），拒绝读取；锁定段只在最终测试时用', 'invalid_day': '日期格式应为 YYYY-MM-DD', 'missing_dividend': '账户流水缺少 dividend_pnl 字段', 'invalid_ledger': '账户流水字段缺失或不是有限数字',
}


def main(argv=None, now=None):
    ap = argparse.ArgumentParser(description='收盘后分析：24 因子 + 市场状态 + 残差 Z-score + 成本会计恒等式')
    ap.add_argument('--day', required=True)
    ap.add_argument('--output', default=None)
    ap.add_argument('--jsonl', default=None)
    ap.add_argument('--daily-dir', default=str(ROOT / 'data' / 'daily' / 'cn_all'))
    ap.add_argument('--ledger', default=None)
    args = ap.parse_args(argv)
    jsonl = args.jsonl or str(INTRADAY_DIR / f'{args.day}.jsonl')
    ledger = None
    try:
        if args.ledger:
            ledger = json.loads(Path(args.ledger).read_text(encoding='utf-8'))
        result = run_eod_analysis(args.day, jsonl, args.daily_dir, args.output, ledger=ledger, now=now)
    except FileExistsError:
        print('拒绝覆盖：输出目录已有内容，请换一个 --output')
        return 2
    except (EodError, OSError, ValueError) as exc:
        print('失败：' + MESSAGES.get(str(exc), type(exc).__name__))
        return 1
    print(f"状态 {result['regime']}，建议敞口 {result['scaling']:.0%}；报告 {result['files']['report']}")
    for w in result['warnings']:
        print('警告：' + w)
    return 0


if __name__ == '__main__':
    sys.exit(main())
