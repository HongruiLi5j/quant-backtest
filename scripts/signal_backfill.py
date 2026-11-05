"""过去 N 天信号对账回填（T006 S3d）：把信号账本里每天的信号，用本地日线查出之后 1/3/5/20 个交易日的真实收益，写进独立文件。

用法：python3 -B scripts/signal_backfill.py --days 90 --output data/market_monitor_intraday/signal_backfill.csv
只读：信号账本用 SQLite 只读模式打开（不建表、不写入），日线只读本地 parquet；不联网。输出三个新文件，任何一个已存在就拒绝覆盖：
  <output>                    逐条信号 -> 各持有期收益
  <output 去后缀>_summary.csv  分策略 x 分动作 x 持有期的汇总（平均收益、方向调整后收益、胜率、超额、与 blend2 整体对比）
  <output 去后缀>_report.md    可读报告

收益口径（与 src/signal_ledger.py 一致）：信号日 D 收盘后出信号 -> D 的下一交易日开盘价入场 -> D+h 个交易日收盘价出场，ret = close[D+h]/open[D+1]-1。
    买入/加仓看涨（方向 +1），卖出/减仓看跌（方向 -1）；"方向调整后收益" = 方向 x ret，正数 = 信号方向猜对了。胜率 = 方向调整后收益 > 0 的比例。
    h 个交易日还没走完（太新）的信号，该持有期收益留空，不参与统计。
随机对照：同一天、同一口径下全市场所有有行情股票的平均收益（等于"随机挑一只"的期望）；超额 = 方向调整后收益 - 方向 x 当天全市场均值。
费用：只对买入/加仓扣一次往返费用（--cost-bp，默认 30bp，比账本里的 16bp 保守；含佣金、印花税、滑点的粗估）；卖出/减仓是"避开的跌幅"，A 股不能做空，不扣费用也不能当作可赚的收益。
同一天的信号共享同一个大盘行情，不是独立样本；报告里同时给信号条数和信号日数。
"""
import argparse
from bisect import bisect_right
from datetime import date, datetime, timedelta
import math
import os
from pathlib import Path
import sqlite3
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor.core import BEIJING
from src import market_clock

HORIZONS = (1, 3, 5, 20)
MAIN_STRATEGY = 'blend2'
DIRECTION = {'买入': 1, '加仓': 1, '卖出': -1, '减仓': -1}
DEFAULT_COST_BP = 30.0
DEFAULT_LEDGER = ROOT / 'data' / 'signal_ledger.db'
MIN_SAMPLE = 30         # 与账本一致：少于 30 条只展示，不作结论
DETAIL_COLS = ['day', 'strategy', 'code', 'name', 'action', 'direction'] + \
    [f'{k}{h}' for h in HORIZONS for k in ('ret_', 'mkt_')]
SUMMARY_COLS = ['strategy', 'action', 'h', 'n', 'n_days', 'mean_ret', 'mean_adj', 'mean_net', 'win_rate', 'mean_mkt', 'mean_excess',
                'vs_blend2_adj', 'small_sample']

MESSAGES = {
    'no_daily_data': '日线目录为空或不存在', 'no_ledger': '找不到信号账本（或无法只读打开）', 'invalid_days': '--days 必须是正整数',
    'invalid_date': '--asof 日期格式应为 YYYY-MM-DD',
}


class BackfillError(ValueError):
    """输入不足或不合法；args[0] 为固定原因码。"""


