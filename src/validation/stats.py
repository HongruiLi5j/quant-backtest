"""绩效统计与样本内外对比(T007 / S4)。纯计算:标准库实现,不联网、不读写任何文件。

两组口径,键名带前缀以免混淆:
  - 日级(只需逐日简单收益):累计收益、年化收益、年化波动、夏普、最大回撤及起止日、卡玛、
    day_win_rate / day_profit_factor / day_payoff_ratio / day_expectancy / max_losing_days。
  - 交易级(需要 trades,含逐笔 ret):trade_count / trade_win_rate / trade_profit_factor /
    trade_expectancy / max_losing_trades。逐日收益无法恢复逐笔交易,trades=None 时这些键全为 None,
    并带 trade_status="unavailable" 与原因,不用日级数字顶替。

口径约定:
  - 收益是逐日"简单收益"(小数,不是百分比),每个值必须 > -1。
  - 年化:按 periods(默认 252)个交易日一年;年化收益 = (1+累计收益)^(periods/n) - 1(几何);
    年化波动 = 样本标准差(ddof=1)× √periods;夏普 = 日均超额收益 / 日标准差 × √periods,
    日无风险收益 = rf / periods(rf 为年化无风险利率,单利折算)。
  - 最大回撤:净值从 1 起算(起点算作第一个峰值),回撤 = 净值/历史峰值 - 1(≤0)。
    起点(峰值日)是峰值所在日期;若峰值就是起点(第 0 日之前),peak 位置为 -1、日期为 None。
  - 日胜率 / 日盈亏比只统计"有盈亏的日子"(收益恰为 0 的日子不进分母,单列 n_zero_days),
    否则长期空仓的策略日胜率会被空仓日稀释。day_profit_factor = 盈利日收益之和 / 亏损日收益绝对值之和;
    day_payoff_ratio = 平均盈利 / 平均亏损(绝对值)。没有亏损日时二者无定义,返回 None 并在 warnings 说明;
    全是亏损日时 day_profit_factor = 0(与交易级一致),day_payoff_ratio 为 None。
  - 最大回撤区间:净值回到或超过历史峰值即重置峰值位置,区间从最近一次触及峰值起算。
  - 稳健性:空序列、含 inf、含 <= -1 的收益、非数值 → StatsError;含 NaN 默认也抛错,
    nan_policy="drop" 时剔除并计数(n_dropped_nan);单点、全零等降级为 None + warnings,不假精确。

compare_segments 只给数值与提示字符串,不下"有效/无效"结论。
与 src/metrics.py 互相独立,仅在测试里交叉校验。
"""
from __future__ import annotations

import math
import numbers
from typing import Optional, Sequence

__all__ = ["StatsError", "perf_stats", "max_drawdown_info", "decay_ratio", "compare_segments",
           "TRADING_DAYS"]

TRADING_DAYS = 252
_TRADE_UNAVAILABLE = "未提供交易列表"
_MIN_OBS_WARN = 30          # 样本少于此数时在 warnings 里提醒(经验阈值,非统计保证)
_ZERO_STD = 1e-15           # 标准差低于此视为 0(全零/常数序列)


class StatsError(ValueError):
    """输入非法(空序列、NaN、非数值等)。"""


def _num(x, what: str) -> float:
    """把单个值转成 float;None 视为 NaN;bool 与非数值报错。"""
    if x is None:
        return math.nan
    if isinstance(x, bool) or not isinstance(x, numbers.Real):
        raise StatsError(f"{what} 含非数值元素: {x!r}")
    return float(x)


def _clean_returns(returns, dates, nan_policy: str, label: str):
    """校验并清洗收益序列,返回 (values, dates, n_dropped)。"""
    if nan_policy not in ("error", "drop"):
        raise StatsError(f"nan_policy 只能是 'error' 或 'drop',收到 {nan_policy!r}")
    if returns is None or isinstance(returns, (str, bytes)) or not hasattr(returns, "__iter__"):
        raise StatsError(f"{label} 必须是收益序列(可迭代),收到 {type(returns).__name__}")
    idx = getattr(returns, "index", None)
    if dates is None and idx is not None and not callable(idx):   # list.index 是方法,要排除
        dates = list(idx)                  # pandas Series:用索引当日期,不 import pandas
    vals = [_num(x, label) for x in returns]
    if dates is not None:
        dates = list(dates)
        if len(dates) != len(vals):
            raise StatsError(f"{label}: dates 长度 {len(dates)} 与收益长度 {len(vals)} 不一致")
    keep = [i for i, v in enumerate(vals) if v == v]
    n_dropped = len(vals) - len(keep)
    if n_dropped and nan_policy == "error":
        raise StatsError(f"{label} 含 {n_dropped} 个 NaN/None(可用 nan_policy='drop' 剔除并计数)")
    vals = [vals[i] for i in keep]
    if dates is not None:
        dates = [dates[i] for i in keep]
        _check_dates(dates, label)
    if not vals:
        raise StatsError(f"{label} 为空序列(或剔除 NaN 后为空),无法计算")
    for v in vals:
        if math.isinf(v):
            raise StatsError(f"{label} 含 inf")
        if v <= -1.0:
            raise StatsError(f"{label} 含 <= -100% 的收益 {v!r}(简单收益必须 > -1,是否误传了百分比?)")
    return vals, dates, n_dropped


