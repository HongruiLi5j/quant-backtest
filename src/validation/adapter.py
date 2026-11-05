"""适配器(T007 / S6):把"某策略在某段数据上的逐日收益 + 交易列表"统一成与引擎无关的 RunRecord。

两条路径:
  1. 主路径 from_strategy:只读调用现有 src.strategies / src.portfolio.run_weights(函数内部延迟导入,
     不修改被调用模块、不 monkeypatch、不用 data.load / datasource / trade_list.generate / simtrade.connect 等
     会读写 data/ 或联网的东西)。strategy_fn / run_fn 可注入假函数,测试不依赖真实回测。
  2. 降级路径(显式,不静默切换):from_returns / from_csv / from_trades 由调用者传入外部收益或交易数据。
     降级路径完全不 import src,也不 import pandas。

口径:
  - 收益是逐日"简单收益"(小数,不是百分比),每个值必须是有限数且 > -1。
  - 日期统一成 datetime.date,必须严格升序、无重复;不在这里猜交易日历(缺日只能与调用者给的 calendar 比对)。
  - cost_included 只有 True/False/None 三态,默认 None(未声明),不替调用者猜。
  - 只有交易列表时无法恢复逐日净值:RunRecord.has_daily 为 False,调用 require_daily() 会抛 AdapterError。
  - 本模块不创建、不写任何文件;csv 只读调用者显式给的路径。run_grid 的落盘只发生在调用者传入的 ledger 里。
"""
from __future__ import annotations

import csv
import datetime as _dt
import importlib
import itertools
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .. import costs as rates    # 费率常量的单一来源(src/costs.py,叶子模块);别名与 validation/costs.py 保持一致

# from_strategy 默认路径支持的单品种策略(dual_momentum 需要多品种 dict,blend/vol_scale 不是策略,均不在内)
KNOWN_STRATEGIES = ("trend_vol_target", "trend_ensemble", "donchian_breakout", "donchian_s1",
                    "rsi2_reversion", "ma_adx_plus", "ma_adx_original")
SRC_SOURCE = "portfolio.run_weights"
EXTERNAL_SOURCE = "external"
GRID_METRIC = "sharpe_daily"      # 与 trials.sharpe 同口径:单周期(日)夏普,未年化
MAX_GRID = 500                    # 网格上限,防止误传超大网格;网格必须由调用者显式给出
_NO_DATA_MSG = "无法调用现有回测,且未提供外部收益/交易数据"


class AdapterError(ValueError):
    """输入不合法、无法调用现有回测、或请求了当前数据不支持的指标。"""


# ---------------------------------------------------------------- 基础校验

def _to_date(x, what="日期") -> _dt.date:
    """date / datetime / pandas.Timestamp(datetime 子类) / 'YYYY-MM-DD[ T...]' 字符串 -> date。"""
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    if isinstance(x, str):
        s = x.strip()
        try:
            # 严格解析:10 位只当日期;更长的只接受完整的 ISO 日期时间,结尾多余字符一律报错
            return _dt.date.fromisoformat(s) if len(s) == 10 else _dt.datetime.fromisoformat(s).date()
        except ValueError:
            pass
    raise AdapterError(f"{what}无法解析为日期: {x!r}")


def _to_float(x, what: str, allow_nan=False) -> float:
    if isinstance(x, bool):
        raise AdapterError(f"{what}不能是布尔值")
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise AdapterError(f"{what}不是数值: {x!r}") from None
    if math.isinf(v) or (math.isnan(v) and not allow_nan):
        raise AdapterError(f"{what}必须是有限数值: {x!r}")
    return v


def _check_dates(dates: Sequence[_dt.date]):
    for i in range(1, len(dates)):
        if dates[i] == dates[i - 1]:
            raise AdapterError(f"日期重复: {dates[i]}")
        if dates[i] < dates[i - 1]:
            raise AdapterError(f"日期乱序: {dates[i - 1]} 之后出现 {dates[i]}")
    # 非相邻重复在严格升序检查下不可能出现,此处无需再查


