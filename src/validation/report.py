"""统一验证报告(T007 / S7):把切分、滚动前进、成本压力、试验折扣、绩效统计串成一份 Markdown。

纯函数:不联网、不读真实数据目录;只有 write_report 会写调用者指定的那一个路径(拒绝覆盖)。
读取 TrialLog 只是读(账本文件不存在视为空,不会被创建)。

诚实原则:报告只描述"没能证伪 / 证据不足",不给"策略有效、可以实盘"的结论。
缺少输入的章节写"无法检验"并说明原因,绝不当作通过。

成本压力(收益序列路径)的公式(与设计文档一致,乘性,不用加减近似):
  c' = base_cost × 倍数 + 额外滑点bp/1e4,T 为当日换手
  cost_included=True  :r' = (1+r)(1−c'T)/(1−base_cost·T) − 1   (先还原已扣的基准费用再按新费率重扣)
  cost_included=False :r' = (1+r)(1−c'T) − 1                   (外部给的税前收益,不还原)
"""
from __future__ import annotations

import math
import os
from typing import Optional

from . import adapter as _ad
from . import splits as _sp
from . import stats as _st
from . import trials as _tr

__all__ = ["ReportError", "DISCLAIMER", "cost_stress", "warnings_for", "build_report",
           "build_report_from_arrays", "write_report"]

DISCLAIMER = "仅供研究,不构成投资建议,回测与模拟结果不代表未来"

MIN_TRADES = 30
MIN_YEARS = 3.0
MIN_PAPER_DAYS = 20
DEFAULT_TRIAL_THRESHOLD = 20
_UNIT = ("train", "valid", "embargo", "wf_train_len", "wf_test_len", "wf_anchored", "rf", "strategy",
         "trial_threshold", "decay_warn", "locked_open_count", "paper_days", "n_boot", "boot_block", "seed",
         "multipliers", "slip_bps")


class ReportError(ValueError):
    """报告输入不合法或输出路径被拒绝(已存在且未 force 等)。"""


# ---------------------------------------------------------------- 小工具

def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _f(x, nd=2) -> str:
    return f"{x:.{nd}f}" if _isnum(x) else "—"


def _pct(x, nd=1) -> str:
    return f"{x * 100:.{nd}f}%" if _isnum(x) else "—"


def _params(params) -> dict:
    p = dict(params or {})
    bad = [k for k in p if k not in _UNIT]
    if bad:
        raise ReportError(f"未知的 params 键: {bad}(可用: {list(_UNIT)})")
    return p


# ---------------------------------------------------------------- 成本压力(收益序列路径)

def cost_stress(returns, turnover=None, base_cost=None, cost_included=None, multipliers=(1, 2, 3),
                slip_bps=(0, 5, 10), periods: int = 252) -> dict:
    """换手 × 成本倍数的压力测试。缺任一必需输入返回 status="unavailable"(不抛错、不猜)。

    base_cost 是 1x 场景的单边成本率:cost_included=True 时也是收益里已扣的费率;False 时只是场景基数。
    参数本身非法(长度不一致、base_cost<0、倍数为空、c'T ≥ 1)才抛 ValueError。"""
    missing = []
    if turnover is None:
        missing.append("缺少换手")
    if base_cost is None:
        missing.append("缺少基准成本")
    if cost_included is None:
        missing.append("未声明收益是否已含费")
    if missing:
        return {"status": "unavailable", "reason": "、".join(missing)}
    rets = list(returns)
    turn = list(turnover)
    if len(rets) != len(turn):
        raise ValueError(f"turnover 与 returns 长度不一致: {len(turn)} vs {len(rets)}")
    if not _isnum(base_cost) or base_cost < 0:
        raise ValueError("base_cost 必须是非负有限数")
    mults, slips = list(multipliers), list(slip_bps)
    if not mults or not slips:
        raise ValueError("multipliers 与 slip_bps 不能为空")
    if any(not _isnum(m) or m < 0 for m in mults) or any(not _isnum(s) or s < 0 for s in slips):
        raise ValueError("multipliers 与 slip_bps 必须是非负有限数")
    if any(not _isnum(t) or t < 0 for t in turn):
        raise ValueError("turnover 必须是非负有限数")
    if any(base_cost * t >= 1 for t in turn):
        raise ValueError("base_cost × 换手 ≥ 1,无法还原已扣费用")
    rows = []
    for m in mults:
        for s in slips:
            c = base_cost * m + s / 1e4
            if any(c * t >= 1 for t in turn):
                raise ValueError(f"成本率 {c} × 换手 ≥ 1,收益会 ≤ -100%")
            if cost_included:
                new = [(1 + r) * (1 - c * t) / (1 - base_cost * t) - 1 for r, t in zip(rets, turn)]
            else:
                new = [(1 + r) * (1 - c * t) - 1 for r, t in zip(rets, turn)]
            ps = _st.perf_stats(new, periods=periods)
            rows.append({"multiplier": m, "slip_bps": s, "cost_rate": c, "sharpe": ps["sharpe"],
                         "ann_return": ps["ann_return"], "cum_return": ps["cum_return"]})
    return {"status": "ok", "reason": None, "cost_included": bool(cost_included), "base_cost": base_cost,
            "avg_turnover": math.fsum(turn) / len(turn) if turn else None, "rows": rows}