def _check_dates(dates, label: str) -> None:
    """日期必须无 NaN/NaT、类型可比较且严格升序;否则回撤区间的起止日会没有意义。"""
    for d in dates:
        if d is None or d != d:
            raise StatsError(f"{label} 的日期含 NaN/NaT/None")
    try:
        for a, b in zip(dates, dates[1:]):
            if not a < b:
                raise StatsError(f"{label} 的日期必须严格升序且无重复,发现 {a!r} 之后是 {b!r}")
    except TypeError:
        raise StatsError(f"{label} 的日期类型不一致,无法比较先后")


def _mean(xs):
    return math.fsum(xs) / len(xs)


def _std(xs):
    """样本标准差(ddof=1);少于 2 个点返回 None。"""
    n = len(xs)
    if n < 2:
        return None
    m = _mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (n - 1))


def _max_run(flags) -> int:
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return best


def max_drawdown_info(returns: Sequence[float], dates: Optional[Sequence] = None) -> dict:
    """最大回撤及区间。输入应已清洗(无 NaN、均 > -1)。

    返回 max_drawdown(≤0)、peak_idx(峰值位置,-1 表示起点)、trough_idx、recovery_idx
    (首次回到峰值的位置,None 表示至今未恢复)与对应日期(无 dates 时为 None)。
    无回撤(净值从未低于历史峰值)时 max_drawdown=0 且各位置/日期为 None。
    """
    # 用对数净值 L = Σ log(1+r) 计算:直接连乘净值在极端长/大收益序列上会溢出成 inf,
    # 回撤会被静默算成 0;回撤 = exp(L - 峰值L) - 1,指数部分恒 ≤ 0,不会溢出。
    # 对数累加有浮点误差,"回到峰值"用 1e-12 的容差判定
    tol = 1e-12
    lg = []
    cum = 0.0
    peak, peak_i = 0.0, -1
    worst, best_peak_i, best_trough_i = 0.0, None, None
    for i, r in enumerate(returns):
        cum += math.log1p(r)
        lg.append(cum)
        if cum >= peak - tol:
            # 净值回到(或超过)历史峰值就重置峰值位置:回撤区间从"最近一次触及峰值"起算,
            # 不会把已经恢复的前一次回撤并入当前区间
            peak, peak_i = max(peak, cum), i
        else:
            dd = math.expm1(cum - peak)
            if dd < worst:
                worst, best_peak_i, best_trough_i = dd, peak_i, i
    rec_i = None
    if best_trough_i is not None:
        peak_val = 0.0 if best_peak_i == -1 else lg[best_peak_i]
        for j in range(best_trough_i + 1, len(lg)):
            if lg[j] >= peak_val - tol:
                rec_i = j
                break

    def d(i):
        if i is None or i < 0 or dates is None:
            return None
        return str(dates[i])

    return {"max_drawdown": worst, "peak_idx": best_peak_i, "trough_idx": best_trough_i,
            "recovery_idx": rec_i, "peak_date": d(best_peak_i), "trough_date": d(best_trough_i),
            "recovery_date": d(rec_i)}