# 由适配器自己校验并写入的 meta 键:调用者传的 meta 不能覆盖它们(防止伪造来源/含费声明)
RESERVED_META = ("source", "base_cost", "cost_included", "n_dropped_nan")


def _user_meta(meta) -> dict:
    m = dict(meta or {})
    bad = [k for k in RESERVED_META if k in m]
    if bad:
        raise AdapterError(f"meta 不能包含保留键 {bad};请用对应的参数传入(base_cost / cost_included)")
    return m


def _check_flag(cost_included):
    if cost_included not in (True, False, None):
        raise AdapterError("cost_included 只能是 True / False / None(未声明)")
    return cost_included


@dataclass(frozen=True)
class RunRecord:
    """统一的回测结果。dates/returns/turnover 为等长元组;trades 为字典元组。

    has_daily=False(仅交易列表)时 dates/returns 为空元组。"""
    dates: tuple
    returns: tuple
    trades: tuple = ()
    turnover: Optional[tuple] = None
    meta: dict = field(default_factory=dict)

    @property
    def has_daily(self) -> bool:
        return len(self.returns) > 0

    @property
    def n_days(self) -> int:
        return len(self.returns)

    def require_daily(self, what: str = "该指标"):
        """需要逐日收益的指标(夏普/回撤/滚动前进/切分)调用它;仅交易列表时抛错,不做假精确。"""
        if not self.has_daily:
            raise AdapterError(f"{what}需要逐日收益序列,但当前只有交易列表(仅交易列表无法恢复逐日净值);"
                               "请提供逐日收益,或只请求交易级指标")
        return self

    def trade_returns(self) -> list:
        """逐笔收益列表(交易级指标用)。"""
        return [t["ret"] for t in self.trades]


# ---------------------------------------------------------------- 降级路径:外部数据

def _build_trades(trades) -> tuple:
    if trades is None:
        return ()
    if hasattr(trades, "to_dict") and hasattr(trades, "columns"):      # DataFrame
        trades = trades.to_dict("records")
    out = []
    for i, t in enumerate(trades):
        if not isinstance(t, Mapping):
            raise AdapterError(f"第 {i} 笔交易必须是字典(含 entry_date/exit_date/ret): {t!r}")
        for k in ("entry_date", "exit_date", "ret"):
            if k not in t:
                raise AdapterError(f"第 {i} 笔交易缺少字段 {k}")
        ed, xd = _to_date(t["entry_date"], f"第 {i} 笔 entry_date"), _to_date(t["exit_date"], f"第 {i} 笔 exit_date")
        if xd < ed:
            raise AdapterError(f"第 {i} 笔交易出场日早于进场日: {ed} > {xd}")
        r = _to_float(t["ret"], f"第 {i} 笔 ret")
        if r <= -1:
            raise AdapterError(f"第 {i} 笔 ret 必须 > -1(小数收益,不是百分比): {r}")
        row = {k: v for k, v in t.items() if k not in ("entry_date", "exit_date", "ret")}
        row.update(entry_date=ed, exit_date=xd, ret=r)
        out.append(row)
    return tuple(out)


