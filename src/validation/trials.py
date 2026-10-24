"""试验次数记录与多重检验折扣(T007 / S3)。标准库实现,不联网;只读写调用者指定的账本路径。

为什么需要:同一份数据上试过 N 组参数后,"最好的那组"的夏普天然偏高(选择偏差)。
必须记录总共试了多少次,并在评价时按试验次数折扣显著性。

一、TrialLog:追加式 JSONL 账本(每行一次试验:id、时间、策略名、参数哈希、指标)。
  - 账本路径由调用者指定,文件名不得是项目试验账本的保留名(那是项目另有用途的文件)。建议 trial_log.jsonl。
  - 只追加,永不改写、不覆盖;同一(策略, 参数)重复登记仍计数(防止"偷偷重跑"),并标记 duplicate。
  - 显式给定的 id 若已存在,抛 TrialError(不静默覆盖、不重复写入)。
  - 损坏行(非 JSON / 缺字段)读取时跳过,并在 bad_lines 里报告,而不是整体失败。

二、统计工具(假设写在各函数文档里):
  1. 多重检验校正:bonferroni / sidak / holm / bh_fdr。
  2. 夏普比率的标准误(Lo 2002 / Mertens 2002 的非正态修正)、PSR、Deflated Sharpe Ratio
     (Bailey & López de Prado 2014 的近似实现)。
  3. 循环块 bootstrap 置信区间(固定种子可复现)。
  4. min_trades_hint:经验性的最小样本量提示,不是统计保证。

所有夏普均指"每个周期(如每日)的夏普 = 均值 / 样本标准差",未年化;年化只在 sharpe_ci 里作为可选换算。
样本量 n < 30 时返回 NaN 并发出警告,而不是给出假精确的数字。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import statistics
import warnings
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional, Sequence

try:  # 无 fcntl 的平台(Windows)退化为无锁
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

__all__ = [
    "TrialError", "params_hash", "TrialLog",
    "bonferroni", "sidak", "holm", "bh_adjusted", "bh_fdr", "bonferroni_threshold",
    "sample_moments", "sharpe", "sharpe_se", "prob_sharpe_gt",
    "expected_max_sharpe", "deflated_sharpe",
    "block_bootstrap", "bootstrap_stat", "sharpe_ci", "mean_ci", "min_trades_hint",
    "MIN_N",
]

MIN_N = 30
_EULER_GAMMA = 0.5772156649015329
_ND = statistics.NormalDist()
_RESERVED_NAME = "trials" + ".jsonl"  # 运行时拼接:项目试验账本的保留名


class TrialError(ValueError):
    """试验账本或统计输入非法。"""


# ---------------------------------------------------------------- 账本

def params_hash(params) -> str:
    """参数字典的稳定哈希:键排序、紧凑分隔、禁止 NaN,sha256 十六进制。
    相同参数(与字典键顺序无关)得到相同哈希;不可 JSON 序列化的参数抛 TrialError。"""
    try:
        s = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise TrialError(f"参数无法稳定序列化: {e}") from e
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class _Locked:
    """账本文件上的独占锁(flock),保证"读 → 取号 → 追加"原子。"""

    def __init__(self, path):
        self.path = path
        self.f = None

    def __enter__(self):
        self.f = open(self.path, "a+", encoding="utf-8")
        if fcntl is not None:
            fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        return self.f

    def __exit__(self, *exc):
        try:
            if fcntl is not None:
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        finally:
            self.f.close()


def _parse(lines):
    """解析账本各行,返回 (合法记录列表, 损坏行说明列表)。"""
    recs, bad = [], []
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            ok = (isinstance(d, dict) and isinstance(d["id"], str) and d["id"]
                  and isinstance(d["strategy"], str) and isinstance(d["params_hash"], str)
                  and isinstance(d["metric"], str)
                  and isinstance(d["time"], str) and isinstance(d["tag"], str)
                  and isinstance(d["n_obs"], int) and not isinstance(d["n_obs"], bool) and d["n_obs"] >= 0
                  and isinstance(d["value"], (int, float)) and not isinstance(d["value"], bool)
                  and math.isfinite(d["value"]))
        except (ValueError, KeyError):
            ok = False
        if ok:
            recs.append(d)
        else:
            bad.append(f"第 {i} 行无法解析或缺字段")
    return recs, bad


class TrialLog:
    """追加式试验账本。实例化不创建文件,首次 record 才创建。"""

    def __init__(self, path):
        path = os.fspath(path)
        if _RESERVED_NAME in os.path.basename(path).lower():
            raise TrialError("账本文件名不能使用项目试验账本的保留名,请换一个(建议 trial_log.jsonl)")
        self.path = path
        self.bad_lines: list = []

    # -- 读
    def read(self):
        """返回 (合法记录, 损坏行说明);文件不存在视为空。"""
        if not os.path.exists(self.path):
            return [], []
        with open(self.path, "r", encoding="utf-8") as f:
            return _parse(f.readlines())

    def load(self) -> list:
        """合法记录列表(按写入顺序);损坏行记入 self.bad_lines。"""
        recs, self.bad_lines = self.read()
        return recs

    def count(self, strategy: Optional[str] = None) -> int:
        """试验次数(含重复登记);给 strategy 则只数该策略。"""
        recs = self.load()
        return len(recs) if strategy is None else sum(1 for r in recs if r["strategy"] == strategy)

    # -- 写
    def record(self, strategy: str, params, metric_name: str, value: float, n_obs: int,
               tag: str = "", trial_id: Optional[str] = None, now: Optional[str] = None) -> str:
        """追加一次试验,返回 id。

        id 默认 "t000001" 形式(已有记录数 + 1,在文件锁内取号;若已被占用则顺延);
        显式 trial_id 已存在则抛 TrialError。同一(策略, 参数哈希)再次登记会写入并标记 duplicate=True。
        now 仅供测试注入固定时间(ISO 字符串)。"""
        if not isinstance(strategy, str) or not strategy:
            raise TrialError("strategy 必须是非空字符串")
        if not isinstance(metric_name, str) or not metric_name:
            raise TrialError("metric_name 必须是非空字符串")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise TrialError("value 必须是有限数值")
        if isinstance(n_obs, bool) or not isinstance(n_obs, int) or n_obs < 0:
            raise TrialError("n_obs 必须是非负整数")
        if trial_id is not None and (not isinstance(trial_id, str) or not trial_id):
            raise TrialError("trial_id 必须是非空字符串")
        ph = params_hash(params)
        ts = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _Locked(self.path) as f:
            f.seek(0)
            content = f.read()
            recs, _ = _parse(content.splitlines())
            ids = {r["id"] for r in recs}
            if trial_id is not None:
                if trial_id in ids:
                    raise TrialError(f"试验 id 已存在: {trial_id}")
                tid = trial_id
            else:
                k = len(recs) + 1
                while f"t{k:06d}" in ids:
                    k += 1
                tid = f"t{k:06d}"
            dup = any(r["strategy"] == strategy and r["params_hash"] == ph for r in recs)
            row = {"id": tid, "time": ts, "strategy": strategy, "params_hash": ph,
                   "metric": metric_name, "value": float(value), "n_obs": n_obs,
                   "tag": str(tag), "duplicate": dup}
            f.seek(0, os.SEEK_END)
            # 末行若是中断写入留下的残缺行(无换行结尾),先补换行,让新记录独立成行、不被吞掉
            prefix = "\n" if content and not content.endswith("\n") else ""
            f.write(prefix + json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return tid

    def summary(self) -> dict:
        """汇总:总次数、不同(策略,参数)数、重复次数、分策略计数、各指标的描述统计、损坏行。"""
        recs = self.load()
        by_strategy: dict = {}
        metrics: dict = {}
        for r in recs:
            by_strategy[r["strategy"]] = by_strategy.get(r["strategy"], 0) + 1
            metrics.setdefault(r["metric"], []).append(float(r["value"]))
        mstats = {}
        for name, vs in metrics.items():
            mstats[name] = {"n": len(vs), "min": min(vs), "max": max(vs), "mean": statistics.fmean(vs),
                            "var": statistics.variance(vs) if len(vs) > 1 else float("nan")}
        return {"total": len(recs),
                "unique_params": len({(r["strategy"], r["params_hash"]) for r in recs}),
                "duplicates": sum(1 for r in recs if r.get("duplicate")),
                "by_strategy": by_strategy, "metrics": mstats,
                "first_time": recs[0]["time"] if recs else None,
                "last_time": recs[-1]["time"] if recs else None,
                "bad_lines": list(self.bad_lines)}


# ---------------------------------------------------------------- 多重检验校正

def _check_p(p, name="p"):
    if not isinstance(p, (int, float)) or isinstance(p, bool) or not (0.0 <= p <= 1.0):
        raise TrialError(f"{name} 必须在 [0,1] 内")


def _check_m(m):
    if isinstance(m, bool) or not isinstance(m, int) or m < 1:
        raise TrialError("检验次数 m 必须是 >= 1 的整数")


def bonferroni(p: float, m: int) -> float:
    """Bonferroni 校正:p_adj = min(1, m·p)。假设:对任意相关结构都保守地控制族错误率(FWER)。"""
    _check_p(p)
    _check_m(m)
    return min(1.0, p * m)


def bonferroni_threshold(alpha: float, m: int) -> float:
    """m 次检验下单个检验的显著性门槛 alpha/m;m 越大门槛越严。"""
    _check_p(alpha, "alpha")
    _check_m(m)
    return alpha / m


def sidak(p: float, m: int) -> float:
    """Šidák 校正:p_adj = 1 − (1 − p)^m。假设 m 个检验相互独立;比 Bonferroni 略宽松。"""
    _check_p(p)
    _check_m(m)
    return 1.0 - (1.0 - p) ** m


def holm(ps: Sequence[float]) -> list:
    """Holm 逐步校正(控制 FWER,一致地不比 Bonferroni 差)。
    升序 p(1)≤…≤p(m),adj(i) = max_{j≤i} min(1, (m−j+1)·p(j));结果按输入顺序返回。"""
    ps = list(ps)
    for p in ps:
        _check_p(p)
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    out = [0.0] * m
    run = 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * ps[i]))
        out[i] = run
    return out


def bh_adjusted(ps: Sequence[float]) -> list:
    """Benjamini–Hochberg 调整 p(q 值):adj(i) = min_{j≥i} min(1, m·p(j)/j)。假设独立或正相关。"""
    ps = list(ps)
    for p in ps:
        _check_p(p)
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    out = [0.0] * m
    run = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        run = min(run, min(1.0, ps[i] * m / (rank + 1)))
        out[i] = run
    return out


def bh_fdr(ps: Sequence[float], q: float = 0.05) -> list:
    """BH 程序:控制错误发现率 FDR ≤ q,返回与输入同序的布尔列表(True = 拒绝原假设)。"""
    _check_p(q, "q")
    return [a <= q for a in bh_adjusted(ps)]


# ---------------------------------------------------------------- 夏普相关

def _nan_warn(msg):
    warnings.warn(msg, UserWarning, stacklevel=3)
    return float("nan")


def sample_moments(r: Sequence[float]) -> dict:
    """样本估计:均值、样本标准差(n−1)、偏度、峰度(非超额,正态=3)。
    偏度 = mean((x−μ)³)/σ_p³,峰度 = mean((x−μ)⁴)/σ_p⁴,σ_p 为总体标准差(除以 n)。"""
    x = [float(v) for v in r]
    n = len(x)
    if n < 2 or any(not math.isfinite(v) for v in x):
        raise TrialError("需要至少 2 个有限数值")
    mu = math.fsum(x) / n
    m2 = math.fsum((v - mu) ** 2 for v in x) / n
    if m2 == 0:
        return {"n": n, "mean": mu, "std": 0.0, "skew": float("nan"), "kurt": float("nan")}
    m3 = math.fsum((v - mu) ** 3 for v in x) / n
    m4 = math.fsum((v - mu) ** 4 for v in x) / n
    return {"n": n, "mean": mu, "std": math.sqrt(m2 * n / (n - 1)),
            "skew": m3 / m2 ** 1.5, "kurt": m4 / m2 ** 2}


def sharpe(r: Sequence[float]) -> float:
    """单周期夏普 = 均值 / 样本标准差;标准差为 0 时返回 NaN。"""
    m = sample_moments(r)
    return m["mean"] / m["std"] if m["std"] > 0 else float("nan")


def sharpe_se(sr: float, n: int, skew: float = 0.0, kurt: float = 3.0, min_n: int = MIN_N) -> float:
    """夏普估计量的标准误(Mertens 2002 / Lo 2002 的非正态修正):
        SE = sqrt( (1 − skew·SR + (kurt−1)/4 · SR²) / (n − 1) )
    kurt 为非超额峰度(正态=3)。正态且 skew=0 时退化为 sqrt((1+SR²/2)/(n−1))。
    假设:收益近似独立同分布(有自相关时低估);n < min_n(或 n < 2,分母 n−1 无意义)返回 NaN 并警告,不抛异常。"""
    if n < max(2, min_n):
        return _nan_warn(f"样本量 {n} 过小(下限 {max(2, min_n)}),夏普标准误不可靠,返回 NaN")
    v = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if v <= 0:
        return _nan_warn("偏度/峰度与夏普组合使方差估计为非正,返回 NaN")
    return math.sqrt(v / (n - 1))


def prob_sharpe_gt(sr: float, sr_bench: float, n: int, skew: float = 0.0, kurt: float = 3.0,
                   min_n: int = MIN_N) -> float:
    """PSR = Φ((SR − SR*) / SE):真实夏普超过基准 SR* 的概率估计(Bailey & López de Prado 2012)。"""
    se = sharpe_se(sr, n, skew, kurt, min_n)
    if math.isnan(se):
        return float("nan")
    return _ND.cdf((sr - sr_bench) / se)


def expected_max_sharpe(n_trials: int, var_sr_trials: float) -> float:
    """N 次独立试验下,"零技能"最大夏普的期望(极值近似,Bailey & López de Prado 2014):
        E[max] ≈ sqrt(V) · ( (1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ),γ≈0.5772 为欧拉常数
    V 是各试验夏普估计值的方差(与 SR 同周期)。N=1 时为 0;N 越大越大。
    假设:各试验夏普近似独立同分布于均值 0 的正态——相关的试验(参数相近)会让有效 N 小于记录数,因此偏保守。"""
    _check_m(n_trials)
    if not (math.isfinite(var_sr_trials) and var_sr_trials >= 0):
        raise TrialError("var_sr_trials 必须是非负有限数")
    if n_trials == 1:
        return 0.0
    z = (1 - _EULER_GAMMA) * _ND.inv_cdf(1 - 1.0 / n_trials) \
        + _EULER_GAMMA * _ND.inv_cdf(1 - 1.0 / (n_trials * math.e))
    return math.sqrt(var_sr_trials) * z


def deflated_sharpe(sr: float, n: int, skew: float, kurt: float, n_trials: int,
                    var_sr_trials: float, min_n: int = MIN_N) -> float:
    """Deflated Sharpe Ratio = PSR(SR* = E[max SR | N 次试验]):
    把"挑了最好的一个"的选择偏差折进基准。返回 [0,1] 概率(越接近 1 越不像运气)。
    skew/kurt 由被选中策略的收益序列用 sample_moments 估计。n_trials 增大 → 基准变高 → DSR 单调下降。
    这是近似:不处理试验间相关,也不保证统计性质,只是一个随试验次数变严的折扣。"""
    bench = expected_max_sharpe(n_trials, var_sr_trials)
    return prob_sharpe_gt(sr, bench, n, skew, kurt, min_n)


# ---------------------------------------------------------------- bootstrap

def block_bootstrap(r: Sequence[float], block: int, n_boot: int, seed: int):
    """循环块 bootstrap:逐次产出与 r 等长的重采样序列。
    每次随机取起点(对 n 取模,循环),连续取 block 个,拼到长度 n 再截断。块保留短程自相关。
    固定 seed 用独立的 random.Random(seed),结果可复现,且不污染全局随机状态。"""
    x = [float(v) for v in r]
    n = len(x)
    if n < 1:
        raise TrialError("序列为空")
    if isinstance(block, bool) or not isinstance(block, int) or not (1 <= block <= n):
        raise TrialError("block 必须是 1..n 的整数")
    if isinstance(n_boot, bool) or not isinstance(n_boot, int) or n_boot < 1:
        raise TrialError("n_boot 必须是正整数")
    rng = random.Random(seed)
    for _ in range(n_boot):
        out = []
        while len(out) < n:
            s = int(rng.random() * n)
            out.extend(x[(s + k) % n] for k in range(block))
        yield out[:n]


def _quantile(sorted_vals, q):
    """线性插值分位数(与 numpy 默认一致)。"""
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def bootstrap_stat(r: Sequence[float], stat: Callable, block: int = 1, n_boot: int = 1000,
                   seed: int = 0) -> list:
    """对每个重采样序列计算 stat,返回统计量列表(NaN 会被丢弃)。"""
    vals = [stat(b) for b in block_bootstrap(r, block, n_boot, seed)]
    return [v for v in vals if not math.isnan(v)]


def _ci(r, stat, block, n_boot, seed, alpha, scale, label):
    if not (0.0 < alpha < 1.0):
        raise TrialError("alpha 必须在 (0,1) 内")
    x = list(r)
    n = len(x)
    nan = float("nan")
    if n < MIN_N:
        msg = f"样本量 {n} < {MIN_N},{label}置信区间不可靠,返回 NaN"
        warnings.warn(msg, UserWarning, stacklevel=3)
        return {"point": nan, "lower": nan, "upper": nan, "n": n, "n_boot": 0, "warning": msg}
    point = stat(x)
    vals = sorted(bootstrap_stat(x, stat, block, n_boot, seed))
    if len(vals) < 2:
        msg = f"{label} bootstrap 有效重采样不足,返回 NaN"
        warnings.warn(msg, UserWarning, stacklevel=3)
        return {"point": point * scale, "lower": nan, "upper": nan, "n": n, "n_boot": len(vals), "warning": msg}
    return {"point": point * scale, "lower": _quantile(vals, alpha / 2) * scale,
            "upper": _quantile(vals, 1 - alpha / 2) * scale, "n": n, "n_boot": len(vals), "warning": ""}


def sharpe_ci(r: Sequence[float], block: int = 1, n_boot: int = 1000, seed: int = 0,
              alpha: float = 0.05, periods_per_year: float = 1.0) -> dict:
    """夏普的 bootstrap 百分位置信区间。periods_per_year 给定(如 252)则按 √该值年化。
    n < 30 返回 NaN 并带 warning 字段。区间只反映抽样波动,不含选择偏差——要另做试验折扣。"""
    if periods_per_year <= 0:
        raise TrialError("periods_per_year 必须为正")
    return _ci(r, sharpe, block, n_boot, seed, alpha, math.sqrt(periods_per_year), "夏普")


def mean_ci(r: Sequence[float], block: int = 1, n_boot: int = 1000, seed: int = 0,
            alpha: float = 0.05) -> dict:
    """平均收益的 bootstrap 百分位置信区间。"""
    return _ci(r, lambda b: math.fsum(b) / len(b), block, n_boot, seed, alpha, 1.0, "平均收益")


# ---------------------------------------------------------------- 最小交易笔数提示

def min_trades_hint(mean_ret: float, std_ret: float, z: float = 1.96, floor: int = MIN_N) -> dict:
    """经验提示:要让"逐笔平均收益"的 t 统计量达到 z,大致需要 n ≈ (z·σ/μ)² 笔,
    再与经验下限 floor(默认 30)取较大者。
    只是按 iid 假设的粗略估算;真实交易有相关性、厚尾、参数是被挑出来的,所需笔数通常更多。
    不是统计保证,达到该笔数也不代表策略有效。μ ≤ 0 时无意义,返回 n=None。"""
    if not (math.isfinite(mean_ret) and math.isfinite(std_ret)) or std_ret <= 0 or z <= 0:
        raise TrialError("需要有限的 mean_ret、正的 std_ret 和 z")
    note = "经验提示,不是统计保证;iid 假设下的粗略估算,未计入试验次数折扣"
    if mean_ret <= 0:
        return {"n": None, "formula_n": None, "floor": floor, "note": "平均收益非正,无从估算; " + note}
    fn = math.ceil((z * std_ret / mean_ret) ** 2)
    return {"n": max(fn, floor), "formula_n": fn, "floor": floor, "note": note}
