"""T008 S4:训练段一次性淘汰 + 验证段只看 + 封存段走锁定守卫(设计文档第 7 节)。

输入是 S3 回放引擎写出的逐日行(`engine.load_rows`),每行是某个(策略, 参数组合)某个信号日的结果:
net(扣成本净收益)、gross(成本前)、cost、mkt(当日全市场等权)、excess(= net - mkt)、
weights(目标权重,条数 = 当日选股数 k)、shares(调仓后持仓)、next_date(持有到的日期)。
另需一个 NullPanel(个股逐日收益 + 可交易掩码),用来做随机对照的零分布。

一、训练段(train_eliminate):先写死的 4 条规则,满足任一条即淘汰,只做一次,不翻转信号
  1. 扣成本后超额显著低于随机对照:策略"周平均净超额"的均值 < 零分布第 5 百分位。
     零分布 = 匹配抽样:每个信号日,按该候选当天的真实选股数 k,从当天可交易股票池(tradable_mask)里
     无放回随机抽 k 只、等权持有,扣掉同口径成本(取回放里随机对照 ctrl_* 当天的平均成本)与当日全市场收益,
     按周聚合后对周取平均;重复 null_k 次(null_k 由调用者给,写进决策记录,不硬编码)。
     显著性一律用经验 p = (零分布中 ≥ 观测值的个数 + 1) / (零分布大小 + 1),不用正态近似;
     零分布小于 MIN_NULL(20)时不做判定,返回 insufficient_null。
  2. 换手吃光收益:月换手 > 100%,或 成本后年化超额 ≤ 0 而 成本前年化超额 > 0。
     日换手 = 0.5 × Σ|今日目标权重 - 昨日目标权重|(单边),月换手 = 日均 × 21。
  3. 长期空仓:训练段内调仓后有持仓的交易日占比 < 50%。
  4. 单因子单调性检验未通过(S2 的 monotonic 报告)。报告是必须的:不提供就整体 ineligible
     (reason=monotonicity_report_missing),不评估、不淘汰、不记试验。
  淘汰者仍计入试验次数:所有被评估的非对照(策略, 参数组合)都写进 T007 TrialLog(m 不减),
  多重检验的 m = max(清单网格组合总数, promotion.M_FLOOR)。
  淘汰/幸存决定写进冻结文件(默认 handoff/data/replay/eliminated_registry.json):再次调用时先读它,
  已有记录的组合直接返回冻结的 verdict,不重新计算,也不重复记试验;输入变了只标 inputs_changed。
  外部锚点:TrialLog(固定路径 handoff/data/replay/trial_log.jsonl,环境变量 T008_TRIAL_LOG_PATH 可重定向)里
  已有 "T008-S4|train|<清单哈希>|eliminated/survivor" 的记录,而冻结文件里没有对应条目 -> ElimRegistryTamperedError,
  所以换一个新路径的冻结文件不能让淘汰重来。传入的 trial_log 必须就是这个固定路径的账本。
  淘汰规则不得因验证段/封存段结果而改:验证段数据传进训练函数会直接报错。
  样本不足(周数 < MIN_WEEKS 或零分布太小)且没触发 2/3/4 的,记为 undetermined(不冻结)。

二、验证段(validate_survivors):只对幸存者出报告,不改参数、不再淘汰、不重选。
  报告 Holm 校正后的显著性(输入是经验 p,m 同上)、成本翻倍后的超额、月换手、参数邻域
  (相邻网格档位也要盈利,否则是参数孤岛)、与随机对照对比,并给出"是否满足影子池入池条件"。
  经验 p 的下限是 1/(null_k+1),乘以 m 后要小于 0.05 需要 null_k > 20×m;不够时报告里明说"显著性被下限卡住"。

三、封存段(holdout_report):必须先过 T007 LockedTestGuard.open(label)(一次性授权,看过即污染);
  另用配对账本做唯一键 (策略, 封存起点, 股票池哈希):不含终点,挪终点、换 label 都不能再看同一策略一次
  (LockedSegmentReuseError)。账本路径固定(handoff/data/replay/locked_segment_ledger.jsonl,测试用环境变量
  LOCKED_LEDGER_PATH 重定向),不从守卫的 log_path 推出;先写 pending、再 guard.open,成功记 committed、
  失败记 failed。不走守卫的函数只要输入里有封存段日期的行就报错。
  验证段/封存段的 m 从清单重算并与记录核对;seed/null_k 默认取记录里的值,传了不同的值就报错。

局限(报告里也写):回放结果好不代表策略有效;100 个策略 × 约 490 个网格组合是大量试验,
通过者里混有运气;样本只有约 1.5 年;成本费率是占位值;股票池可能有幸存者偏差;
零分布的成本用随机对照的平均成本近似;停牌股收益按 0 计。
本模块不联网;读写的只有调用者指定的路径(冻结文件、封存段用过的配对账本),不碰仓库 data/。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import zlib
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import registry
from .engine import strategy_key
from .. import promotion
from ..validation.splits import LockedTestGuard
from ..validation.trials import TrialLog, params_hash

NULL_K = promotion.NULL_K            # 零分布默认大小 1000(只是默认值,实际值由调用者给并写进记录)
ALPHA = promotion.ALPHA              # 0.05
M_FLOOR = promotion.M_FLOOR          # 多重检验 m 的下限
MIN_NULL = 20                        # 零分布小于它就不做显著性判定
NULL_PCT = 5.0                       # 规则 1:低于零分布第 5 百分位
MAX_MONTHLY_TURNOVER = 1.0           # 规则 2:月换手上限 100%
MIN_HELD_FRAC = 0.5                  # 规则 3:有持仓交易日占比下限
DAYS_PER_MONTH = 21
DAYS_PER_YEAR = 252
MIN_WEEKS = 12                       # 周数不足时规则 1 无法评估
EPS = 1e-12
FREEZE_DEFAULT = Path(__file__).resolve().parents[2] / "handoff" / "data" / "replay" / "eliminated_registry.json"
REPLAY_DATA_DIR = Path(__file__).resolve().parents[2] / "handoff" / "data" / "replay"
TRIAL_LOG_DEFAULT = REPLAY_DATA_DIR / "trial_log.jsonl"              # 淘汰锚点用的固定账本
TRIAL_LOG_ENV = "T008_TRIAL_LOG_PATH"                                # 仅供测试重定向
LEDGER_DEFAULT = REPLAY_DATA_DIR / "locked_segment_ledger.jsonl"     # 封存段配对账本(固定路径)
LEDGER_ENV = "LOCKED_LEDGER_PATH"                                    # 仅供测试重定向
EXCESS_TOL = 1e-9                                                    # excess 与 net - mkt 的容差
FROZEN_VERDICTS = ("eliminated", "survivor")        # 只冻结这两种;undetermined/ineligible 不冻结
RULES = {"r1_null_percentile": NULL_PCT, "r1_p_value": "empirical (count(null>=obs)+1)/(n+1)",
         "r1_null_sampling": "matched k, tradable pool", "min_null": MIN_NULL,
         "r2_max_monthly_turnover": MAX_MONTHLY_TURNOVER,
         "r2_net_annual_excess_le": 0.0, "r3_min_held_fraction": MIN_HELD_FRAC,
         "r4": "monotonic_passed_required", "min_weeks": MIN_WEEKS,
         "turnover": "0.5*sum|w_t-w_{t-1}| 日均 x 21", "flip_signals": False}
_NEED = ("key", "date", "next_date", "net", "gross", "cost", "mkt", "excess")
LIMITS = ["回放结果好不代表策略有效;幸存只表示没被先写死的规则淘汰",
          "多重检验:m 取清单网格组合总数(淘汰者也计入),通过者里混有运气;经验 p 有下限 1/(null_k+1),null_k 不够大时 Holm 不可能显著",
          "样本量:训练约 1.5 年、验证约 9 个月,周数有限,置信度有限",
          "零分布按候选的 k 在可交易池里匹配抽样;成本用随机对照的平均成本近似,停牌股收益按 0 计",
          "交易成本费率为占位默认值,需所有者按实际账户核对;未过滤 ST/上市不足一年,存在幸存者偏差"]


class SelectionError(ValueError):
    pass


class LockedSegmentReuseError(SelectionError):
    """同一个 (策略, 封存起点, 股票池) 已经看过(或正在看)封存段,不允许换终点/换 label 再看。"""


class ElimRegistryTamperedError(SelectionError):
    """TrialLog 里有已冻结判定的记录,而冻结文件里没有:冻结文件被换/被删,淘汰不能重来。"""


class RecordTamperedError(SelectionError):
    """决策记录里的字段与从清单重算的值不一致(例如 m_trials)。"""


class ExcessInconsistentError(SelectionError):
    """回放行的 excess 不等于 net - mkt:数据自相矛盾或被改过。"""


class SeedMismatchError(SelectionError):
    """验证段/封存段传入的 seed/null_k 与训练记录里的不一致。"""


@dataclass
class NullPanel:
    """零分布用的个股面板。
    ret:   DataFrame,index = 信号日 T(字符串,与回放行的 date 一致),columns = 股票代码;
           值 = 该信号日的持有期收益(T+1 开盘 → T+2 开盘,与回放引擎同口径),缺失按 0 计。
    tradable_mask: 同形状的 bool DataFrame,True = 该信号日的次日可买可卖;缺失按 False。
    抽样只在 mask 为 True 的股票里进行。"""
    ret: pd.DataFrame
    tradable_mask: pd.DataFrame

    def __post_init__(self):
        self.ret = self.ret.copy()
        self.ret.index = [str(i) for i in self.ret.index]
        m = self.tradable_mask.copy()
        m.index = [str(i) for i in m.index]
        self.tradable_mask = m.reindex(index=self.ret.index, columns=self.ret.columns).fillna(False).astype(bool)

    @property
    def codes(self) -> list:
        return sorted(str(c) for c in self.ret.columns)

    def fingerprint(self) -> str:
        h = hashlib.sha1()
        h.update(_canon([list(self.ret.index), [str(c) for c in self.ret.columns]]).encode("utf-8"))
        h.update(np.ascontiguousarray(np.nan_to_num(self.ret.to_numpy(dtype="f8"))).tobytes())
        h.update(np.ascontiguousarray(self.tradable_mask.to_numpy(dtype=bool)).tobytes())
        return h.hexdigest()


# ---------------------------------------------------------------- 基础工具

def is_control_key(key: str) -> bool:
    return str(key).startswith(registry.CONTROL_PREFIX)


def _week(d: str) -> str:
    y, w, _ = date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


def segments(cat: dict) -> dict:
    s = cat["segments"]
    return {"train": tuple(s["train"]), "valid": tuple(s["valid"]), "holdout": tuple(s["holdout"])}


def m_trials(cat: dict) -> int:
    """多重检验的 m:清单网格组合总数,与 promotion.M_FLOOR 取大;淘汰者不扣除。"""
    return max(registry.total_trials(cat), M_FLOOR)


def _check_rows(rows) -> None:
    for r in rows:
        for f in _NEED:
            if f not in r:
                raise SelectionError(f"回放行缺字段 {f}: {r.get('key')}|{r.get('date')}")
        for f in ("net", "gross", "cost", "mkt", "excess"):
            v = r[f]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise SelectionError(f"回放行字段 {f} 不是有限数值: {r['key']}|{r['date']}")
        if abs(r["excess"] - (r["net"] - r["mkt"])) > EXCESS_TOL:      # excess 是派生量,必须与 net - mkt 一致
            raise ExcessInconsistentError(f"回放行 excess != net - mkt: {r['key']}|{r['date']}")


def rows_for_segment(rows, segment: str, cat: dict | None = None):
    """取出某段内的行;返回 (保留行, 因禁运丢弃的行数)。
    - 日期不在本段的行直接忽略(同一文件里可含训练+验证);
    - 但只要有行落在封存段日期(≥ holdout 起点)就报错——封存段只能走 holdout_report 的守卫;
    - 持有期结束日 next_date 超过本段末日的行丢弃(禁运,避免用到段后的价格)。"""
    if segment not in ("train", "valid"):
        raise SelectionError("这里只能取 train/valid;封存段走 holdout_report")
    cat = cat or registry.load_validated()[0]
    seg = segments(cat)
    _check_rows(rows)
    ho0 = seg["holdout"][0]
    if any(r["date"] >= ho0 for r in rows):
        raise SelectionError(f"输入含封存段({ho0} 起)的行:封存段只能经 LockedTestGuard 读取,请先截断")
    lo, hi = seg[segment]
    inside = [r for r in rows if lo <= r["date"] <= hi]
    kept = [r for r in inside if r["next_date"] <= hi]
    return kept, len(inside) - len(kept)


def _by_key(rows) -> dict:
    out: dict = {}
    for r in rows:
        out.setdefault(r["key"], []).append(r)
    for v in out.values():
        v.sort(key=lambda r: r["date"])
    return out


def _weekly(series: list, field: str = "excess") -> dict:
    """周 -> 当周日值的平均(先按周平均再对周取平均,同 promotion)。"""
    acc: dict = {}
    for r in series:
        acc.setdefault(_week(r["date"]), []).append(r[field])
    return {w: float(np.mean(v)) for w, v in acc.items()}


def _turnover_monthly(series: list) -> float:
    prev: dict = {}
    tot = 0.0
    for r in series:
        w = r.get("weights") or {}
        keys = set(w) | set(prev)
        tot += 0.5 * sum(abs(float(w.get(k, 0.0)) - float(prev.get(k, 0.0))) for k in keys)
        prev = w
    return tot / len(series) * DAYS_PER_MONTH if series else 0.0


def _held_frac(series: list) -> float:
    if not series:
        return 0.0
    return sum(1 for r in series if r.get("shares")) / len(series)


def _k_by_day(series: list) -> list:
    """候选每天的真实选股数 k = 当日目标权重条数(空仓日为 0)。"""
    return [len(r.get("weights") or {}) for r in series]


def _k_real(ks: list) -> int:
    """决策记录里的 k_real:有持仓日选股数的中位数(取整);全程空仓为 0。"""
    pos = [k for k in ks if k > 0]
    return int(round(float(np.median(pos)))) if pos else 0


def _control_cost(by_key: dict) -> dict:
    """随机对照当天的平均成本(占净值比例),作为零分布组合的成本近似;{日期: 成本}。"""
    ctrls = [v for k, v in by_key.items() if is_control_key(k)]
    if not ctrls:
        raise SelectionError("没有随机对照 ctrl_* 的回放行,无法估计零分布的成本(规则 1/对照对比)")
    acc: dict = {}
    for v in ctrls:
        for r in v:
            acc.setdefault(r["date"], []).append(r["cost"])
    return {d: float(np.mean(x)) for d, x in acc.items()}


def _adj(series: list, cost_mult: float | None) -> list:
    """cost_mult 不为 None 时,把成本换成 cost_mult 倍重算超额(gross - mult×cost - mkt)。"""
    if cost_mult is None:
        return series
    return [{**r, "excess": r["gross"] - cost_mult * r["cost"] - r["mkt"]} for r in series]


def _draw_matched(rng, pool: np.ndarray, k: int, n_draws: int) -> np.ndarray:
    """从可交易池 pool(列位置数组)里无放回随机抽 k 只,重复 n_draws 次;返回 (n_draws, k) 的列位置。
    每次恰好 k 只、全部来自 pool:对随机键做 argpartition 取最小的 k 个,等价于均匀随机 k 子集。"""
    if k <= 0:
        return np.empty((n_draws, 0), dtype=int)
    if k > len(pool):
        raise SelectionError(f"可交易池只有 {len(pool)} 只,小于候选的 k={k},无法匹配抽样")
    keys = rng.random((n_draws, len(pool)))
    pick = np.argpartition(keys, k - 1, axis=1)[:, :k]
    return pool[pick]


def _null_dist(series: list, panel: NullPanel, ctrl_cost: dict, seed, null_k: int = NULL_K,
               cost_mult: float | None = None) -> np.ndarray:
    """匹配抽样的零分布:返回 null_k 个"周平均净超额的均值"。固定种子:相同输入结果一致。
    每个信号日:按候选当天的 k 在当天可交易池里抽 k 只等权,收益 - 当日全市场 mkt - 成本(k>0 时取随机对照当天
    平均成本 × cost_mult,k=0 不交易无成本);再按周聚合、对周取平均。不是从现有对照策略的收益里抽。"""
    dates = [r["date"] for r in series]
    missing = [d for d in dates if d not in panel.ret.index]
    if missing:
        raise SelectionError(f"NullPanel 缺少信号日 {missing[0]} 等 {len(missing)} 天")
    mult = 1.0 if cost_mult is None else float(cost_mult)
    ks = _k_by_day(series)
    ex = np.empty((null_k, len(series)), dtype="f8")
    for j, (r, k) in enumerate(zip(series, ks)):
        d = r["date"]
        rets = np.nan_to_num(panel.ret.loc[d].to_numpy(dtype="f8"))
        pool = np.flatnonzero(panel.tradable_mask.loc[d].to_numpy(dtype=bool))
        rng = np.random.default_rng(zlib.crc32(f"{seed}|null|{d}|{k}".encode()))
        idx = _draw_matched(rng, pool, k, null_k)
        gross = rets[idx].mean(axis=1) if k > 0 else np.zeros(null_k)
        cost = mult * ctrl_cost.get(d, float(np.mean(list(ctrl_cost.values())))) if k > 0 else 0.0
        ex[:, j] = gross - r["mkt"] - cost
    weeks: dict = {}
    for j, d in enumerate(dates):
        weeks.setdefault(_week(d), []).append(j)
    wk = np.column_stack([ex[:, c].mean(axis=1) for c in weeks.values()])
    return wk.mean(axis=1)


def _empirical_p(null: np.ndarray, observed: float) -> float:
    """经验 p(单侧,越大越好):(null ≥ 观测值 的个数 + 1) / (null 大小 + 1)。"""
    return float((int((null >= observed - EPS).sum()) + 1) / (len(null) + 1))


def _score_vs_null(series: list, panel: NullPanel, ctrl_cost: dict, seed, null_k: int = NULL_K,
                   cost_mult: float | None = None) -> dict:
    wk = _weekly(_adj(series, cost_mult))
    ks = _k_by_day(series)
    if null_k < MIN_NULL:
        return {"evaluable": False, "status": "insufficient_null", "weeks": len(wk), "null_k_real": int(null_k),
                "k_real": _k_real(ks)}
    if len(wk) < MIN_WEEKS:
        return {"evaluable": False, "status": "insufficient_weeks", "weeks": len(wk), "k_real": _k_real(ks)}
    score = float(np.mean(list(wk.values())))
    null = _null_dist(series, panel, ctrl_cost, seed, null_k, cost_mult)
    return {"evaluable": True, "status": "ok", "weeks": len(wk), "score": score,
            "null_mean": float(null.mean()), "null_p5": float(np.percentile(null, NULL_PCT)),
            "p_value": _empirical_p(null, score), "k_real": _k_real(ks), "k_min": min(ks), "k_max": max(ks),
            "null_k_real": int(len(null)), "null_dist_fingerprint": _null_fp(null)}


def _null_fp(null: np.ndarray) -> str:
    return hashlib.sha1(np.round(null, 12).astype("f8").tobytes()).hexdigest()


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sha1(obj) -> str:
    return hashlib.sha1(_canon(obj).encode("utf-8")).hexdigest()


def _seal(record: dict) -> dict:
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    return {**body, "record_sha256": hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()}


def verify_record(record: dict) -> None:
    """决策记录没被改过(哈希核对),且清单哈希与当前清单一致。
    自身哈希只管内容有没有变;输入有没有变看各决策里的指纹字段(行情面板/单调性报告/零分布)。"""
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    if record.get("record_sha256") != hashlib.sha256(_canon(body).encode("utf-8")).hexdigest():
        raise SelectionError("决策记录哈希不符:记录被改动过")
    if record.get("catalog_hash") != registry.load_validated()[1]:
        raise SelectionError("决策记录的清单哈希与当前清单不一致(清单已升版?应作为新试验重做)")


def _check_not_in_data(p: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    rp = p.resolve()
    data = (root / "data").resolve()
    if rp == data or data in rp.parents:
        raise SelectionError("输出不能写进仓库 data/ 目录")


def write_json_once(obj: dict, path) -> Path:
    """只允许新建:已存在则报错(不覆盖);不得写进仓库 data/。"""
    p = Path(path)
    _check_not_in_data(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(p, "x", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False) + "\n")
    except FileExistsError:
        raise SelectionError(f"文件已存在,拒绝覆盖(淘汰是一次性的): {p}") from None
    return p


def _write_json_atomic(obj: dict, path: Path) -> None:
    _check_not_in_data(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 冻结文件

def _entry_seal(entry: dict) -> dict:
    body = {k: v for k, v in entry.items() if k != "entry_sha256"}
    return {**body, "entry_sha256": hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()}


def trial_log_path() -> Path:
    """淘汰锚点用的固定 TrialLog 路径(环境变量只供测试重定向)。"""
    return Path(os.environ.get(TRIAL_LOG_ENV) or TRIAL_LOG_DEFAULT)


def _same_path(a, b) -> bool:
    return os.path.realpath(os.fspath(a)) == os.path.realpath(os.fspath(b))


def _anchor_check(entries: dict, chash: str) -> None:
    """外部锚点:固定 TrialLog 里每条 "T008-S4|train|<清单哈希前12位>|eliminated/survivor" 记录,
    冻结文件里都必须有对应条目(同一策略 id + 参数哈希),且冻结 verdict 出现在该组合记过的判定里。
    undetermined 不冻结,不在锚点范围内。"""
    prefix = f"T008-S4|train|{chash[:12]}|"
    logged: dict = {}
    for r in TrialLog(trial_log_path()).load():
        tag = r.get("tag", "")
        if tag.startswith(prefix) and tag[len(prefix):] in FROZEN_VERDICTS:
            logged.setdefault((r["strategy"], r["params_hash"]), set()).add(tag[len(prefix):])
    have = {}
    for e in entries.values():
        dd = e.get("decision") or {}
        have[(dd.get("id"), params_hash(dd.get("params") or {}))] = e["verdict"]
    for k, verdicts in logged.items():
        if k not in have:
            raise ElimRegistryTamperedError(f"TrialLog 里 {k[0]} 已有冻结判定,冻结文件里却没有:不能换文件重来淘汰")
        if have[k] not in verdicts:
            raise ElimRegistryTamperedError(f"冻结文件里 {k[0]} 的 verdict 与 TrialLog 记录不一致")


def load_frozen(path, chash: str) -> dict:
    """读冻结文件 -> {strategy_id: entry}。先用固定 TrialLog 做外部锚点(见 _anchor_check),再读文件。
    一致性断言:条目哈希、verdict 取值、清单哈希、键与 strategy_id 一致。"""
    p = Path(path)
    if not p.exists():
        _anchor_check({}, chash)
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        entries = obj["entries"]
    except (ValueError, KeyError, TypeError):
        raise SelectionError(f"冻结文件无法解析,拒绝继续(不要删了重来): {p}") from None
    for sid, e in entries.items():
        body = {k: v for k, v in e.items() if k != "entry_sha256"}
        if e.get("entry_sha256") != hashlib.sha256(_canon(body).encode("utf-8")).hexdigest():
            raise SelectionError(f"冻结条目哈希不符,被改动过: {sid}")
        if e.get("strategy_id") != sid or e.get("verdict") not in FROZEN_VERDICTS:
            raise SelectionError(f"冻结条目内容不一致: {sid}")
        if e.get("catalog_hash") != chash:
            raise SelectionError(f"冻结条目的清单哈希与当前清单不一致(清单升版应换新冻结文件): {sid}")
    _anchor_check(entries, chash)
    return dict(entries)


def _save_frozen(path, entries: dict) -> None:
    _write_json_atomic({"version": 1, "entries": entries}, Path(path))


# ---------------------------------------------------------------- 训练段淘汰

def _mono_lookup(mono: dict, cat_hash: str):
    if mono.get("catalog_hash") != cat_hash:
        raise SelectionError("单调性报告的清单哈希与当前清单不一致")
    if mono.get("segment") != "train":
        raise SelectionError("单调性报告必须来自训练段(不得用验证段结果淘汰)")
    table = {}
    for res in mono.get("results", []):
        for pr in res.get("params", []):
            table[strategy_key(res["id"], pr["params"])] = bool(pr.get("passed"))
    return table


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def train_eliminate(rows, strategies, trial_log, mono_report: dict | None = None, seed=0,
                    null_k: int = NULL_K, panel: NullPanel | None = None, registry_path=FREEZE_DEFAULT,
                    timestamp: str | None = None) -> dict:
    """训练段一次性淘汰。rows=回放行(可含验证段,会按日期过滤;含封存段则报错);
    strategies=[{"id","params"}](非对照,与 run_replay 的列表一致);trial_log=T007 TrialLog(必须给,淘汰者也记);
    panel=NullPanel(零分布的个股收益与可交易掩码);mono_report=S2 训练段单调性报告(必须给,否则 ineligible);
    registry_path=冻结文件,已有记录的组合直接返回冻结 verdict。
    返回完整决策记录(带自身哈希,可交给 write_json_once 落盘)。"""
    cat, chash = registry.load_validated()
    if trial_log is None:
        raise SelectionError("必须提供 TrialLog:淘汰者也要计入试验次数")
    if panel is None:
        raise SelectionError("必须提供 NullPanel(个股收益 + 可交易掩码),零分布按匹配抽样生成")
    if not _same_path(trial_log.path, trial_log_path()):
        raise SelectionError("trial_log 必须是固定路径的 TrialLog(淘汰锚点读的就是它),不能另给一份")
    ts = timestamp or _now()
    tr, dropped = rows_for_segment(rows, "train", cat)
    by_key = _by_key(tr)
    ctrl_cost = _control_cost(by_key)
    n_ctrl = sum(1 for k in by_key if is_control_key(k))
    m = m_trials(cat)
    panel_fp = panel.fingerprint()
    base = {"catalog_hash": chash, "segment": "train", "segment_range": list(segments(cat)["train"]),
            "rules": RULES, "seed": str(seed), "null_k": int(null_k), "n_controls": n_ctrl, "m_trials": m,
            "grid_trials_total": registry.total_trials(cat), "embargo_dropped_rows": dropped,
            "ohlc_panel_fingerprint": panel_fp, "timestamp": ts, "limits": LIMITS}
    real = [(s["id"], dict(s.get("params") or {})) for s in strategies
            if not s["id"].startswith(registry.CONTROL_PREFIX)]
    if not real:
        raise SelectionError("strategies 里没有非对照策略")
    if mono_report is None:           # 单调性是入场券:没有报告就不评估、不冻结、不记试验
        decisions = [{"key": strategy_key(sid, p), "strategy_id": strategy_key(sid, p), "id": sid, "params": p,
                      "decision": "ineligible", "verdict": "ineligible", "reasons": ["monotonicity_report_missing"],
                      "timestamp": ts, "ohlc_panel_fingerprint": panel_fp, "monotonic_report_fingerprint": None}
                     for sid, p in real]
        return _seal({"kind": "T008_S4_train_selection", **base, "status": "ineligible",
                      "reason": "monotonicity_report_missing", "monotonic_report_fingerprint": None,
                      "decisions": decisions, "survivors": [], "eliminated": [], "undetermined": [],
                      "ineligible": [d["key"] for d in decisions], "frozen": [], "trials_recorded": 0})
    table = _mono_lookup(mono_report, chash)
    mono_fp = _sha1(mono_report)
    # 行指纹覆盖规则用到的全部字段:收益五项(含 excess)+ next_date(禁运)+ weights 内容(规则2换手、k)+ shares(规则3持仓)
    rows_fp = _sha1([[r["key"], r["date"], r["next_date"], r["net"], r["gross"], r["cost"], r["mkt"], r["excess"],
                      r.get("weights") or {}, r.get("shares") or {}]
                     for r in sorted(tr, key=lambda r: (r["key"], r["date"]))])
    frozen = load_frozen(registry_path, chash)
    new_entries = dict(frozen)
    decisions = []
    for sid, params in real:
        key = strategy_key(sid, params)
        inputs_fp = _sha1({"rows": rows_fp, "panel": panel_fp, "mono": mono_fp, "seed": str(seed),
                           "null_k": int(null_k), "key": key, "catalog": chash})
        if key in frozen:            # 冻结读取:不重算,不重复记试验
            e = frozen[key]
            d = {**e["decision"], "frozen": True, "inputs_changed": e["inputs_fingerprint"] != inputs_fp}
            if d.get("decision") != e["verdict"]:
                raise SelectionError(f"冻结条目 verdict 与决策不一致: {key}")
            decisions.append(d)
            continue
        series = by_key.get(key)
        if not series:
            raise SelectionError(f"训练段没有 {key} 的回放行")
        wk = _weekly(series)
        r1 = _score_vs_null(series, panel, ctrl_cost, seed, null_k)
        reasons = []
        if r1["evaluable"]:
            r1["triggered"] = r1["score"] < r1["null_p5"] - EPS
            if r1["triggered"]:
                reasons.append(f"规则1:扣成本后周均超额 {r1['score']:.6f} 低于匹配随机对照第5百分位 {r1['null_p5']:.6f}")
        else:
            r1["triggered"] = False
        net_ann = float(np.mean([r["excess"] for r in series])) * DAYS_PER_YEAR
        gross_ann = float(np.mean([r["gross"] - r["mkt"] for r in series])) * DAYS_PER_YEAR
        turn = _turnover_monthly(series)
        r2a, r2b = turn > MAX_MONTHLY_TURNOVER + EPS, (net_ann <= 0 and gross_ann > 0)
        r2 = {"monthly_turnover": turn, "net_annual_excess": net_ann, "gross_annual_excess": gross_ann,
              "turnover_triggered": r2a, "cost_eats_triggered": r2b, "triggered": r2a or r2b}
        if r2a:
            reasons.append(f"规则2:月换手 {turn:.2f} 超过 {MAX_MONTHLY_TURNOVER:.0%}")
        if r2b:
            reasons.append(f"规则2:成本前年化超额 {gross_ann:.4f} > 0 但成本后 {net_ann:.4f} ≤ 0")
        held = _held_frac(series)
        r3 = {"held_fraction": held, "triggered": held < MIN_HELD_FRAC - EPS}
        if r3["triggered"]:
            reasons.append(f"规则3:有持仓交易日占比 {held:.2%} < {MIN_HELD_FRAC:.0%}")
        ok = table.get(key)           # 缺失(未检验/出错)按未通过处理:单调性是入场券
        r4 = {"applied": True, "passed": ok is True, "triggered": ok is not True}
        if r4["triggered"]:
            reasons.append("规则4:单因子单调性未通过(或报告里没有该组合,按未通过处理)")
        triggered = r1["triggered"] or r2["triggered"] or r3["triggered"] or r4["triggered"]
        if triggered:
            decision = "eliminated"
        elif not r1["evaluable"]:
            decision = "undetermined"
            reasons.append(f"规则1无法评估:{r1['status']}(周数 {r1['weeks']},最少 {MIN_WEEKS};零分布最少 {MIN_NULL})")
        else:
            decision = "survivor"
        score = r1.get("score", float(np.mean(list(wk.values()))))
        tid = trial_log.record(sid, params, "train_weekly_net_excess", score, len(wk),
                               tag=f"T008-S4|train|{chash[:12]}|{decision}")
        k_real = r1.get("k_real", _k_real(_k_by_day(series)))
        d = {"key": key, "strategy_id": key, "id": sid, "params": params, "decision": decision, "verdict": decision,
             "reasons": reasons, "trial_id": tid, "days": len(series), "weeks": len(wk),
             "k_real": k_real, "null_k_real": r1.get("null_k_real", 0),
             "ohlc_panel_fingerprint": panel_fp, "monotonic_report_fingerprint": mono_fp,
             "null_dist_fingerprint": r1.get("null_dist_fingerprint"), "inputs_fingerprint": inputs_fp,
             "rows_fingerprint": rows_fp, "seed": str(seed), "null_k": int(null_k), "timestamp": ts, "frozen": False, "inputs_changed": False,
             "rule1": r1, "rule2": r2, "rule3": r3, "rule4": r4}
        decisions.append(d)
        if decision in FROZEN_VERDICTS:
            new_entries[key] = _entry_seal({"strategy_id": key, "verdict": decision, "timestamp": ts,
                                            "inputs_fingerprint": inputs_fp, "catalog_hash": chash,
                                            "decision": {**d, "frozen": False}})
    if len(new_entries) != len(frozen):
        _save_frozen(registry_path, new_entries)
    pick = lambda v: [x["key"] for x in decisions if x["decision"] == v]   # noqa: E731
    record = {"kind": "T008_S4_train_selection", **base, "status": "ok", "reason": None,
              "monotonic_report_fingerprint": mono_fp, "rows_fingerprint": rows_fp, "decisions": decisions, "survivors": pick("survivor"),
              "eliminated": pick("eliminated"), "undetermined": pick("undetermined"), "ineligible": [],
              "frozen": [x["key"] for x in decisions if x.get("frozen")],
              "trials_recorded": sum(1 for x in decisions if not x.get("frozen"))}
    return _seal(record)


# ---------------------------------------------------------------- 验证段 / 封存段:只看

def _neighbors(cat: dict, sid: str, params: dict) -> list:
    spec = next(s for s in cat["strategies"] if s["id"] == sid)
    out = []
    for name, grid in spec["params"].items():
        if name not in params or params[name] not in grid:
            continue
        i = grid.index(params[name])
        for j in (i - 1, i + 1):
            if 0 <= j < len(grid):
                out.append({**params, name: grid[j]})
    return out


def _holm_adj(ps: dict, m: int) -> dict:
    """Holm 校正(总假设数 m ≥ 实际检验个数):升序第 i 个乘 (m-i+1),取累计最大并截到 1。输入必须是经验 p。"""
    run, out = 0.0, {}
    for i, name in enumerate(sorted(ps, key=ps.get)):
        run = max(run, min(1.0, (m - i) * ps[name]))
        out[name] = run
    return out


def _check_m(record: dict, cat: dict) -> int:
    """m 从清单实际算出,记录里的 m_trials 只用来核对:不等就是记录被改过(哪怕哈希被重新封装)。"""
    m = m_trials(cat)
    if record.get("m_trials") != m:
        raise RecordTamperedError(f"记录里的 m_trials={record.get('m_trials')} 与清单算出的 {m} 不一致")
    return m


def _resolve_call(record: dict, seed, null_k, registry_path=FREEZE_DEFAULT):
    """seed/null_k 以冻结条目里的值为准(淘汰当时用的值,冻结后改不了);记录顶层的值必须与之相同,
    否则说明有人拿同一冻结文件换 seed 重跑了训练,抛 SeedMismatchError。调用者传了不同的值也报错。"""
    frozen = load_frozen(registry_path, record.get("catalog_hash"))
    pairs = {(frozen[d["key"]]["decision"].get("seed"), frozen[d["key"]]["decision"].get("null_k"))
             for d in record["decisions"] if d["decision"] == "survivor"}
    rs, rk = record.get("seed"), record.get("null_k")
    if pairs and pairs != {(rs, rk)}:
        raise SeedMismatchError(f"记录顶层 seed/null_k={(rs, rk)!r} 与幸存者冻结条目里的 {sorted(pairs, key=str)!r} 不一致")
    if seed is not None and str(seed) != rs:
        raise SeedMismatchError(f"seed={seed!r} 与训练记录的 seed={rs!r} 不一致")
    if null_k is not None and int(null_k) != rk:
        raise SeedMismatchError(f"null_k={null_k!r} 与训练记录的 null_k={rk!r} 不一致")
    return rs, int(rk)


def _evaluate(series_rows, record: dict, cat: dict, panel: NullPanel, seed, null_k: int) -> dict:
    by_key = _by_key(series_rows)
    ctrl_cost = _control_cost(by_key)
    n_ctrl = sum(1 for k in by_key if is_control_key(k))
    m = _check_m(record, cat)
    surv = [d for d in record["decisions"] if d["decision"] == "survivor"]
    ps, items = {}, []
    for d in surv:
        s = by_key.get(d["key"], [])
        base = _score_vs_null(s, panel, ctrl_cost, seed, null_k) if s else {"evaluable": False, "status": "no_data", "weeks": 0}
        dbl = (_score_vs_null(s, panel, ctrl_cost, seed, null_k, cost_mult=2.0) if s
               else {"evaluable": False, "status": "no_data"})
        turn = _turnover_monthly(s)
        nb = []
        for p in _neighbors(cat, d["id"], d["params"]):
            ns = by_key.get(strategy_key(d["id"], p), [])
            if not ns:
                nb.append({"params": p, "status": "no_data", "profitable": False})
            else:
                mean_ex = float(np.mean([r["excess"] for r in ns]))
                nb.append({"params": p, "status": "ok", "mean_daily_net_excess": mean_ex, "profitable": mean_ex > 0})
        island = any(not x["profitable"] for x in nb)
        item = {"key": d["key"], "strategy_id": d["key"], "id": d["id"], "params": d["params"], "days": len(s),
                "net_weekly": base, "cost_x2_weekly": dbl, "monthly_turnover": turn,
                "neighbors": nb, "param_island": island, "k_real": base.get("k_real", 0),
                "null_k_real": base.get("null_k_real", 0), "null_dist_fingerprint": base.get("null_dist_fingerprint"),
                "ohlc_panel_fingerprint": panel.fingerprint(),
                "monotonic_report_fingerprint": d.get("monotonic_report_fingerprint")}
        if base["evaluable"]:
            ps[d["key"]] = base["p_value"]              # Holm 的输入只能是经验 p
        items.append(item)
    adj = _holm_adj(ps, m)
    floor_blocks = m * (1.0 / (null_k + 1)) > ALPHA
    for it in items:
        b = it["net_weekly"]
        it["holm_p"] = adj.get(it["key"])
        it["significance_status"] = ("insufficient_null" if b.get("status") == "insufficient_null"
                                     else ("ok" if b["evaluable"] else b.get("status", "not_evaluable")))
        it["holm_significant"] = it["holm_p"] is not None and it["holm_p"] < ALPHA
        it["beats_control"] = bool(b["evaluable"] and b["score"] > b["null_mean"])
        it["cost_x2_positive"] = bool(it["cost_x2_weekly"].get("evaluable") and it["cost_x2_weekly"]["score"] > 0)
        it["shadow_pool_eligible"] = bool(it["beats_control"] and it["monthly_turnover"] <= MAX_MONTHLY_TURNOVER
                                          and not it["param_island"])
    return {"n_survivors": len(surv), "n_controls": n_ctrl, "m_trials": m, "null_k": int(null_k), "items": items,
            "empirical_p_floor_blocks_holm": floor_blocks}


def check_against_frozen(record: dict, registry_path=FREEZE_DEFAULT) -> None:
    """幸存者名单必须与冻结文件一致:记录里每个 survivor 在冻结文件里都有 verdict=survivor 的条目,
    且指纹、试验编号相同;survivors 列表必须恰好等于 decisions 里的幸存者。重算哈希重新封装的伪造记录过不了这一关。"""
    frozen = load_frozen(registry_path, record.get("catalog_hash"))
    surv = [d["key"] for d in record.get("decisions", []) if d.get("decision") == "survivor"]
    if list(record.get("survivors", [])) != surv:
        raise SelectionError("记录的 survivors 与 decisions 里的幸存者不一致")
    for d in record.get("decisions", []):
        if d.get("decision") != "survivor":
            continue
        e = frozen.get(d["key"])
        if e is None or e["verdict"] != "survivor":
            raise SelectionError(f"幸存者 {d['key']} 不在冻结名单里(或冻结 verdict 不是 survivor):名单不能自己挑")
        if e["inputs_fingerprint"] != d.get("inputs_fingerprint") or e["decision"].get("trial_id") != d.get("trial_id"):
            raise SelectionError(f"幸存者 {d['key']} 与冻结条目的指纹/试验编号不符")


def validate_survivors(rows, record: dict, panel: NullPanel | None = None, seed=None, null_k: int | None = None,
                       registry_path=FREEZE_DEFAULT) -> dict:
    """验证段只看:对训练段幸存者出报告。不改参数、不淘汰、不重选;record 不会被修改。
    幸存者名单要与冻结文件(registry_path)核对一致;m 从清单重算;seed/null_k 取记录里的值。"""
    verify_record(record)
    check_against_frozen(record, registry_path)
    cat, chash = registry.load_validated()
    _check_m(record, cat)
    seed, null_k = _resolve_call(record, seed, null_k, registry_path)
    va, dropped = rows_for_segment(rows, "valid", cat)
    if panel is None:
        raise SelectionError("必须提供 NullPanel")
    ev = _evaluate(va, record, cat, panel, seed, null_k)
    return {"kind": "T008_S4_valid_report", "catalog_hash": chash, "segment": "valid",
            "segment_range": list(segments(cat)["valid"]), "train_record_sha256": record["record_sha256"],
            "params_changed": False, "eliminated_by_valid": [], "embargo_dropped_rows": dropped,
            "note": "验证段只产生报告;表现差也不得回头调参,只能在下一个 catalog_version 里作为新试验。"
                    "影子池入池只是进入影子观察,不是策略有效的证据。",
            **ev, "limits": LIMITS}


def panel_codes_hash(codes) -> str:
    return hashlib.sha1("\n".join(sorted({str(c) for c in codes})).encode("utf-8")).hexdigest()


def segment_hash(strategy_id: str, start: str, codes) -> str:
    """封存段唯一键 = sha1(策略 + 清单写死的封存起点 + 去重排序后的股票代码)。
    不含终点,起点也不取守卫区间的起点(调用方可挪):挪终点、挪守卫起点都是同一个键。"""
    return hashlib.sha1("|".join([str(strategy_id), str(start), panel_codes_hash(codes)]).encode("utf-8")).hexdigest()


def ledger_path() -> Path:
    """配对账本固定路径;不接受调用方(含守卫 log_path)指定,环境变量只供测试重定向。"""
    return Path(os.environ.get(LEDGER_ENV) or LEDGER_DEFAULT)


def _read_ledger(text: str) -> dict:
    """账本各行 -> {segment_hash: 状态};同键后来的行覆盖前面的状态(pending -> committed/failed)。"""
    state: dict = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            h, ev = d["segment_hash"], d["event"]
            if ev not in ("pending", "committed", "failed"):
                raise ValueError(ev)
        except (ValueError, KeyError, TypeError):
            raise SelectionError("封存段配对账本无法解析,拒绝放行") from None
        state[h] = ev
    return state


def _ledger_append(entries: list) -> None:
    """加锁追加:同一把锁内先读(含解析)、再判重、再写。pending 行遇到已 pending/committed 的键就拒绝。"""
    p = ledger_path()
    _check_not_in_data(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            text = f.read()
            state = _read_ledger(text)
            clash = [e["strategy_id"] for e in entries
                     if e["event"] == "pending" and state.get(e["segment_hash"]) in ("pending", "committed")]
            if clash:
                raise LockedSegmentReuseError(f"封存段(同策略、同起点、同股票池)已看过或正在看,不论终点与 label: {sorted(clash)}")
            prefix = "\n" if text and not text.endswith("\n") else ""
            f.seek(0, os.SEEK_END)
            f.write(prefix + "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in entries))
            f.flush()
            os.fsync(f.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def holdout_report(rows, dates, guard, label: str, record: dict, panel: NullPanel | None = None, seed=None,
                   null_k: int | None = None, registry_path=FREEZE_DEFAULT) -> dict:
    """封存段:必须经 T007 LockedTestGuard。dates=守卫切分所用的完整交易日序列(升序、字符串);
    guard.split.test 是其中的位置区间(左闭右开)。同一 label 只能开一次;(策略, 封存起点, 股票池) 配对
    也只能看一次(换 label、换终点都抛 LockedSegmentReuseError);看过即污染,之后不得据此改动。
    账本固定路径、不从守卫推出。顺序:全部输入校验 -> 账本写 pending -> guard.open -> committed/failed。
    校验失败不会消耗一次性授权,也不写账本。"""
    if not isinstance(guard, LockedTestGuard):
        raise SelectionError("封存段必须经 LockedTestGuard,收到的不是守卫对象")
    verify_record(record)
    check_against_frozen(record, registry_path)
    cat, chash = registry.load_validated()
    _check_m(record, cat)
    seed, null_k = _resolve_call(record, seed, null_k, registry_path)
    dates = [str(d) for d in dates]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise SelectionError("dates 必须升序且无重复")
    a, b = guard.split.test
    if not (0 <= a < b <= len(dates)):
        raise SelectionError("守卫的封存区间超出 dates 范围")
    ho0 = segments(cat)["holdout"][0]
    if dates[a] < ho0:
        raise SelectionError(f"守卫封存区间起点 {dates[a]} 早于清单封存段起点 {ho0}:不是同一个封存段")
    _check_rows(rows)
    lo, hi = dates[a], dates[b - 1]
    if any(not (lo <= r["date"] <= hi) for r in rows):
        raise SelectionError("输入行必须全部落在守卫授权的封存区间内")
    if panel is None:
        raise SelectionError("必须提供 NullPanel")
    hashes = {d["key"]: segment_hash(d["key"], ho0, panel.codes)
              for d in record["decisions"] if d["decision"] == "survivor"}
    pending = [{"event": "pending", "segment_hash": h, "strategy_id": k, "segment_start": ho0, "guard_start": lo,
                "panel_codes_hash": panel_codes_hash(panel.codes), "label": label, "timestamp": _now()}
               for k, h in sorted(hashes.items())]
    _ledger_append(pending)                              # 先写账本(含判重),后开守卫
    try:
        guard.open(label)                                # 一次性授权:重复 label / 日志异常都会抛 LockedTestError
    except BaseException:
        _ledger_append([{**e, "event": "failed", "timestamp": _now()} for e in pending])
        raise
    _ledger_append([{**e, "event": "committed", "timestamp": _now()} for e in pending])
    inside = [r for r in rows if r["next_date"] <= hi]
    ev = _evaluate(inside, record, cat, panel, seed, null_k)
    return {"kind": "T008_S4_holdout_report", "catalog_hash": chash, "segment": "holdout",
            "segment_range": [lo, hi], "segment_hashes": hashes, "label": label,
            "train_record_sha256": record["record_sha256"],
            "params_changed": False, "contaminated": True, "embargo_dropped_rows": len(rows) - len(inside),
            "note": "封存段已被看过(已污染):此后不得据此改动策略、参数或名单。", **ev, "limits": LIMITS}