def from_returns(dates, returns, trades=None, turnover=None, base_cost=None, cost_included=None,
                 nan_policy: str = "error", meta: Optional[dict] = None,
                 _trusted: Optional[dict] = None) -> RunRecord:
    """由外部给定的逐日收益构建 RunRecord(降级路径,不依赖 src)。

    nan_policy: "error"(默认,含 NaN 抛错) 或 "drop"(剔除含 NaN 的日子,计数写入 meta["n_dropped_nan"],
    turnover 同步剔除)。日期必须严格升序且无重复;长度必须一致。"""
    if nan_policy not in ("error", "drop"):
        raise AdapterError("nan_policy 只能是 'error' 或 'drop'")
    dates = list(dates)
    returns = list(returns)
    if len(dates) != len(returns):
        raise AdapterError(f"dates 与 returns 长度不一致: {len(dates)} vs {len(returns)}")
    if not dates:
        raise AdapterError("收益序列为空")
    turn = None
    if turnover is not None:
        turnover = list(turnover)
        if len(turnover) != len(dates):
            raise AdapterError(f"turnover 与 dates 长度不一致: {len(turnover)} vs {len(dates)}")
        turn = [_to_float(v, "turnover", allow_nan=(nan_policy == "drop")) for v in turnover]
    ds = [_to_date(d) for d in dates]
    _check_dates(ds)
    rs = [_to_float(v, "收益", allow_nan=True) for v in returns]
    keep = [i for i, v in enumerate(rs) if not math.isnan(v)]
    n_drop = len(rs) - len(keep)
    if n_drop and nan_policy == "error":
        raise AdapterError(f"收益含 {n_drop} 个 NaN(首个在 {ds[next(i for i, v in enumerate(rs) if math.isnan(v))]});"
                           "可用 nan_policy='drop' 剔除并计数")
    if turn is not None:
        keep = [i for i in keep if not math.isnan(turn[i])] if nan_policy == "drop" else keep
        n_drop = len(rs) - len(keep)
        for v in turn:
            if not math.isnan(v) and v < 0:
                raise AdapterError("turnover 不能为负")
    ds, rs = [ds[i] for i in keep], [rs[i] for i in keep]
    turn = [turn[i] for i in keep] if turn is not None else None
    if not rs:
        raise AdapterError("剔除 NaN 后收益序列为空")
    bad = [(d, v) for d, v in zip(ds, rs) if v <= -1]
    if bad:
        raise AdapterError(f"收益必须 > -1(小数收益,不是百分比);{bad[0][0]} 的值为 {bad[0][1]}")
    if base_cost is not None:
        base_cost = _to_float(base_cost, "base_cost")
        if base_cost < 0:
            raise AdapterError("base_cost 不能为负")
    m = _user_meta(meta)
    m.update(source=EXTERNAL_SOURCE, base_cost=base_cost, cost_included=_check_flag(cost_included),
             n_dropped_nan=n_drop)
    m.update(_trusted or {})                     # 仅适配器内部(from_strategy 等)使用,字段已在调用处校验
    return RunRecord(tuple(ds), tuple(rs), _build_trades(trades), tuple(turn) if turn is not None else None, m)


def from_csv(path, date_col: str = "date", ret_col: str = "ret", turnover_col: Optional[str] = None,
             trades=None, base_cost=None, cost_included=None, nan_policy: str = "error",
             meta: Optional[dict] = None) -> RunRecord:
    """只读调用者显式给的 csv 路径(utf-8,允许 BOM),转 from_returns。空单元格视为 NaN。"""
    dates, rets, turns = [], [], []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            cols = rd.fieldnames or []
            need = [date_col, ret_col] + ([turnover_col] if turnover_col else [])
            for c in need:
                if c not in cols:
                    raise AdapterError(f"csv 缺少列 {c!r}(现有列: {cols})")
            for row in rd:
                dates.append(row[date_col])
                rets.append(float("nan") if (row[ret_col] or "").strip() == "" else row[ret_col])
                if turnover_col:
                    turns.append(float("nan") if (row[turnover_col] or "").strip() == "" else row[turnover_col])
    except OSError as e:
        raise AdapterError(f"无法读取 csv: {e}") from None
    m = _user_meta(meta)
    m["csv_path"] = str(path)
    return from_returns(dates, rets, trades=trades, turnover=turns if turnover_col else None,
                        base_cost=base_cost, cost_included=cost_included, nan_policy=nan_policy, meta=m)


def trades_from_csv(path, entry_col: str = "entry_date", exit_col: str = "exit_date",
                    ret_col: str = "ret") -> list:
    """只读 csv 的逐笔交易,返回可传给 from_returns/from_trades 的字典列表。"""
    out = []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            for c in (entry_col, exit_col, ret_col):
                if c not in (rd.fieldnames or []):
                    raise AdapterError(f"交易 csv 缺少列 {c!r}(现有列: {rd.fieldnames})")
            for row in rd:
                out.append({"entry_date": row[entry_col], "exit_date": row[exit_col], "ret": row[ret_col]})
    except OSError as e:
        raise AdapterError(f"无法读取 csv: {e}") from None
    return out


