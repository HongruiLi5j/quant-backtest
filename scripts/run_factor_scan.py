"""全市场历史因子扫描（T006 S3d）：对 data/daily/cn_all 里每只股票、每个交易日算 24 个因子，评估它们对未来 20 日收益的预测力。

用法：python3 -B scripts/run_factor_scan.py --start 2024-01-01 --end 2026-10-22 --output data/market_monitor_intraday/factor_history/
只读本地日线 parquet，不联网。输出目录必须是新的（或是本脚本上次被中断留下的断点目录，此时自动续跑）；已有最终结果则拒绝覆盖。

三种有效性指标（每个交易日截面上算一次，再对日期汇总）：
  a) IC：因子值与未来 20 日收益的 Spearman 秩相关（每日一个），汇总为均值、标准差、t 值；
  b) 单调性：按因子值把当天股票分 10 组，看 10 组"日均未来收益"随组号的 Spearman 秩相关（接近 +1 才是单调）；
  c) 分组收益差：最高 10% 组 - 最低 10% 组的未来 20 日平均收益，汇总为均值与 t 值。
汇总表里的 ic_mean / mono_rho / tmb_mean 已乘以因子方向（direction），所以"正数 = 因子按自己声明的方向有效"；raw 列保留原始符号。

标签（未来收益）：信号日 t 收盘后出信号 -> t+1 开盘买 -> t+20 收盘卖，即 close[t+20]/open[t+1]-1，与信号账本口径一致；
面板只截到 --end（且不越过封存段），所以最后 20 个交易日因为没有完整未来而自然不参与评估。

封存段：2026-04-01 起的数据是锁定测试段，不允许作为扫描/筛选用的训练数据。--end 晚于 2026-03-31 时自动截到 2026-03-31 并打印警告，
这些之后的日线在内存里也被丢弃，所以标签不会偷看封存段。

统计提醒：20 日收益窗口相邻日高度重叠，每日 IC 序列有强自相关，朴素 t 值被高估；所以同时给 Newey-West（滞后 19）t 值，判断以 t_nw 为准。
24 个因子同时看有多重检验问题，所以再给一列 Bonferroni 阈值（双侧 5%/24）。没有扣费用，没有处理涨停买不进、退市股（幸存者偏差）。
"""
import argparse
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
from statistics import NormalDist
import sys
import time

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.market_monitor import factors as fx
from src.market_monitor.core import BEIJING
from src import market_clock
from src.market_monitor.eod_analysis import LOCKED_SEGMENT_START, _read_list

HORIZON = 20                 # 未来收益窗口（交易日）
WARMUP = 260                 # 每个 chunk 前多取的预热行数（mom252 需要 253 根）
CHUNK_DAYS = 100             # 每多少个评估日写一次断点
MIN_N = 30                   # 当日有效股票少于这个数就不算 IC / 分组（样本太少）
NW_LAG = 19                  # Newey-West 滞后：20 日收益窗口相邻重叠 19 天
N_GROUPS = 10
GROUP_COLS = [f'g{k}' for k in range(1, N_GROUPS + 1)]
DAILY_COLS = ['day', 'factor', 'ic', 'n'] + GROUP_COLS + ['tmb', 'n_trigger', 'trig_ret']
SUMMARY_FILE = 'factor_history_summary.csv'
DAILY_FILE = 'factor_history_daily.parquet'
REPORT_FILE = 'report.md'
META_FILE = 'scan_meta.json'
CKPT_DIR = 'checkpoints'
BONFERRONI_T = NormalDist().inv_cdf(1 - 0.05 / 2 / len(fx.FACTORS))     # 双侧 5%/24 对应的 |t| 阈值，约 2.87

MESSAGES = {
    'no_daily_data': '日线目录为空或不存在', 'invalid_date': '日期格式应为 YYYY-MM-DD', 'bad_range': '--start 必须早于或等于 --end',
    'start_in_locked': '--start 落在封存段（2026-04-01 起），拒绝扫描；锁定段只在最终测试时用', 'no_days_in_range': '区间内没有可评估的交易日（数据不足或区间过短）',
    'checkpoint_mismatch': '输出目录里的断点与本次参数不一致，不能续跑；请换一个 --output',
}


class ScanError(ValueError):
    """输入不足或不合法；args[0] 为固定原因码。"""


