"""策略体检（T022）：把一次详细回测的结果汇总成一份诚实的体检报告，用几项统计指标检查"这个回测结果能否和运气区分开"。

做什么：本模块只汇总和判定，**自己不做新的回测、不预测、不推荐、不构成投资建议**（注意：命令行脚本会调用 registry.run(detail=True)，那一步包含成本重跑和参数邻点等新计算）。
指标含义：各项概率是 PSR/DSR 的正态近似指标，依赖独立同分布等假设，**不是**"策略有效的概率"或"纯运气的概率"。
输入：registry.run(..., detail=True) 的返回值，加上你为了得到这个策略一共试过多少个（策略×参数）组合 n_trials。
输出：结构化的检查项（每项 pass/warn/fail/na）+ 一个结论代码 + 中英文文字，可直接转成 JSON。

诚实性约定：
  - 所有阈值写死在 THRESHOLDS，**事先定好，不根据结果调整**（tests 里的守卫测试只能提醒当前常量被改过，它不是不可篡改的预注册，也没有独立的时间戳）；
  - n_trials 由使用者如实填写。填少了，Deflated Sharpe 会高估；这是本报告最大的、无法由代码替你把关的假设；
  - 通过全部检查**不等于策略有效**，只是"这几项统计证据没有露馅"；核心项（样本、DSR、超额、样本外）缺数据时，不会给出"通过"；
  - 输入契约：res 应是 registry.run(detail=True) 的正常输出（净值为正的有限数、日期对齐），不是任意输入都稳健。
"""
from __future__ import annotations

import math

from .validation import trials as tr

TD = 252      # 一年按 252 个交易日

# 事先写死的阈值。别因为某个策略"差一点就通过"就去改它——那恰好是在数据窥探。
THRESHOLDS = {
    "min_days": 504,           # 至少约 2 年日线
    "min_trades": 30,          # 至少 30 笔完整交易（metrics 统计出的 Trades）
    "dsr_pass": 0.95,          # Deflated Sharpe ≥ 0.95 才算通过
    "dsr_warn": 0.80,
    "excess_psr_pass": 0.95,   # 相对买入持有的超额收益，其夏普大于 0 的概率
    "excess_psr_warn": 0.80,
    "oos_keep_ratio": 0.5,     # 样本外夏普至少保留样本内的一半
    "cost_label": "2x",        # 成本翻倍后夏普仍要为正
    "cost_label_hard": "4x",   # 成本 4 倍后仍为正才算"很稳"
    "plateau_pass": 0.60,      # 参数邻点里"还在平台上"的比例
    "plateau_warn": 0.30,
    "plateau_keep": 0.50,      # 邻点夏普 ≥ 中心点的 50% 才算还在平台上
    "p_loss_pass": 20.0,       # 蒙特卡洛：亏损概率 ≤ 20%
    "p_loss_warn": 40.0,
    "ci_block": 10,            # bootstrap 块长（交易日），覆盖短程相关
    "ci_n_boot": 1000,
    "ci_seed": 0,
}

DISCLAIMER_ZH = "本体检只检验历史回测里的统计证据，不预测未来，不构成投资建议。"
DISCLAIMER_EN = "This check only examines the statistical evidence in a historical backtest. It does not predict the future and is not investment advice."


def _num(x):
    """把 NaN/inf 变成 None，保证结果能被 json.dumps(allow_nan=False) 序列化。"""
    if x is None or isinstance(x, bool):
        return None
    try:
        x = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def _clean(o):
    """递归把 NaN/inf 变成 None（用于回显进结果的 params 等嵌套结构），保证 allow_nan=False 可序列化。"""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, bool) or o is None or isinstance(o, str):
        return o
    if isinstance(o, (int, float)):
        return _num(o)
    return str(o)


def _fmt(x, digits=2, unit=""):
    v = _num(x)
    return "—" if v is None else f"{v:.{digits}f}{unit}"


def _returns(equity) -> list[float]:
    """相邻净值的日收益（会丢掉第一天）。净值必须是正的有限数，否则拒绝，不悄悄删点（删点会让策略和基准错位）。"""
    e = [float(v) for v in equity]
    if any(not math.isfinite(v) or v <= 0 for v in e):
        raise ValueError("净值序列必须全是正的有限数")
    return [e[i] / e[i - 1] - 1.0 for i in range(1, len(e))]