# ---------------------------------------------------------------- 各章节的计算

def _slice(seq, span):
    return list(seq[span[0]:span[1]])


def _segments(rets, dates, p) -> dict:
    """三段切分与各段指标。切不开(太短等)时返回 status="error"。"""
    embargo = p.get("embargo", 5)
    try:
        sp = _sp.three_way_split(dates, train=p.get("train", 0.6), valid=p.get("valid", 0.2), embargo=embargo)
        cmp_ = _st.compare_segments(_slice(rets, sp.train), _slice(rets, sp.valid), _slice(rets, sp.test),
                                    rf=p.get("rf", 0.0), decay_warn=p.get("decay_warn", 0.5))
    except (_sp.SplitError, _st.StatsError) as e:
        return {"status": "error", "reason": str(e), "embargo": embargo}
    spans = {"train": sp.train, "valid": sp.valid, "test": sp.test}
    ranges = {k: (str(dates[v[0]]), str(dates[v[1] - 1])) for k, v in spans.items()}
    return {"status": "ok", "embargo": embargo, "spans": spans, "ranges": ranges, "compare": cmp_}


def _walk_forward(rets, dates, p) -> dict:
    tl, sl = p.get("wf_train_len", 252), p.get("wf_test_len", 63)
    default = "wf_train_len" not in p and "wf_test_len" not in p
    try:
        folds = _sp.walk_forward(dates, tl, sl, embargo=p.get("embargo", 5),
                                 anchored=bool(p.get("wf_anchored", False)))
    except _sp.SplitError as e:
        return {"status": "error", "reason": str(e), "train_len": tl, "test_len": sl, "default_windows": default}
    rows, oos = [], []
    for k, fd in enumerate(folds):
        tr, te = _slice(rets, fd.train_idx), _slice(rets, fd.test_idx)
        pt, pe = _st.perf_stats(tr), _st.perf_stats(te)
        oos.extend(te)
        rows.append({"fold": k, "test_start": str(dates[fd.test_idx[0]]), "test_end": str(dates[fd.test_idx[1] - 1]),
                     "train_sharpe": pt["sharpe"], "test_sharpe": pe["sharpe"], "test_cum_return": pe["cum_return"]})
    oos_stats = _st.perf_stats(oos, rf=p.get("rf", 0.0)) if oos else None
    pos = sum(1 for r in rows if _isnum(r["test_sharpe"]) and r["test_sharpe"] > 0)
    return {"status": "ok", "train_len": tl, "test_len": sl, "default_windows": default, "n_folds": len(rows),
            "rows": rows, "oos": oos_stats, "n_positive_folds": pos}


