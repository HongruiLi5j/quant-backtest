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
D100 起 p 值改用「区块抽样」对照（block_weekly）：随机持仓抽到一只股票后连续持有 L 个信号日，L 从该策略自己
      “同一只股票、同一方向连续出信号的天数”里有放回地抽。原因见 docs/research_findings.md C02：每天独立抽样的对照
      在策略连续持仓时只有真实波动的 50%~61%，没本事的策略在 α=5% 下有 16%~19% 被判显著；区块对照把它拉回约 4%。
      算法与 scripts/promotion_block_null.py 的 block_null 逐位相同（tests/test_promotion_block_d100.py 核对）。
      每天独立抽样的 null_draws 照旧写入，只作对照显示（p_iid）。
局限：对照是在全市场里均匀抽股票，真实策略如果偏好某类股票（小盘、高波动），它的得分波动本来就和均匀抽样不同；
      同一天信号之间的相关（行业、市值偏好）两种对照都没模拟；区块对照每次从头重算（不存表），账本越长越慢
      （沙盒账本 12 个信号日约 1.5 秒）；K=1000 时 Holm 第一步（p ≤ 0.002）最多只容 1 个对照不低于它，p 的分辨率很粗
      （旧补丁 08 用 K=5000，但按天存的零分布一年约 375MB，这里没采用）。
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
NULL_METHOD = "block"       # D100：晋升 p 值用的对照；"iid" = 旧的每天独立抽样（null_draws）


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


# ------------------------------------------------------------ 区块抽样对照（D100）
def run_lengths(daily) -> np.ndarray:
    """每段「同一代码连续出现的天数」（按给定的日序列计连续），与 scripts/promotion_block_null.run_lengths 相同。"""
    out, cur = [], {}
    for s in daily:
        s = set(np.asarray(s).tolist())
        out += [v for cd, v in cur.items() if cd not in s]
        cur = {cd: cur.get(cd, 0) + 1 for cd in s}
    out += list(cur.values())
    return np.array(out, dtype=int)


def block_side(ex, ok_all, sig, lengths, k, rng):
    """一个方向的区块抽样，与 scripts/promotion_block_null._block_side 同一算法、同样的随机数消耗顺序。
    ex/ok_all：每个信号日的 (U,) 超额收益 / 能否被抽到；sig：每天的信号（代码下标）。
    返回 ((T,k) 抽到的超额之和, (T,k) 其中 ex>0 的个数, (T,k) 其中 ex<0 的个数)；买入命中用前者，卖出命中用后者。"""
    T = len(sig)
    out = np.zeros((T, k))
    pos = np.zeros((T, k), dtype="<i4")
    neg = np.zeros((T, k), dtype="<i4")
    codes = np.zeros((k, 0), dtype=int)
    rem = np.zeros((k, 0), dtype=int)                                  # 含当天在内还剩几天
    for t in range(T):
        n = len(sig[t])
        if n == 0:                                                     # 当天这个方向没有信号：所有块中断
            codes = rem = np.zeros((k, 0), dtype=int)
            continue
        ok = ok_all[t]
        pool = np.flatnonzero(ok)
        new_c = pool[rng.integers(0, len(pool), (k, n))]               # 先全部按新开块准备好
        new_r = rng.choice(lengths, size=(k, n))
        if codes.shape[1]:
            alive = (rem > 1) & ok[codes]                               # 寿命没到、且今天还能被抽到
            keys = np.where(alive, rng.random(alive.shape), 2.0)       # 活块排前面、彼此随机；多于 n 个时随机留 n 个
            order = np.argsort(keys, axis=1, kind="stable")[:, :n]
            m = order.shape[1]
            ka = np.take_along_axis(alive, order, 1)
            new_c[:, :m] = np.where(ka, np.take_along_axis(codes, order, 1), new_c[:, :m])
            new_r[:, :m] = np.where(ka, np.take_along_axis(rem, order, 1) - 1, new_r[:, :m])
        codes, rem = new_c, new_r
        e = ex[t][codes]
        out[t] = e.sum(1)
        pos[t] = (e > 0).sum(1)
        neg[t] = (e < 0).sum(1)
    return out, pos, neg


