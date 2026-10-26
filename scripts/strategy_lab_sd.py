"""夏普之差的事先登记确认检验（T023 / S2）：只读本地缓存，不联网，不导入 app。

背景：T023 主跑里的「超额夏普」是主动收益序列的夏普（信息比率），不是「策略夏普−基准夏普」。
事后补算发现 ma_adx_plus 和 blend2 的夏普差中位数略大于 0（约 +0.04~+0.06）。这是看到结果后才换的指标，
所以这里在算任何结果之前把判定规则写进 PREREG_SD.json，只用前面主跑和第二轮都没用过的股票做一次确认。

用法（项目根目录）：
  .venv/bin/python -B scripts/strategy_lab_sd.py --out-dir DIR [--smoke]

事先登记的判定规则（写死，不看结果后再改）：
  - 假设策略：ma_adx_plus、blend2（共 2 个，Bonferroni：单个符号检验 p < 0.025）；
    对照策略：trend_ens、rsi2（预期约 0 或为负，用来看这套检验会不会对谁都说「显著」）。
  - 指标：每只股票的 d = 策略年化夏普 − 买入持有年化夏普（全部默认参数，成本用市场默认）。
  - 确认 = 全样本 d 的中位数 > 0 且双侧符号检验 p < 0.025，并且 2012–2018 与 2019 年起两个子区间的中位数 d 都 > 0。
  - 局限（登记时就承认）：只剩 423 只没用过的股票；它们与之前的样本处在同一段历史、同一个市场环境里，
    彼此高度相关，符号检验的 p 值把每只股票当独立样本，会偏乐观；名单是当前在市股票（幸存者偏差）。
    所以即使确认，也只说明「在这批股票上没有被推翻」，不是样本外证明。
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import MARKETS  # noqa: E402
from src import allmarket, datasource, registry  # noqa: E402

TD = 252
SPLIT = "2019-01-01"
HYP = ["ma_adx_plus", "blend2"]
CTRL = ["trend_ens", "rsi2"]
ALPHA = 0.05 / len(HYP)
MIN_DAYS = 3 * TD + 300           # 与主跑一致：至少约 4 年数据


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def ann_sharpe(r):
    r = np.asarray(r, dtype=float)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return float(r.mean() / sd * math.sqrt(TD)) if sd > 0 else float("nan")


def sharpe_diff(r, b):
    """策略年化夏普 − 基准年化夏普；任一为 nan 返回 nan。"""
    return ann_sharpe(r) - ann_sharpe(b)


def finite(xs):
    """只保留有限数（去掉 nan 和正负无穷）。"""
    return [float(x) for x in xs if x is not None and math.isfinite(x)]


def sign_test(d):
    """对 d>0 的个数做双侧符号检验（正态近似，d==0、nan、无穷都不计）。返回 (n, 正的个数, p)。"""
    d = np.asarray([x for x in finite(d) if x != 0], dtype=float)
    n = len(d)
    if n == 0:
        return 0, 0, float("nan")
    k = int((d > 0).sum())
    z = (k - n / 2) / math.sqrt(n / 4)
    return n, k, math.erfc(abs(z) / math.sqrt(2))


def sign_test_exact(d):
    """精确双侧二项检验，仅作透明的敏感性分析（登记的判定仍用正态近似）。"""
    d = [x for x in finite(d) if x != 0]
    n = len(d)
    if n == 0:
        return float("nan")
    k = sum(1 for x in d if x > 0)
    m = min(k, n - k)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(m + 1)) / 2 ** n)


def _med(xs):
    xs = finite(xs)
    return float(np.median(xs)) if xs else float("nan")


def judge(full, early, late):
    """事先登记的确认规则：三个列表分别是全样本、早期、晚期的 d（按股票）。
    非有限值不参与；同时报告各段有效只数和精确检验 p，规则本身（中位数>0、p<alpha、两段中位数>0）不变。"""
    n, k, p = sign_test(full)
    med, me, ml = _med(full), _med(early), _med(late)
    ok = bool(n > 0 and med > 0 and p < ALPHA and me > 0 and ml > 0)
    return {"n": n, "positive": k, "median_d": med, "sign_p": p, "sign_p_exact": sign_test_exact(full),
            "n_early": len(finite(early)), "n_late": len(finite(late)),
            "median_early": me, "median_late": ml, "confirmed": ok}


def fresh_codes(prereg_files):
    used = set()
    for f in prereg_files:
        pj = json.loads(Path(f).read_text(encoding="utf-8"))
        used |= set(pj["stock_sample_A"]) | set(pj["stock_sample_B"])
    lst = pd.read_csv(allmarket.LIST_FILE, dtype=str)
    return sorted(c for c in lst["code"] if datasource.is_valid_stock(c) and c not in used)


def per_stock(strat, code, base, cfg):
    prices = dict(base)
    prices[code] = allmarket.load(code)
    res = registry.run(strat, prices, code, {}, cfg, market="cn", detail=True)
    e, bn = np.asarray(res["equity"], dtype=float), np.asarray(res["bench"], dtype=float)
    r, b = e[1:] / e[:-1] - 1.0, bn[1:] / bn[:-1] - 1.0
    dates = pd.to_datetime(pd.Series(res["dates"][1:]))
    m = (dates < SPLIT).values
    return {"d": sharpe_diff(r, b), "d_early": sharpe_diff(r[m], b[m]) if m.sum() > 60 else float("nan"),
            "d_late": sharpe_diff(r[~m], b[~m]) if (~m).sum() > 60 else float("nan")}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--prereg", nargs="+", required=True, help="要排除的已用样本的 PREREG.json（主跑和第二轮）")
    ap.add_argument("--smoke", action="store_true", help="只跑 5 只股票做自检")
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    pf = out / "PREREG_SD.json"
    if pf.exists():
        sys.exit("PREREG_SD.json 已存在：拒绝覆盖。换一个输出目录。")
    codes = fresh_codes(a.prereg)
    if a.smoke:
        codes = codes[:5]
    out.mkdir(parents=True, exist_ok=True)
    pf.write_text(json.dumps({"started": time.strftime("%Y-%m-%d %H:%M"), "hypotheses": HYP, "controls": CTRL, "alpha_per_hypothesis": ALPHA,
                              "metric": "d = 策略年化夏普 − 买入持有年化夏普，默认参数，市场默认成本", "split": SPLIT,
                              "rule": "全样本 d 中位数>0 且双侧符号检验 p<alpha，且早期(<2019)与晚期中位数 d 都>0",
                              "stocks": codes, "min_days": MIN_DAYS,
                              "note": "本文件在计算任何结果之前写入，之后不得修改；指标是看到主跑结果后才换的，本检验只是确认，不是样本外证明"},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    cfg = MARKETS["cn"]
    base = datasource.load_market("cn", cfg, with_stocks=False)
    rows, fails = [], 0
    for i, code in enumerate(codes):
        try:
            if len(allmarket.load(code)) < MIN_DAYS:
                continue
        except Exception:
            fails += 1
            continue
        for s in HYP + CTRL:
            try:
                rows.append({"code": code, "strategy": s, **per_stock(s, code, base, cfg)})
            except Exception:
                fails += 1
        if (i + 1) % 50 == 0:
            log(f"{i + 1}/{len(codes)}，累计 {len(rows)} 行，出错 {fails}")
    (out / "sd_rows.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    res = {}
    for s in HYP + CTRL:
        x = [r for r in rows if r["strategy"] == s]
        res[s] = judge([r["d"] for r in x], [r["d_early"] for r in x], [r["d_late"] for r in x])
    L = ["# 夏普之差确认检验（事先登记，未经 Codex 审）", "",
         f"样本：前两轮个股横截面没跑过的 {len(codes)} 只股票（新增股票样本，不是独立复现）（实际入选 {len({r['code'] for r in rows})} 只，出错 {fails} 次）。规则见 PREREG_SD.json。", "",
         "| 角色 | 策略 | 股票数 | d>0 的只数 | d 中位数 | 符号检验 p | 早期中位数 | 晚期中位数 | 结论 |", "|---|---|---|---|---|---|---|---|---|"]
    for s in HYP + CTRL:
        v = res[s]
        role = "假设" if s in HYP else "对照"
        c = ("**确认**" if v["confirmed"] else "未确认") if s in HYP else "（对照，不下结论）"
        L.append(f"| {role} | {s} | {v['n']} | {v['positive']} | {v['median_d']:+.3f} | {v['sign_p']:.3f} | {v['median_early']:+.3f} | {v['median_late']:+.3f} | {c} |")
    L += ["", "## 局限", "", "- 这批股票与之前的样本处在同一段历史里，彼此高度相关，符号检验把每只当独立样本，这个独立性假设不成立，名义 p 值不能直接解释，可能偏乐观。",
          "- 当前在市名单，幸存者偏差；成本为估算；只有默认参数；夏普 i.i.d. 近似。",
          "- 指标是事后换的；即使「确认」，也只是在这批股票上没有被推翻，不是样本外证明，更不构成投资建议。"]
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")
    log("完成")


if __name__ == "__main__":
    main()