def _check(cid: str, status: str, value: dict, zh: str, en: str) -> dict:
    return {"id": cid, "status": status, "value": {k: _num(v) if isinstance(v, (int, float)) else v for k, v in value.items()},
            "zh": zh, "en": en}


# ---------------------------------------------------------------- 各项检查

def _check_sample(n_days: int, n_trades) -> dict:
    t = THRESHOLDS
    trades_known = isinstance(n_trades, (int, float)) and not isinstance(n_trades, bool)
    shown = int(n_trades) if trades_known and float(n_trades).is_integer() else n_trades   # 20.0 显示成 20
    short_days = n_days < t["min_days"]
    short_trades = trades_known and n_trades < t["min_trades"]
    status = "fail" if (short_days or short_trades) else "pass" if trades_known else "na"
    return _check(
        "sample", status, {"days": n_days, "years": n_days / TD, "trades": n_trades if trades_known else None},
        f"样本 {n_days} 个交易日（约 {n_days / TD:.1f} 年），完整交易 {shown if trades_known else '未知'} 笔；"
        f"至少需要 {t['min_days']} 天和 {t['min_trades']} 笔。" + ("样本不足，下面的统计都不可靠。" if status == "fail" else "完整交易笔数未知，无法核对笔数要求。" if status == "na" else ""),
        f"Sample: {n_days} trading days (~{n_days / TD:.1f} years), {shown if trades_known else 'unknown'} completed trades; "
        f"need at least {t['min_days']} days and {t['min_trades']} trades." + (" Sample is too small; the statistics below are unreliable." if status == "fail" else " Trade count unknown; the trade requirement cannot be verified." if status == "na" else ""))


def _check_sharpe_ci(r, sr_ann) -> dict:
    t = THRESHOLDS
    ci = tr.sharpe_ci(r, block=t["ci_block"], n_boot=t["ci_n_boot"], seed=t["ci_seed"], periods_per_year=TD)
    lo, hi, point = _num(ci.get("lower")), _num(ci.get("upper")), _num(ci.get("point"))
    if lo is None or hi is None:
        status = "na"
    elif lo > 0:
        status = "pass"
    elif point is not None and point > 0:
        status = "warn"
    else:
        status = "fail"
    return _check(
        "sharpe_ci", status, {"sharpe": sr_ann, "lower": lo, "upper": hi},
        f"年化夏普 {_fmt(sr_ann)}，95% 置信区间 [{_fmt(lo)}, {_fmt(hi)}]（块 bootstrap）。"
        + ("区间下界大于 0。" if status == "pass" else "区间包含 0 或为负：看不出夏普是否真的大于 0。" if status in ("warn", "fail") else "样本太小，算不出区间。"),
        f"Annualised Sharpe {_fmt(sr_ann)}, 95% CI [{_fmt(lo)}, {_fmt(hi)}] (block bootstrap). "
        + ("The lower bound is above 0." if status == "pass" else "The interval includes 0 or is negative: we cannot tell whether the Sharpe is really above 0." if status in ("warn", "fail") else "Sample too small for an interval."))


def _check_dsr(sr_d, n, skew, kurt, n_trials) -> dict:
    t = THRESHOLDS
    psr = tr.prob_sharpe_gt(sr_d, 0.0, n, skew, kurt)
    se = tr.sharpe_se(sr_d, n, skew, kurt)
    var_trials = se * se if _num(se) is not None else float("nan")
    bench = tr.expected_max_sharpe(n_trials, var_trials) if _num(var_trials) is not None else float("nan")
    dsr = tr.deflated_sharpe(sr_d, n, skew, kurt, n_trials, var_trials) if _num(var_trials) is not None else float("nan")
    v = _num(dsr)
    status = "na" if v is None else "pass" if v >= t["dsr_pass"] else "warn" if v >= t["dsr_warn"] else "fail"
    return _check(
        "dsr", status, {"psr": psr, "dsr": dsr, "n_trials": n_trials, "luck_benchmark_sharpe_ann": _num(bench) * math.sqrt(TD) if _num(bench) is not None else None},
        f"在你试过 {n_trials} 个组合的前提下，Deflated Sharpe = {_fmt(dsr, 3)}（不折扣的 PSR = {_fmt(psr, 3)}）；"
        f"≥ {t['dsr_pass']} 才算通过（这是正态近似指标，不是策略有效的概率）。试验次数越多，'挑到运气好的那一个' 的折扣越大。"
        + ("" if n_trials > 1 else "你填的试验次数是 1：如果实际试过更多，请改成真实次数，否则这一项会高估。"),
        f"Assuming you tried {n_trials} combinations, Deflated Sharpe = {_fmt(dsr, 3)} (undiscounted PSR = {_fmt(psr, 3)}); "
        f"≥ {t['dsr_pass']} passes (a normal-approximation indicator, not the probability that the strategy works). More trials mean a larger discount for picking the lucky one. "
        + ("" if n_trials > 1 else "You entered 1 trial: if you actually tried more, enter the real count or this check is too generous."))