class ScanInterrupted(RuntimeError):
    """仅供测试：模拟跑到一半被中断。"""


# ---------------------------------------------------------------- 统计小工具
def nw_t(x, lag=NW_LAG):
    """均值的 Newey-West t 值（Bartlett 权重）。样本太少或方差退化返回 NaN。"""
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(x)
    if n < 3:
        return float('nan')
    lag = min(lag, n - 2)
    e = x - x.mean()
    var = float(e @ e) / n
    for k in range(1, lag + 1):
        var += 2 * (1 - k / (lag + 1)) * float(e[k:] @ e[:-k]) / n
    if var <= 0:
        return float('nan')
    return float(x.mean() / math.sqrt(var / n))


def naive_t(x):
    """独立同分布假设下的 t 值：均值 / (标准差 / sqrt(n))。"""
    x = pd.Series(x).dropna()
    if len(x) < 2 or x.std(ddof=1) == 0 or not np.isfinite(x.std(ddof=1)):
        return float('nan')
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))


def spearman_1d(a, b):
    """两个一维序列的 Spearman 秩相关；任一为常数返回 NaN。"""
    s = pd.DataFrame({'a': a, 'b': b}).dropna()
    if len(s) < 3:
        return float('nan')
    ra, rb = s['a'].rank().values, s['b'].rank().values
    if np.ptp(ra) == 0 or np.ptp(rb) == 0:
        return float('nan')
    return float(np.corrcoef(ra, rb)[0, 1])


# ---------------------------------------------------------------- 逐日截面指标
def day_metrics(values, fwd, kind='score', trigger=None, min_n=MIN_N):
    """values / fwd 同形状宽表（行=日，列=代码）。返回按日索引的 DataFrame：ic、n、g1..g10、tmb(最高组-最低组)、n_trigger、trig_ret。
    score 因子：秩相关 + 十分组；flag/filter 因子（布尔）：IC 用 0/1 的秩相关，不分十组，tmb = 触发组 - 未触发组。
    只在 values 与 fwd 同时有值的股票上算（两边用同一个掩码，保证配对）。"""
    v = values.astype(float).where(values.notna())
    ok = v.notna() & fwd.notna()
    v, f = v.where(ok), fwd.where(ok)
    n = ok.sum(axis=1)
    enough = n >= min_n
    rv, rf = v.rank(axis=1), f.rank(axis=1)
    cv, cf = rv.sub(rv.mean(axis=1), axis=0), rf.sub(rf.mean(axis=1), axis=0)
    den = np.sqrt((cv ** 2).sum(axis=1) * (cf ** 2).sum(axis=1))
    ic = ((cv * cf).sum(axis=1) / den.where(den > 0)).where(enough)
    out = pd.DataFrame({'ic': ic, 'n': n})
    if kind == 'score':
        g = np.ceil(rv.div(n.where(n > 0), axis=0) * N_GROUPS).clip(1, N_GROUPS)
        for k, col in enumerate(GROUP_COLS, start=1):
            out[col] = f.where(g == k).mean(axis=1).where(enough)
        out['tmb'] = out[GROUP_COLS[-1]] - out[GROUP_COLS[0]]
    else:
        hit = v == 1
        top = f.where(hit).mean(axis=1)
        rest = f.where(~hit).mean(axis=1)
        for col in GROUP_COLS:
            out[col] = np.nan
        out['tmb'] = (top - rest).where(enough)
    trig = (trigger if trigger is not None else v == 1).reindex_like(f).fillna(False).astype(bool) & ok
    out['n_trigger'] = trig.sum(axis=1)
    out['trig_ret'] = f.where(trig).mean(axis=1)
    return out


