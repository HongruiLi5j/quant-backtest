"""R2 前向检验的评估脚本（预注册：overnight/research/R2_forward_prereg.md）。只读。
不满 52 个合格周时，只输出每周各组的信号条数，**不计算、不输出任何收益**（防止提前偷看）。
用法：.venv/bin/python overnight/research/r2_forward_eval.py <signal_ledger.db 的副本> [--final]
--final 只有在满 52 个合格周、而且最后一周已走满 5 个交易日时才生效；否则报错退出。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src import limits  # noqa: E402

START = "2026-11-09"          # §2 只用这天及以后的信号
N_WEEKS = 52                  # §5 唯一一次判定
H = 5
BIG = 0.5                     # §3 主标签门槛：当天涨幅 ≥ 涨停幅度的一半
BUYS = ("买入", "加仓")


def calendar():
    return sorted(pd.read_csv(ROOT / "data/daily/cn_trade_calendar.csv").iloc[:, 0].astype(str))


def load_signals(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    s = pd.read_sql("SELECT day, code, name, action FROM signals WHERE strategy='blend2'", c)
    s = s[s.action.isin(BUYS) & (s.day >= START)]
    return s


def weekly_days(days, cal):
    """每个 ISO 周最后一个有信号的交易日。"""
    d = pd.Series(sorted(set(days)))
    iso = pd.to_datetime(d).dt.isocalendar()
    return d.groupby([iso.year, iso.week]).last().tolist()


def bars(code):
    p = ROOT / "data/daily/cn_all" / f"{code}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p, columns=["Open", "High", "Low", "Close", "Volume"])
    df.index = df.index.strftime("%Y-%m-%d")
    return df


def tag(code, name, D, df, cal):
    """§3：ratio = 当天涨幅 ÷ 当天涨跌停幅度（数据 ≤ D）。新股不设涨跌停的日子返回 None。"""
    if D not in df.index:
        return None
    i = df.index.get_loc(D)
    if i < 1:
        return None
    lim = limits.limit_pct(code, name or "", D, limits.listed_days(code, D))
    if lim is None:
        return None
    return (df.Close.iloc[i] / df.Close.iloc[i - 1] - 1) / lim


def counts(sig, cal):
    """不满 52 周时唯一允许的输出：每周 BIG / 非 BIG 的条数。"""
    days = weekly_days(sig.day, cal)
    rows = []
    for D in days:
        g = sig[sig.day == D]
        nb = nn = 0
        for r in g.itertuples():
            df = bars(r.code)
            t = None if df is None else tag(r.code, r.name, D, df, cal)
            if t is None:
                continue
            nb += t >= BIG
            nn += t < BIG
        rows.append({"day": D, "big": int(nb), "rest": int(nn)})
    return pd.DataFrame(rows, columns=["day", "big", "rest"])


def matured(day, cal, today):
    i = cal.index(day) if day in cal else None
    return i is not None and i + H < len(cal) and cal[i + H] <= today


def main():
    db = sys.argv[1]
    final = "--final" in sys.argv
    cal = calendar()
    sig = load_signals(db)
    c = counts(sig, cal)
    qualified = c[(c.big > 0) & (c.rest > 0)]
    today = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d")
    ready = len(qualified) >= N_WEEKS and matured(qualified.day.iloc[N_WEEKS - 1], cal, today) if len(qualified) >= N_WEEKS else False
    print(f"信号日 ≥ {START} 的合格周：{len(qualified)} / {N_WEEKS}（只输出条数）")
    print(c.to_string(index=False))
    if not final:
        return
    if not ready:
        raise SystemExit(f"不满 {N_WEEKS} 个合格周或最后一周未走满 {H} 天：按预注册不得计算收益。")
    # ---- 以下只在判定时运行（§4、§5）。写在这里是为了冻结算法；在判定日之前不会执行到。
    raise SystemExit("判定日的收益计算按 §4/§5 实现：风格格子 = 60 日波动率 × 20 日成交额各 5 档；"
                     "周差值 NW(2) 单侧 t；通过 = t ≤ −1.65 且均值 ≤ −0.30%。实现须在判定前、不看结果的情况下提交并记入偏离记录。")


if __name__ == "__main__":
    main()
