"""上传收益序列的策略体检（T030 S1）：把用户自己的日收益序列包装成 health_check 能吃的输入，交给 T022 的体检逻辑。

只做输入校验和格式转换：**不联网、不读 data/、不写文件、不调用任何交易接口、不预测、不推荐**。统计判定全部复用 src/health_check.py。

输入（全部是普通 Python 对象，由调用方——将来是接口层——负责解析 CSV/JSON 并限制上传大小）：
  dates       日期（ISO 字符串或 date），严格递增；
  returns     日收益（小数，0.01 = 1%），每个值是有限数且 > -1；
  n_trials    你为了得到这个策略一共试过多少个版本（≥1 的整数）；少报会让 Deflated Sharpe 高估，代码无法替你核实；
  benchmark   可选，同长度的买入持有日收益；没有则「相对基准」一项记 na；
  n_trades    可选，完整交易笔数；没有则样本一项的笔数记 na；
  split_date  可选，样本内/外切分日（该日起算样本外）；缺省按前 70% / 后 30% 事后切分；
  turnover + cost_bp + cost_included   可选，每日成交额（买+卖）占净值的比例、单边费率（bp）、收益是否已扣成本。

诚实性：核心项（样本、Deflated Sharpe、相对基准、样本外）缺数据时 health_check 不会给「通过」；
上传序列没有可重跑的策略，所以「参数平台」「蒙特卡洛」两项永远是 na，不会因此被当成通过。
"""
from __future__ import annotations

import datetime as _dt
import math

from . import health_check as hc

MAX_ROWS = 20000          # 单次上传的最大行数（约 80 年日线）
MIN_ROWS = 3
MIN_SEGMENT = 30          # 用户指定切分日时，样本内和样本外各至少这么多天
MAX_TRIALS = 10 ** 9
MAX_ABS_COST_BP = 1000.0


def _date(x, what="日期") -> _dt.date:
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    if isinstance(x, str):
        try:
            return _dt.date.fromisoformat(x.strip())
        except ValueError:
            pass
    raise ValueError(f"{what}必须是 YYYY-MM-DD 格式的日期：{x!r}")


def _floats(xs, what, low=None, high=None, low_inclusive=True):
    out = []
    for i, v in enumerate(xs):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{what}第 {i + 1} 个值不是数字：{v!r}")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(f"{what}第 {i + 1} 个值不是有限数")
        if low is not None and (f < low if low_inclusive else f <= low):
            raise ValueError(f"{what}第 {i + 1} 个值 {f} 超出允许范围")
        if high is not None and f > high:
            raise ValueError(f"{what}第 {i + 1} 个值 {f} 超出允许范围")
        out.append(f)
    return out


def _equity(rets):
    e = [1.0]
    for r in rets:
        e.append(e[-1] * (1.0 + r))
    if any(not math.isfinite(v) or v <= 0 for v in e):
        raise ValueError("收益累乘后净值不是正的有限数")
    return e


def _sharpe_ann(rets):
    n = len(rets)
    if n < 2:
        return None
    mu = math.fsum(rets) / n
    var = math.fsum((x - mu) ** 2 for x in rets) / (n - 1)
    if var <= 0:
        return None
    return mu / math.sqrt(var) * math.sqrt(hc.TD)