def _check_excess(r, b) -> dict:
    t = THRESHOLDS
    if b is None or len(b) != len(r):
        return _check("excess", "na", {}, "没有可用的买入持有基准，无法判断是否跑赢。", "No benchmark available; cannot judge whether it beats buy-and-hold.")
    ex = [x - y for x, y in zip(r, b)]
    m = tr.sample_moments(ex)
    if m["std"] <= 0:
        return _check("excess", "na", {}, "超额收益序列没有波动，无法检验。", "The excess-return series has no variance; cannot test.")
    sr = m["mean"] / m["std"]
    psr = tr.prob_sharpe_gt(sr, 0.0, m["n"], m["skew"], m["kurt"])
    v = _num(psr)
    status = "na" if v is None else "pass" if v >= t["excess_psr_pass"] else "warn" if v >= t["excess_psr_warn"] else "fail"
    return _check(
        "excess", status, {"excess_sharpe_ann": sr * math.sqrt(TD), "psr": psr},
        f"相对买入持有的超额收益（主动收益序列的年化夏普，即信息比率，不是两条夏普之差）：{_fmt(sr * math.sqrt(TD))}，PSR（正态近似指标，不是策略有效的概率）= {_fmt(psr, 3)}；≥ {t['excess_psr_pass']} 才算通过。",
        f"Excess return vs buy-and-hold: annualised Sharpe {_fmt(sr * math.sqrt(TD))}, PSR (a normal-approximation indicator, not the probability the strategy works) = {_fmt(psr, 3)}; ≥ {t['excess_psr_pass']} passes. Note: this \"excess Sharpe\" is the information ratio of the active-return series, not the difference of two Sharpe ratios.")


def _check_oos(split: dict | None) -> dict:
    t = THRESHOLDS
    is_s = _num(((split or {}).get("is") or {}).get("sharpe"))
    oos_s = _num(((split or {}).get("oos") or {}).get("sharpe"))
    if is_s is None or oos_s is None:
        return _check("oos", "na", {}, "没有样本内外对比数据。", "No in-sample / out-of-sample split available.")
    if oos_s <= 0:
        status = "fail"
    elif is_s > 0 and oos_s < t["oos_keep_ratio"] * is_s:
        status = "warn"
    else:
        status = "pass"
    return _check(
        "oos", status, {"is_sharpe": is_s, "oos_sharpe": oos_s},
        f"前 70% 为样本内、后 30% 为样本外：夏普 {_fmt(is_s)} → {_fmt(oos_s)}。样本外要为正，且至少保留样本内的 {int(t['oos_keep_ratio'] * 100)}%。"
        "注意：这个切分是事后切的，策略和参数本身可能已经看过全部数据，所以不是干净的样本外。",
        f"First 70% in-sample, last 30% out-of-sample: Sharpe {_fmt(is_s)} → {_fmt(oos_s)}. Out-of-sample must be positive and keep at least {int(t['oos_keep_ratio'] * 100)}% of in-sample. "
        "Note: the split is made after the fact, and the strategy or its parameters may already have seen all the data, so this is not a clean out-of-sample test.")


