"""T008 S5:把 S1-S5 串起来的命令行(设计文档第 9、10 节)。

用法:
  python3 -B scripts/replay_run.py --start 2024-01-01 --end 2026-03-31 --universe 500 --out /新目录            (默认 dry-run)
  python3 -B scripts/replay_run.py --start 2024-01-01 --end 2026-03-31 --universe 500 --out /新目录 --execute  (真实回放,由所有者手动启动)

默认 dry-run:只校验清单与参数、检查输出目录,打印计划,不读行情、不写任何文件。
--execute 才会读 --daily-dir 的日线(只读)并依次做:
  S1/S2 单因子单调性(训练段) → S3 逐日回放(股票池 = 起点之前成交额前 N 只)→ S4 训练段淘汰 + 验证段只看
  → S5 幸存者组合(投票/在线加权)+ 市场状态过滤 → 报告。
- 输出到 --out(必须不存在或为空目录,不能在仓库 data/ 或 --daily-dir 里面),拒绝覆盖;不写 data/;不联网。
- 北京时间 15:30 前的当天 K 线丢弃;行情再截到验证段末,封存段不读、不看(封存段只能走 LockedTestGuard,这里不提供入口)。
- 淘汰/幸存冻结文件与试验账本在固定位置(handoff/data/replay/),同一份清单只能淘汰一次;重跑会读冻结结果,不会重来。
- 回放结果好不代表策略有效:过拟合、多重检验(m=网格组合总数)、样本量、成本、幸存者偏差五类局限见报告末尾。
"""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from src.replay import engine, ensemble, factors, monotonic, registry, selection  # noqa: E402
from src.validation.trials import TrialLog  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
DEFAULT_DAILY = ROOT / "data" / "daily" / "cn_all"
FIVE_LIMITS = [
    "过拟合:约 490 个网格组合里挑出幸存者,训练段表现是样本内的;组合方式/半衰期/top_n 也是看过幸存者后加的自由度",
    "多重检验:Holm 的 m = 清单网格组合总数(+ 组合变体数),淘汰者不扣除;经验 p 下限为 1/(null_k+1),null_k 不够时 Holm 后不可能显著",
    "样本量:验证段约 9 个月、约 40 周,置信区间很宽;20 日标签重叠",
    "成本:费率为占位值(需按广发实际费率核对),最低佣金在组合层面忽略,涨跌停/停牌用 ±9.9% 近似",
    "幸存者偏差:股票池来自现存股票的日线缓存,没有时点状态表(ST/次新/退市)过滤",
]


class RunError(ValueError):
    pass


def check_out_dir(out: Path, daily_dir: Path | None = None) -> None:
    """拒绝覆盖:输出目录必须不存在或为空;不能在仓库 data/ 或行情目录里面。"""
    rp = out.resolve()
    data_dir = (ROOT / "data").resolve()
    if rp == data_dir or data_dir in rp.parents:
        raise RunError("输出目录不能在仓库 data/ 里面")
    if daily_dir is not None:
        dr = Path(daily_dir).resolve()
        if rp == dr or dr in rp.parents:
            raise RunError("输出目录不能在行情目录里面")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise FileExistsError(f"输出目录已有内容,拒绝覆盖: {out}")


def check_dates(start: str, end: str, cat: dict) -> None:
    seg = selection.segments(cat)
    try:
        t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    except ValueError as e:
        raise RunError(f"日期无法解析: {e}") from None
    if t1 < t0:
        raise RunError("end 早于 start")
    if t1 > pd.Timestamp(seg["valid"][1]):
        raise RunError(f"end 不能晚于验证段末 {seg['valid'][1]}:封存段({seg['holdout'][0]} 起)不在这里读")


def plan(args, cat: dict, chash: str) -> dict:
    ids = [s for s in args.strategies.split(",") if s] or factors.scorable_ids()
    unknown = [s for s in ids if s not in factors.scorable_ids()]
    if unknown:
        raise RunError(f"不是可回放的非对照策略: {unknown}")
    strats = engine.all_grid_strategies(ids, include_controls=False)
    return {"catalog_hash": chash, "catalog_version": cat.get("catalog_version"), "start": args.start, "end": args.end,
            "universe_top_n_by_amount": args.universe, "n_strategy_ids": len(ids), "n_grid_combos": len(strats),
            "n_controls": registry.CONTROL_COUNT, "m_trials": selection.m_trials(cat),
            "segments": {k: list(v) for k, v in selection.segments(cat).items()}, "top_n": args.top_n,
            "null_k": args.null_k, "seed": args.seed, "n_boot": args.n_boot, "out": str(args.out),
            "executes": bool(args.execute)}