# ---------------------------------------------------------------- 读账本（只读）
def read_signals(ledger_path, since_day, until_day):
    """只读打开 SQLite，返回 [since_day, until_day] 内的信号行（dict 列表）。表不存在按空处理。"""
    p = Path(ledger_path)
    if not p.is_file():
        raise BackfillError('no_ledger')
    try:
        c = sqlite3.connect(f'file:{p}?mode=ro', uri=True, timeout=10)
    except sqlite3.Error:
        raise BackfillError('no_ledger')
    try:
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute('SELECT day, strategy, code, name, action FROM signals WHERE day >= ? AND day <= ? ORDER BY day, strategy, code',
                             (since_day, until_day)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        return [dict(r) for r in rows]
    finally:
        c.close()


# ---------------------------------------------------------------- 读日线
def load_open_close(daily_dir, now=None):
    """返回 (open, close) 两张宽表；北京时间 15:30 前丢弃当天 K 线。"""
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    drop_today = not market_clock.is_close_ready(now)
    root = Path(daily_dir)
    if not root.is_dir():
        raise BackfillError('no_daily_data')
    so, sc = {}, {}
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if not entry.name.endswith('.parquet') or not entry.is_file(follow_symlinks=False):
            continue
        try:
            df = pd.read_parquet(entry.path, columns=['Open', 'Close'])
            df.index = pd.to_datetime(df.index)
        except Exception:
            continue
        if drop_today:
            df = df[df.index.date < now.date()]
        if df.empty:
            continue
        code = entry.name[:-len('.parquet')]
        so[code], sc[code] = df['Open'].astype(float), df['Close'].astype(float)
    if not sc:
        raise BackfillError('no_daily_data')
    close = pd.DataFrame(sc).sort_index()
    close = close[close.notna().sum(axis=1) >= max(1, int(0.2 * close.shape[1]))]
    return pd.DataFrame(so).sort_index().reindex(close.index), close


def entry_exit_returns(open_, close, horizons=HORIZONS):
    """每个持有期的全市场收益宽表 {h: DataFrame}：ret[D] = close[D+h]/open[D+1]-1（价格非正为缺失）。按交易日行位移，不是日历日。"""
    o1 = open_.where(open_ > 0).shift(-1)
    out = {}
    for h in horizons:
        out[h] = (close.where(close > 0).shift(-h) / o1 - 1).replace([np.inf, -np.inf], np.nan)
    return out


def backfill_rows(signals, rets):
    """给每条信号查各持有期收益与当天全市场均值。信号日不在交易日历里（用不到的日期）-> 取不晚于它的最近交易日，仍查不到则留空。"""
    cal = list(next(iter(rets.values())).index)
    mkt = {h: r.mean(axis=1) for h, r in rets.items()}
    out = []
    for s in signals:
        d = DIRECTION.get(s['action'])
        if d is None:
            continue
        code = str(s['code']).zfill(6)
        i = bisect_right(cal, pd.Timestamp(s['day'])) - 1
        row = {'day': s['day'], 'strategy': s['strategy'], 'code': code, 'name': s.get('name') or '', 'action': s['action'], 'direction': d}
        for h in HORIZONS:
            r, m = np.nan, np.nan
            if i >= 0 and cal[i] == pd.Timestamp(s['day']):         # 信号日必须正好是交易日，否则次日开盘的定义不成立
                m = mkt[h].iloc[i]
                if code in rets[h].columns:
                    r = rets[h][code].iloc[i]
            row[f'ret_{h}'], row[f'mkt_{h}'] = r, m
        out.append(row)
    return pd.DataFrame(out, columns=DETAIL_COLS)


# ---------------------------------------------------------------- 汇总
def summarize(detail, cost_bp=DEFAULT_COST_BP):
    """分策略 x 分动作（含"全部"）x 持有期汇总；另有 blend2 整体对比列。"""
    rows = []
    cost = cost_bp / 1e4
    if len(detail):
        groups = [(s, a, g) for (s, a), g in detail.groupby(['strategy', 'action'])]
        groups += [(s, '全部', g) for s, g in detail.groupby('strategy')]
    else:
        groups = []
    base = {}
    for s, a, g in groups:
        if s == MAIN_STRATEGY and a == '全部':
            for h in HORIZONS:
                base[h] = (g[f'ret_{h}'] * g['direction']).mean()
    for s, a, g in groups:
        for h in HORIZONS:
            r = g[f'ret_{h}']
            ok = r.notna()
            if not ok.any():
                rows.append({'strategy': s, 'action': a, 'h': h, 'n': 0, 'n_days': 0, 'small_sample': True})
                continue
            gg = g[ok]
            adj = gg[f'ret_{h}'] * gg['direction']
            net = adj - np.where(gg['direction'] > 0, cost, 0.0)
            excess = adj - gg['direction'] * gg[f'mkt_{h}']
            rows.append({
                'strategy': s, 'action': a, 'h': h, 'n': int(ok.sum()), 'n_days': int(gg['day'].nunique()),
                'mean_ret': gg[f'ret_{h}'].mean(), 'mean_adj': adj.mean(), 'mean_net': net.mean(), 'win_rate': float((adj > 0).mean()),
                'mean_mkt': gg[f'mkt_{h}'].mean(), 'mean_excess': excess.mean(),
                'vs_blend2_adj': adj.mean() - base[h] if h in base and np.isfinite(base[h]) else np.nan,
                'small_sample': int(ok.sum()) < MIN_SAMPLE,
            })
    return pd.DataFrame(rows, columns=SUMMARY_COLS)


def _fmt(x, nd=4):
    return '' if x is None or (isinstance(x, float) and not math.isfinite(x)) else f'{x:.{nd}f}'


def write_report(path, summary, n_signals, n_days, since, until, cost_bp):
    lines = ['# 信号对账回填报告', '',
             f'- 信号区间：{since} ~ {until}，共 {n_signals} 条有方向的信号、{n_days} 个信号日；收益口径：次日开盘入场，D+h 收盘出场。',
             f'- 方向调整后收益 > 0 表示信号方向猜对；买入/加仓净收益已扣往返费用 {cost_bp:g}bp；超额 = 相对"同日全市场平均"（随机挑一只的期望）。',
             '- 同一天的信号共享同一个大盘行情，不是独立样本；n < 30 的行只展示不作结论。这不是策略有效性的证明（未考虑多重检验、涨停买不进、冲击成本）。', '']
    if summary.empty or summary['n'].fillna(0).sum() == 0:
        lines += ['区间内没有可核对的信号（账本为空、信号太新、或日线缺失）。', '']
    else:
        lines += ['| 策略 | 动作 | h | n | 信号日 | 平均收益 | 方向调整后 | 净收益 | 胜率 | 全市场均值 | 超额 | 对比 blend2 整体 | 小样本 |',
                  '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
        for r in summary[summary['n'] > 0].itertuples():
            lines.append(f'| {r.strategy} | {r.action} | {r.h} | {r.n} | {r.n_days} | {_fmt(r.mean_ret)} | {_fmt(r.mean_adj)} | {_fmt(r.mean_net)} | '
                         f'{_fmt(r.win_rate, 3)} | {_fmt(r.mean_mkt)} | {_fmt(r.mean_excess)} | {_fmt(r.vs_blend2_adj)} | {"是" if r.small_sample else ""} |')
        lines.append('')
    path.write_text('\n'.join(lines), encoding='utf-8')


# ---------------------------------------------------------------- 主流程
def derived_paths(output):
    out = Path(output)
    return out, out.with_name(out.stem + '_summary.csv'), out.with_name(out.stem + '_report.md')


def run_backfill(ledger_path, daily_dir, output, *, days=90, asof=None, cost_bp=DEFAULT_COST_BP, now=None):
    if not isinstance(days, int) or days < 1:
        raise BackfillError('invalid_days')
    try:
        asof_d = date.fromisoformat(asof) if asof else datetime.now(BEIJING).date()
    except ValueError:
        raise BackfillError('invalid_date')
    paths = derived_paths(output)
    if any(p.exists() for p in paths):
        raise FileExistsError(str(output))
    since = str(asof_d - timedelta(days=days))
    until = str(asof_d)
    signals = read_signals(ledger_path, since, until)
    open_, close = load_open_close(daily_dir, now)
    rets = entry_exit_returns(open_, close)
    detail = backfill_rows(signals, rets)
    summary = summarize(detail, cost_bp)
    out, sum_path, rep_path = paths
    out.parent.mkdir(parents=True, exist_ok=True)
    detail.to_csv(out, index=False, encoding='utf-8')
    summary.to_csv(sum_path, index=False, encoding='utf-8')
    n_days = int(detail['day'].nunique()) if len(detail) else 0
    write_report(rep_path, summary, len(detail), n_days, since, until, cost_bp)
    return {'n_signals': len(detail), 'n_days': n_days, 'detail': detail, 'summary': summary,
            'files': {'detail': str(out), 'summary': str(sum_path), 'report': str(rep_path)}}


def main(argv=None, now=None):
    ap = argparse.ArgumentParser(description='过去 N 天信号对账回填（只读账本，写独立文件）')
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--output', required=True)
    ap.add_argument('--ledger', default=str(DEFAULT_LEDGER))
    ap.add_argument('--daily-dir', default=str(ROOT / 'data' / 'daily' / 'cn_all'))
    ap.add_argument('--asof', default=None)
    ap.add_argument('--cost-bp', type=float, default=DEFAULT_COST_BP)
    args = ap.parse_args(argv)
    try:
        res = run_backfill(args.ledger, args.daily_dir, args.output, days=args.days, asof=args.asof, cost_bp=args.cost_bp, now=now)
    except FileExistsError:
        print('拒绝覆盖：输出文件（或其 _summary.csv / _report.md）已存在，请换一个 --output')
        return 2
    except (BackfillError, OSError) as exc:
        print('失败：' + MESSAGES.get(str(exc), type(exc).__name__))
        return 1
    print(f"完成：{res['n_signals']} 条信号、{res['n_days']} 个信号日；报告 {res['files']['report']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
