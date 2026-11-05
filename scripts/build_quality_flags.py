"""D110：扫描全市场前复权/后复权日线里「任何涨跌停制度下都不可能」的日涨跌，写出数据质量标记文件
data_quality/cn_all_quality.json（进仓库，几 KB）。只读 data/daily/cn_all，不改任何行情文件，不删数据。

规则（写死，不调参）：
  不可能的一天 = 日涨幅 > +100% 或 日跌幅 < -50%（上市前 5 个交易日除外），或 收盘 < 0.1（复权价近零）。
  同一只股票里相隔 ≤ 220 天的不可能日合并成一段。
  一段里不可能日 ≥ 4 天 → kind="mask"：整段 [from, to] 在全市场统计里置 NaN（allmarket.load 默认处理）。
  否则 → kind="flag"：只标记，不置空（可能是真实重组/复牌，需人工看）。
运行：.venv/bin/python scripts/build_quality_flags.py [--write]   （不加 --write 只打印）
"""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import allmarket  # noqa: E402

OUT = ROOT / "data_quality" / "cn_all_quality.json"
UP, DOWN, LOWP, GAP, MASK_MIN_DAYS = 1.0, -0.5, 0.1, 220, 4


def scan_one(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    cl = df["Close"]
    r = cl.pct_change(fill_method=None)
    bad = ((r > UP) | (r < DOWN) | (cl < LOWP)).fillna(False)
    bad.iloc[:5] = False
    days = list(df.index[bad.values])
    segs, cur = [], []
    for d in days:
        if cur and (d - cur[-1]).days > GAP:
            segs.append(cur); cur = []
        cur.append(d)
    if cur:
        segs.append(cur)
    return [{"from": s[0].strftime("%Y-%m-%d"), "to": s[-1].strftime("%Y-%m-%d"), "n": len(s),
             "kind": "mask" if len(s) >= MASK_MIN_DAYS else "flag",
             "reason": "日涨跌幅超出任何涨跌停制度允许的范围（>+100% 或 <-50%）或复权价近零（<0.1）"} for s in segs]


def main(write: bool) -> None:
    codes = allmarket.cached_codes(delisted=False)
    res = {}
    for c in codes:
        segs = scan_one(allmarket.load(c, raw=True))   # 原始数据：≤0 的行也算不可能日
        if segs:
            res[c] = segs
    doc = {"version": 1, "rule": f"day>{UP:+.0%} or day<{DOWN:.0%} or close<{LOWP}; merge gap {GAP}d; mask if >= {MASK_MIN_DAYS} days else flag",
           "scanned": len(codes), "codes": dict(sorted(res.items()))}
    for c, segs in doc["codes"].items():
        print(c, segs)
    if write:
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("wrote", OUT)


if __name__ == "__main__":
    main("--write" in sys.argv)