SECTOR_MIN_POOL = 10                    # D109 方案 B：同行业可抽的股票少于这个数就退回全市场池
SECTOR_FALLBACKS = [0]                 # 计数：退回全市场池的次数（报告里要写）


def block_side_sector(ex, ok_all, sig, lengths, k, rng, industry=None, min_pool: int = SECTOR_MIN_POOL):
    """D109 方案 B：同 block_side，但新开块的股票从「信号所在行业」里抽（行业孪生）。
    industry：(U,) 整数数组，行业编号，-1 = 未知（未知行业的信号用全市场池）；None = 不分行业，直接调用 block_side（逐位相同）。
    做法：当天 n 条信号的行业随机打乱后分给 n 个槽位；每个槽位的新块从「同行业且今天能被抽到」的股票里均匀抽；
    该行业可抽的股票 < min_pool 只则退回全市场池（计数在 SECTOR_FALLBACKS）。已在跑的块不换行业，其余逻辑和 block_side 一样。
    检验的是同行业内选股，不奖励行业轮动。"""
    if industry is None:
        return block_side(ex, ok_all, sig, lengths, k, rng)
    industry = np.asarray(industry)
    T = len(sig)
    out = np.zeros((T, k))
    pos = np.zeros((T, k), dtype="<i4")
    neg = np.zeros((T, k), dtype="<i4")
    codes = np.zeros((k, 0), dtype=int)
    rem = np.zeros((k, 0), dtype=int)
    for t in range(T):
        n = len(sig[t])
        if n == 0:
            codes = rem = np.zeros((k, 0), dtype=int)
            continue
        ok = ok_all[t]
        pool = np.flatnonzero(ok)
        slot_ind = industry[np.asarray(sig[t], dtype=int)][np.argsort(rng.random((k, n)), axis=1)]   # (k, n) 每个槽位的行业
        new_c = pool[rng.integers(0, len(pool), (k, n))]                  # 默认全市场池（行业未知/池太小的槽位用它）
        for g in np.unique(slot_ind):
            if g < 0:
                continue
            m_g = slot_ind == g
            pg = np.flatnonzero(ok & (industry == g))
            if len(pg) < min_pool:
                SECTOR_FALLBACKS[0] += int(m_g.sum())
                continue
            new_c[m_g] = pg[rng.integers(0, len(pg), int(m_g.sum()))]
        new_r = rng.choice(lengths, size=(k, n))
        if codes.shape[1]:
            alive = (rem > 1) & ok[codes]
            keys = np.where(alive, rng.random(alive.shape), 2.0)
            order = np.argsort(keys, axis=1, kind="stable")[:, :n]
            m = order.shape[1]
            ka = np.take_along_axis(alive, order, 1)
            new_c[:, :m] = np.where(ka, np.take_along_axis(codes, order, 1), new_c[:, :m])
            new_r[:, :m] = np.where(ka, np.take_along_axis(rem, order, 1) - 1, new_r[:, :m])
        codes, rem = new_c, new_r
        e = ex[t][codes]
        out[t] = e.sum(1)
        pos[t] = (e > 0).sum(1)
        neg[t] = (e < 0).sum(1)
    return out, pos, neg


_BLOCK_MEMO: dict = {}


def _ledger_stamp(c) -> tuple:
    """账本内容的指纹：哪个库文件、本连接写过多少行、别的连接有没有提交过、各表行数。任何一项变了就重算。"""
    q = lambda sql: tuple(tuple(r) for r in c.execute(sql).fetchall())
    return (q("PRAGMA database_list"), c.total_changes, q("PRAGMA data_version"),
            q("SELECT COUNT(*), MAX(day) FROM done WHERE h=5 AND n>0"), q("SELECT strategy, COUNT(*), MAX(day) FROM signals GROUP BY strategy"),
            q("SELECT COUNT(*) FROM moves WHERE h=5"))