def _trade_stats(trades) -> dict:
    """交易级指标。trades 为 dict 或带 ret 属性的对象列表;空列表给 count=0、其余 None。"""
    if trades is None:
        return {"trade_status": "unavailable", "trade_reason": _TRADE_UNAVAILABLE,
                "trade_count": None, "trade_win_rate": None, "trade_profit_factor": None,
                "trade_expectancy": None, "max_losing_trades": None}
    if isinstance(trades, (str, bytes)) or not hasattr(trades, "__iter__"):
        raise StatsError("trades 必须是交易列表(可迭代)")
    items = list(trades)

    def get(t, k):
        return t.get(k) if isinstance(t, dict) else getattr(t, k, None)

    rets = []
    for t in items:
        r = _num(get(t, "ret"), "trades.ret")
        if r != r or math.isinf(r):
            raise StatsError("trades 的 ret 含 NaN/None/inf")
        rets.append(r)
    # 连续亏损按时间顺序数:全部有 exit_date 时按其稳定排序,全都没有时按传入顺序;
    # 只有一部分有、或日期类型不可比较(如字符串混 date),顺序无法确定,直接报错而不是静默退回
    exits = [get(t, "exit_date") for t in items]
    for x in exits:
        if x is not None and x != x:
            raise StatsError("trades 的 exit_date 含 NaN/NaT,无法排序")
    n_has = sum(x is not None for x in exits)
    if items and n_has == len(items):
        try:
            order = sorted(range(len(items)), key=lambda i: exits[i])
        except TypeError:
            raise StatsError("trades 的 exit_date 类型不一致,无法排序(请统一为同一种日期类型)")
        rets = [rets[i] for i in order]
        trade_order = "exit_date"
    elif n_has == 0:
        trade_order = "input_order"
    else:
        raise StatsError(f"trades 中只有 {n_has}/{len(items)} 笔有 exit_date,无法确定先后顺序"
                         "(要么都给,要么都不给)")
    out = {"trade_status": "ok", "trade_reason": None, "trade_order": trade_order,
           "trade_count": len(rets),
           "trade_win_rate": None, "trade_profit_factor": None, "trade_expectancy": None,
           "max_losing_trades": None}
    if not rets:
        return out
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    out["trade_win_rate"] = len(wins) / len(rets)
    out["trade_expectancy"] = _mean(rets)
    out["max_losing_trades"] = _max_run(r < 0 for r in rets)
    if losses:
        out["trade_profit_factor"] = math.fsum(wins) / -math.fsum(losses)
    return out


def perf_stats(returns, rf: float = 0.0, trades=None, dates=None, nan_policy: str = "error",
               periods: int = TRADING_DAYS) -> dict:
    """从逐日简单收益计算绩效指标(口径见模块说明)。

    returns: 逐日简单收益序列(list/tuple/numpy/pandas Series);pandas Series 的索引自动当日期。
    rf: 年化无风险利率(默认 0);trades: 可选,逐笔交易(含 ret);dates: 可选,与 returns 等长。
    无法定义的指标返回 None,原因写进 warnings。
    """
    if not isinstance(periods, int) or isinstance(periods, bool) or periods <= 0:
        raise StatsError(f"periods 必须是正整数,收到 {periods!r}")
    rf = _num(rf, "rf")
    if rf != rf or math.isinf(rf):
        raise StatsError("rf 必须是有限数")
    vals, dts, n_dropped = _clean_returns(returns, dates, nan_policy, "returns")
    n = len(vals)
    warnings = []
    if n_dropped:
        warnings.append(f"剔除了 {n_dropped} 个 NaN")
    if n < _MIN_OBS_WARN:
        warnings.append(f"样本仅 {n} 个交易日(< {_MIN_OBS_WARN}),年化与夏普不稳定")

    log_sum = math.fsum(math.log1p(r) for r in vals)
    try:
        cum = math.expm1(log_sum)
    except OverflowError:
        cum = None
        warnings.append("累计收益溢出")
    ann_ret = None
    if n >= 2 and cum is not None:
        try:
            ann_ret = math.expm1(log_sum * periods / n)
        except OverflowError:
            warnings.append("年化收益溢出,不给数字")
    elif n < 2:
        warnings.append("只有 1 个点,年化收益/波动/夏普无定义")

    sd = _std(vals)
    ann_vol = sd * math.sqrt(periods) if sd is not None else None
    sharpe = None
    if sd is not None:
        if sd < _ZERO_STD:
            warnings.append("收益标准差为 0(全零或常数),夏普无定义")
        else:
            excess = _mean(vals) - rf / periods
            sharpe = excess / sd * math.sqrt(periods)

    dd = max_drawdown_info(vals, dts)
    calmar = None
    if ann_ret is not None and dd["max_drawdown"] < 0:
        calmar = ann_ret / -dd["max_drawdown"]
    elif dd["max_drawdown"] == 0:
        warnings.append("没有回撤,卡玛无定义")

    wins = [r for r in vals if r > 0]
    losses = [r for r in vals if r < 0]
    active = len(wins) + len(losses)
    pf = payoff = None
    if losses:
        pf = math.fsum(wins) / -math.fsum(losses)      # 全是亏损日时为 0,与交易级口径一致
    if losses and wins:
        payoff = (math.fsum(wins) / len(wins)) / (-math.fsum(losses) / len(losses))
    elif active:
        warnings.append("没有亏损日:日盈亏比无定义" if not losses
                        else "没有盈利日:平均盈利无定义,day_payoff_ratio 为 None(day_profit_factor 为 0)")

    out = {
        "n_obs": n, "n_dropped_nan": n_dropped, "rf": rf, "periods": periods,
        "start_date": str(dts[0]) if dts else None, "end_date": str(dts[-1]) if dts else None,
        "cum_return": cum, "ann_return": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe,
        "max_drawdown": dd["max_drawdown"], "max_dd_peak_date": dd["peak_date"],
        "max_dd_trough_date": dd["trough_date"], "max_dd_recovery_date": dd["recovery_date"],
        "max_dd_peak_idx": dd["peak_idx"], "max_dd_trough_idx": dd["trough_idx"],
        "max_dd_recovery_idx": dd["recovery_idx"], "calmar": calmar,
        "day_win_rate": len(wins) / active if active else None,
        "day_profit_factor": pf, "day_payoff_ratio": payoff,
        "day_expectancy": _mean(vals), "max_losing_days": _max_run(r < 0 for r in vals),
        "n_zero_days": n - active,
    }
    out.update(_trade_stats(trades))
    out["warnings"] = warnings
    return out