def _fix_oos_text(out, split_used, split_note, is_s, oos_s):
    """health_check 的样本外说明固定写「前 70% / 后 30%」；上传时按实际切分日改写，避免与用户指定的切分日矛盾。"""
    for c in out["checks"]:
        if c["id"] == "oos" and c["status"] != "na":
            keep = int(hc.THRESHOLDS["oos_keep_ratio"] * 100)
            c["zh"] = (f"以 {split_used} 为界（{split_note}）：样本内夏普 {hc._fmt(is_s)} → 样本外夏普 {hc._fmt(oos_s)}。样本外要为正，且至少保留样本内的 {keep}%。"
                       "注意：这个切分是事后选的，策略和参数本身可能已经看过全部数据，所以不是干净的样本外。")
            c["en"] = (f"Split at {split_used} ({'post-hoc 70/30 split' if 'user' not in split_note and '用户' not in split_note else 'owner-chosen split date'}): "
                       f"in-sample Sharpe {hc._fmt(is_s)} -> out-of-sample Sharpe {hc._fmt(oos_s)}. Out-of-sample must be positive and keep at least {keep}% of in-sample. "
                       "Note: the split is chosen after the fact and the strategy or its parameters may already have seen all the data, so this is not a clean out-of-sample test.")
    for c in out["caveats"]:
        if "70/30" in c["zh"]:
            c["zh"] = "样本内/外是事后切的（缺省 70/30，或你指定的切分日），不是封存的干净样本外。"
            c["en"] = "The in/out-of-sample split is chosen after the fact (default 70/30, or your date), not a sealed clean out-of-sample test."


def _fix_cost_ruin(out, ruined):
    """成本压力下出现单日亏光（净值归零）：这是失败，不是缺数据；health_check 会把 None 当成没数据，所以在这里改判。"""
    for c in out["checks"]:
        if c["id"] == "cost":
            c["status"] = "fail" if ("1x" in ruined or "2x" in ruined) else "warn"
            c["value"]["ruined"] = list(ruined)
            c["zh"] += f" 注意：在成本 {'/'.join(ruined)} 的压力场景下出现单日亏光（≤ -100%），这按失败处理，不是缺数据。"
            c["en"] += f" Note: under the {'/'.join(ruined)} cost stress a single day loses everything (<= -100%); this counts as a failure, not missing data."
    out["verdict"] = hc._verdict(out["checks"])