def _trials_section(rets, trial_log, p) -> dict:
    thr = p.get("trial_threshold", DEFAULT_TRIAL_THRESHOLD)
    if trial_log is None:
        return {"status": "no_log", "n_trials": None, "threshold": thr,
                "reason": "未提供试验账本,无法按试验次数折扣"}
    strat = p.get("strategy")
    n = trial_log.count(strat) if strat else trial_log.count()
    out = {"status": "ok", "n_trials": n, "threshold": thr, "strategy": strat, "dsr": None, "psr0": None,
           "p_raw": None, "p_bonferroni": None, "p_sidak": None, "dsr_reason": None}
    if n == 0:
        out["dsr_reason"] = "账本里没有该策略的记录(试验次数为 0),折扣无从计算"
        return out
    try:
        mo = _tr.sample_moments(rets)
        sr = _tr.sharpe(rets)
    except _tr.TrialError as e:
        out["dsr_reason"] = str(e)
        return out
    if not _isnum(sr):
        out["dsr_reason"] = "收益标准差为 0,夏普无定义"
        return out
    out.update(sr_daily=sr, skew=mo["skew"], kurt=mo["kurt"])
    psr = _tr.prob_sharpe_gt(sr, 0.0, len(rets), mo["skew"], mo["kurt"])
    if not _isnum(psr):
        out["dsr_reason"] = f"样本量 {len(rets)} 过小(或高阶矩异常),PSR/DSR 返回 NaN,不给假精确数字"
        return out
    out["psr0"] = psr
    out["p_raw"] = 1 - psr                       # 单边:H0 真实夏普 ≤ 0
    out["p_bonferroni"] = _tr.bonferroni(out["p_raw"], n)
    out["p_sidak"] = _tr.sidak(out["p_raw"], n)
    var = trial_log.summary()["metrics"].get("sharpe_daily", {}).get("var")
    if not _isnum(var):
        out["dsr_reason"] = "缺各试验夏普的方差(账本中需要 ≥2 条 metric=sharpe_daily 的记录),DSR 无法计算"
        return out
    out["dsr"] = _tr.deflated_sharpe(sr, len(rets), mo["skew"], mo["kurt"], n, var)
    out["var_sr_trials"] = var
    return out


def _checklist(summary, p) -> list:
    seg, tr, trd = summary["segments"], summary["trials"], summary["trades"]
    items = []
    emb = seg.get("embargo", 0)
    items.append(("切分留了 embargo 隔离带", "是" if emb and emb > 0 else "否",
                  f"embargo={emb}" + ("" if emb else ";持仓/标签跨天时段间会泄漏信息")))
    n_open = p.get("locked_open_count")
    if n_open is None:
        items.append(("锁定测试段尚未被访问过", "未知", "未提供 locked_open_count(LockedTestGuard.open_count()),无法确认"))
    else:
        items.append(("锁定测试段尚未被访问过", "是" if n_open == 0 else "否", f"已访问 {n_open} 次"))
    c = trd["count"]
    items.append((f"交易笔数 ≥ {MIN_TRADES}", "未知" if c is None else ("是" if c >= MIN_TRADES else "否"),
                  "未提供交易列表" if c is None else f"{c} 笔"))
    items.append((f"样本 ≥ {MIN_YEARS:g} 年", "是" if summary["years"] >= MIN_YEARS else "否", f"{summary['years']:.2f} 年"))
    items.append(("登记了试验次数", "是" if tr["n_trials"] else "否",
                  "未提供账本" if tr["n_trials"] is None else f"记录 {tr['n_trials']} 次"))
    items.append(("成本压力已检验", "是" if summary["cost"]["status"] == "ok" else "否",
                  summary["cost"].get("reason") or "已给出结果"))
    return items