def summarize(daily, factors=None):
    """逐日长表 -> 每因子一行的汇总。direction 调整后：正数 = 因子按声明方向有效。"""
    factors = factors or fx.FACTORS
    rows = []
    for fid, meta in factors.items():
        d = daily[daily['factor'] == fid].sort_values('day')
        sgn = meta.direction
        ic, tmb = d['ic'] * sgn, d['tmb'] * sgn
        mono = float('nan')
        if meta.kind == 'score' and d[GROUP_COLS].notna().any().any():
            mono = spearman_1d(range(1, N_GROUPS + 1), d[GROUP_COLS].mean().values) * sgn
        ic_t_nw, tmb_t_nw = nw_t(ic), nw_t(tmb)
        ic_ok = bool(ic_t_nw > 2) if np.isfinite(ic_t_nw) else False
        mono_ok = bool(mono > 0.7) if np.isfinite(mono) else False
        tmb_ok = bool(tmb_t_nw > 2) if np.isfinite(tmb_t_nw) else False
        rows.append({
            'factor': fid, 'name': meta.name, 'kind': meta.kind, 'direction': sgn, 'n_days': int(d['ic'].notna().sum()),
            'ic_mean_raw': d['ic'].mean(), 'ic_mean': ic.mean(), 'ic_std': ic.std(ddof=1), 'ic_t': naive_t(ic), 'ic_t_nw': ic_t_nw,
            'mono_rho': mono, 'tmb_mean': tmb.mean(), 'tmb_t': naive_t(tmb), 'tmb_t_nw': tmb_t_nw,
            'avg_n_trigger': d['n_trigger'].mean(), 'trig_ret_mean': d['trig_ret'].mean(),
            'ic_ok': ic_ok, 'mono_ok': mono_ok, 'tmb_ok': tmb_ok,
            'passes_all': ic_ok and (mono_ok or meta.kind != 'score') and tmb_ok,
            'passes_bonferroni': bool(np.isfinite(ic_t_nw) and ic_t_nw > BONFERRONI_T),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 读数据
def parse_day(text):
    try:
        return pd.Timestamp(date.fromisoformat(text))
    except (TypeError, ValueError):
        raise ScanError('invalid_date')


def load_history(daily_dir, end_ts, now=None):
    """读 <目录>/*.parquet（Open/High/Low/Close/Volume，日期索引），截到 end_ts（含）。北京时间 15:30 前当天的 K 线丢弃。返回 Panel。"""
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    drop_today = not market_clock.is_close_ready(now)
    root = Path(daily_dir)
    if not root.is_dir():
        raise ScanError('no_daily_data')
    cols = ('Open', 'High', 'Low', 'Close', 'Volume')
    series = {c: {} for c in cols}
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if not entry.name.endswith('.parquet') or not entry.is_file(follow_symlinks=False):
            continue
        try:
            df = pd.read_parquet(entry.path, columns=list(cols))
            df.index = pd.to_datetime(df.index)
        except Exception:
            continue
        df = df[df.index <= end_ts]
        if drop_today:
            df = df[df.index.date < now.date()]
        if df.empty:
            continue
        code = entry.name[:-len('.parquet')]
        for c in cols:
            series[c][code] = df[c].astype(float)
    if not series['Close']:
        raise ScanError('no_daily_data')
    wide = {c: pd.DataFrame(series[c]).sort_index() for c in cols}
    close = wide['Close']
    close = close[close.notna().sum(axis=1) >= max(1, int(0.2 * close.shape[1]))]      # 去掉只有零星股票有数据的日期
    wide = {c: wide[c].reindex(close.index) for c in cols}
    names, industry = _read_list(root.parent / 'cn_all_list.csv')
    return fx.Panel(wide['Open'], wide['High'], wide['Low'], wide['Close'], wide['Volume'], names, industry)


def forward_returns(panel, horizon=HORIZON):
    """close[t+h] / open[t+1] - 1；价格非正视为缺失。"""
    o = panel.open.where(panel.open > 0).shift(-1)
    c = panel.close.where(panel.close > 0).shift(-horizon)
    return (c / o - 1).replace([np.inf, -np.inf], np.nan)


# ---------------------------------------------------------------- 一个 chunk
def scan_chunk(panel, days, horizon=HORIZON, min_n=MIN_N):
    """对 days（panel.close.index 的一段连续交易日）算 24 个因子的逐日指标，返回长表。预热行取自 days 之前，未来行（标签用）取自 days 之后。"""
    idx = panel.close.index
    p0, p1 = idx.get_loc(days[0]), idx.get_loc(days[-1])
    a, b = max(0, p0 - WARMUP), min(len(idx), p1 + 1 + horizon + 1)
    sub = fx.Panel(panel.open.iloc[a:b], panel.high.iloc[a:b], panel.low.iloc[a:b], panel.close.iloc[a:b], panel.volume.iloc[a:b],
                   panel.names, panel.industry, None)
    fwd = forward_returns(sub, horizon).loc[days]
    parts = []
    for fid, meta in fx.FACTORS.items():
        values = fx.compute_factor(fid, sub).loc[days]
        trig = fx.trigger_mask(fid, values)
        m = day_metrics(values, fwd, meta.kind, trig, min_n)
        m.insert(0, 'factor', fid)
        m.index.name = 'day'
        parts.append(m.reset_index())
    out = pd.concat(parts, ignore_index=True)
    out['day'] = out['day'].dt.strftime('%Y-%m-%d')
    return out[DAILY_COLS]


# ---------------------------------------------------------------- 目录 / 断点
def _atomic_write(path, writer):
    tmp = path.with_name(path.name + '.tmp')
    writer(tmp)
    os.replace(tmp, path)


def prepare_output(output_dir, meta):
    """返回断点目录。目录不存在或为空 -> 新建；有 scan_meta.json 且无最终结果 -> 续跑（参数必须一致）；其余已有内容 -> FileExistsError。"""
    out = Path(output_dir)
    if out.exists() and not out.is_dir():
        raise FileExistsError(str(out))
    names = {p.name for p in out.iterdir()} if out.is_dir() else set()
    if names & {SUMMARY_FILE, DAILY_FILE, REPORT_FILE}:
        raise FileExistsError(str(out))
    if names and META_FILE not in names:
        raise FileExistsError(str(out))
    out.mkdir(parents=True, exist_ok=True)
    meta_path = out / META_FILE
    if names:
        try:
            old = json.loads(meta_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ScanError('checkpoint_mismatch')
        if old != meta:
            raise ScanError('checkpoint_mismatch')
    else:
        _atomic_write(meta_path, lambda p: p.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding='utf-8'))
    ck = out / CKPT_DIR
    ck.mkdir(exist_ok=True)
    return ck


def _fmt(x, nd=4):
    return '' if x is None or (isinstance(x, float) and not math.isfinite(x)) else f'{x:.{nd}f}'


def write_report(path, summary, meta, warnings):
    ranked = summary.sort_values('ic_t_nw', ascending=False, na_position='last')
    lines = [
        '# 全市场历史因子扫描报告', '',
        f"- 区间：{meta['first_day']} ~ {meta['last_day']}，共 {meta['n_days']} 个评估日；标签 = 信号日次日开盘买、{meta['horizon']} 个交易日后收盘卖。",
        f"- 判断标准：IC 的 Newey-West t > 2；十分组单调秩相关 > 0.7（仅 score 因子）；最高组-最低组差的 Newey-West t > 2；多重检验阈值 |t| > {BONFERRONI_T:.2f}（24 个因子，双侧 5%）。",
        '- 这是描述性的历史扫描，**不是**经过验证的 Alpha：没扣费用、没处理涨停买不进、退市股不在数据里（幸存者偏差）、24 个因子同时看有多重检验问题。', '',
    ]
    for w in warnings:
        lines.append(f'> 警告：{w}')
    if warnings:
        lines.append('')
    lines += ['## 因子排名（按 IC 的 Newey-West t 降序；IC/单调/差均已乘以方向，正数 = 按声明方向有效）', '',
              '| 因子 | 类型 | IC 均值 | IC t(朴素) | IC t(NW) | 单调秩相关 | 高-低 均值 | 高-低 t(NW) | 通过全部 | 过 Bonferroni |',
              '|---|---|---|---|---|---|---|---|---|---|']
    for r in ranked.itertuples():
        lines.append(f'| {r.factor} | {r.kind} | {_fmt(r.ic_mean)} | {_fmt(r.ic_t, 2)} | {_fmt(r.ic_t_nw, 2)} | {_fmt(r.mono_rho, 2)} | '
                     f'{_fmt(r.tmb_mean)} | {_fmt(r.tmb_t_nw, 2)} | {"是" if r.passes_all else "否"} | {"是" if r.passes_bonferroni else "否"} |')
    n_pass = int(summary['passes_all'].sum())
    lines += ['', f'通过全部三项的因子：{n_pass} / {len(summary)}。即使"通过"，也只说明历史上有相关性；升级为策略必须走 docs/validation_howto.md 的流程。', '']
    path.write_text('\n'.join(lines), encoding='utf-8')


# ---------------------------------------------------------------- 主流程
def run_factor_scan(daily_dir, start, end, output_dir, *, now=None, chunk_days=CHUNK_DAYS, horizon=HORIZON, min_n=MIN_N,
                    stop_after_chunks=None, progress=print):
    s_ts, e_ts = parse_day(start), parse_day(end)
    if s_ts > e_ts:
        raise ScanError('bad_range')
    if s_ts.date() >= LOCKED_SEGMENT_START:
        raise ScanError('start_in_locked')
    warnings = []
    locked_last = pd.Timestamp(LOCKED_SEGMENT_START - timedelta(days=1))
    if e_ts > locked_last:
        warnings.append(f'--end {end} 晚于封存段起点，已截到 {locked_last.date()}；封存段日线不会被读入内存')
        e_ts = locked_last
    panel = load_history(daily_dir, e_ts, now)
    idx = panel.close.index
    days = idx[(idx >= s_ts) & (idx <= e_ts)]
    if len(days) == 0:
        raise ScanError('no_days_in_range')
    meta = {'start': start, 'end_effective': str(e_ts.date()), 'horizon': horizon, 'chunk_days': chunk_days, 'n_days': len(days),
            'first_day': str(days[0].date()), 'last_day': str(days[-1].date()), 'n_codes': int(panel.close.shape[1])}
    out = Path(output_dir)
    ck = prepare_output(out, meta)
    n_chunks = math.ceil(len(days) / chunk_days)
    t0, done = time.time(), 0
    parts = []
    for k in range(n_chunks):
        path = ck / f'chunk_{k:05d}.parquet'
        chunk_days_k = days[k * chunk_days:(k + 1) * chunk_days]
        part = None
        if path.exists():
            try:
                part = pd.read_parquet(path)
                tag = '断点'
            except Exception:
                part = None
        if part is None:
            part = scan_chunk(panel, chunk_days_k, horizon, min_n)
            _atomic_write(path, lambda p, df=part: df.to_parquet(p, index=False))
            tag = '新算'
            done += 1
        parts.append(part)
        bar = '#' * int(20 * (k + 1) / n_chunks)
        progress(f'[{bar:<20}] chunk {k + 1}/{n_chunks} {chunk_days_k[0].date()}~{chunk_days_k[-1].date()} ({tag}) 用时 {time.time() - t0:.0f}s')
        if stop_after_chunks is not None and done >= stop_after_chunks and k + 1 < n_chunks:
            raise ScanInterrupted(f'stopped after {done} new chunks')
    daily = pd.concat(parts, ignore_index=True)
    summary = summarize(daily)
    _atomic_write(out / DAILY_FILE, lambda p: daily.to_parquet(p, index=False))
    _atomic_write(out / SUMMARY_FILE, lambda p: summary.to_csv(p, index=False, encoding='utf-8'))
    write_report(out / REPORT_FILE, summary, meta, warnings)
    return {'summary': summary, 'daily_rows': len(daily), 'warnings': warnings, 'meta': meta,
            'files': {'summary': str(out / SUMMARY_FILE), 'daily': str(out / DAILY_FILE), 'report': str(out / REPORT_FILE)}}


def main(argv=None, now=None):
    ap = argparse.ArgumentParser(description='全市场历史因子扫描：IC / 单调性 / 高-低分组收益差')
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--daily-dir', default=str(ROOT / 'data' / 'daily' / 'cn_all'))
    ap.add_argument('--chunk-days', type=int, default=CHUNK_DAYS)
    args = ap.parse_args(argv)
    if args.chunk_days < 1:
        print('失败：--chunk-days 必须 >= 1')
        return 1
    try:
        result = run_factor_scan(args.daily_dir, args.start, args.end, args.output, now=now, chunk_days=args.chunk_days)
    except FileExistsError:
        print('拒绝覆盖：输出目录已有内容（且不是可续跑的断点目录），请换一个 --output')
        return 2
    except (ScanError, OSError) as exc:
        print('失败：' + MESSAGES.get(str(exc), type(exc).__name__))
        return 1
    for w in result['warnings']:
        print('警告：' + w)
    print(f"完成：{result['meta']['n_days']} 个评估日，报告 {result['files']['report']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