def _check_cost(cost_sens) -> dict:
    t = THRESHOLDS
    by = {str(x.get("label")): _num(x.get("sharpe")) for x in (cost_sens or []) if isinstance(x, dict)}
    s2, s4, s1 = by.get(t["cost_label"]), by.get(t["cost_label_hard"]), by.get("1x")
    if s2 is None:
        return _check("cost", "na", {}, "没有成本敏感性数据。", "No cost-sensitivity data available.")
    status = "fail" if s2 <= 0 else "warn" if (s4 is not None and s4 <= 0) else "pass"
    return _check(
        "cost", status, {"sharpe_1x": s1, "sharpe_2x": s2, "sharpe_4x": s4},
        f"成本 1 倍 / 2 倍 / 4 倍时夏普：{_fmt(s1)} / {_fmt(s2)} / {_fmt(s4)}。成本翻倍后夏普要仍为正。费率是估算值，真实成本和滑点可能更高。",
        f"Sharpe at 1x / 2x / 4x costs: {_fmt(s1)} / {_fmt(s2)} / {_fmt(s4)}. Sharpe must stay positive at 2x costs. Fees are estimates; real costs and slippage may be higher.")


def _expected_param_keys(res):
    """这个策略在 registry 里登记的可调参数 key（用来发现预算截断造成的整行缺失）；策略未登记则返回 None（不做整行核对）。"""
    try:
        from . import registry
        spec = registry.STRATEGIES.get(res.get("strategy"), {}).get("params")
        return [p["key"] for p in spec] if spec else None
    except Exception:
        return None


def _check_plateau(param_sens, params=None, expected_keys=None) -> dict:
    """参数平台：以回测实际使用的参数值（params[key]）为中心，不靠列表位置猜中心。
    中心缺失/无效、有效点太少的参数记为"未覆盖"；expected_keys（策略登记的全部可调参数）里没有出现在 param_sens 的整行也记为"未覆盖"
    （上游有 8 秒预算，耗尽后后面的参数整行不会输出）；有未覆盖的参数时最高只给 warn。重复的 v（边界截断）只算一个点。"""
    t = THRESHOLDS
    params = params or {}
    ratios, rows, uncovered = [], [], []
    present = {row.get("key") for row in (param_sens or [])}
    uncovered += [k for k in (expected_keys or []) if k not in present]
    for row in param_sens or []:
        key = row.get("key")
        center_v = _num(params.get(key))
        uniq = {}
        for p in row.get("pts") or []:
            v = _num(p.get("v"))
            if v is not None and not any(abs(v - u) < 1e-9 for u in uniq):
                uniq[v] = _num(p.get("sharpe"))
        cv = next((u for u in uniq if center_v is not None and abs(u - center_v) < 1e-4), None)
        c = uniq.get(cv) if cv is not None else None
        neigh = [sh for u, sh in uniq.items() if u != cv]
        if c is None or len(neigh) < 2:
            uncovered.append(key)
            continue
        # 无效（非有限）的邻点算"没保住"，不删掉后只数幸存的好点
        keep = [1 if (c > 0 and sh is not None and sh > 0 and sh >= t["plateau_keep"] * c) else 0 for sh in neigh]
        ratio = sum(keep) / len(keep)
        ratios.append(ratio)
        rows.append({"key": key, "ratio": ratio, "n_neighbours": len(neigh)})
    if not ratios:
        return _check("plateau", "na", {"uncovered": uncovered}, "这个策略没有可调参数，或参数敏感性数据不可用/中心点缺失。", "No tunable parameters, or parameter-sensitivity data unavailable / centre point missing.")
    worst = min(ratios)       # 只要有一个参数是尖峰，就算脆弱
    status = "pass" if worst >= t["plateau_pass"] else "warn" if worst >= t["plateau_warn"] else "fail"
    if uncovered and status == "pass":
        status = "warn"       # 有参数没能检验，不能给全通过
    note_zh = f"（另有 {len(uncovered)} 个参数因中心点缺失或点太少未能检验：{', '.join(map(str, uncovered))}）" if uncovered else ""
    note_en = f" ({len(uncovered)} parameter(s) could not be checked: centre point missing or too few points.)" if uncovered else ""
    return _check(
        "plateau", status, {"worst_ratio": worst, "params": rows, "uncovered": uncovered},
        f"每个参数向两边各挪两档，邻点夏普还保留中心点 {int(t['plateau_keep'] * 100)}% 以上且为正的比例：最差的参数是 {worst:.0%}。"
        f"≥ {int(t['plateau_pass'] * 100)}% 说明在平台上，太低说明可能只是一个尖峰（过拟合的典型形状）。" + note_zh,
        f"Move each parameter two steps either way; share of neighbours that keep ≥ {int(t['plateau_keep'] * 100)}% of the centre Sharpe and stay positive: worst parameter {worst:.0%}. "
        f"≥ {int(t['plateau_pass'] * 100)}% suggests a plateau; much lower suggests a spike (the classic overfitting shape)." + note_en)


