"""A 股日线 Volume 单位（股 / 手）混用检测。只读，不改任何数据文件，不联网。

三种证据：
  1. 突变点：每只股票 log10(Volume) 的"前 w 天中位数"与"后 w 天中位数"之差，取局部极值，
     差值落在 2±0.5（即约 100 倍，范围约 32～316 倍）的点。两侧有一字板 / 涨跌停收盘的点
     标成"疑似真实缩放"（新股连续一字板、复牌一字板时成交量本来就会差上百倍）。
  2. 单日孤立点：某天与前后各 5 天中位数相差 2±0.5（约 100 倍），且当天不是一字板 / 涨跌停收盘
     ——用来抓"某一天用了东方财富快照（手），前后都是新浪（股）"这种中位数看不出来的情况。
  3. 成交额旁表交叉验证（src/turnover.py）：成交额(元) / 未复权收盘价 ≈ 当天股数，
     文件 Volume 与之比值 ≈1 为"股"，≈0.01 为"手"。
另外用 src/volume_units.py 的读取层换算做一次全量推断，并做"留出验证"：
每只股票只留最后一个旁表核实日当锚点，看推断结果和其余核实日的真实单位是否一致。

用法（副本里的数据在 data/data/daily 下）：
  .venv/bin/python scripts/volume_unit_check.py --cn-all data/data/daily/cn_all --turnover data/data/daily/turnover_cn.parquet --out <输出目录>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import volume_units as vu  # noqa: E402

JUMP_TOL = 0.5            # 突变 / 孤立点判定：|跳变 - 2| ≤ 0.5（log10）
CUTOFF = pd.Timestamp("2026-03-31")   # 研究截止日：单独统计这一天及以前的情况


def change_points(daily: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    """前后各 window 个有成交日的 log10 中位数跳变 ≈ ±2 的局部极值点。"""
    v = daily["Volume"].astype(float)
    ok = v > 0
    if ok.sum() < 2 * window + 1:
        return pd.DataFrame(columns=["date", "jump", "locked"])
    s = np.log10(v[ok])
    before = s.rolling(window).median().shift(1)
    after = s[::-1].rolling(window).median()[::-1]
    jump = after - before
    absj = jump.abs()
    peak = absj == absj.rolling(2 * window + 1, center=True, min_periods=1).max()
    hit = jump[peak & ((absj - vu.LOG_LOT).abs() <= JUMP_TOL)]
    locked = pd.Series(vu._locked_days(daily), index=daily.index)[ok]
    locked_any = locked | locked.shift(1, fill_value=False)    # 当天或前一天
    return pd.DataFrame({"date": hit.index, "jump": hit.round(3).to_numpy(),
                         "locked": locked_any.reindex(hit.index).to_numpy(dtype=bool)})


def isolated_days(daily: pd.DataFrame, half: int = 5) -> pd.DataFrame:
    """与前后各 half 天中位数相差约 100 倍、且不是一字板 / 涨跌停收盘的单日。"""
    v = daily["Volume"].astype(float)
    ok = v > 0
    if ok.sum() < 2 * half + 1:
        return pd.DataFrame(columns=["date", "dev"])
    s = np.log10(v[ok])
    arr = s.to_numpy()
    ref = np.full(len(arr), np.nan)
    win = np.lib.stride_tricks.sliding_window_view(arr, 2 * half + 1)
    ref[half:len(arr) - half] = np.median(np.delete(win, half, axis=1), axis=1)   # 去掉中间那天本身
    dev = arr - ref
    locked = vu._locked_days(daily)[ok.to_numpy()]
    hit = np.isfinite(dev) & (np.abs(np.abs(dev) - vu.LOG_LOT) <= JUMP_TOL) & ~locked
    return pd.DataFrame({"date": s.index[hit], "dev": np.round(dev[hit], 3)})


def holdout_check(daily: pd.DataFrame, verified: pd.Series) -> dict:
    """只保留最后一个核实日当锚点重新推断，和其余核实日的真实单位比较。"""
    known = verified.dropna()
    if len(known) < 2:
        return {}
    anchor = pd.Series(np.nan, index=verified.index)
    anchor[known.index[-1]] = known.iloc[-1]
    info = vu.infer_lot_flags(daily, anchor)
    test = known.index[:-1]
    truth = known[test].astype(int).to_numpy()
    pred = info.loc[test, "is_lot"].astype(int).to_numpy()
    return {"tp": int(((truth == 1) & (pred == 1)).sum()), "fn": int(((truth == 1) & (pred == 0)).sum()),
            "fp": int(((truth == 0) & (pred == 1)).sum()), "tn": int(((truth == 0) & (pred == 0)).sum())}


def scan(cn_all: Path, turnover_path: Path | None, codes=None, window: int = 10,
         end: str | None = None, holdout: bool = True) -> dict:
    """逐只股票检测，返回汇总和明细表（只读）。"""
    t0 = time.time()
    files = sorted(Path(cn_all).glob("*.parquet"))
    if codes:
        want = set(codes)
        files = [f for f in files if f.stem in want]
    table = pd.read_parquet(turnover_path) if turnover_path and Path(turnover_path).exists() else pd.DataFrame(
        columns=["date", "code", "turnover_yuan", "close_raw", "source"])
    table = table.copy()
    table["code"] = table["code"].astype(str)
    by_code = {c: g for c, g in table.groupby("code")} if len(table) else {}
    cps, isos, checks, per_code = [], [], [], []
    confusion = {"tp": 0, "fn": 0, "fp": 0, "tn": 0}
    lot_by_year: dict[int, int] = {}
    for f in files:
        code = f.stem
        daily = pd.read_parquet(f)
        daily.index = pd.DatetimeIndex(daily.index)
        daily = daily.sort_index()
        if end:
            daily = daily[daily.index <= pd.Timestamp(end)]
        if daily.empty or "Volume" not in daily.columns:
            continue
        cp = change_points(daily, window)
        cp.insert(0, "code", code)
        cps.append(cp)
        iso = isolated_days(daily)
        iso.insert(0, "code", code)
        isos.append(iso)
        rows = by_code.get(code)
        verified = vu.verified_lot_flags(daily, rows) if rows is not None else pd.Series(np.nan, index=daily.index)
        if rows is not None:
            r = rows.copy()
            r["date"] = pd.to_datetime(r["date"])
            r = r[r["date"].isin(daily.index)]
            if len(r):
                vol = daily["Volume"].reindex(r["date"]).to_numpy(dtype=float)
                cls = vu.classify_ratio(vol, (r["turnover_yuan"].astype(float) / r["close_raw"].astype(float)).to_numpy())
                checks.append(pd.DataFrame({"date": r["date"].dt.strftime("%Y-%m-%d").to_numpy(), "code": code,
                                            "source": r["source"].to_numpy(), "unit": cls}))
        info = vu.infer_lot_flags(daily, verified)
        summ = vu.summarize(info)
        lot_inferred = info["is_lot"] & (info["basis"] == vu.BASIS_INFERRED)
        for y, k in lot_inferred.groupby(info.index.year).sum().items():
            if k:
                lot_by_year[int(y)] = lot_by_year.get(int(y), 0) + int(k)
        per_code.append({"code": code, "first_date": daily.index[0].strftime("%Y-%m-%d"),
                         "last_date": daily.index[-1].strftime("%Y-%m-%d"), "rows": summ["rows"],
                         "lot_rows": summ["lot_rows"], "lot_inferred": summ["lot_inferred"],
                         "lot_rows_to_cutoff": int(info["is_lot"][info.index <= CUTOFF].sum()),
                         "switches": summ["switches"], "first_lot": summ["first_lot"], "last_lot": summ["last_lot"],
                         "verified_days": int(verified.notna().sum()), "anchor": bool(verified.notna().any()),
                         "change_points": int(len(cp)), "change_points_unlocked": int((~cp["locked"].astype(bool)).sum()),
                         "isolated_days": int(len(iso))})
        if holdout:
            h = holdout_check(daily, verified)
            for k in h:
                confusion[k] += h[k]
    cps = pd.concat(cps, ignore_index=True) if cps else pd.DataFrame(columns=["code", "date", "jump", "locked"])
    isos = pd.concat(isos, ignore_index=True) if isos else pd.DataFrame(columns=["code", "date", "dev"])
    checks = pd.concat(checks, ignore_index=True) if checks else pd.DataFrame(columns=["date", "code", "source", "unit"])
    per = pd.DataFrame(per_code)
    return {"change_points": cps, "isolated": isos, "checks": checks, "per_code": per,
            "summary": _summary(cps, isos, checks, per, confusion, lot_by_year, time.time() - t0, holdout)}


def _counts(series) -> dict:
    return {str(k): int(v) for k, v in series.items()}


def _summary(cps, isos, checks, per, confusion, lot_by_year, seconds, holdout) -> dict:
    cps = cps.copy()
    cps["date"] = pd.to_datetime(cps["date"])
    isos = isos.copy()
    isos["date"] = pd.to_datetime(isos["date"])
    unlocked = cps[~cps["locked"].astype(bool)]
    label = checks["unit"].map({0.0: "股", 1.0: "手"}).fillna("不明") if len(checks) else pd.Series(dtype=str)
    by_date = pd.crosstab(checks["date"], label) if len(checks) else pd.DataFrame()
    by_source = pd.crosstab(checks["source"], label) if len(checks) else pd.DataFrame()
    n = len(per)
    s = {
        "stocks_scanned": n,
        "stocks_with_anchor": int(per["anchor"].sum()) if n else 0,
        "change_points": {
            "total": int(len(cps)), "locked": int(cps["locked"].astype(bool).sum()), "unlocked": int(len(unlocked)),
            "stocks_any": int(cps["code"].nunique()), "stocks_unlocked": int(unlocked["code"].nunique()),
            "unlocked_up_x100": int((unlocked["jump"] > 0).sum()), "unlocked_down_x100": int((unlocked["jump"] < 0).sum()),
            "unlocked_by_year": _counts(unlocked.groupby(unlocked["date"].dt.year).size()),
            "locked_by_year": _counts(cps[cps["locked"].astype(bool)].groupby(cps["date"].dt.year).size()),
            "unlocked_to_cutoff": int((unlocked["date"] <= CUTOFF).sum()),
            "stocks_unlocked_to_cutoff": int(unlocked.loc[unlocked["date"] <= CUTOFF, "code"].nunique()),
            "unlocked_by_date_since_2026_09": _counts(unlocked[unlocked["date"] >= "2026-09-01"].groupby(
                unlocked["date"].dt.strftime("%Y-%m-%d")).size()),
        },
        "isolated_days": {
            "total": int(len(isos)), "stocks": int(isos["code"].nunique()),
            "low_x100": int((isos["dev"] < 0).sum()), "high_x100": int((isos["dev"] > 0).sum()),
            "by_year": _counts(isos.groupby(isos["date"].dt.year).size()),
            "to_cutoff": int((isos["date"] <= CUTOFF).sum()),
            "top_dates": _counts(isos.groupby(isos["date"].dt.strftime("%Y-%m-%d")).size().sort_values(ascending=False).head(15)),
        },
        "turnover_check": {
            "rows": int(len(checks)), "stocks": int(checks["code"].nunique()) if len(checks) else 0,
            "by_unit": _counts(label.value_counts()),
            "by_date": {d: _counts(r[r > 0]) for d, r in by_date.iterrows()},
            "by_source": {d: _counts(r[r > 0]) for d, r in by_source.iterrows()},
            "by_board": {b: _counts(label[checks["code"].str[:3].map(_board) == b].value_counts())
                         for b in ("沪市主板", "深市主板", "创业板", "科创板")} if len(checks) else {},
        },
        "inference": {
            "stocks_with_lot_rows": int((per["lot_rows"] > 0).sum()) if n else 0,
            "stocks_with_inferred_lot_rows": int((per["lot_inferred"] > 0).sum()) if n else 0,
            "stocks_with_lot_rows_to_cutoff": int((per["lot_rows_to_cutoff"] > 0).sum()) if n else 0,
            "lot_rows_total": int(per["lot_rows"].sum()) if n else 0,
            "lot_rows_inferred": int(per["lot_inferred"].sum()) if n else 0,
            "lot_rows_to_cutoff": int(per["lot_rows_to_cutoff"].sum()) if n else 0,
            "rows_total": int(per["rows"].sum()) if n else 0,
            "inferred_lot_rows_by_year": {str(k): v for k, v in sorted(lot_by_year.items())},
            "switches_distribution": _counts(per["switches"].clip(upper=10).value_counts().sort_index()) if n else {},
        },
        "holdout_validation": confusion if holdout else None,
        "seconds": round(seconds, 1),
    }
    return s


def _board(prefix: str) -> str:
    if prefix in ("688", "689"):
        return "科创板"
    if prefix in ("300", "301"):
        return "创业板"
    return "沪市主板" if prefix.startswith("6") else "深市主板"


def main(argv=None):
    from src import allmarket, turnover
    ap = argparse.ArgumentParser(description="检测 A 股日线 Volume 股/手单位混用（只读）")
    ap.add_argument("--cn-all", default=str(allmarket.ALL), help="日线 parquet 目录")
    ap.add_argument("--turnover", default=str(turnover.OUT), help="成交额旁表 parquet")
    ap.add_argument("--out", help="明细输出目录（不填只打印汇总）")
    ap.add_argument("--codes", nargs="*", help="只查这些代码")
    ap.add_argument("--window", type=int, default=10, help="中位数窗口（有成交日数）")
    ap.add_argument("--end", help="只用这一天及以前的日线")
    ap.add_argument("--no-holdout", action="store_true", help="跳过留出验证（快一倍）")
    a = ap.parse_args(argv)
    res = scan(Path(a.cn_all), Path(a.turnover) if a.turnover else None, a.codes, a.window, a.end, not a.no_holdout)
    text = json.dumps(res["summary"], ensure_ascii=False, indent=1)
    print(text)
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(text, encoding="utf-8")
        res["change_points"].to_csv(out / "change_points.csv", index=False)
        res["isolated"].to_csv(out / "isolated_days.csv", index=False)
        res["checks"].to_csv(out / "turnover_check.csv", index=False)
        res["per_code"].to_csv(out / "per_code.csv", index=False)
    return res


if __name__ == "__main__":
    main()
