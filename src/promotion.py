"""主策略晋升检验：用「匹配的随机对照」衡量策略的 5 日得分是否真的高于运气，并按多重检验修正后才允许晋升。

为什么不是 1000 个“随机持仓的策略”：
  每天 1000 个独立随机策略 × 5000 只股票 = 500 万行，而且随机策略每天发的信号条数（几百到上千）远多于真实策略（几十条），
  条数多 → 平均得分的波动更小 → 对照的“噪声范围”被低估 → 真实策略显得更显著。所以这里的对照是「匹配抽样」：
  对每个策略、每个已走满 5 日的信号日，保持它当天的 买入条数 / 卖出条数 完全不变，把这些信号随机分配给当天可交易的股票，
  重复 NULL_K=1000 次。得到的 1000 个得分是“如果这个策略的信号只是随机挑股票，会得到什么”的分布（置换检验的零分布）。
  每个 (策略, 信号日) 只存 1000 个得分的一小块二进制，不是 1000 × 5000 行。

怎么检验：
  1. 得分口径和 signal_ledger.scored 完全一样：方向 × (5 日收益 − 当天全市场均值)，买入类再扣来回 16bp；开盘一字涨/跌停的不算。
  2. 按「交易周」聚合：先算每周的平均得分，再对周取平均（窗口统计量）。5 日收益在相邻两天重叠 4/5，按日当独立样本会高估显著性；
     按周聚合后只剩周边界处的少量重叠。对照用同样的口径，所以可比。
  3. 经验 p 值 = (1 + 得分 ≥ 该策略的对照个数) / (NULL_K + 1)。最小值 1/1001≈0.001。
  4. 多重检验：Holm 逐步修正，m = max(25, 账本里“出现过的”策略总数)，被淘汰的策略不会从注册表里删除；随机对照不算假设。
     第一步要求 p ≤ 0.05/25 = 0.002，即最多只有 1 个对照比它高。
  5. 晋升门槛（缺一不可）：≥12 个完整交易周、≥500 条已走满 5 日的信号、连续 4 个周检查点都通过 Holm、
     相对现任（现任已不通过检验，或周配对平均多 0.3% 且 t≥2）、现任满 12 周任期。
局限：对照假设每天独立抽样，没有保留“同一只股票连续多天被持有”带来的相关性，所以零分布仍可能偏窄；
      对照是在全市场里均匀抽股票，真实策略如果偏好某类股票（小盘、高波动），它的得分波动本来就和均匀抽样不同。
纯函数 + sqlite；numpy 只在这里用。
"""
from __future__ import annotations

import math
import zlib
from datetime import date

import numpy as np

NULL_K = 1000
ALPHA = 0.05
M_FLOOR = 25                # 多重检验的假设数下限（7 个正式 + 18 个候选）
H = 5
BOARD_MIN_N = 100           # 榜单排名：至少 100 条信号、跨 10 个信号日
BOARD_MIN_DAYS = 10
PROMO_MIN_WEEKS = 12
PROMO_MIN_N = 500
PROMO_STREAK = 4            # 连续 4 个周检查点通过
MIN_TENURE_DAYS = 84        # 12 周
BEAT_GAIN = 0.003           # 相对现任：每条信号平均多 0.3%（约两倍来回费用）
BEAT_T = 2.0
NOISE_PCT = (2.5, 97.5)


def is_control(name) -> bool:
    return str(name).startswith("ctrl_")


def week_key(day: str) -> str:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def _f(x):
    return None if x is None else float(x)


# ------------------------------------------------------------ 注册表（多重检验的 m）
def register(c) -> int:
    """把账本里出现过的非对照策略登记下来（只增不删，淘汰的策略仍然计入 m）。返回当前 m。"""
    c.execute("""INSERT OR IGNORE INTO strategy_registry(strategy, first_day)
                 SELECT strategy, MIN(day) FROM (SELECT strategy, day FROM signals UNION ALL SELECT strategy, day FROM state)
                 WHERE strategy NOT LIKE 'ctrl\\_%' ESCAPE '\\' GROUP BY strategy""")
    c.commit()
    return n_hypotheses(c)