def _check_mc(mc) -> dict:
    t = THRESHOLDS
    p = _num((mc or {}).get("p_loss"))
    if p is None or not 0.0 <= p <= 100.0:
        return _check("mc", "na", {}, "没有可用的蒙特卡洛数据（样本太短或数值无效）。", "No usable Monte Carlo data (sample too short or invalid value).")
    n_mc, blk = _num((mc or {}).get("n")), _num((mc or {}).get("block"))
    n_mc, blk = (int(n_mc) if n_mc else "?"), (int(blk) if blk else "?")
    status = "pass" if p <= t["p_loss_pass"] else "warn" if p <= t["p_loss_warn"] else "fail"
    return _check(
        "mc", status, {"p_loss_pct": p},
        f"对历史日收益做 {blk} 天一块、有放回的块自助重采样 {n_mc} 次：重采样路径里整段期间累计亏损的占比 {p:.1f}%（≤ {t['p_loss_pass']:.0f}% 通过）。"
        "这是历史收益分布下的敏感性，不是未来真实亏损概率，也不含选择偏差。",
        f"Block bootstrap (with replacement, {blk}-day blocks, {n_mc} draws) of historical daily returns: share of resampled paths with a cumulative loss over the whole period {p:.1f}% (≤ {t['p_loss_pass']:.0f}% passes). "
        "This is a sensitivity under the historical return distribution, not the probability of a real future loss, and it ignores selection bias.")


# ---------------------------------------------------------------- 结论

VERDICTS = {
    "insufficient_sample": ("样本不足", "Insufficient sample",
                            "样本太短或交易太少，统计检验不可靠。先别下任何结论。"),
    "incomplete": ("检查项不全", "Incomplete",
                   "有检查项缺数据（核心项：样本笔数、Deflated Sharpe、相对基准、样本外，缺任何一项都不能给出\"通过\"），无法给出结论。"),
    "no_evidence": ("看不出与运气的区别", "No evidence beyond luck",
                    "按你填写的试验次数折扣后，或相对买入持有、或样本外，至少有一项没通过：这个回测结果还不能和运气区分开。"),
    "mixed": ("证据好坏参半", "Mixed evidence",
              "有的检查通过、有的没有，不要据此下结论。已经发生的试验次数不能改小；以后的研究应先登记假设、减少搜索次数。"),
    "passes_checks": ("通过这几项检查（不代表有效）", "Passes these checks (not a proof it works)",
                      "这几项统计证据没有露馅。但这不是策略有效的证明：过去不代表未来，成本与滑点是估算，并且结果依赖你如实填写的试验次数。"),
}


CORE = ("sample", "dsr", "excess", "oos")      # 核心项：缺任何一项（na）都不能给"通过"


def _verdict(checks: list[dict]) -> dict:
    st = {c["id"]: c["status"] for c in checks}
    fails = [c["id"] for c in checks if c["status"] == "fail"]
    warns = [c["id"] for c in checks if c["status"] == "warn"]
    nas = [c["id"] for c in checks if c["status"] == "na"]
    if st.get("sample") == "fail":
        code = "insufficient_sample"
    elif any(st.get(k) == "fail" for k in CORE):
        code = "no_evidence"            # 已知的核心失败优先，不被"缺数据"遮住
    elif len(nas) >= 3:
        code = "incomplete"
    elif any(st.get(k) != "pass" and st.get(k) != "warn" for k in CORE):
        code = "incomplete"        # 核心项缺数据（na）：不能当通过
    elif fails or len(warns) >= 2:
        code = "mixed"
    else:
        code = "passes_checks"
    zh_t, en_t, zh_body = VERDICTS[code]
    return {"code": code, "title_zh": zh_t, "title_en": en_t, "zh": zh_body + DISCLAIMER_ZH,
            "en": en_t + ". " + DISCLAIMER_EN, "fails": fails, "warns": warns, "na": nas, "missing_core": [k for k in CORE if st.get(k) == "na"]}