def block_days(c, k: int = NULL_K) -> dict:
    """全部非对照策略的区块对照，按信号日：{策略: [(day, n, own_sum, own_hits, sums(k,), hits(k,)), ...]}。
    口径与 ensure_null 相同（当天全市场均值做基准、买入扣来回费用、开盘一字板不算、跳过没有有效信号的天）；
    只是随机持仓会连续持有。每个策略一个固定种子，结果只取决于账本内容（同一账本重算结果相同）。"""
    from . import signal_ledger as L
    key = (_ledger_stamp(c), k)
    if key in _BLOCK_MEMO:
        return _BLOCK_MEMO[key]
    cost = L.ROUND_TRIP_BP / 1e4
    days = [r["day"] for r in c.execute("SELECT day FROM done WHERE h=? AND n>0 ORDER BY day", (H,))]
    per = {}
    for r in c.execute("SELECT day, code, ret, lim FROM moves WHERE h=? ORDER BY day", (H,)):
        per.setdefault(r["day"], []).append((r["code"], r["ret"], r["lim"]))
    days = [d for d in days if per.get(d)]
    codes = sorted({cd for d in days for cd, _, _ in per[d]})
    ix = {cd: i for i, cd in enumerate(codes)}
    U = len(codes)
    EX, OKB, OKS, BASE = {}, {}, {}, {}
    for d in days:
        i = np.array([ix[cd] for cd, _, _ in per[d]], dtype=int)
        ret = np.array([x for _, x, _ in per[d]], dtype="f8")
        lim = np.array([x for _, _, x in per[d]])
        BASE[d] = float(ret.mean())
        ex = np.full(U, np.nan); ex[i] = ret - BASE[d]
        okb = np.zeros(U, bool); oks = np.zeros(U, bool)
        okb[i[lim != 1]] = True; oks[i[lim != -1]] = True
        EX[d], OKB[d], OKS[d] = ex, okb, oks
    dset = set(days)
    sig = {}
    for r in c.execute("""SELECT s.day, s.strategy, s.code, s.action, m.lim FROM signals s
                          JOIN moves m ON m.day=s.day AND m.code=s.code AND m.h=? ORDER BY s.day, s.rowid""", (H,)):
        if r["day"] not in dset or is_control(r["strategy"]):
            continue
        buy = r["action"] in L.BUYS
        if r["lim"] == (1 if buy else -1):
            continue
        b, s_ = sig.setdefault(r["strategy"], {}).setdefault(r["day"], ([], []))
        (b if buy else s_).append(ix[r["code"]])
    out = {}
    for strat in sorted(sig):
        bd = [d for d in days if d in sig[strat]
              and not ((sig[strat][d][0] and not OKB[d].any()) or (sig[strat][d][1] and not OKS[d].any()))]
        if not bd:
            continue
        buys = [np.array(sig[strat][d][0], dtype=int) for d in bd]
        sells = [np.array(sig[strat][d][1], dtype=int) for d in bd]
        ex = [EX[d] for d in bd]
        rng = np.random.default_rng(zlib.crc32(f"block|{strat}".encode()))
        T = len(bd)
        sums = np.zeros((T, k)); hits = np.zeros((T, k), dtype="<i4")
        nb = np.array([len(b) for b in buys])
        if nb.any():
            lb = run_lengths(buys)
            e, p_, _ = block_side(ex, [OKB[d] for d in bd], buys, lb if len(lb) else np.array([1]), k, rng)
            sums += e - (nb * cost)[:, None]; hits += p_
        if any(len(s_) for s_ in sells):
            ls = run_lengths(sells)
            e, _, n_ = block_side(ex, [OKS[d] for d in bd], sells, ls if len(ls) else np.array([1]), k, rng)
            sums += -e; hits += n_
        rows = []
        for t, d in enumerate(bd):
            eb, es = ex[t][buys[t]], ex[t][sells[t]]
            own = float((eb - cost).sum() + (-es).sum())
            oh = int((eb > 0).sum() + (es < 0).sum())
            rows.append((d, len(buys[t]) + len(sells[t]), own, oh, sums[t], hits[t]))
        out[strat] = rows
    _BLOCK_MEMO.clear()                                    # 只留最近一份：一年账本约 250 天 × 25 策略 × K 个，按天存约 75MB
    _BLOCK_MEMO[key] = out
    return out


