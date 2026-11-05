"""核实 A 股 ETF 本地日线是不是后复权（B04，T021 的待办）。只读本地文件，不联网，不写 data/。

背景：磁盘上的 A 股日线按约定是后复权（分红再投资，价格被整体抬高），而持仓页要显示不复权的真实价。
display_price 的缓存 engine_price_scale.json 存了"某一天腾讯的真实收盘价 raw"，持仓页用
  最新价 = 本地最后一根 Close × (raw / 本地最后一根 Close) = raw
所以只要缓存日期和最后一根 K 线同一天，持仓页显示的就是缓存里的真实价，和本地 K 线是不是后复权无关。

本脚本回答三个问题：
  1. 本地日线相对真实价是什么关系？f = 本地最后一根 Close / 缓存里的真实收盘价 raw：
     - f > 1：本地价比真实价高，与"分红后复权"一致（后复权因子 = 累计分红再投资，永远 >= 1）；
     - f = 1：本地价就是真实价。没分红过的品种（如黄金 ETF）后复权和不复权完全相同，此时无法区分；
     - f < 1：本地价比真实价低，不可能是单纯的分红后复权（可能有份额折算/合并等，口径另有来源）。
  2. 除息日有没有跳空（辅助证据）：不复权的价格在除息日会向下跳空约"分红÷价格"，后复权不会。
     用"开盘价 ÷ 前收盘 - 1"的稳健 σ（MAD）衡量，数 -6σ 以下和 +6σ 以上的天数。
     只有噪声远小于分红比例的品种（债券/货币 ETF）这个检验才有力；股票 ETF 的日常跳空 σ≈0.2%～0.4%，
     和一年一次 1% 左右的分红混在一起，检验没有区分力——所以以第 1 项为主，第 2 项只做佐证。
  3. 持仓页会不会显示错价：直接调用 display_price.cached_scale，看它还原出的价格是否等于缓存里的真实价。

统计区间（跳空检验）截止到 --end（默认 2026-03-31，研究截止日）。f 的比较和持仓价检查用的是缓存里
"最后一天的一个真实收盘价"，属于数据完整性核对，不是回测/收益研究。

用法（副本里的数据在 data/data/daily 下；真项目里直接不带参数）：
  .venv/bin/python scripts/check_etf_adjust.py --daily-dir data/data/daily --out <输出目录>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import display_price  # noqa: E402

END_DEFAULT = "2026-03-31"   # 研究截止日
F_EPS = 1e-6                 # f 与 1 的差小于它，就当作相等
TICK = 1e-3                  # ETF 报价最小单位 0.001 元：新浪/东方财富原始报价都落在这个格子上
SIGMA_K = 6                  # 跳空异常的阈值：6 倍稳健 σ


def load_etf(daily_dir: Path, code: str) -> pd.DataFrame:
    p = daily_dir / "cn" / f"{code}.csv"
    if not p.exists():
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    return pd.read_csv(p, index_col=0, parse_dates=True)


def integrity(df: pd.DataFrame) -> dict:
    """基本体检：重复日期、乱序、非正价格、High/Low 与 Open/Close 矛盾的行数。"""
    px = df[["Open", "High", "Low", "Close"]]
    hi_bad = df["High"] < df[["Open", "Close"]].max(axis=1) - 1e-9
    lo_bad = df["Low"] > df[["Open", "Close"]].min(axis=1) + 1e-9
    return {
        "rows": int(len(df)),
        "first": df.index[0].strftime("%Y-%m-%d") if len(df) else None,
        "last": df.index[-1].strftime("%Y-%m-%d") if len(df) else None,
        "dup_dates": int(df.index.duplicated().sum()),
        "sorted": bool(df.index.is_monotonic_increasing),
        "nonpositive": int((px <= 0).any(axis=1).sum()),
        "ohlc_inconsistent": int((hi_bad | lo_bad).sum()),
    }


def scale_check(df: pd.DataFrame, code: str, cache: dict) -> dict:
    """f = 本地最后一根 Close / 缓存里的真实收盘价；并给出判定。"""
    if df.empty:
        return {"verdict": "无本地数据", "f": None}
    last_day = df.index[-1].strftime("%Y-%m-%d")
    last = float(df["Close"].iloc[-1])
    hit = cache.get(code)
    if not isinstance(hit, dict) or hit.get("day") != last_day or not display_price._good_price(hit.get("raw")):
        return {"verdict": "缓存里没有最后一根K线当天的真实价，无法核对（持仓页会显示『无现价』）",
                "f": None, "last_day": last_day, "local_last": last}
    raw = float(hit["raw"])
    f = last / raw
    if f > 1 + F_EPS:
        verdict = "后复权（本地价高于真实价，与分红复权一致）"
    elif abs(f - 1) <= F_EPS:
        verdict = "本地价=真实价（无分红时后复权与不复权相同，无法区分）"
    else:
        verdict = "本地价低于真实价：不是单纯的分红后复权（口径另有来源，如份额折算）"
    return {"verdict": verdict, "f": f, "last_day": last_day, "local_last": last, "raw": raw}


def holdings_check(df: pd.DataFrame, code: str, cache_path: Path) -> dict:
    """调用真实的 display_price.cached_scale（只读缓存），看持仓页会显示什么。"""
    if df.empty:
        return {"shows": None, "note": "无本地数据"}
    old = display_price._SCALE_FILE
    display_price._SCALE_FILE = cache_path
    try:
        k, note = display_price.cached_scale("cn", code, df)
    finally:
        display_price._SCALE_FILE = old
    if note != "前复权":
        return {"shows": None, "note": "无现价"}
    px = round(float(df["Close"].iloc[-1]) * k, 4)
    return {"shows": px, "k": k, "note": "真实价"}


def gap_stats(df: pd.DataFrame, end: str) -> dict:
    """开盘跳空 g = Open/前收盘 - 1，截止 end。返回稳健 σ、-6σ 以下/+6σ 以上的天数、最大负跳空。"""
    d = df.loc[:end]
    g = (d["Open"] / d["Close"].shift() - 1).dropna()
    if len(g) < 100:
        return {"n": int(len(g))}
    med = g.median()
    sigma = 1.4826 * (g - med).abs().median()
    z = (g - med) / sigma if sigma > 0 else g * np.nan
    worst = g.idxmin()
    return {
        "n": int(len(g)), "sigma": float(sigma),
        "neg": int((z < -SIGMA_K).sum()), "pos": int((z > SIGMA_K).sum()),
        "worst_date": worst.strftime("%Y-%m-%d"), "worst_gap": float(g.min()),
    }


def bad_open_days(df: pd.DataFrame, end: str, jump: float = 0.05) -> list[str]:
    """开盘大跳空但收盘回到原位的日子：开盘相对前收盘跳 jump 以上，而收盘相对前收盘没有跳（涨跌幅 < jump/2）。
    这不是除息跳空（除息会让后面的收盘价一起下移）。债券/货币 ETF 日常波动极小，这种日子多半是数据源的
    开盘价错误；股票 ETF 上多半是真实行情（如 2023-08-28 印花税减半当天高开回落），需要人工看，不能直接当错误。"""
    d = df.loc[:end]
    gap = (d["Open"] / d["Close"].shift() - 1).abs()
    ret = (d["Close"] / d["Close"].shift() - 1).abs()
    return [t.strftime("%Y-%m-%d") for t in d.index[(gap > jump) & (ret < jump / 2)]]


def seam(df: pd.DataFrame, lookback: int = 60) -> dict:
    """找"尾段"：最近若干根里不在 0.001 报价格子上的连续段。
    原始报价（新浪/东方财富）都是 3 位小数；被 datasource._sina_fallback 乘过比例 k 的行会有很多位小数。
    注意：格子检验不能区分后复权和不复权（东方财富的后复权价也只保留 3 位小数），只能找出"乘过 k 的尾段"。"""
    tail = df.iloc[-lookback:]
    x = tail["Close"] / TICK
    off = ((x - x.round()).abs() > 2e-3).to_numpy()
    for s in range(len(tail)):
        if off[s] and off[s:].mean() >= 0.9:
            vol_before = float(df["Volume"].iloc[-lookback + s - 5:-lookback + s].median()) if s else float("nan")
            vol_after = float(tail["Volume"].iloc[s:s + 5].median())
            return {"tail_start": tail.index[s].strftime("%Y-%m-%d"), "tail_rows": int(len(tail) - s),
                    "volume_ratio_at_seam": vol_after / vol_before if vol_before and vol_before > 0 else None}
    return {"tail_start": None, "tail_rows": 0, "volume_ratio_at_seam": None}


def analyze(daily_dir: Path, codes: list[str], names: dict, end: str) -> list[dict]:
    cache_path = daily_dir / "engine_price_scale.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    out = []
    for code in codes:
        df = load_etf(daily_dir, code)
        out.append({
            "code": code, "name": names.get(code, ""),
            "integrity": integrity(df) if len(df) else {"rows": 0},
            "scale": scale_check(df, code, cache),
            "holdings": holdings_check(df, code, cache_path),
            "gap": gap_stats(df, end) if len(df) else {},
            "bad_open_days": bad_open_days(df, end) if len(df) else [],
            "seam": seam(df) if len(df) > 60 else {},
        })
    return out


def render(res: list[dict], end: str) -> str:
    L = [f"ETF 后复权核实（跳空统计截止 {end}；f 与持仓价使用缓存里最后一天的真实收盘价）", ""]
    for r in res:
        s, h, g, i, q = r["scale"], r["holdings"], r["gap"], r["integrity"], r["seam"]
        L.append(f"== {r['code']} {r['name']}  {i.get('first')} ~ {i.get('last')}  {i.get('rows')} 行")
        L.append(f"  体检: 重复日期 {i.get('dup_dates')}, 乱序 {not i.get('sorted', True)}, 非正价格 {i.get('nonpositive')}, "
                 f"High/Low 矛盾 {i.get('ohlc_inconsistent')}; 开盘大跳空后收回的日子 {r['bad_open_days'] or '无'}")
        if s.get("f") is not None:
            L.append(f"  本地最后收盘 {s['local_last']:.6f}  真实收盘(缓存 {s['last_day']}) {s['raw']}  f={s['f']:.6f}")
        L.append(f"  判定: {s['verdict']}")
        if g.get("sigma") is not None:
            L.append(f"  开盘跳空: σ={g['sigma'] * 100:.4f}%, 低于-{SIGMA_K}σ {g['neg']} 天, 高于+{SIGMA_K}σ {g['pos']} 天, "
                     f"最大负跳空 {g['worst_gap'] * 100:.2f}% ({g['worst_date']})")
        if q.get("tail_start"):
            ratio = q["volume_ratio_at_seam"]
            L.append(f"  尾段(乘过比例k的新浪原始价): {q['tail_start']} 起 {q['tail_rows']} 行, 接缝处成交量中位数之比 "
                     f"{'无' if ratio is None else f'{ratio:.1f}'}")
        L.append(f"  持仓页最新价: {h['shows'] if h.get('shows') is not None else '无现价'}")
        L.append("")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--daily-dir", type=Path, default=None, help="日线目录（含 cn/ 子目录和 engine_price_scale.json），默认 src.datasource.CACHE")
    ap.add_argument("--codes", nargs="*", default=None, help="默认 config.MARKETS['cn']['universe'] 里的 ETF")
    ap.add_argument("--end", default=END_DEFAULT, help="跳空统计的截止日期")
    ap.add_argument("--out", type=Path, default=None, help="输出目录；给了就写 check_etf_adjust.json")
    a = ap.parse_args(argv)
    from config import MARKETS
    from src import datasource
    names = dict(MARKETS["cn"]["universe"])
    daily_dir = a.daily_dir or datasource.CACHE
    res = analyze(daily_dir, a.codes or list(names), names, a.end)
    print(render(res, a.end))
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "check_etf_adjust.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