def clean_nan(obj):
    """递归把 NaN/inf 换成 None(单调性报告在样本不足时会有 NaN;后面的哈希与 JSON 落盘都不接受 NaN)。"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_nan(v) for v in obj]
    return obj


def _controls() -> list:
    return [{"id": s["id"], "params": {}} for s in registry.load_catalog()["strategies"]
            if s["id"].startswith(registry.CONTROL_PREFIX)]


def run_pipeline(panel: dict, start, end, out, universe: int, strategy_ids=None, top_n: int = 20, null_k: int = 1000,
                 seed=0, n_boot: int = 1000, industry: dict | None = None, registry_path=selection.FREEZE_DEFAULT,
                 daily_dir=None, cost_params=None) -> dict:
    """S1-S5 全流程(panel 为已按 15:30 规则处理的 {代码: OHLCV DataFrame})。返回报告字典并把文件写进 out。"""
    out = Path(out)
    cat, chash = registry.load_validated()
    check_dates(str(start), str(end), cat)
    check_out_dir(out, daily_dir)
    seg = selection.segments(cat)
    cut_end = pd.Timestamp(seg["valid"][1])
    panel = {c: factors._norm(df).loc[lambda d: d.index <= cut_end] for c, df in panel.items()}
    panel = {c: df for c, df in panel.items() if len(df)}
    codes = engine.select_universe(panel, start, universe)           # 起点之前的成交额排名,不用未来信息
    sub = {c: panel[c] for c in codes}
    ids = list(strategy_ids) if strategy_ids else factors.scorable_ids()
    real = engine.all_grid_strategies(ids, include_controls=False)
    strats = real + _controls()

    mono = monotonic.check_all(sub, "train", industry=industry, n_boot=n_boot, seed=int(seed), only=ids)   # S1/S2
    mono = clean_nan(mono)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "replay.sqlite"
    info = engine.run_replay(panel, start, end, db, strats, top_n=top_n, universe_n=universe, industry=industry,
                             seed=int(seed), cost_params=cost_params)                                      # S3
    rows = engine.load_rows(db)
    Wf, Ws = factors.Wide(panel, None), factors.Wide(sub, industry)
    cp = cost_params if cost_params is not None else engine.CostParams()
    eng = engine.Engine(Ws.o, Ws.v, cost_params=cp, close=Ws.c)
    npanel = ensemble.build_null_panel(eng, Ws.idx)

    tlog = TrialLog(selection.trial_log_path())
    record = selection.train_eliminate(rows, real, tlog, mono, seed=seed, null_k=null_k, panel=npanel,
                                       registry_path=registry_path)                                        # S4 训练段
    valid = selection.validate_survivors(rows, record, npanel, registry_path=registry_path) if record["survivors"] \
        else {"kind": "T008_S4_valid_report", "items": [], "n_survivors": 0, "note": "没有幸存者"}         # S4 验证段
    ps = {it["key"]: it["net_weekly"].get("p_value") for it in valid.get("items", []) if it["net_weekly"].get("evaluable")}
    ens = ensemble.ensemble_study(rows, record["survivors"], npanel, Wf.mret, ps, seed=record["seed"],
                                  null_k=null_k, top_n=top_n, cost_params=cp, catalog=cat)                 # S5
    meta = {"kind": "T008_S5_run_meta", "catalog_hash": chash, "start": str(start), "end": str(end),
            "universe": universe, "n_codes": len(codes), "universe_codes_hash": selection.panel_codes_hash(codes),
            "strategy_ids": ids, "n_grid_combos": len(real), "top_n": top_n, "null_k": null_k, "seed": str(seed),
            "n_boot": n_boot, "replay": info, "mask_source": eng.mask_source, "cost": engine.asdict(cp),
            "survivor_bias_warning": "存在幸存者偏差/状态未过滤", "created": datetime.now(BEIJING).isoformat(timespec="seconds")}
    report = {"meta": meta, "monotonic_summary": {
        "checked": sum(1 for r in mono["results"] if r["status"] == "checked"),
        "n_params_passed": sum(r["n_passed"] for r in mono["results"]),
        "n_params": sum(r["n_params"] for r in mono["results"])}, "train": record, "valid": valid, "ensemble": ens}
    for name, obj in (("run_meta.json", meta), ("monotonic_report.json", mono), ("train_record.json", record),
                      ("valid_report.json", valid), ("ensemble_report.json", ens)):
        selection.write_json_once(obj, out / name)
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8")
    return report


def _f(x, d=4):
    return "n/a" if x is None else (f"{x:.{d}f}" if isinstance(x, (int, float)) else str(x))


def render_markdown(rep: dict) -> str:
    m, tr, va, en = rep["meta"], rep["train"], rep["valid"], rep["ensemble"]
    L = ["> **存在幸存者偏差/状态未过滤**(股票池来自现存股票日线缓存,无时点状态表)。回放结果好不代表策略有效。", "",
         "# T008 逐日回放报告(S1-S5)", "",
         f"- 清单哈希 {m['catalog_hash']};区间 {m['start']} ~ {m['end']};股票池 {m['n_codes']} 只(起点前成交额排名);"
         f"网格组合 {m['n_grid_combos']};top_n={m['top_n']};null_k={m['null_k']};seed={m['seed']}",
         f"- 成本为占位费率,涨跌停/停牌口径:{m['mask_source']}",
         f"- 单调性(训练段):检验 {rep['monotonic_summary']['checked']} 个策略,通过的参数组合 "
         f"{rep['monotonic_summary']['n_params_passed']}/{rep['monotonic_summary']['n_params']}", "",
         "## 训练段淘汰(一次性)", "",
         f"- 幸存 {len(tr['survivors'])},淘汰 {len(tr['eliminated'])},无法判定 {len(tr['undetermined'])},"
         f"m(Holm)= {tr['m_trials']}", ""]
    L += [f"- 幸存者:{k}" for k in tr["survivors"]] or ["- (无幸存者)"]
    L += ["", "## 验证段(只看,不调参)", ""]
    for it in va.get("items", []):
        b = it["net_weekly"]
        L.append(f"- {it['key']}:周均净超额 {_f(b.get('score'), 6)},经验 p {_f(b.get('p_value'))},Holm p {_f(it.get('holm_p'))},"
                 f"成本翻倍 {'为正' if it.get('cost_x2_positive') else '非正'},月换手 {_f(it['monthly_turnover'], 2)},"
                 f"参数孤岛 {'是' if it['param_island'] else '否'},优于随机对照 {'是' if it['beats_control'] else '否'}")
    if not va.get("items"):
        L.append("- (无幸存者,无可报告)")
    L += ["", "## 幸存者组合与市场状态过滤", ""]
    if en["status"] != "ok":
        L.append(f"- {en['note']}")
    else:
        t = en["vol_threshold"]
        L += [f"- 波动率阈值(训练段 {t['pct']:.0f} 分位,冻结):{t['threshold']:.5f};60 日均线下或高波动 → 仓位 ×{en['params']['reduce']};"
              f"单票 ≤{en['params']['max_single']:.0%},总仓 ≤{en['params']['max_gross']:.0%}",
              f"- 在线加权:半衰期 {en['params']['halflife']} 日(λ={en['params']['lambda']:.4f}),向等权收缩 {en['params']['shrink']}",
              f"- Holm 的 m(含组合变体)= {en['m_with_ensembles']};经验 p 下限卡住显著性:{'是' if en['empirical_p_floor_blocks_holm'] else '否'}",
              f"- 随机对照(验证段)周均净超额:{', '.join(f'{k}={_f(v, 6)}' for k, v in en['controls']['mean_weekly_net_excess'].items())}", ""]
        for mode, o in en["modes"].items():
            b = o["raw_valid_vs_null"]
            L += [f"### {mode}",
                  f"- 满仓不过滤(选股质量):周均净超额 {_f(b.get('score'), 6)},随机对照零分布均值 {_f(b.get('null_mean'), 6)},"
                  f"经验 p {_f(b.get('p_value'))},Holm p {_f(o['holm_p'])},显著 {'是' if o['holm_significant'] else '否'},"
                  f"优于随机对照 {'是' if o['beats_control'] else '否'}",
                  f"- 成本翻倍:选股质量{'仍为正' if o['cost_x2_positive'] else '非正'};风控后年化收益 "
                  f"{_f(o['cost_x2_managed_valid']['ann_return_cost_x1'])} → {_f(o['cost_x2_managed_valid']['ann_return'])}"]
            for name in ("raw", "managed_nofilter", "managed"):
                mt = o["metrics"][name]["valid"]
                L.append(f"- 验证段 {name}:累计 {_f(mt.get('cum_return'))},年化 {_f(mt.get('ann_return'))},"
                         f"波动 {_f(mt.get('ann_vol'))},最大回撤 {_f(mt.get('max_drawdown'))},月换手 {_f(mt.get('monthly_turnover'), 2)},"
                         f"平均总仓 {_f(mt.get('mean_gross_exposure'), 2)},降仓天数占比 {_f(mt.get('cut_days_frac'), 2)}")
            nb = o["neighborhood"]
            L.append(f"- 参数邻域:{'存在参数孤岛' if nb['param_island'] else '相邻档位均盈利'};"
                     + ";".join(f"{i['param']}={i['value']}:{_f(i['mean_daily_net_excess'], 6)}" for i in nb["neighbors"]))
            L += [f"- 入影子池条件:{'满足' if o['shadow_pool_eligible'] else '不满足'}(只是进入影子观察,不是策略有效的证据)", ""]
    L += ["## 局限", ""] + [f"- {x}" for x in FIVE_LIMITS] + [f"- {x}" for x in selection.LIMITS]
    return "\n".join(L) + "\n"


def main(argv=None, now=None, panel_loader=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--universe", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--daily-dir", default=str(DEFAULT_DAILY))
    ap.add_argument("--execute", action="store_true", help="真实回放;不加则只做 dry-run")
    ap.add_argument("--strategies", default="")
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--null-k", type=int, default=selection.NULL_K)
    ap.add_argument("--n-boot", type=int, default=monotonic.N_BOOT)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--industry-json", default="")
    ap.add_argument("--freeze-file", default=str(selection.FREEZE_DEFAULT), help="淘汰/幸存冻结文件,默认固定位置;一般不要改")
    args =ap.parse_args(argv)
    args.out = Path(args.out)
    try:
        cat, chash = registry.load_validated()
        check_dates(args.start, args.end, cat)
        if args.universe <= 0 or args.top_n <= 0:
            raise RunError("--universe 与 --top-n 必须是正整数")
        if args.n_boot < monotonic.MIN_N_BOOT:
            raise RunError(f"--n-boot 必须 >= {monotonic.MIN_N_BOOT}")
        check_out_dir(args.out, Path(args.daily_dir))
        p = plan(args, cat, chash)
    except FileExistsError as e:
        print(f"拒绝覆盖:{e}")
        return 2
    except (RunError, registry.CatalogError) as e:
        print(f"失败:{e}")
        return 1
    print(json.dumps(p, ensure_ascii=False, indent=1))
    if not args.execute:
        print("dry-run:清单与参数校验通过,未读行情、未写任何文件。加 --execute 才会真实回放(由所有者手动启动)。")
        return 0
    from replay_factor_check import load_panel
    end_ts = pd.Timestamp(selection.segments(cat)["valid"][1])
    industry = json.loads(Path(args.industry_json).read_text(encoding="utf-8")) if args.industry_json else None
    try:
        panel = (panel_loader or load_panel)(args.daily_dir, end_ts, now)
        ids = [s for s in args.strategies.split(",") if s] or None
        rep = run_pipeline(panel, args.start, args.end, args.out, args.universe, ids, args.top_n, args.null_k,
                           args.seed, args.n_boot, industry, registry_path=args.freeze_file,
                           daily_dir=args.daily_dir)
    except FileExistsError as e:
        print(f"拒绝覆盖:{e}")
        return 2
    except (RunError, FileNotFoundError, selection.SelectionError, engine.ReplayError) as e:
        print(f"失败:{e}")
        return 1
    print(f"完成:幸存 {len(rep['train']['survivors'])} / 淘汰 {len(rep['train']['eliminated'])};报告 {args.out / 'report.md'}")
    print("提醒:" + ";".join(x.split(":")[0] for x in FIVE_LIMITS) + " —— 回放结果好不代表策略有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