def from_trades(trades, base_cost=None, cost_included=None, meta: Optional[dict] = None) -> RunRecord:
    """仅由交易列表构建 RunRecord。无逐日收益:has_daily=False,逐日指标请求会抛 AdapterError。"""
    tr = _build_trades(trades)
    if not tr:
        raise AdapterError("交易列表为空")
    if base_cost is not None:
        base_cost = _to_float(base_cost, "base_cost")
        if base_cost < 0:
            raise AdapterError("base_cost 不能为负")
    m = _user_meta(meta)
    m.update(source=EXTERNAL_SOURCE, base_cost=base_cost, cost_included=_check_flag(cost_included),
             n_dropped_nan=0, daily_available=False)
    return RunRecord((), (), tr, None, m)


# ---------------------------------------------------------------- 对齐与日历检查

def check_calendar(record: RunRecord, calendar: Iterable) -> dict:
    """与调用者给的交易日历比对(不猜日历):返回 missing(日历有、记录无)与 extra(记录有、日历无)。

    只比对,不修改记录;日历内有缺日时调用者自己决定补 0 还是报错。"""
    record.require_daily("日历检查")
    cal = [_to_date(d, "日历日期") for d in calendar]
    _check_dates(cal)
    have, cs = set(record.dates), set(cal)
    lo, hi = record.dates[0], record.dates[-1]
    missing = [d for d in cal if lo <= d <= hi and d not in have]
    extra = [d for d in record.dates if d not in cs]
    return {"missing": missing, "extra": extra, "ok": not missing and not extra}


def align_pair(a: RunRecord, b: RunRecord) -> dict:
    """按日期取两条记录的交集(用于回测 vs 模拟盘对比):返回 dates / a / b 与各自独有的日期。"""
    a.require_daily("日期对齐")
    b.require_daily("日期对齐")
    da, db = dict(zip(a.dates, a.returns)), dict(zip(b.dates, b.returns))
    common = sorted(set(da) & set(db))
    return {"dates": common, "a": [da[d] for d in common], "b": [db[d] for d in common],
            "only_a": sorted(set(da) - set(db)), "only_b": sorted(set(db) - set(da))}


# ---------------------------------------------------------------- 主路径:只读调用现有回测

def _items(x, what):
    """pandas Series / dict -> [(key, value)];不 import pandas。"""
    if hasattr(x, "items"):
        return list(x.items())
    raise AdapterError(f"{what}必须是带日期索引的 Series 或字典")


def _result_to_record(res, meta) -> RunRecord:
    """把 run_weights 风格的结果对象(returns / turnover / trades)转成 RunRecord。"""
    for attr in ("returns", "turnover"):
        if not hasattr(res, attr):
            raise AdapterError(f"回测结果缺少属性 {attr}")
    ritems = _items(res.returns, "returns")
    titems = _items(res.turnover, "turnover")
    if [k for k, _ in ritems] != [k for k, _ in titems]:
        raise AdapterError("returns 与 turnover 的日期索引不一致")
    trades = getattr(res, "trades", None)
    if trades is not None and hasattr(trades, "__len__") and len(trades) == 0:
        trades = None
    return from_returns([k for k, _ in ritems], [v for _, v in ritems], trades=trades,
                        turnover=[v for _, v in titems], base_cost=meta["cost"],
                        cost_included=meta.get("cost_included"), _trusted=meta)


def _import_src():
    try:
        # 延迟导入;用 import_module 而不是 from src import ...,以便导入失败(含被屏蔽)时可靠地抛错
        portfolio = importlib.import_module("src.portfolio")
        strategies = importlib.import_module("src.strategies")
    except Exception as e:                          # ImportError 以及导入期的其它错误
        raise AdapterError(f"{_NO_DATA_MSG}(导入 src 失败: {type(e).__name__}: {e});"
                           "可改用 from_returns / from_csv / from_trades") from None
    return portfolio, strategies