def warnings_for(summary: dict) -> list:
    """按 summary 自动触发警告;每条都说明依据,不下"有效/无效"的结论。"""
    w = []
    cost = summary["cost"]
    if cost["status"] != "ok":
        w.append(f"成本压力未检验:{cost['reason']}。成本是回测最常见的高估来源,未检验不代表成本可承受。")
    else:
        for r in cost["rows"]:
            if r["multiplier"] == 2 and r["slip_bps"] == 0 and _isnum(r["sharpe"]) and r["sharpe"] < 0:
                w.append(f"成本放大到 2 倍后夏普转负({r['sharpe']:.2f}):收益对成本很敏感。")
    if summary.get("max_ret", 0) > 1:
        w.append(f"存在单日收益 {summary['max_ret']:.2f}(> 100%):请确认收益是小数而不是百分数(1% 应写 0.01)。")
    tr = summary["trials"]
    if tr["n_trials"] is None:
        w.append("未登记试验:没有试验账本,多重检验折扣无法计算,夏普可能被'挑最好'高估。")
    elif tr["n_trials"] == 0:
        w.append("试验账本里没有该策略的记录(0 次),折扣无从计算;请确认每次尝试都已登记。")
    elif tr["n_trials"] >= tr["threshold"]:
        w.append(f"试验次数 {tr['n_trials']} ≥ 阈值 {tr['threshold']}:最好的那个结果更可能只是运气,请看折扣后的指标。")
    if summary["years"] < MIN_YEARS:
        w.append(f"样本只有 {summary['years']:.2f} 年(< {MIN_YEARS:g} 年):年化与夏普的置信区间很宽。")
    c = summary["trades"]["count"]
    if c is None:
        w.append("未提供交易列表:交易级指标(笔胜率、盈亏比等)无法检验。")
    elif c < MIN_TRADES:
        w.append(f"交易笔数只有 {c}(< {MIN_TRADES}):统计量不稳定,结论强度有限。")
    seg = summary["segments"]
    if seg["status"] == "error":
        w.append(f"三段切分未能进行:{seg['reason']}")
    else:
        cmp_ = seg["compare"]
        thr = cmp_["decay_warn"]
        for name, label in (("validate", "验证段"), ("test", "测试段")):
            for k, kn in (("sharpe", "夏普"), ("ann_return", "年化收益"), ("calmar", "卡玛")):
                d = cmp_["decay"][name][k]
                if d is not None and d > thr:
                    w.append(f"{label}{kn}相对训练段衰减 {d * 100:.1f}%(> {thr * 100:.0f}%):可能存在过拟合,仅为提示。")
    n_open = summary.get("locked_open_count")
    if n_open:
        w.append(f"锁定测试段已被访问 {n_open} 次:它已不再是未见过的数据。")
    pd_ = summary.get("paper_days")
    if pd_ is not None and pd_ < MIN_PAPER_DAYS:
        w.append(f"模拟盘覆盖仅 {pd_} 天(< {MIN_PAPER_DAYS}):跟踪误差与滑点只是粗估。")
    return w


# ---------------------------------------------------------------- 文本渲染