def upload_check(dates, returns, n_trials, *, benchmark=None, n_trades=None, split_date=None,
                 turnover=None, cost_bp=None, cost_included=None) -> dict:
    if isinstance(n_trials, bool) or not isinstance(n_trials, int) or not 1 <= n_trials <= MAX_TRIALS:
        raise ValueError("n_trials 必须是 ≥ 1 的整数（你一共试过多少个策略/参数版本）")
    if not isinstance(dates, (list, tuple)) or not isinstance(returns, (list, tuple)):
        raise ValueError("dates 和 returns 必须是列表")
    n = len(returns)
    if n != len(dates):
        raise ValueError("日期和收益的行数不一致")
    if not MIN_ROWS <= n <= MAX_ROWS:
        raise ValueError(f"行数必须在 {MIN_ROWS} 到 {MAX_ROWS} 之间")
    ds = [_date(d) for d in dates]
    if any(b <= a for a, b in zip(ds, ds[1:])):
        raise ValueError("日期必须严格递增（不能有重复或倒序）")
    r = _floats(returns, "收益", low=-1.0, high=10.0, low_inclusive=False)       # 日收益 > -100%，且不超过 +1000%（防数量级错误）

    b = None
    if benchmark is not None:
        if not isinstance(benchmark, (list, tuple)) or len(benchmark) != n:
            raise ValueError("基准收益必须是与收益等长的列表")
        b = _floats(benchmark, "基准收益", low=-1.0, high=10.0, low_inclusive=False)

    if n_trades is not None and (isinstance(n_trades, bool) or not isinstance(n_trades, int) or n_trades < 0):
        raise ValueError("n_trades 必须是 ≥ 0 的整数或不填")

    has_cost = turnover is not None or cost_bp is not None
    if has_cost and (turnover is None or cost_bp is None):
        raise ValueError("成本敏感性需要同时提供 turnover 和 cost_bp")
    if cost_included is not None and not isinstance(cost_included, bool):
        raise ValueError("cost_included 必须是 true/false 或不填")
    warnings = []
    t = None
    if has_cost:
        if cost_included is None:
            raise ValueError("提供了换手和费率，必须说明收益是否已经扣过成本（cost_included）")
        if not isinstance(turnover, (list, tuple)) or len(turnover) != n:
            raise ValueError("turnover 必须是与收益等长的列表")
        t = _floats(turnover, "换手", low=0.0, high=20.0)
        if isinstance(cost_bp, bool) or not isinstance(cost_bp, (int, float)) or not 0 <= float(cost_bp) <= MAX_ABS_COST_BP:
            raise ValueError("cost_bp 必须是 0 到 1000 之间的数字（单边，bp）")
        unit = float(cost_bp) / 1e4
        if cost_included is False:
            r = [x - ti * unit for x, ti in zip(r, t)]                       # 用户给的是毛收益：先扣 1 倍成本，再在净收益上做体检
            if any(x <= -1.0 for x in r):
                raise ValueError("扣成本后出现 ≤ -100% 的日收益")
    elif cost_included is False:
        warnings.append("收益未扣成本且没有提供换手数据：结论会偏乐观。")
    elif cost_included is None:
        warnings.append("没有说明收益是否已扣成本：请自行确认，未扣成本的收益会让结论偏乐观。")

    if split_date is None:
        cut = int(n * 0.7)
        split_used, split_note = ds[cut].isoformat() if 0 < cut < n else None, "事后按前 70% / 后 30% 切分"
    else:
        sd = _date(split_date, "切分日")
        cut = next((i for i, d in enumerate(ds) if d >= sd), n)
        if cut < MIN_SEGMENT or n - cut < MIN_SEGMENT:
            raise ValueError(f"切分日需让样本内和样本外各至少 {MIN_SEGMENT} 个交易日")
        split_used, split_note = ds[cut].isoformat(), "用户指定的切分日（仍是事后选择，不是封存的样本外）"
    is_s, oos_s = _sharpe_ann(r[:cut]), _sharpe_ann(r[cut:])
    split = {"date": split_used, "is": {"sharpe": is_s}, "oos": {"sharpe": oos_s}}

    cost_sens, ruined = None, []
    if t is not None:
        cost_sens, ruined = [], []                                            # 此时 r 已是净收益（含 1 倍成本）
        for label, k in (("1x", 0), ("2x", 1), ("4x", 3)):                     # 在净收益上再多扣 k 倍成本
            rk = [x - k * ti * unit for x, ti in zip(r, t)]
            if any(x <= -1.0 for x in rk):
                ruined.append(label)                                          # 单日亏光：不是「缺数据」，下面单独判失败
                cost_sens.append({"label": label, "sharpe": None})
            else:
                cost_sens.append({"label": label, "sharpe": _sharpe_ann(rk)})

    res = {"equity": _equity(r), "bench": _equity(b) if b is not None else None, "stats": {"Trades": n_trades},
           "analysis": {"split": split, "cost_sens": cost_sens}, "strategy": None, "symbol": None, "params": None,
           "start": ds[0].isoformat(), "end": ds[-1].isoformat()}
    out = hc.health_check(res, n_trials)

    _fix_oos_text(out, split_used, split_note, is_s, oos_s)
    if ruined:
        _fix_cost_ruin(out, ruined)
    missing = []
    if b is None:
        missing.append("benchmark")
    if n_trades is None:
        missing.append("n_trades")
    if t is None:
        missing.append("turnover+cost_bp")
    out["upload"] = {"rows": n, "first": ds[0].isoformat(), "last": ds[-1].isoformat(), "split_date": split_used, "split_note": split_note,
                     "cost_included": cost_included, "warnings": warnings, "missing_inputs": missing,
                     "not_applicable": ["plateau", "mc"],
                     "note": "上传的序列没有可重跑的策略，参数平台和蒙特卡洛两项不适用；缺少上面列出的输入时，对应检查项为 na，整体最多给到「检查项不全」。"}
    return out
