"""收盘后分析（T006 S3c）：全市场跑 24 个因子、对比监控名单与基准、判断市场状态、残差 Z-score 排名、成本会计恒等式。

只读本地文件（盘中 jsonl、日线 parquet、股票列表 csv），不联网，不改信号账本/策略；
结果写进一个全新的输出目录（拒绝覆盖已有内容）。所有输出都只是"复盘与关注参考"，不是经过验证的交易建议。
A 股只用多头部分：残差 Z-score 的"后 20 名"表示"避开"，不是做空（T+1、融券受限，不能真做空）。
"""
import csv
from datetime import date, datetime, time, timedelta
import json
import math
import os
from pathlib import Path
import random
import re

import numpy as np
import pandas as pd

from . import factors as fx
from . import regime as rg
from .core import BEIJING
from .intraday import INTRADAY_DIR

CLOSE_READY = time(15, 30)       # 北京时间 15:30 前，当天 K 线视为盘中半天数据，不能用
MAX_BARS = 320                   # 每只股票最多取最近多少根日线（够 252 日动量 + 余量）
BASELINE_SIZE = 60               # 与监控名单上限一致
LOCKED_SEGMENT_START = date(2026, 4, 1)  # 封存(锁定测试)段起点:仅供 run_factor_scan.py 等历史回测/因子筛选用途引用;EOD 当日复盘不受此限制
ACCOUNTING_TOL = 0.001           # 成本会计恒等式偏差阈值：占期初净值 0.1%
TOP_N = 20
LEDGER_KEYS = ('start_nav', 'end_nav', 'price_pnl', 'dividend_pnl', 'trading_fee', 'borrow_fee', 'financing_fee')
OPTIONAL_ZERO = ('borrow_fee', 'financing_fee')      # A 股为 0 的项可省略；dividend_pnl 必填，缺了不能悄悄当 0


class EodError(ValueError):
    """输入不足或不合法；args[0] 为固定原因码。"""


# ---------------------------------------------------------------- 成本会计恒等式
def accounting_identity(ledger, tol=ACCOUNTING_TOL):
    """期末净值-期初净值 = 价格盈亏 + 股息盈亏 - 交易费用 - 借券费 - 融资费。

    返回 {'lhs','rhs','deviation','deviation_pct','ok'}；偏差 = lhs - rhs，deviation_pct = |偏差|/期初净值；> tol 则 ok=False。
    费用项按正数（成本）传入；A 股借券费/融资费为 0，字段保留以便以后扩展到融券/杠杆。
    """
    if not isinstance(ledger, dict):
        raise EodError('invalid_ledger')
    if ledger.get('dividend_pnl') is None:
        raise EodError('missing_dividend')          # 股息盈亏必填：缺了不能悄悄当 0 而让恒等式显示"通过"
    vals = {}
    for key in LEDGER_KEYS:
        v = ledger.get(key, 0.0 if key in OPTIONAL_ZERO else None)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise EodError('invalid_ledger')
        vals[key] = float(v)
    if vals['start_nav'] <= 0:
        raise EodError('invalid_ledger')
    lhs = vals['end_nav'] - vals['start_nav']
    rhs = vals['price_pnl'] + vals['dividend_pnl'] - vals['trading_fee'] - vals['borrow_fee'] - vals['financing_fee']
    dev = lhs - rhs
    pct = abs(dev) / vals['start_nav']
    return {'lhs': lhs, 'rhs': rhs, 'deviation': dev, 'deviation_pct': pct, 'ok': pct <= tol + 1e-15, **vals}


# ---------------------------------------------------------------- 读入
def load_monitored(path):
    """读 S3b 写的 jsonl，返回按首次出现顺序去重的代码列表；文件不存在/坏行都不报错。"""
    codes, seen = [], set()
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                code = item.get('code') if isinstance(item, dict) else None
                if isinstance(code, str) and code not in seen:
                    seen.add(code)
                    codes.append(code)
    except OSError:
        pass
    return codes


def _read_list(list_path):
    names, industry = None, None
    try:
        df = pd.read_csv(list_path, dtype=str)
        df['code'] = df['code'].str.zfill(6)
        df = df.drop_duplicates('code').set_index('code')
        names = df['name'] if 'name' in df else None
        industry = df['industry'] if 'industry' in df else None
    except Exception:
        pass
    return names, industry