def _render(summary, record) -> str:
    L = []
    a = L.append
    a("# 策略验证报告")
    a("")
    a(f"> {DISCLAIMER}。")
    a("> 本报告只描述'没能证伪 / 证据不足',不判定策略有效。")
    a("")
    a("## 1. 数据与切分")
    a(f"- 区间:{summary['start']} ~ {summary['end']},{summary['n_days']} 个交易日({summary['years']:.2f} 年)")
    a(f"- 数据来源:{record.meta.get('source', '未知')};剔除 NaN {record.meta.get('n_dropped_nan', 0)} 个")
    seg = summary["segments"]
    if seg["status"] == "ok":
        a(f"- embargo:{seg['embargo']} 根")
        for k, nm in (("train", "训练"), ("valid", "验证"), ("test", "锁定测试")):
            s, e = seg["spans"][k]
            a(f"- {nm}段:{seg['ranges'][k][0]} ~ {seg['ranges'][k][1]}({e - s} 根)")
    else:
        a(f"- **无法切分**:{seg['reason']}")
    a("")
    a("## 2. 样本内 / 验证 / 锁定测试")
    if seg["status"] == "ok":
        cmp_ = seg["compare"]
        a("| 指标 | 训练 | 验证 | 锁定测试 |")
        a("|---|---|---|---|")
        for key, nm, fmt in (("n_obs", "天数", lambda v: str(v)), ("cum_return", "累计收益", _pct),
                             ("ann_return", "年化收益", _pct), ("ann_vol", "年化波动", _pct),
                             ("sharpe", "夏普", _f), ("max_drawdown", "最大回撤", _pct), ("calmar", "卡玛", _f)):
            a(f"| {nm} | " + " | ".join(fmt(cmp_[s][key]) if _isnum(cmp_[s].get(key)) else "—"
                                        for s in ("train", "validate", "test")) + " |")
        a("")
        a("衰减 =(训练 − 本段)/ 训练,0.5 表示掉了一半,负数表示反而更好;训练指标 ≤ 0 时无意义(—):")
        for name, nm in (("validate", "验证"), ("test", "测试")):
            d = cmp_["decay"][name]
            a(f"- {nm}段:夏普 {_pct(d['sharpe'])},年化收益 {_pct(d['ann_return'])},卡玛 {_pct(d['calmar'])}")
        for h in cmp_["hints"]:
            a(f"- 提示:{h}")
    else:
        a("无法检验(见上)。")
    a("")
    a("## 3. 滚动前进")
    wf = summary["walk_forward"]
    if wf["status"] == "ok":
        note = "(默认窗口,未经选择;窗口长度本身也算一次试验,应登记)" if wf["default_windows"] else ""
        a(f"- 训练窗 {wf['train_len']} 根,测试窗 {wf['test_len']} 根,共 {wf['n_folds']} 折{note}")
        a("- 说明:这里的收益序列是已经跑完的策略,滚动前进只检验'各时间段表现是否稳定',不重新优化参数。")
        a("")
        a("| 折 | 测试窗 | 训练夏普 | 测试夏普 | 测试累计收益 |")
        a("|---|---|---|---|---|")
        for r in wf["rows"]:
            a(f"| {r['fold']} | {r['test_start']} ~ {r['test_end']} | {_f(r['train_sharpe'])} | "
              f"{_f(r['test_sharpe'])} | {_pct(r['test_cum_return'])} |")
        o = wf["oos"]
        a("")
        a(f"- 拼接的样本外:{o['n_obs']} 天,夏普 {_f(o['sharpe'])},年化 {_pct(o['ann_return'])},"
          f"最大回撤 {_pct(o['max_drawdown'])};测试夏普为正的折 {wf['n_positive_folds']}/{wf['n_folds']}")
    else:
        a(f"- **无法运行**:{wf['reason']}")
    a("")
    a("## 4. 成本压力")
    cost = summary["cost"]
    if cost["status"] == "ok":
        inc = "已含基准成本(先还原再重扣)" if cost["cost_included"] else "税前收益(不还原)"
        a(f"- 收益口径:{inc};1x 单边成本率 {cost['base_cost']:.6f};平均换手 {_f(cost['avg_turnover'], 4)}")
        a("- 费率与滑点均为调用者给定的假设,需与真实账户核对。")
        a("")
        a("| 成本倍数 | 额外滑点(bp) | 单边成本率 | 夏普 | 年化收益 |")
        a("|---|---|---|---|---|")
        for r in cost["rows"]:
            a(f"| {r['multiplier']}x | {r['slip_bps']} | {r['cost_rate']:.6f} | {_f(r['sharpe'])} | {_pct(r['ann_return'])} |")
        a("")
        a("- 本表不含涨跌停买不进/卖不出、T+1、整手约束(那需要逐笔成交数据,见 costs.apply_costs)。")
    else:
        a(f"- **无法检验**:{cost['reason']}。因此无法判断策略在 2x/3x 成本下是否存活;这不代表成本可承受。")
        a("- 成交约束(涨跌停、T+1、整手)同样无法检验:需要逐笔成交数据。")
        a("- 补救:提供逐日换手列、1x 单边成本率,并声明收益已含费(--cost-included)或为税前(--gross)。")
    a("")
    a("## 5. 试验次数与折扣")
    tr = summary["trials"]
    if tr["status"] == "no_log":
        a(f"- **无法折扣**:{tr['reason']}。")
    else:
        a(f"- 记录的试验次数 N = {tr['n_trials']}(提醒阈值 {tr['threshold']})")
        if tr["dsr"] is not None or tr["p_raw"] is not None:
            a(f"- 全样本日夏普 {_f(tr.get('sr_daily'), 4)}(未年化),PSR(SR>0) = {_f(tr['psr0'], 3)}")
            a(f"- 单边 p 值 {_f(tr['p_raw'], 4)};Bonferroni 调整 {_f(tr['p_bonferroni'], 4)};Šidák 调整 {_f(tr['p_sidak'], 4)}")
            a(f"- Deflated Sharpe = {_f(tr['dsr'], 3)}" + ("" if tr["dsr"] is not None else f"(无法计算:{tr['dsr_reason']})"))
        else:
            a(f"- 折扣无法计算:{tr['dsr_reason']}")
        a("- 调整假设试验近似独立;参数相近的试验高度相关,有效 N 小于记录数,折扣偏保守。")
    a("")
    a("## 6. 模拟盘对比")
    if summary.get("paper") is None:
        a("- 未提供(可选章节)。模拟盘日志见 paper.PaperJournal.report。")
    else:
        for k, v in summary["paper"].items():
            a(f"- {k}: {v}")
    a("")
    a("## 7. 自检清单")
    mark = {"是": "x", "否": " ", "未知": "?"}
    for nm, st, note in summary["checklist"]:
        a(f"- [{mark[st]}] {nm}:{st}({note})")
    a("")
    a("## 8. 警告")
    for w in summary["warnings"] or ["(未触发自动警告;这不等于没有问题)"]:
        a(f"- {w}")
    a("- 常驻提醒:回测好看不等于有效。请同时考虑过拟合、多重检验、样本量、交易成本。")
    a("")
    a("---")
    a(f"**{DISCLAIMER}。**")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 入口

