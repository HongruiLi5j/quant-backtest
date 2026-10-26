"""汇总已有的因子扫描结果（T023 / C3）：不重算，只看同一个因子在互不重叠的几个时间窗里是否稳定。只读、不联网。

用法（项目根目录）：
  .venv/bin/python -B scripts/aggregate_factor_scans.py --out-dir DIR [--scan-root reports/overnight_20261023]

默认汇总 5 个互不重叠的窗口（2024 上/下半年、2025 上/下半年、2026 年一季度）；脚本会检查它们的日期真的不重叠，重叠就报错退出。
稳定的标准（事先写死）：5 个窗口里 IC 符号全部一致，且 NW t > 2 的窗口至少 3 个。
这仍是描述性的：24 个因子同时看有多重检验问题，窗口少（5 个），数据是后复权价且有幸存者偏差。
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WINDOWS = ["factor_v4_2024H1", "factor_v4_2024H2", "factor_v4_2025H1", "factor_v4_2025H2", "factor_2026Q1"]
MIN_T_WINDOWS = 3


def load(root, names):
    out = []
    for n in names:
        d = Path(root) / n
        meta, csv = d / "scan_meta.json", d / "factor_history_summary.csv"
        if not meta.is_file() or not csv.is_file():
            sys.exit(f"2: 缺少 {n} 的 scan_meta.json 或 factor_history_summary.csv")
        m = json.loads(meta.read_text(encoding="utf-8"))
        out.append({"name": n, "first": m["first_day"], "last": m["last_day"], "n_days": m["n_days"], "df": pd.read_csv(csv)})
    out.sort(key=lambda w: w["first"])
    for a, b in zip(out, out[1:]):
        if b["first"] <= a["last"]:
            sys.exit(f"2: 窗口重叠：{a['name']}({a['first']}~{a['last']}) 与 {b['name']}({b['first']}~{b['last']})")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--scan-root", default=str(ROOT / "reports" / "overnight_20261023"))
    ap.add_argument("--windows", nargs="*", default=DEFAULT_WINDOWS)
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    if out.exists() and any(out.iterdir()):
        sys.exit("2: 输出目录已存在且非空，拒绝覆盖")
    ws = load(a.scan_root, a.windows)
    facs = sorted(set.intersection(*[set(w["df"]["factor"]) for w in ws]))
    rows = []
    for f in facs:
        recs = [w["df"].loc[w["df"]["factor"] == f].iloc[0] for w in ws]
        ic = [float(r["ic_mean"]) for r in recs]
        t = [float(r["ic_t_nw"]) for r in recs]
        same = all(x > 0 for x in ic) or all(x < 0 for x in ic)
        n_t = sum(1 for x in t if abs(x) > 2)
        rows.append({"factor": f, "kind": recs[0]["kind"], "windows": len(ws), "ic_median": float(pd.Series(ic).median()),
                     "ic_min": min(ic), "ic_max": max(ic), "same_sign_all": same, "t_gt2_windows": n_t,
                     "t_by_window": " / ".join(f"{x:+.1f}" for x in t), "stable": bool(same and n_t >= MIN_T_WINDOWS)})
    df = pd.DataFrame(rows).sort_values(["stable", "t_gt2_windows"], ascending=False)
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "factor_stability.csv", index=False)
    L = ["# 因子扫描稳定性汇总（只读已有结果，未重算）", "",
         "窗口：" + "；".join(f"{w['name']}（{w['first']}~{w['last']}，{w['n_days']} 个评估日）" for w in ws), "",
         f"稳定标准（事先写死）：{len(ws)} 个窗口 IC 符号全部一致，且 NW t 绝对值 > 2 的窗口至少 {MIN_T_WINDOWS} 个。**稳定的因子：{int(df['stable'].sum())} / {len(df)}。**", "",
         "| 因子 | 类型 | IC 中位 | 各窗口 NW t | |t|>2 窗口数 | 符号一致 | 稳定 |", "|---|---|---|---|---|---|---|"]
    for r in df.itertuples():
        L.append(f"| {r.factor} | {r.kind} | {r.ic_median:+.4f} | {r.t_by_window} | {r.t_gt2_windows} | {'是' if r.same_sign_all else '否'} | {'**是**' if r.stable else '否'} |")
    L += ["", "## 局限", "", "- 只有 5 个窗口，窗口内 20 日收益重叠，NW t 只是近似；24 个因子同时看有多重检验问题（碰巧稳定的会混进来）。",
          "- 后复权价、成交额近似、幸存者偏差；没扣费用，没处理涨停买不进。", "- 描述性结果，不构成投资建议。"]
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"完成：{len(df)} 个因子，稳定 {int(df['stable'].sum())} 个 -> {out}")


if __name__ == "__main__":
    main()