def _day_ts(day):
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise EodError('invalid_day')
    try:
        ts = pd.Timestamp(day)
    except ValueError:
        raise EodError('invalid_day')
    return ts


def load_panel(daily_data_dir, day, now=None, list_path=None):
    """读 <目录>/*.parquet（列 Open/High/Low/Close/Volume，日期索引），截到 day（含），返回 Panel。"""
    ts = _day_ts(day)
    now = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    if now.date() < ts.date():
        raise EodError('future_day')
    drop_today = now.date() == ts.date() and now.time() < CLOSE_READY      # 盘中半天数据不能用
    cols = ('Open', 'High', 'Low', 'Close', 'Volume')
    series = {c: {} for c in cols}
    root = Path(daily_data_dir)
    if not root.is_dir():
        raise EodError('no_daily_data')
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        if not entry.name.endswith('.parquet') or not entry.is_file(follow_symlinks=False):
            continue
        try:
            df = pd.read_parquet(entry.path, columns=list(cols))
            df.index = pd.to_datetime(df.index)
        except Exception:
            continue
        df = df[df.index <= ts]      # EOD 分析按分析日截断,不限制封存段(封存只对 run_factor_scan.py 的历史回测/因子筛选生效,见 run_factor_scan.py)
        if drop_today:
            df = df[df.index < ts]
        df = df.tail(MAX_BARS)
        if df.empty:
            continue
        code = entry.name[:-len('.parquet')]
        for c in cols:
            series[c][code] = df[c].astype(float)
    if not series['Close']:
        raise EodError('no_daily_data')
    wide = {c: pd.DataFrame(series[c]).sort_index() for c in cols}
    close = wide['Close']
    close = close[close.notna().sum(axis=1) >= max(1, int(0.2 * close.shape[1]))]      # 去掉只有零星股票有数据的日期
    close = close.tail(MAX_BARS)
    if ts not in close.index:
        raise EodError('no_bar_for_day')
    wide = {c: wide[c].reindex(close.index) for c in cols}
    names, industry = _read_list(list_path or Path(daily_data_dir).parent / 'cn_all_list.csv')
    return fx.Panel(wide['Open'], wide['High'], wide['Low'], wide['Close'], wide['Volume'], names, industry)