def build_report(record, params=None, trial_log=None, cost_inputs=None):
    """由 RunRecord 生成 (markdown 文本, 汇总 dict)。

    params 可用键:train/valid/embargo(三段切分)、wf_train_len/wf_test_len/wf_anchored(滚动前进)、
    rf、strategy(按策略名取试验次数)、trial_threshold、decay_warn、locked_open_count、paper_days、
    multipliers/slip_bps(成本压力网格)。未知键抛 ReportError。
    cost_inputs 仅覆盖 record 里的 turnover/base_cost/cost_included。"""
    p = _params(params)
    record.require_daily("统一报告")
    ci = dict(cost_inputs or {})
    bad = [k for k in ci if k not in ("turnover", "base_cost", "cost_included")]
    if bad:
        raise ReportError(f"cost_inputs 含未知键: {bad}")
    turnover = ci.get("turnover", record.turnover)
    base_cost = ci.get("base_cost", record.meta.get("base_cost"))
    included = ci.get("cost_included", record.meta.get("cost_included"))
    rets, dates = list(record.returns), list(record.dates)
    try:
        ps_all = _st.perf_stats(rets, rf=p.get("rf", 0.0), dates=dates,
                                trades=list(record.trades) if record.trades else None)
        cost = cost_stress(rets, turnover, base_cost, included, p.get("multipliers", (1, 2, 3)),
                           p.get("slip_bps", (0, 5, 10)))
    except (_st.StatsError, ValueError) as e:
        raise ReportError(str(e)) from None
    summary = {
        "max_ret": max(rets), "n_days": len(rets), "years": len(rets) / 252.0, "start": str(dates[0]), "end": str(dates[-1]),
        "overall": ps_all, "segments": _segments(rets, dates, p), "walk_forward": _walk_forward(rets, dates, p),
        "cost": cost, "trials": _trials_section(rets, trial_log, p),
        "trades": {"count": len(record.trades) if record.trades else None},
        "paper": None, "paper_days": p.get("paper_days"), "locked_open_count": p.get("locked_open_count"),
        "disclaimer": DISCLAIMER,
    }
    if p.get("paper_days") is not None:
        summary["paper"] = {"覆盖天数": p["paper_days"]}
    summary["checklist"] = _checklist(summary, p)
    summary["warnings"] = warnings_for(summary)
    return _render(summary, record), summary


def build_report_from_arrays(dates, returns, trades=None, turnover=None, base_cost=None, cost_included=None,
                             params=None, trial_log=None):
    """便捷包装:先 adapter.from_returns 再 build_report。"""
    rec = _ad.from_returns(dates, returns, trades=trades, turnover=turnover, base_cost=base_cost,
                           cost_included=cost_included)
    return build_report(rec, params, trial_log)


def write_report(path, text: str, force: bool = False) -> str:
    """只写调用者给的路径;已存在且未 force 时拒绝(用独占创建,避免检查与写入之间的竞争)。"""
    path = os.fspath(path)
    if not isinstance(text, str) or not text:
        raise ReportError("报告文本为空")
    if os.path.isdir(path):
        raise ReportError(f"输出路径是目录: {path}")
    parent = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(parent):
        raise ReportError(f"输出目录不存在: {parent}(不会自动创建)")
    try:
        with open(path, "w" if force else "x", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError:
        raise ReportError(f"输出文件已存在,拒绝覆盖: {path}(确认要覆盖请加 --force)") from None
    except OSError as e:
        raise ReportError(f"无法写入: {e}") from None
    return path