def block_weekly(c, strategy: str, cw: list[str], lo: str | None = None, hi: str | None = None, k: int = NULL_K) -> dict:
    """与 load_weekly 同样的返回结构，只是对照换成区块抽样。"""
    ok = set(cw)
    agg = {}
    for d, n, own, oh, s, h in block_days(c, k).get(strategy, []):
        if (lo and d <= lo) or (hi and d > hi):
            continue
        w = week_key(d)
        if w not in ok:
            continue
        a = agg.get(w)
        if a is None:
            agg[w] = [n, own, oh, s.copy(), h.astype("f8"), 1]
        else:
            a[0] += n; a[1] += own; a[2] += oh; a[3] += s; a[4] += h; a[5] += 1
    weeks = sorted(agg)
    if not weeks:
        return {"weeks": [], "n": [], "days": [], "own": np.zeros(0), "null": np.zeros((0, k)), "own_hits": np.zeros(0), "null_hits": np.zeros((0, k))}
    return {"weeks": weeks, "n": [agg[w][0] for w in weeks], "days": [agg[w][5] for w in weeks],
            "own": np.array([agg[w][1] / agg[w][0] for w in weeks]),
            "null": np.array([agg[w][3] / agg[w][0] for w in weeks]),
            "own_hits": np.array([agg[w][2] for w in weeks], dtype="f8"),
            "null_hits": np.array([agg[w][4] for w in weeks])}


def weekly(c, strategy, cw, lo=None, hi=None, k=NULL_K, method=None) -> dict:
    """晋升检验用的周数据：method="block"（默认，D100）或 "iid"（旧的 null_draws）。"""
    return (block_weekly if (method or NULL_METHOD) == "block" else load_weekly)(c, strategy, cw, lo, hi, k)


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


def weekly_t_p(own, null) -> float:
    """D109（方案 A）：按周 t 检验的单侧 p。own (W,) 每周平均得分，null (W,K) 对照每周平均得分；
    d_w = 自己 − 对照均值（K 个对照取平均），用周与周之间的实际波动算 t，df = 周数 − 1。
    同一周里所有信号共同的冲击（行业、风格）会体现在 d_w 的波动里，不需要对照去模拟。周数 < 2 返回 1.0。"""
    from scipy import stats
    d = np.asarray(own, dtype="f8") - np.asarray(null, dtype="f8").mean(1)
    W = len(d)
    if W < 2:
        return 1.0
    sd = d.std(ddof=1)
    if not sd > 0:
        return 1.0 if d.mean() <= 0 else 0.0
    return float(stats.t.sf(d.mean() / (sd / math.sqrt(W)), W - 1))


WEEKLY_T_GATE = False        # D109：方案 A 未通过预注册（N3 在 5% 处 8.5% > 6.5%），B 也没通过 → 默认关闭，是否启用是所有者的独立决定


def gate_p(p_null: float, p_t: float) -> float:
    """晋升的组合门：区块（或旧）对照 p 与按周 t 检验的 p 都要小，所以取较大的那个送进 Holm。WEEKLY_T_GATE 关闭时只用对照 p。"""
    return max(p_null, p_t) if WEEKLY_T_GATE else p_null


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
def promotion_status(c, today: str, k: int = NULL_K, method: str | None = None) -> dict:
    method = method or NULL_METHOD
    m = n_hypotheses(c)
    cw = complete_weeks(c, today)
    names = [r["strategy"] for r in c.execute("SELECT strategy FROM strategy_registry ORDER BY strategy")]
    cps = cw[-PROMO_STREAK:]                                  # 最近 4 个周检查点
    data, cums = {}, {}
    for s in names:
        wk = weekly(c, s, cw, k=k, method=method)
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
                p[s] = gate_p(_emp_p(float(own_c[j]), null_c[j]),
                              weekly_t_p(wk["own"][:j + 1], wk["null"][:j + 1]))      # D109：与按周 t 检验都过才算
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
        if method == "block":                                 # 旧口径（每天独立抽样）的 p 只作对照显示，不参与判定
            wi = load_weekly(c, s, cw, k=k)
            info["p_iid"] = summarize(wi, m, k).get("p") if wi["weeks"] == wk["weeks"] else None
        own_c, null_c = cums[s]
        W, n = info["weeks"], info["n"]
        info["p_t"] = weekly_t_p(wk["own"], wk["null"])        # D109：按周 t 检验的 p（只展示；判定用的是 holm 里的组合门）
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
    return {"today": today, "m": m, "alpha": ALPHA, "k": k, "null_method": method, "complete_weeks": len(cw), "checkpoints": cps,
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
    out["vs_control"] = summarize(weekly(c, strategy, complete_weeks(c, today), lo, hi, k), m, k)
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
