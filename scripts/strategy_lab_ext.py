"""策略验证实验室·扩展任务（T023 / E1~E4）：在主跑的基础上做稳健性检验。只读本地数据、不联网、不导入 app。

E1 成本压力：同一批个股，在默认成本的 1x / 2x / 4x 下重跑，看超额夏普的中位数和占比怎么掉。
E2 优化协议敏感性：参数优化的训练窗/检验窗换 4 种，看"选出的参数是否优于默认"的结论是否依赖切法。
E3 网格大小扫描：同一批标的，搜索空间 小/中/大，看搜得越多样本外是否越差（过拟合曲线）。
E4 多因子组合：只用主跑里"发现期（2022 年以前）FDR 显著"的因子（方向取发现期符号），等权秩平均，到确认期（2022~2023）检验。
所有规则在算结果之前写进 PREREG_EXT.json。样本直接取主跑 PREREG.json 里的 A 批（不要求独立，因为这里比较的是"同一批股票上条件变了会怎样"）。

用法（项目根目录）：
  .venv/bin/python -B scripts/strategy_lab_ext.py --out-dir DIR --main-dir lab_main目录 [--smoke] [--max-minutes 480]
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import strategy_lab as L  # noqa: E402
from config import MARKETS  # noqa: E402
from src import allmarket, datasource, registry  # noqa: E402
from src.market_monitor import factors as fx  # noqa: E402

COST_MULTS = (1.0, 2.0, 4.0)
PROTOCOLS = {"训练3年/检验1年": (3 * 252, 252), "训练5年/检验1年": (5 * 252, 252), "训练4年/检验半年": (4 * 252, 126), "训练2年/检验半年": (2 * 252, 126)}
GRIDS_LARGE = {
    "trend_ens": {"target_vol": [0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25], "buffer": [0.0, 0.005, 0.01, 0.02, 0.03]},
    "trend_vol": {"sma_len": [50, 75, 100, 125, 150, 175, 200, 250, 300], "buffer": [0.0, 0.01, 0.02, 0.03]},
    "donchian_s1": {"entry_len": [10, 15, 20, 30, 40, 60, 80], "exit_len": [5, 8, 10, 15, 20, 30]},
    "rsi2": {"trend_len": [100, 150, 200], "entry_th": [5, 10, 15, 20], "exit_ma": [3, 5, 10]},
    "ma_adx_original": {"fast": [3, 5, 10, 15, 20], "slow": [20, 40, 60, 100, 150], "adx_min": [10, 20, 30]},
    "ma_adx_plus": {"fast": [3, 5, 10, 15, 20], "slow": [20, 40, 60, 100, 150], "atr_mult": [2.0, 3.0, 4.0]},
}


def half(grid):
    """小网格：每个参数只取每隔一个的取值（至少保留 2 个）。"""
    return {k: (v[::2] if len(v[::2]) >= 2 else v[:2]) for k, v in grid.items()}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------- E1
def e1_cost(out, codes, deadline):
    cfg = MARKETS["cn"]
    base = datasource.load_market("cn", cfg, with_stocks=False)
    base_bp = cfg.get("stock_cost_bp", cfg["cost_bp"])
    rows = []
    for i, code in enumerate(codes):
        if time.time() > deadline:
            log(f"E1 到时间上限，已完成 {i}/{len(codes)} 只")
            break
        try:
            df = allmarket.load(code)
            if df is None or len(df) < 3 * L.TD + 300:
                continue
            prices = dict(base)
            prices[code] = df
        except Exception:
            continue
        for s in L.STRATS:
            for m in COST_MULTS:
                try:
                    res = registry.run(s, prices, code, {}, cfg, cost_bp=base_bp * m, market="cn", detail=True)
                    r, b = L.rets(res["equity"]), L.rets(res["bench"])
                    rows.append({"strategy": s, "symbol": code, "cost_mult": m, "sharpe": L.ann_sharpe(r),
                                 "excess_sharpe": L.excess_p(r, b)[1], "cagr": res["stats"].get("CAGR%")})
                except Exception:
                    continue
        if (i + 1) % 50 == 0:
            log(f"E1 {i + 1}/{len(codes)}，累计 {len(rows)} 行")
    (out / "e1_cost.json").write_text(json.dumps({"base_bp": base_bp, "rows": rows}, ensure_ascii=False, default=str), encoding="utf-8")
    return rows


def sum_e1(rows):
    df = pd.DataFrame(rows)
    if df.empty:
        return []
    # 只保留三个成本档都有结果的股票×策略，保证配对比较
    ok = df.groupby(["strategy", "symbol"])["cost_mult"].transform("nunique") == len(COST_MULTS)
    df = df[ok & df["excess_sharpe"].notna()]
    out = []
    for s, g in df.groupby("strategy"):
        row = {"strategy": s, "n": int(g["symbol"].nunique())}
        for m in COST_MULTS:
            x = g[g["cost_mult"] == m]["excess_sharpe"]
            row[f"med_{m:g}x"] = float(x.median())
            row[f"pos_{m:g}x"] = float((x > 0).mean())
        out.append(row)
    return out


# ---------------------------------------------------------------- E2 / E3
def with_patch(train, test, grids, fn):
    old = (L.TRAIN_D, L.TEST_D, L.GRIDS)
    L.TRAIN_D, L.TEST_D = train, test
    if grids is not None:
        L.GRIDS = grids
    try:
        return fn()
    finally:
        L.TRAIN_D, L.TEST_D, L.GRIDS = old


def run_opt(out, tag, codes, deadline, train=L.TRAIN_D, test=L.TEST_D, grids=None):
    d = out / "tmp_opt"
    d.mkdir(exist_ok=True)
    etf = with_patch(train, test, grids, lambda: L.stage_opt_etf(d))
    stk = with_patch(train, test, grids, lambda: L.stage_opt_stocks(d, tag, codes, deadline))
    return etf, stk


def e2_protocol(out, codes, deadline):
    res = {}
    for name, (tr_, te_) in PROTOCOLS.items():
        if time.time() > deadline:
            log("E2 到时间上限，跳过其余协议")
            break
        log(f"E2 协议 {name}")
        res[name] = dict(zip(("etf", "stocks"), run_opt(out, "e2", codes, deadline, tr_, te_)))
    (out / "e2_protocol.json").write_text(json.dumps(res, ensure_ascii=False, default=str), encoding="utf-8")
    return res


def e3_gridsize(out, codes, deadline):
    variants = {"小网格": {k: half(v) for k, v in L.GRIDS.items()}, "中网格(主跑)": L.GRIDS, "大网格": GRIDS_LARGE}
    res = {}
    for name, g in variants.items():
        if time.time() > deadline:
            log("E3 到时间上限，跳过其余网格")
            break
        log(f"E3 {name}（每个策略 {[len(L.combos(v)) for v in g.values()]} 组）")
        res[name] = dict(zip(("etf", "stocks"), run_opt(out, "e3", codes, deadline, grids=g)))
    (out / "e3_gridsize.json").write_text(json.dumps(res, ensure_ascii=False, default=str), encoding="utf-8")
    return res


# ---------------------------------------------------------------- E4
def e4_composite(out, main_dir, codes):
    fj = Path(main_dir) / "factors.json"
    for _ in range(360):                     # 主跑的因子体检大约 10 分钟内会产出；最多等 1 小时
        if fj.exists():
            break
        time.sleep(10)
    if not fj.exists():
        return {"error": "主跑的 factors.json 一直没有出现"}
    fdata = json.loads(fj.read_text(encoding="utf-8"))
    if fdata.get("boundary_version", 1) < L.FACTOR_BOUNDARY_VERSION:
        return {"error": f"factors.json 是旧版（boundary_version={fdata.get('boundary_version', 1)}），发现期标签可能跨入确认期，拒绝用它挑因子"}
    res = fdata["results"]
    # 规则（事先写死）：只看 score 因子、5 日和 20 日持有期里发现期 FDR 显著的；方向取发现期 IC 的符号
    pick = {}
    for r in res:
        if r["kind"] == "score" and r["h"] in (5, 20) and r["fdr_significant_disc"] and r["disc"]["mean"]:
            pick.setdefault(r["factor"], np.sign(r["disc"]["mean"]))
    if len(pick) < 2:
        return {"error": f"发现期显著的 score 因子不足 2 个（{len(pick)}）", "picked": list(pick)}
    panel = L.build_factor_panel(codes)
    close = panel.close
    ranks = []
    for fid, sg in pick.items():
        F = fx.compute_factor(fid, panel) * sg * fx.FACTORS[fid].direction
        ranks.append(F.rank(axis=1, pct=True))
    comp = sum(ranks) / len(ranks)
    out_rows = {}
    for h, gap in ((5, 5), (20, 20)):
        fwd = close.shift(-(1 + h)) / close.shift(-1) - 1
        dates = [d for d in close.index if d >= pd.Timestamp(L.FACTOR_START)][::gap]
        ics = {"disc": [], "conf": []}
        best_single = {}
        for d in dates:
            seg = L.label_segment(d, L.label_end(close.index, d, h))
            if seg == "purged":
                continue
            x, r = comp.loc[d], fwd.loc[d]
            m = x.notna() & r.notna()
            if m.sum() >= 100:
                ics[seg].append(x[m].rank().corr(r[m].rank()))
        out_rows[f"h{h}"] = {"disc": L.seg_stats(ics["disc"]), "conf": L.seg_stats(ics["conf"])}
    singles = [{"factor": r["factor"], "h": r["h"], "disc_mean": r["disc"]["mean"], "conf_mean": r["conf"]["mean"]} for r in res if r["factor"] in pick and r["h"] in (5, 20)]
    result = {"picked": {k: float(v) for k, v in pick.items()}, "composite": out_rows, "singles": singles}
    (out / "e4_composite.json").write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    return result


# ---------------------------------------------------------------- 汇总
def write_summary(out, meta, st):
    Lm = ["# 扩展检验 · 早上看这里", "", f"开始 {meta['started']}，用时 {meta.get('minutes', '?')} 分钟。规则在算结果之前已写入 PREREG_EXT.json。", "",
          "**读法提醒**：都是历史回测统计，不是预测；个股名单是当前在市股票（幸存者偏差）；这里比较的是「同一批标的上条件变了会怎样」。", ""]
    if st.get("e1"):
        Lm += ["## E1 成本压力（同一批股票，成本 1x / 2x / 4x）", "", "| 策略 | 股票数 | 中位超额夏普 1x | 2x | 4x | 超额>0 占比 1x | 2x | 4x |", "|---|---|---|---|---|---|---|---|"]
        for r in sum_e1(st["e1"]):
            Lm.append(f"| {r['strategy']} | {r['n']} | {r['med_1x']:+.2f} | {r['med_2x']:+.2f} | {r['med_4x']:+.2f} | {r['pos_1x']:.0%} | {r['pos_2x']:.0%} | {r['pos_4x']:.0%} |")
        Lm += ["", "超额夏普 = 策略相对买入持有的夏普；买入持有基准本身不受成本倍数影响。", ""]
    for key, title, what in (("e2", "E2 优化协议敏感性", "协议"), ("e3", "E3 网格大小扫描", "网格")):
        if st.get(key):
            Lm += [f"## {title}", "", f"各{what}下：样本外夏普（拼接后的中位数，选出 / 默认 / 网格中位 / 事后最优）；PBO；选出优于默认的占比。", "",
                   f"| {what} | 范围 | 策略 | 样本 | 选出 | 默认 | 网格中位 | 事后最优 | PBO | 选出>默认 | 符号检验 p |", "|---|---|---|---|---|---|---|---|---|---|---|"]
            for name, v in st[key].items():
                for scope in ("etf", "stocks"):
                    for sname, s in L.summarize_opt(v.get(scope) or []).items():
                        Lm.append(f"| {name} | {'ETF' if scope == 'etf' else '个股'} | {sname} | {s['n']} | {s['oos_selected']:.2f} | {s['oos_default']:.2f} | "
                                  f"{s['oos_grid_median']:.2f} | {s['oos_grid_best']:.2f} | {s['pbo']:.0%} | {s['share_selected_beats_default']:.0%} | {s['sign_p']:.3f} |")
            Lm.append("")
    if st.get("e4"):
        e = st["e4"]
        Lm += ["## E4 多因子组合", ""]
        if "error" in e:
            Lm += [f"未能构造：{e['error']}", ""]
        else:
            Lm += [f"入选因子（发现期 FDR 显著的 score 因子，方向取发现期符号）：{', '.join(e['picked'])}", "", "| 持有期 | 发现期 IC | t | 确认期 IC | t |", "|---|---|---|---|---|"]
            for h, v in e["composite"].items():
                f = lambda x, nd=4: "-" if x is None else f"{x:+.{nd}f}"
                Lm.append(f"| {h[1:]}日 | {f(v['disc']['mean'])} | {f(v['disc']['t'], 2)} | {f(v['conf']['mean'])} | {f(v['conf']['t'], 2)} |")
            Lm += ["", "注意：入选因子本来就是在发现期挑出来的，所以发现期数字天然偏高，只有确认期（2022~2023）才有参考价值。", ""]
    Lm += ["## 局限", "", "- 幸存者偏差；夏普的 i.i.d. 近似；成本是估算；协议和网格都是事先定的有限几种，不代表全部可能。",
           "- 多个检验并列展示，没有统一做多重检验校正，读结果时请看整体趋势而不是单个格子。", "- 不构成投资建议。"]
    (out / "MORNING_EXT.md").write_text("\n".join(Lm), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--main-dir", required=True, help="主跑目录（里面要有 PREREG.json，E4 还要 factors.json）")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--max-minutes", type=int, default=480)
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    if out.exists() and any(out.iterdir()):
        sys.exit("拒绝覆盖：输出目录已存在且非空")
    out.mkdir(parents=True, exist_ok=True)
    pj = Path(a.main_dir) / "PREREG.json"
    for _ in range(180):                     # 主跑刚启动时 PREREG 可能还没写出来
        if pj.exists():
            break
        time.sleep(5)
    A = json.loads(pj.read_text(encoding="utf-8"))["stock_sample_A"]
    FAC = json.loads(pj.read_text(encoding="utf-8"))["factor_stock_sample"]
    n1, n2, n3 = (3, 3, 3) if a.smoke else (1200, 600, 600)
    meta = {"started": time.strftime("%Y-%m-%d %H:%M")}
    (out / "PREREG_EXT.json").write_text(json.dumps({**meta, "cost_mults": COST_MULTS, "protocols": PROTOCOLS, "grids_large": GRIDS_LARGE,
                                                    "small_grid_rule": "每个参数每隔一个取值", "E1_stocks": n1, "E2_stocks": n2, "E3_stocks": n3,
                                                    "E4_rule": "score 因子；h∈{5,20}；发现期 FDR 显著；方向=发现期 IC 符号；等权秩平均；确认期检验",
                                                    "stock_source": "主跑 PREREG.json 的 A 批前 N 只"}, ensure_ascii=False, indent=1), encoding="utf-8")
    t0 = time.time()
    deadline = t0 + a.max_minutes * 60
    st = {}
    for name, key, fn in (("E1 成本压力", "e1", lambda: e1_cost(out, A[:n1], deadline)),
                          ("E2 优化协议", "e2", lambda: e2_protocol(out, A[:n2], deadline)),
                          ("E3 网格大小", "e3", lambda: e3_gridsize(out, A[:n3], deadline)),
                          ("E4 多因子组合", "e4", lambda: e4_composite(out, a.main_dir, FAC[:300] if a.smoke else FAC))):
        if time.time() > deadline:
            log(f"跳过 {name}：已到时间上限")
            continue
        try:
            log(f"开始 {name}")
            st[key] = fn()
        except Exception as e:
            log(f"阶段 {name} 失败：{type(e).__name__}: {e}")
        meta["minutes"] = round((time.time() - t0) / 60)
        write_summary(out, meta, st)
    log("全部结束")


if __name__ == "__main__":
    main()