def n_hypotheses(c) -> int:
    return max(M_FLOOR, c.execute("SELECT COUNT(*) n FROM strategy_registry").fetchone()["n"])


# ------------------------------------------------------------ 匹配的随机对照
def ensure_null(c, k: int = NULL_K) -> int:
    """给每个 (非对照策略, 已走满 5 日的信号日) 生成并保存匹配的零分布。已有的不重算。返回新写入的行数。"""
    from . import signal_ledger as L
    cost = L.ROUND_TRIP_BP / 1e4
    done_days = [r["day"] for r in c.execute("SELECT day FROM done WHERE h=? AND n>0 ORDER BY day", (H,))]
    written = 0
    for day in done_days:
        names = [r["strategy"] for r in c.execute("SELECT DISTINCT strategy FROM signals WHERE day=?", (day,)) if not is_control(r["strategy"])]
        todo = [s for s in names if not c.execute("SELECT 1 FROM null_draws WHERE day=? AND strategy=?", (day, s)).fetchone()]
        if not todo:
            continue
        mv = c.execute("SELECT ret, lim FROM moves WHERE day=? AND h=?", (day, H)).fetchall()
        if not mv:
            continue
        ret = np.array([r["ret"] for r in mv], dtype="f8")
        lim = np.array([r["lim"] for r in mv])
        base = float(ret.mean())                                # 和 L.baseline 一样：当天全市场平均
        ex = ret - base
        exb, exs = ex[lim != 1], ex[lim != -1]                  # 买入不能抽到开盘涨停的，卖出不能抽到开盘跌停的
        for s in todo:
            nb = ns = 0
            own, hits = 0.0, 0
            for r in c.execute("""SELECT s.action, m.ret, m.lim FROM signals s JOIN moves m ON m.day=s.day AND m.code=s.code AND m.h=?
                                  WHERE s.day=? AND s.strategy=?""", (H, day, s)):
                buy = r["action"] in L.BUYS
                if r["lim"] == (1 if buy else -1):
                    continue
                e = r["ret"] - base
                if buy:
                    nb += 1; own += e - cost; hits += 1 if e > 0 else 0
                else:
                    ns += 1; own += -e; hits += 1 if e < 0 else 0
            if nb + ns == 0 or (nb and len(exb) == 0) or (ns and len(exs) == 0):
                continue
            rng = np.random.default_rng(zlib.crc32(f"{s}|{day}".encode()))    # 固定种子：重跑结果一样
            sums = np.zeros(k)
            nh = np.zeros(k, dtype="<i4")
            if nb:
                e = exb[rng.integers(0, len(exb), (k, nb))]       # 有放回抽样：略保守（比不放回的方差大）
                sums += e.sum(1) - nb * cost
                nh += (e > 0).sum(1).astype("<i4")
            if ns:
                e = exs[rng.integers(0, len(exs), (k, ns))]
                sums += -e.sum(1)
                nh += (e < 0).sum(1).astype("<i4")
            c.execute("INSERT OR IGNORE INTO null_draws VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (day, s, nb + ns, nb, ns, own, hits, k, sums.astype("<f8").tobytes(), nh.tobytes()))
            written += 1
    c.commit()
    return written


# ------------------------------------------------------------ 按周聚合
def complete_weeks(c, today: str) -> list[str]:
    """已经结束、且这一周所有信号日都已走满 5 日的交易周。当前这一周不算。"""
    cur = week_key(today)
    done = {r["day"] for r in c.execute("SELECT day FROM done WHERE h=? AND n>0", (H,))}
    by = {}
    for r in c.execute("SELECT day FROM days"):
        by.setdefault(week_key(r["day"]), []).append(r["day"])
    return sorted(w for w, ds in by.items() if w < cur and all(d in done for d in ds))


def load_weekly(c, strategy: str, cw: list[str], lo: str | None = None, hi: str | None = None, k: int = NULL_K) -> dict:
    """某策略在完整周里的 每周信号数 / 平均得分 / 命中，以及 1000 个对照在同样周里的平均得分 / 命中。lo < 信号日 ≤ hi 可限定区间。"""
    ok = set(cw)
    agg = {}
    for r in c.execute("SELECT day,n,own_sum,own_hits,k,sums,hits FROM null_draws WHERE strategy=? ORDER BY day", (strategy,)):
        if (lo and r["day"] <= lo) or (hi and r["day"] > hi) or r["k"] != k:
            continue
        w = week_key(r["day"])
        if w not in ok:
            continue
        s = np.frombuffer(r["sums"], dtype="<f8")
        h = np.frombuffer(r["hits"], dtype="<i4").astype("f8")
        a = agg.get(w)
        if a is None:
            agg[w] = [r["n"], r["own_sum"], r["own_hits"], s.copy(), h.copy(), 1]
        else:
            a[0] += r["n"]; a[1] += r["own_sum"]; a[2] += r["own_hits"]; a[3] += s; a[4] += h; a[5] += 1
    weeks = sorted(agg)
    if not weeks:
        return {"weeks": [], "n": [], "days": [], "own": np.zeros(0), "null": np.zeros((0, k)), "own_hits": np.zeros(0), "null_hits": np.zeros((0, k))}
    return {"weeks": weeks, "n": [agg[w][0] for w in weeks], "days": [agg[w][5] for w in weeks],
            "own": np.array([agg[w][1] / agg[w][0] for w in weeks]),
            "null": np.array([agg[w][3] / agg[w][0] for w in weeks]),
            "own_hits": np.array([agg[w][2] for w in weeks], dtype="f8"),
            "null_hits": np.array([agg[w][4] for w in weeks])}


def _cum(wk):
    """第 j 周为止的窗口统计量（各周平均得分的平均）：(自己 (W,), 对照 (W,K))。"""
    idx = np.arange(1, len(wk["weeks"]) + 1)
    return wk["own"].cumsum() / idx, wk["null"].cumsum(0) / idx[:, None]


def _emp_p(own, null_row) -> float:
    return float((1 + (null_row >= own).sum()) / (len(null_row) + 1))


def holm(p: dict, m: int, alpha: float = ALPHA) -> set:
    """Holm 逐步修正：p 从小到大，第 i 个要求 ≤ alpha/(m−i+1)，一旦失败就停。"""
    passed = set()
    for i, name in enumerate(sorted(p, key=p.get), start=1):
        if p[name] <= alpha / (m - i + 1):
            passed.add(name)
        else:
            break
    return passed


def mde_info(own_c, null_c, m, k=NULL_K):
    """最小可检测效应：在当前周数下，得分比随机平均至少高多少，才能过 Holm 第一步（p ≤ α/m）。
    按“每周独立、效应不变”外推到更多周：噪声范围随 1/√周数 收窄。这是近似，不是保证。"""
    W = len(own_c)
    if W < 1:
        return None
    nl = null_c[-1]
    mu = float(nl.mean())
    cmax = math.floor(ALPHA * (k + 1) / m - 1 + 1e-9)       # (1+c)/(k+1) ≤ α/m 最多允许 c 个对照不低于它
    if cmax < 0:
        return {"possible": False, "mde": None, "mu": mu}
    crit = float(np.sort(nl)[::-1][cmax])
    return {"possible": True, "mde": crit - mu, "crit": crit, "mu": mu}


def summarize(wk: dict, m: int, k: int = NULL_K) -> dict:
    """一段区间（一组完整周）的总体表现，以及和随机对照比较。"""
    from . import signal_ledger as L
    W = len(wk["weeks"])
    n = int(sum(wk["n"]))
    out = {"weeks": W, "n": n, "days": int(sum(wk["days"]))}
    if W == 0 or n == 0:
        return out
    own_c, null_c = _cum(wk)
    stat, nl = float(own_c[-1]), null_c[-1]
    lo, hi = np.percentile(nl, NOISE_PCT)
    ch = wk["null_hits"].sum(0) / n
    hit = float(wk["own_hits"].sum() / n)
    h_lo, h_hi = L._wilson(int(wk["own_hits"].sum()), n)
    out.update(stat=stat, ctrl_mean=float(nl.mean()), ctrl_lo=float(lo), ctrl_hi=float(hi), advantage=stat - float(nl.mean()),
               p=_emp_p(stat, nl), hit=hit, hit_lo=h_lo, hit_hi=h_hi, ctrl_hit=float(ch.mean()), hit_diff=hit - float(ch.mean()),
               noise="显著高于随机" if stat > hi else ("显著低于随机" if stat < lo else "在噪声范围内"))
    return out


# ------------------------------------------------------------ 晋升状态（全部策略）
def promotion_status(c, today: str, k: int = NULL_K) -> dict:
    m = n_hypotheses(c)
    cw = complete_weeks(c, today)
    names = [r["strategy"] for r in c.execute("SELECT strategy FROM strategy_registry ORDER BY strategy")]
    cps = cw[-PROMO_STREAK:]                                  # 最近 4 个周检查点
    data, cums = {}, {}
    for s in names:
        wk = load_weekly(c, s, cw, k=k)
        if wk["weeks"]:
            data[s] = wk
            cums[s] = _cum(wk)
    # 每个检查点：数据够的才有资格出 p 值，其余按 p=1 参与 Holm（占一个假设位）
    passes = {cp: set() for cp in cps}
    for cp in cps:
        p = {}
        for s, wk in data.items():
            if cp not in wk["weeks"]:
                continue
            j = wk["weeks"].index(cp)
            own_c, null_c = cums[s]
            if j + 1 >= PROMO_MIN_WEEKS and sum(wk["n"][:j + 1]) >= PROMO_MIN_N:
                p[s] = _emp_p(float(own_c[j]), null_c[j])
        passes[cp] = holm(p, m)
    out = {}
    for s in names:
        wk = data.get(s)
        info = {"strategy": s, "first_day": c.execute("SELECT first_day FROM strategy_registry WHERE strategy=?", (s,)).fetchone()["first_day"]}
        if not wk:
            info.update(weeks=0, n=0, days=0, status="无数据", streak=0, holm_pass=False, weeks_left=None)
            out[s] = info
            continue
        info.update(summarize(wk, m, k))
        own_c, null_c = cums[s]
        W, n = info["weeks"], info["n"]
        streak = 0
        for cp in reversed(cps):
            if s in passes[cp]:
                streak += 1
            else:
                break
        info["streak"] = streak
        info["holm_pass"] = bool(cps) and s in passes[cps[-1]]
        info["data_ok"] = W >= PROMO_MIN_WEEKS and n >= PROMO_MIN_N
        # --- 最小可检测效应 + 还差多少周
        md = mde_info(own_c, null_c, m, k)
        info["mde"] = None
        if md is not None:
            info["mde"] = md["mde"]
            info["mde_possible"] = md["possible"]
            if md["possible"] and md["mde"] is not None:
                info["mde_12w"] = md["mde"] * math.sqrt(W / max(W, PROMO_MIN_WEEKS))
                info["mde_26w"] = md["mde"] * math.sqrt(W / max(W, 26))
                adv = info["advantage"]
                w_stat = max(1.0, W * (md["mde"] / adv) ** 2) if adv > 0 else None
                w_n = W + max(0.0, (PROMO_MIN_N - n) / (n / W)) if n else None
                info["weeks_needed_effect"] = None if w_stat is None else math.ceil(w_stat)
                info["weeks_needed_n"] = None if w_n is None else math.ceil(w_n)
                if info["holm_pass"] and info["data_ok"]:
                    left = max(0, PROMO_STREAK - streak)
                elif w_stat is None:
                    left = None
                else:
                    left = max(0, max(PROMO_MIN_WEEKS, math.ceil(w_stat), math.ceil(w_n)) + PROMO_STREAK - 1 - W)
                info["weeks_left"] = left
        info.setdefault("weeks_left", None)
        if not info["data_ok"]:
            info["status"] = f"数据不足（{W}/{PROMO_MIN_WEEKS} 周，{n}/{PROMO_MIN_N} 条）"
        elif streak >= PROMO_STREAK:
            info["status"] = "通过，可晋升"
        elif info["holm_pass"]:
            info["status"] = f"通过 {streak}/{PROMO_STREAK} 周"
        else:
            info["status"] = "未通过"
        out[s] = info
    any_pass = [s for s, v in out.items() if v.get("holm_pass")]
    return {"today": today, "m": m, "alpha": ALPHA, "k": k, "complete_weeks": len(cw), "checkpoints": cps,
            "any_passed": bool(any_pass), "passed": any_pass,
            "eligible": [s for s, v in out.items() if v.get("streak", 0) >= PROMO_STREAK and v.get("data_ok")],
            "message": ("通过晋升检验：" + "、".join(any_pass)) if any_pass else "尚无策略通过晋升检验",
            "gate": {"min_weeks": PROMO_MIN_WEEKS, "min_n": PROMO_MIN_N, "streak": PROMO_STREAK, "tenure_days": MIN_TENURE_DAYS,
                     "beat_gain": BEAT_GAIN, "beat_t": BEAT_T, "board_min_n": BOARD_MIN_N, "board_min_days": BOARD_MIN_DAYS},
            "strategies": out}


def paired_weekly(c, a: str, b: str, today: str, k: int = NULL_K):
    """a 相对 b 的逐周配对差：(平均差, t, 周数)。只用两者都有数据的完整周。"""
    cw = complete_weeks(c, today)
    wa, wb = load_weekly(c, a, cw, k=k), load_weekly(c, b, cw, k=k)
    da = dict(zip(wa["weeks"], wa["own"]))
    db = dict(zip(wb["weeks"], wb["own"]))
    diffs = [da[w] - db[w] for w in da if w in db]
    if len(diffs) < 2:
        return (sum(diffs) / len(diffs) if diffs else None), None, len(diffs)
    mean = sum(diffs) / len(diffs)
    sd = float(np.std(diffs, ddof=1))
    t = mean / (sd / len(diffs) ** 0.5) if sd > 0 else (float("inf") if mean > 0 else None)
    return mean, t, len(diffs)


def segment(c, strategy: str, today: str, lo: str | None, hi: str | None = None, k: int = NULL_K) -> dict:
    """lo < 信号日 ≤ hi 区间内的表现（晋升前 / 晋升后）。条数、命中率按日；和对照比较按完整周。"""
    from . import signal_ledger as L
    m = n_hypotheses(c)
    rows = [r for r in c.execute("SELECT day,n,own_sum,own_hits FROM null_draws WHERE strategy=? ORDER BY day", (strategy,))
            if not (lo and r["day"] <= lo) and not (hi and r["day"] > hi)]
    n = sum(r["n"] for r in rows)
    out = {"n": n, "days": len(rows), "from": lo, "to": hi}
    if n:
        hits = sum(r["own_hits"] for r in rows)
        out.update(hit=hits / n, score=sum(r["own_sum"] for r in rows) / n)
        out["hit_lo"], out["hit_hi"] = L._wilson(hits, n)
    out["vs_control"] = summarize(load_weekly(c, strategy, complete_weeks(c, today), lo, hi, k), m, k)
    out["enough"] = n >= BOARD_MIN_N and len(rows) >= BOARD_MIN_DAYS
    return out


def jsonable(x):
    """把 numpy 数值转成普通 float/int，保证能写 JSON。"""
    if isinstance(x, dict):
        return {str(a): jsonable(b) for a, b in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [jsonable(a) for a in x]
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, (np.floating, float)):
        return None if (x != x or x in (float("inf"), float("-inf"))) else float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x