def from_strategy(df, strategy: str, params: Optional[Mapping] = None, *, code: str = "X",
                  cost: float = rates.ENGINE_DEFAULT_ONE_WAY, band: float = 0.0, limit: Optional[float] = None,
                  cash_ret=None, strategy_fn: Optional[Callable] = None,
                  run_fn: Optional[Callable] = None, cost_included=None) -> RunRecord:
    """只读调用现有回测:strategies.<strategy>(df, **params) 得目标仓位 -> run_weights -> RunRecord。

    df: 调用者提供的 OHLC DataFrame(单品种)。limit: 涨跌停幅度(如 0.10),传给 run_weights 的 limits。
    strategy_fn(df, **params) -> target、run_fn(prices, target, cost=, band=, limits=, cash_ret=) -> 结果:
    测试时注入假函数。都不注入时走真实 src(策略名必须在 KNOWN_STRATEGIES 内)。
    真实 run_weights 的收益已扣除 cost,所以没注入 run_fn 时 cost_included=True、source="portfolio.run_weights"
    (即使只注入了 strategy_fn);注入 run_fn 时 source="injected_runner",cost_included 由调用者声明(默认 None)。"""
    params = dict(params or {})
    if not isinstance(strategy, str) or not strategy:
        raise AdapterError("strategy 必须是非空字符串")
    cost = _to_float(cost, "cost")
    if cost < 0:
        raise AdapterError("cost 不能为负")
    injected = run_fn is not None          # 来源/含费只看有没有注入 run_fn(只注入 strategy_fn 时仍是真实 run_weights)
    if strategy_fn is None or run_fn is None:
        portfolio, strategies = _import_src()
        if strategy_fn is None:
            if strategy not in KNOWN_STRATEGIES:
                raise AdapterError(f"未知策略 {strategy!r};已知: {', '.join(KNOWN_STRATEGIES)}。"
                                   "可改用 from_returns 等降级入口")
            strategy_fn = getattr(strategies, strategy)
        if run_fn is None:
            run_fn = portfolio.run_weights
    limits = {code: _to_float(limit, "limit")} if limit is not None else None
    try:
        target = strategy_fn(df, **params)
        res = run_fn({code: df}, target, cost=cost, band=band, limits=limits, cash_ret=cash_ret)
    except AdapterError:
        raise
    except Exception as e:
        raise AdapterError(f"调用回测失败: {type(e).__name__}: {e}") from None
    if not injected:
        cost_included = True
    meta = {"source": "injected_runner" if injected else SRC_SOURCE, "strategy": strategy, "params": params,
            "code": code, "cost": cost, "band": band, "limit": limit, "cost_included": _check_flag(cost_included)}
    return _result_to_record(res, meta)


def rerun_with_held(df, held, *, code: str = "X", cost: float = rates.ENGINE_DEFAULT_ONE_WAY,
                    run_fn: Optional[Callable] = None, cost_included=None) -> RunRecord:
    """用"受限后的持仓路径" held 交回 run_weights 重算收益(成交约束压力用)。

    run_weights 把 target 当"收盘后定的目标",内部 shift(1) 才是次日持仓;而 held[t] 已是 t 日开盘后的持仓,
    所以传入 target = held.shift(-1)(末行补 0,该行落在区间之外,只影响最后一天之后);band=0、不传 limits
    (约束已体现在 held 里)。held 的日期必须与 df.index 完全一致。"""
    pairs = _items(held, "held")
    idx = [_to_date(d) for d in df.index]
    hd = [_to_date(k) for k, _ in pairs]
    if hd != idx:
        raise AdapterError("held 的日期必须与 df.index 逐日一致")
    vals = [_to_float(v, "held 持仓") for _, v in pairs]
    if any(v < 0 for v in vals):
        raise AdapterError("held 持仓不能为负")
    if vals and vals[0] != 0:
        # run_weights 内部 shift(1),第 0 天持仓固定为 0;第 0 天有仓位无法重现,拒绝而不是悄悄丢掉
        raise AdapterError(f"held 第 0 天持仓必须为 0(run_weights 的首日持仓恒为 0),实际 {vals[0]}")
    shifted = vals[1:] + [0.0]
    injected = run_fn is not None
    if run_fn is None:
        portfolio, _ = _import_src()
        run_fn = portfolio.run_weights
    try:
        import pandas as pd
        target = pd.Series(shifted, index=df.index)
        res = run_fn({code: df}, target, cost=cost, band=0.0, limits=None, cash_ret=None)
    except AdapterError:
        raise
    except Exception as e:
        raise AdapterError(f"重算失败: {type(e).__name__}: {e}") from None
    if not injected:
        cost_included = True
    meta = {"source": "injected_runner" if injected else SRC_SOURCE, "strategy": "held_path", "params": {},
            "code": code, "cost": cost, "band": 0.0, "limit": None, "cost_included": _check_flag(cost_included),
            "rerun_with_held": True}
    rec = _result_to_record(res, meta)
    return RunRecord(rec.dates, rec.returns, rec.trades, rec.turnover, {**rec.meta, "held": tuple(vals)})