# ---------------------------------------------------------------- 分析
def split_watch_avoid(ranked, top_n=TOP_N):
    """从已降序排好的 Z-score 序列取 关注（前 k）/ 避开（后 k）；k = min(top_n, 只数//2)，两边必定互斥。"""
    k = min(top_n, len(ranked) // 2)
    top = ranked.head(k)
    bottom = ranked.tail(k).sort_values(kind='mergesort') if k > 0 else ranked.iloc[0:0]
    if set(top.index) & set(bottom.index):
        raise AssertionError('关注/避开名单必须互斥')
    return top, bottom


def _top(series, n, ascending=False):
    s = series.dropna().sort_values(ascending=ascending, kind='mergesort')
    return list(s.index[:n])


def _set_return_corr(mask, ret, mkt, window=60):
    """触发集（前一日收盘后确定的集合）次日等权收益 与 大盘收益 的相关系数；样本 <20 返回 None。"""
    prev = mask.shift(1, fill_value=False).astype(bool).tail(window)
    r = ret.tail(window).where(prev).mean(axis=1)
    both = pd.concat([r, mkt.tail(window)], axis=1).dropna()
    if len(both) < 20 or both.iloc[:, 0].std() == 0 or both.iloc[:, 1].std() == 0:
        return None
    return float(both.iloc[:, 0].corr(both.iloc[:, 1]))


def _fmt(x, nd=4):
    return '' if x is None or (isinstance(x, float) and not math.isfinite(x)) else (f'{x:.{nd}f}' if isinstance(x, float) else str(x))


def _write_csv(path, header, rows):
    with open(path, 'x', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def run_eod_analysis(day, intraday_jsonl_path, daily_data_dir, output_dir=None, *, now=None, ledger=None,
                     list_path=None, market_series=None, seed=0, top_n=TOP_N):
    """详见模块说明。返回结果 dict（同时把 CSV + Markdown 写入 output_dir）。"""
    _day_ts(day)
    out_dir = Path(output_dir) if output_dir else INTRADAY_DIR / f'{day}_eod'
    if out_dir.is_symlink() or (out_dir.exists() and (not out_dir.is_dir() or any(out_dir.iterdir()))):
        raise FileExistsError('output_exists')          # 拒绝覆盖
    ident = accounting_identity(ledger) if ledger is not None else None
    warnings = []
    panel = load_panel(daily_data_dir, day, now=now, list_path=list_path)
    ts = pd.Timestamp(day)
    n_all = panel.close.shape[1]
    monitored = load_monitored(intraday_jsonl_path)
    if not monitored:
        warnings.append('盘中 jsonl 缺失或为空：监控名单重合度按 0 计')
    in_scope = {c for c in monitored if c in panel.close.columns}
    traded = panel.close.loc[ts].notna() & (panel.volume.loc[ts] > 0)          # 当日有成交的股票
    eligible = sorted(traded[traded].index)
    ret, mkt = panel.ret, panel.mkt_ret()
    if market_series is not None:
        mkt = market_series.pct_change(fill_method=None).reindex(panel.close.index)
        panel.market_ret = mkt

    # 三类基准（各 60 只）：随机、成交额前 60、涨幅前 60
    rng = random.Random(seed)
    amount_today, ret_today = panel.amount.loc[ts], ret.loc[ts]
    baselines = {
        'random60': set(rng.sample(eligible, min(BASELINE_SIZE, len(eligible)))),
        'amount60': set(_top(amount_today[eligible], BASELINE_SIZE)),
        'gain60': set(_top(ret_today[eligible], BASELINE_SIZE)),
    }
    base_sets = dict(baselines)
    base_sets['monitor'] = in_scope

    factor_rows, triggers = [], {}
    for fid, meta in fx.FACTORS.items():
        values = fx.compute_factor(fid, panel)
        mask = fx.trigger_mask(fid, values.tail(MAX_BARS))
        last = mask.iloc[-1] & traded.reindex(mask.columns).fillna(False) if meta.kind != 'filter' else mask.iloc[-1]
        trig = set(last[last].index)
        triggers[fid] = trig
        n_valid = int(values.iloc[-1].notna().sum()) if meta.kind == 'score' else n_all
        row = {'id': fid, 'name': meta.name, 'category': meta.category, 'kind': meta.kind, 'n_valid': n_valid,
               'n_trigger': len(trig), 'base_rate': len(trig) / n_valid if n_valid else None,
               'mkt_corr60': _set_return_corr(mask, ret, mkt)}
        for key, members in base_sets.items():
            row[f'hit_{key}'] = len(trig & members) / len(members) if members else None
        row['monitor_overlap'] = len(trig & in_scope)
        factor_rows.append(row)

    # 市场状态
    level = (1 + mkt.fillna(0)).cumprod()
    regime = rg.identify_regime(level, day)
    scaling = rg.get_scaling(regime)

    # 残差 Z-score 排名（先过风险过滤，A 股只用多头：后 N 名是"避开"）
    z = rg.get_residual_zscore(ret, mkt).loc[ts]
    ok = (fx.compute_factor('liq_filter', panel).loc[ts] & fx.compute_factor('st_filter', panel).loc[ts]
          & fx.compute_factor('susp_filter', panel).loc[ts])
    z = z[ok.reindex(z.index).fillna(False).astype(bool)].dropna()
    if z.empty:
        warnings.append('残差 Z-score 无有效值（历史不足 2×60 个交易日或过滤后为空）')
    ranked = z.sort_values(ascending=False, kind='mergesort')
    top, bottom = split_watch_avoid(ranked, top_n)
    if len(top) < top_n:
        warnings.append(f'过滤后只有 {len(ranked)} 只有 Z 值，关注/避开名单各缩为 {len(top)} 只（保证互斥）')

    def info(code):
        nm = panel.names.get(code, '') if panel.names is not None else ''
        ind = panel.industry.get(code, '') if panel.industry is not None else ''
        return (nm if isinstance(nm, str) else '', ind if isinstance(ind, str) else '')

    if ident is not None and not ident['ok']:
        warnings.append(f"成本会计恒等式偏差 {ident['deviation_pct'] * 100:.4f}% 超过 {ACCOUNTING_TOL * 100:.1f}%：账目有漏项或重复")
    if market_series is None:
        warnings.append('大盘用全市场等权收益近似（沪深300 不在本地）；日线为后复权价，成交额只是近似')

    # 输出
    os.makedirs(out_dir, mode=0o700, exist_ok=True)
    files = {}
    hit_keys = [f'hit_{k}' for k in base_sets]
    header = ['id', 'name', 'category', 'kind', 'n_valid', 'n_trigger', 'base_rate', 'mkt_corr60'] + hit_keys + ['monitor_overlap']
    files['factors'] = out_dir / 'factor_summary.csv'
    _write_csv(files['factors'], header, [[_fmt(r[h]) if isinstance(r[h], float) or r[h] is None else r[h] for h in header] for r in factor_rows])
    files['residual'] = out_dir / 'residual_zscore_rank.csv'
    res_rows = []
    for side, part in (('watch', top), ('avoid', bottom)):
        for i, (code, val) in enumerate(part.items(), 1):
            res_rows.append([side, i, code, *info(code), _fmt(float(val))])
    _write_csv(files['residual'], ['side', 'rank', 'code', 'name', 'industry', 'zscore'], res_rows)
    files['report'] = out_dir / 'eod_report.md'
    lines = [f'# 收盘后分析 {day}', '',
             '> 仅复盘与关注参考，未经样本外检验，不是买卖建议；24 个因子同时查看存在多重检验问题，触发集的收益没有扣交易成本。', '',
             '## 市场状态', f'- 状态：**{regime}**；建议敞口系数 **{scaling:.0%}**（range 100% / trend 60% / high_vol 25% / unknown 0%）',
             f'- 全市场 {n_all} 只，当日有成交 {len(eligible)} 只；监控名单 {len(monitored)} 只（有日线 {len(in_scope)} 只）', '',
             '## 因子触发', '', '| 因子 | 类型 | 触发数 | 触发率 | 与大盘相关(60日) | 监控重合 | 命中随机60 | 命中成交额60 | 命中涨幅60 |',
             '|---|---|---|---|---|---|---|---|---|']
    for r in factor_rows:
        pct = lambda v: '-' if v is None else f'{v:.1%}'
        corr = '-' if r['mkt_corr60'] is None else f"{r['mkt_corr60']:.2f}"
        lines.append(f"| {r['id']} {r['name']} | {r['kind']} | {r['n_trigger']} | {pct(r['base_rate'])} | {corr} | {r['monitor_overlap']} "
                     f"| {pct(r['hit_random60'])} | {pct(r['hit_amount60'])} | {pct(r['hit_gain60'])} |")
    lines += ['', '触发集 = score 因子取方向调整后截面前 10%；flag 为 True；filter 为通过。"与大盘相关" = 前一日触发集次日等权收益与大盘收益在近 60 日的相关系数（样本 <20 为空）。', '',
              f'## 残差 Z-score 排名（前 {len(top)} 关注 / 后 {len(bottom)} 避开，已过流动性/ST/停牌过滤）', '']
    for title, part in ((f'关注（仅多头参考）', top), ('避开（不是做空）', bottom)):
        lines += [f'### {title}', '', '| 排名 | 代码 | 名称 | 行业 | Z |', '|---|---|---|---|---|']
        lines += [f'| {i} | {c} | {info(c)[0]} | {info(c)[1]} | {v:.2f} |' for i, (c, v) in enumerate(part.items(), 1)]
        lines.append('')
    lines += ['## 成本会计恒等式']
    if ident is None:
        lines.append('- 未提供账户流水（ledger），本项跳过。结构：期末净值-期初净值 = 价格盈亏 + 股息盈亏 - 交易费用 - 借券费 - 融资费（A 股后两项为 0）。')
    else:
        lines += [f"- 期末-期初 = {ident['lhs']:.2f}；价格盈亏+股息盈亏-交易费用-借券费-融资费 = {ident['rhs']:.2f}",
                  f"- 偏差 {ident['deviation']:.2f}（占期初净值 {ident['deviation_pct'] * 100:.4f}%）：{'通过' if ident['ok'] else '警告：超过 0.1%'}"]
    lines += ['', '## 警告与局限'] + [f'- {w}' for w in warnings]
    with open(files['report'], 'x', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return {'day': day, 'regime': regime, 'scaling': scaling, 'n_codes': n_all, 'monitored': monitored,
            'factor_rows': factor_rows, 'triggers': triggers, 'top': top, 'bottom': bottom, 'accounting': ident,
            'warnings': warnings, 'files': {k: str(v) for k, v in files.items()}}