def decay_ratio(train_value, other_value) -> Optional[float]:
    """衰减 = (训练 - 其他) / 训练,正数表示其他段更差(1 表示降到 0,>1 表示变成负数)。

    训练值为 None、NaN 或 <= 0 时没有"下降比例"的意义,返回 None。
    """
    if train_value is None or other_value is None:
        return None
    if train_value != train_value or other_value != other_value or train_value <= 0:
        return None
    return (train_value - other_value) / train_value


_DECAY_KEYS = ("sharpe", "ann_return", "calmar")
_DECAY_NAMES = {"sharpe": "夏普", "ann_return": "年化收益", "calmar": "卡玛"}
_SEG_NAMES = {"validate": "验证段", "test": "测试段"}


def compare_segments(train, validate, test, rf: float = 0.0, nan_policy: str = "error",
                     decay_warn: float = 0.5, periods: int = TRADING_DAYS) -> dict:
    """训练/验证/测试三段的指标对比与衰减。validate、test 可为 None(记为 unavailable)。

    返回 {"train","validate","test": perf_stats 结果或 {"status":"unavailable"},
          "decay": {"validate": {...}, "test": {...}}, "hints": [...], "decay_warn": 阈值}。
    衰减 = (训练指标 - 本段指标) / 训练指标,对夏普、年化收益、卡玛各算一份;训练指标 ≤ 0 时为 None。
    hints 只是提示字符串(带数值),不是"过拟合/有效"的结论:短样本、衰减超阈值、符号翻转。
    """
    if not isinstance(decay_warn, (int, float)) or isinstance(decay_warn, bool) or not 0 <= decay_warn:
        raise StatsError(f"decay_warn 必须是非负数,收到 {decay_warn!r}")
    if train is None:
        raise StatsError("train 段不能为空")
    segs = {"train": perf_stats(train, rf=rf, nan_policy=nan_policy, periods=periods)}
    for name, data in (("validate", validate), ("test", test)):
        if data is None:
            segs[name] = {"status": "unavailable", "reason": f"未提供{_SEG_NAMES[name]}数据"}
        else:
            segs[name] = perf_stats(data, rf=rf, nan_policy=nan_policy, periods=periods)

    hints = []
    tr = segs["train"]
    decay = {}
    for name in ("validate", "test"):
        seg = segs[name]
        if "status" in seg:
            decay[name] = {k: None for k in _DECAY_KEYS}
            continue
        decay[name] = {k: decay_ratio(tr[k], seg[k]) for k in _DECAY_KEYS}
        label = _SEG_NAMES[name]
        if seg["n_obs"] < _MIN_OBS_WARN:
            hints.append(f"{label}仅 {seg['n_obs']} 个交易日(< {_MIN_OBS_WARN}),指标噪声大,对比意义有限")
        for k in _DECAY_KEYS:
            dr = decay[name][k]
            if dr is not None and dr > decay_warn:
                hints.append(f"{label}{_DECAY_NAMES[k]}相对训练段下降 {dr * 100:.1f}%"
                             f"(> {decay_warn * 100:.0f}%):训练段表现可能含运气/拟合成分,仅为提示")
        if tr["sharpe"] is not None and seg["sharpe"] is not None \
                and tr["sharpe"] > 0 and seg["sharpe"] < 0:
            hints.append(f"{label}夏普为负({seg['sharpe']:.2f}),训练段为正({tr['sharpe']:.2f}):符号翻转")
    if tr["sharpe"] is None or tr["sharpe"] <= 0:
        hints.append("训练段夏普缺失或 ≤ 0,衰减比例没有意义(已返回 None)")
    segs["decay"] = decay
    segs["hints"] = hints
    segs["decay_warn"] = decay_warn
    return segs