def _daily_sharpe(returns: Sequence[float]):
    """日夏普(均值/样本标准差,未年化);无法计算(样本太少或零波动)返回 None。"""
    if len(returns) < 2:
        return None
    sd = statistics.stdev(returns)
    return statistics.fmean(returns) / sd if sd > 0 else None


def run_grid(df, strategy: str, grid: Mapping[str, Sequence], ledger=None, *, tag: str = "",
             metric_name: str = GRID_METRIC, **kwargs) -> dict:
    """对调用者显式给出的参数网格逐组调用 from_strategy,并登记试验次数。

    grid: {参数名: [取值...]},按参数名排序后做笛卡尔积(顺序确定);组合数上限 MAX_GRID。
    ledger: 调用者传入的 TrialLog(需有 record 方法)。传了就**每组都登记**(先全部回测成功再统一登记,
    回测报错则一组都不登记);不传时返回 untracked=True 并附警告"这批试验未计入试验次数"。
    某组夏普无法计算(样本太少/零波动)时仍登记(试验确实发生过),value 记 0.0 并加 tag 'degenerate',
    结果里 metric 为 None、degenerate=True。本函数自身不写文件。其余关键字参数传给 from_strategy。"""
    if not isinstance(grid, Mapping) or not grid:
        raise AdapterError("grid 必须是非空字典 {参数名: [取值...]}")
    names = sorted(grid)
    lists = []
    for k in names:
        v = grid[k]
        if isinstance(v, (str, bytes)) or not isinstance(v, Sequence) or len(v) == 0:
            raise AdapterError(f"grid[{k!r}] 必须是非空列表")
        lists.append(list(v))
    total = math.prod(len(v) for v in lists)
    if total > MAX_GRID:
        raise AdapterError(f"网格共 {total} 组,超过上限 {MAX_GRID};请缩小网格(不做自动搜索)")
    if ledger is not None and not callable(getattr(ledger, "record", None)):
        raise AdapterError("ledger 必须有 record() 方法(例如 trials.TrialLog)")
    runs = []
    for combo in itertools.product(*lists):
        p = dict(zip(names, combo))
        rec = from_strategy(df, strategy, p, **kwargs)
        runs.append((p, rec, _daily_sharpe(rec.returns)))
    results = []
    for p, rec, sr in runs:
        tid = None
        if ledger is not None:
            tid = ledger.record(strategy, p, metric_name, 0.0 if sr is None else sr, rec.n_days,
                                tag=tag if sr is not None else (tag + " degenerate").strip())
        results.append({"params": p, "record": rec, "metric": sr, "degenerate": sr is None, "trial_id": tid})
    untracked = ledger is None
    warnings = ["这批试验未计入试验次数"] if untracked else []
    return {"strategy": strategy, "metric_name": metric_name, "results": results, "n_trials": len(results),
            "untracked": untracked, "warnings": warnings}