CAVEATS = [
    ("收益被当作近似独立同分布；有自相关时夏普的标准误被低估。", "Returns are treated as roughly i.i.d.; with autocorrelation the Sharpe standard error is understated."),
    ("Deflated Sharpe 里各次试验夏普的方差用本策略夏普的标准误平方近似，这只是条件近似；试验间相关会让有效试验数小于填写值，但这个近似对结论的净影响方向不确定，不能认为一定偏保守。",
     "In Deflated Sharpe the variance across trials is approximated by this strategy's squared Sharpe standard error. This is a conditional approximation; correlated trials make the effective count smaller than entered, but the net direction of the error is unknown, so it cannot be assumed conservative."),
    ("n_trials 由你如实填写，代码无法核实。少报会让结论过于乐观。", "n_trials is entered by you and cannot be verified. Under-reporting makes the verdict too optimistic."),
    ("样本内/外是事后按 70/30 切的，不是封存的干净样本外。", "The in/out-of-sample split is a post-hoc 70/30 cut, not a sealed clean out-of-sample test."),
    ("数据没有处理幸存者偏差、ST/退市、真实成交容量；费率与滑点是估算。", "No survivorship-bias, ST/delisting or capacity handling; fees and slippage are estimates."),
    ("通过全部检查也不等于策略有效或未来会赚钱。", "Passing every check does not mean the strategy works or will make money."),
]


# ---------------------------------------------------------------- 入口

def health_check(res: dict, n_trials: int = 1) -> dict:
    """res：registry.run(..., detail=True) 的返回值；n_trials：为了得到这个策略一共试过多少个（策略×参数）组合。"""
    if isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1:
        raise ValueError("n_trials 必须是 ≥ 1 的整数（你一共试过多少个策略/参数组合）")
    if not isinstance(res, dict) or not isinstance(res.get("equity"), list) or len(res["equity"]) < 3:
        raise ValueError("res 里需要有 equity 序列（请用 detail=True 的回测结果）")
    r = _returns(res["equity"])
    bench = res.get("bench")
    try:
        b = _returns(bench) if isinstance(bench, list) and len(bench) == len(res["equity"]) else None
    except ValueError:
        b = None            # 基准净值无效：当作没有基准，不删点错位
    n = len(r)
    ana = res.get("analysis") or {}
    stats = res.get("stats") or {}

    checks = [_check_sample(n, stats.get("Trades"))]
    if n >= 30:
        m = tr.sample_moments(r)
        sr_d = m["mean"] / m["std"] if m["std"] > 0 else float("nan")
        sr_ann = sr_d * math.sqrt(TD) if _num(sr_d) is not None else float("nan")
        if _num(sr_d) is not None:
            checks += [_check_sharpe_ci(r, sr_ann), _check_dsr(sr_d, n, m["skew"], m["kurt"], n_trials), _check_excess(r, b)]
        else:
            na = lambda cid: _check(cid, "na", {}, "收益没有波动，无法计算。", "Returns have no variance; cannot compute.")
            checks += [na("sharpe_ci"), na("dsr"), na("excess")]
    else:
        na = lambda cid: _check(cid, "na", {}, "样本太短，无法计算。", "Sample too short to compute.")
        checks += [na("sharpe_ci"), na("dsr"), na("excess")]
    checks += [_check_oos(ana.get("split")), _check_cost(ana.get("cost_sens")), _check_plateau(ana.get("param_sens"), res.get("params"), _expected_param_keys(res)), _check_mc(ana.get("mc"))]

    return {
        "version": 1,
        "inputs": {"strategy": res.get("strategy"), "symbol": res.get("symbol"), "params": _clean(res.get("params")),
                   "start": res.get("start"), "end": res.get("end"), "days": n, "years": n / TD, "n_trials": n_trials},
        "thresholds": dict(THRESHOLDS),
        "checks": checks,
        "verdict": _verdict(checks),
        "caveats": [{"zh": z, "en": e} for z, e in CAVEATS],
        "disclaimer": {"zh": DISCLAIMER_ZH, "en": DISCLAIMER_EN},
    }
