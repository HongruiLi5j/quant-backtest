"""晋升检验随机对照的对比研究（C02）：每天独立抽样（src/promotion.py 现行做法） vs 保留持仓连续性的区块抽样。

问题：promotion.py 的随机对照每天独立地给策略的信号重新挑股票。可真实策略常常连续多天对同一只股票出同方向的信号
  （持有期内反复加仓/减仓），而相邻两天的 5 日收益有 4/5 重叠——同一只股票连续几天的得分高度相关，不能当成几条独立证据。
  独立抽样的对照没有这种相关，它的「噪声范围」（零分布）就偏窄：真实策略纯靠运气也更容易落到范围之外 → p 值偏小 → 假阳性。
做法：
  1. 读同一份信号账本（SQLite immutable 只读打开，不建表、不写入），口径和 promotion.ensure_null 一样：
     得分 = 方向 × (5 日收益 − 当天全市场均值)，买入类再扣来回 16bp；开盘一字涨停的买入、一字跌停的卖出不算。
  2. 独立抽样零分布：逐行复现 promotion.ensure_null（同样的抽样池顺序、同样的种子），测试里核对与它写进账本的结果逐位相同。
  3. 区块抽样零分布：随机“持仓”抽到一只股票后，连续 L 个信号日都用这只股票（只要它当天还能被抽到），
     L 从真实策略的「同一只股票、同一方向、连续出信号的天数」分布里有放回地抽；每天的买入/卖出条数仍与真实策略完全一样
     （延续的块比当天条数多就随机砍掉多余的，不够就新开块补上）。--holding 时 L 改从 state 表的持有期（now>0 连续天数）里抽，作为敏感性对照。
  4. 两种零分布都按同样的「完整交易周」窗口统计量（各周平均得分的平均）汇总，比较标准差、5%/95% 分位数和经验 p 值。
  5. --synthetic：合成市场里放一个「完全没有选股能力、但会连续多天对同一只股票出信号」的策略，重复很多次，
     看两种零分布下 p ≤ 0.05 的比例（零分布正确时应该约等于 5%）。只检验方法本身，不涉及任何真实行情。
研究截止日（队列红线）：信号日、以及它之后第 5 个记录日都不得晚于 2026-03-31；晚于此日期必须显式加 --allow-after-cutoff。
不改 promotion.py，只做对比；numpy 只在这里用。

用法：
  .venv/bin/python -B scripts/promotion_block_null.py --ledger data/signal_ledger.db --out-dir DIR [--strategies blend2] [--holding]
  .venv/bin/python -B scripts/promotion_block_null.py --synthetic --out-dir DIR [--reps 400] [--runs 1,3,5,10]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import zlib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import promotion as PR  # noqa: E402
from src import signal_ledger as L  # noqa: E402

CUTOFF = "2026-03-31"
H = PR.H
COST = L.ROUND_TRIP_BP / 1e4
QS = (5, 95)
LEVELS = (0.05, 0.01, PR.ALPHA / PR.M_FLOOR)       # 0.002 = Holm 第一步的门槛


@dataclass
class Book:
    """一个策略在若干信号日上的全部输入，按天对齐。代码用全体代码表 codes 里的下标表示。"""
    strategy: str
    days: list            # 信号日（升序）
    codes: list           # 全体代码（升序）
    ex: np.ndarray        # (T, U) 当天的 5 日超额收益（ret − 当天全市场均值），没有行情的是 nan
    ok_buy: np.ndarray    # (T, U) 能被买入类信号抽到：有行情且开盘不是一字涨停
    ok_sell: np.ndarray   # (T, U) 能被卖出类信号抽到：有行情且开盘不是一字跌停
    buy: list             # 每天的有效买入类信号（代码下标数组，已剔除开盘涨停的）
    sell: list            # 每天的有效卖出类信号（已剔除开盘跌停的）
    own: np.ndarray       # (T,) 当天真实信号的得分之和

    @property
    def n(self) -> np.ndarray:
        return np.array([len(b) + len(s) for b, s in zip(self.buy, self.sell)], dtype=int)


# ------------------------------------------------------------ 读账本（只读）
def open_ro(path) -> sqlite3.Connection:
    """immutable=1：不加锁、不建 -journal/-wal，也就不可能写入。"""
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    c = sqlite3.connect(f"file:{quote(str(p))}?immutable=1", uri=True)
    c.row_factory = sqlite3.Row
    return c


def usable_days(c, end: str) -> tuple[list, list]:
    """(全部记录日, 可用信号日)。可用 = 已走满 5 日、信号日 ≤ end、它之后第 5 个记录日也 ≤ end。
    记录日有缺口时第 5 个记录日只会更晚，所以这个判断只会更严，不会把截止日之后的收益放进来。"""
    all_days = [r["day"] for r in c.execute("SELECT day FROM days ORDER BY day")]
    done = {r["day"] for r in c.execute("SELECT day FROM done WHERE h=? AND n>0", (H,))}
    ok = [d for i, d in enumerate(all_days)
          if d in done and d <= end and i + H < len(all_days) and all_days[i + H] <= end]
    return all_days, ok


def complete_weeks(all_days, ok_days, today: str) -> list:
    """和 promotion.complete_weeks 同一规则（当前周不算、整周的信号日都可用才算完整周），只是“可用”多了截止日条件。"""
    cur, ok = PR.week_key(today), set(ok_days)
    by = {}
    for d in all_days:
        by.setdefault(PR.week_key(d), []).append(d)
    return sorted(w for w, ds in by.items() if w < cur and all(d in ok for d in ds))


def load_book(c, strategy: str, days: list) -> Book:
    """逐天读 moves 和该策略的 signals。和 ensure_null 一样跳过：没有有效信号、或某方向抽样池为空的天。"""
    per = []
    for d in days:
        mv = c.execute("SELECT code, ret, lim FROM moves WHERE day=? AND h=?", (d, H)).fetchall()
        if mv:
            per.append((d, mv))
    codes = sorted({r["code"] for _, mv in per for r in mv})
    pos = {cd: i for i, cd in enumerate(codes)}
    keep_days, ex_rows, okb_rows, oks_rows, buys, sells, owns = [], [], [], [], [], [], []
    for d, mv in per:
        ret = np.array([r["ret"] for r in mv], dtype="f8")
        lim = np.array([r["lim"] for r in mv])
        idx = np.array([pos[r["code"]] for r in mv], dtype=int)
        base = float(ret.mean())                                     # 和 ensure_null 一样：当天全市场平均
        ex = np.full(len(codes), np.nan)
        ex[idx] = ret - base
        okb = np.zeros(len(codes), bool)
        oks = np.zeros(len(codes), bool)
        okb[idx[lim != 1]] = True
        oks[idx[lim != -1]] = True
        b, s, own = [], [], 0.0
        for r in c.execute("""SELECT s.code, s.action, m.ret, m.lim FROM signals s JOIN moves m ON m.day=s.day AND m.code=s.code AND m.h=?
                              WHERE s.day=? AND s.strategy=?""", (H, d, strategy)):
            buy = r["action"] in L.BUYS
            if r["lim"] == (1 if buy else -1):
                continue
            e = r["ret"] - base
            if buy:
                b.append(pos[r["code"]]); own += e - COST
            else:
                s.append(pos[r["code"]]); own += -e
        if not b and not s or (b and not okb.any()) or (s and not oks.any()):
            continue
        keep_days.append(d); ex_rows.append(ex); okb_rows.append(okb); oks_rows.append(oks)
        buys.append(np.array(b, dtype=int)); sells.append(np.array(s, dtype=int)); owns.append(own)
    U = len(codes)
    return Book(strategy, keep_days, codes,
                np.array(ex_rows).reshape(len(keep_days), U), np.array(okb_rows, bool).reshape(len(keep_days), U),
                np.array(oks_rows, bool).reshape(len(keep_days), U), buys, sells, np.array(owns, dtype="f8"))


def holding_sets(c, strategy: str, days: list) -> list:
    """state 表里每天该策略持有（now>0）的代码集合，--holding 敏感性对照用。"""
    return [{r["code"] for r in c.execute("SELECT code FROM state WHERE day=? AND strategy=? AND now>0", (d, strategy))}
            for d in days]


# ------------------------------------------------------------ 连续出信号的天数
def run_lengths(daily_sets) -> np.ndarray:
    """每段「同一代码连续出现的天数」（按给定的日序列计连续）。样本两头的段被截断，所以平均长度是低估。"""
    out, cur = [], {}
    for s in daily_sets:
        s = s if isinstance(s, set) else set(np.asarray(s).tolist())
        out += [v for cd, v in cur.items() if cd not in s]
        cur = {cd: cur.get(cd, 0) + 1 for cd in s}
    out += list(cur.values())
    return np.array(out, dtype=int)


def continuation_share(daily) -> float | None:
    """第 2 天起，信号里“昨天同方向也有它”的比例。"""
    tot = hit = 0
    for prev, now in zip(daily, daily[1:]):
        p = set(np.asarray(prev).tolist())
        now = np.asarray(now).tolist()
        tot += len(now); hit += sum(1 for x in now if x in p)
    return hit / tot if tot else None


# ------------------------------------------------------------ 两种零分布（每天的 K 个得分之和）
def promo_seed(strategy: str):
    return lambda day: zlib.crc32(f"{strategy}|{day}".encode())     # 和 ensure_null 相同的固定种子


def iid_null(book: Book, k: int, seed_of=None, rng=None) -> np.ndarray:
    """独立抽样：逐行复现 ensure_null。给 seed_of 时每天用它的种子（复现账本里的 null_draws），否则共用 rng。返回 (T, k)。"""
    out = np.zeros((len(book.days), k))
    for t, d in enumerate(book.days):
        g = np.random.default_rng(seed_of(d)) if seed_of else rng
        nb, ns = len(book.buy[t]), len(book.sell[t])
        sums = np.zeros(k)
        if nb:
            exb = book.ex[t][book.ok_buy[t]]
            e = exb[g.integers(0, len(exb), (k, nb))]                  # 有放回抽样，与 ensure_null 一致
            sums += e.sum(1) - nb * COST
        if ns:
            exs = book.ex[t][book.ok_sell[t]]
            e = exs[g.integers(0, len(exs), (k, ns))]
            sums += -e.sum(1)
        out[t] = sums
    return out


def _block_side(book: Book, side: str, lengths: np.ndarray, k: int, rng) -> tuple[np.ndarray, int, int]:
    """一个方向的区块抽样。返回 ((T, k) 原始超额之和, 第 2 天起延续块的个数, 第 2 天起的抽样总数)。"""
    sig = book.buy if side == "buy" else book.sell
    oks = book.ok_buy if side == "buy" else book.ok_sell
    T = len(book.days)
    out = np.zeros((T, k))
    codes = np.zeros((k, 0), dtype=int)
    rem = np.zeros((k, 0), dtype=int)                                  # 含当天在内还剩几天
    kept = total = 0
    for t in range(T):
        n = len(sig[t])
        if n == 0:                                                     # 当天这个方向没有信号：所有块中断（真实策略的连续段也在这里断开）
            codes = rem = np.zeros((k, 0), dtype=int)
            continue
        ok = oks[t]
        pool = np.flatnonzero(ok)
        new_c = pool[rng.integers(0, len(pool), (k, n))]               # 先全部按新开块准备好
        new_r = rng.choice(lengths, size=(k, n))
        if codes.shape[1]:
            alive = (rem > 1) & ok[codes]                               # 寿命没到、且今天还能被抽到（开盘一字板/停牌的块提前结束）
            keys = np.where(alive, rng.random(alive.shape), 2.0)       # 活块排前面、彼此随机；多于 n 个时随机留 n 个
            order = np.argsort(keys, axis=1, kind="stable")[:, :n]
            m = order.shape[1]
            ka = np.take_along_axis(alive, order, 1)
            new_c[:, :m] = np.where(ka, np.take_along_axis(codes, order, 1), new_c[:, :m])
            new_r[:, :m] = np.where(ka, np.take_along_axis(rem, order, 1) - 1, new_r[:, :m])
            kept += int(ka.sum())
        if t:
            total += k * n
        codes, rem = new_c, new_r
        out[t] = book.ex[t][codes].sum(1)
    return out, kept, total


def block_null(book: Book, k: int, len_buy, len_sell, rng) -> tuple[np.ndarray, float | None]:
    """区块抽样零分布 (T, k)，以及零分布里“延续上一天同一只股票”的比例（和真实策略的 continuation_share 对照）。"""
    nb = np.array([len(b) for b in book.buy])
    out = np.zeros((len(book.days), k))
    kept = total = 0
    if nb.any():
        e, a, b = _block_side(book, "buy", np.asarray(len_buy), k, rng)
        out += e - (nb * COST)[:, None]
        kept += a; total += b
    if any(len(s) for s in book.sell):
        e, a, b = _block_side(book, "sell", np.asarray(len_sell), k, rng)
        out += -e
        kept += a; total += b
    return out, (kept / total if total else None)


# ------------------------------------------------------------ 按周的窗口统计量
def window(book: Book, own: np.ndarray, null: np.ndarray, weeks=None) -> tuple[float, np.ndarray, list]:
    """各周平均得分的平均（和 promotion._cum 的最后一行同口径）。weeks 给定时只用这些周。返回 (自己, 对照 (k,), 用到的周)。"""
    wk = [PR.week_key(d) for d in book.days]
    use = sorted(set(wk) if weeks is None else set(wk) & set(weeks))
    n = book.n
    if not use:
        return float("nan"), np.full(null.shape[1], np.nan), []
    o, z = [], []
    for w in use:
        idx = [i for i, x in enumerate(wk) if x == w]
        o.append(own[idx].sum() / n[idx].sum())
        z.append(null[idx].sum(0) / n[idx].sum())
    return float(np.mean(o)), np.mean(z, axis=0), use


def describe(stat: float, null: np.ndarray) -> dict:
    lo, hi = np.percentile(null, QS)
    return {"mean": float(null.mean()), "sd": float(null.std(ddof=1)), "q05": float(lo), "q95": float(hi),
            "p": PR._emp_p(stat, null)}


# ------------------------------------------------------------ 真实账本
def inventory(c, today: str) -> dict:
    """只计数、不读收益：账本里有几个记录日/走满 5 日的信号日/完整周，各策略在完整周里有多少条信号。"""
    all_days = [r["day"] for r in c.execute("SELECT day FROM days ORDER BY day")]
    done = [r["day"] for r in c.execute("SELECT day FROM done WHERE h=? AND n>0 ORDER BY day", (H,))]
    cw = PR.complete_weeks(c, today)
    in_cw = [d for d in done if PR.week_key(d) in set(cw)]
    def count(ds):
        q = f"SELECT strategy, COUNT(*) n FROM signals WHERE day IN ({','.join('?' * len(ds)) or chr(39) * 2}) GROUP BY strategy"
        return {r["strategy"]: r["n"] for r in c.execute(q, ds) if not PR.is_control(r["strategy"])}
    return {"today": today, "record_days": len(all_days), "first_day": all_days[0] if all_days else None,
            "last_day": all_days[-1] if all_days else None, "done5_days": len(done), "complete_weeks": cw,
            "complete_week_days": in_cw, "signals_in_complete_weeks": count(in_cw), "signals_in_done5_days": count(done),
            "gate": {"min_weeks": PR.PROMO_MIN_WEEKS, "min_n": PR.PROMO_MIN_N}}


def compare_real(c, strategy: str, end: str, today: str, k: int, seed: int, holding: bool) -> dict:
    all_days, ok_days = usable_days(c, end)
    cw = complete_weeks(all_days, ok_days, today)
    days = [d for d in ok_days if PR.week_key(d) in set(cw)]
    book = load_book(c, strategy, days)
    res = {"strategy": strategy, "end": end, "today": today, "weeks": [], "days": len(book.days), "n": int(book.n.sum())}
    if not book.days:
        res["note"] = "截止日之前没有可用的完整交易周"
        return res
    lb = run_lengths(book.buy)
    ls = run_lengths(book.sell)
    iid = iid_null(book, k, seed_of=promo_seed(strategy))
    rng = np.random.default_rng(zlib.crc32(f"block|{strategy}|{seed}".encode()))
    blk, cont = block_null(book, k, lb if len(lb) else [1], ls if len(ls) else [1], rng)
    stat, zi, weeks = window(book, book.own, iid)
    _, zb, _ = window(book, book.own, blk)
    res.update(weeks=weeks, stat=stat, iid=describe(stat, zi), block=describe(stat, zb),
               run_len={"buy_mean": float(lb.mean()) if len(lb) else None, "sell_mean": float(ls.mean()) if len(ls) else None,
                        "buy_max": int(lb.max()) if len(lb) else None, "sell_max": int(ls.max()) if len(ls) else None},
               continuation={"real_buy": continuation_share(book.buy), "real_sell": continuation_share(book.sell), "block_null": cont},
               sd_ratio=float(zb.std(ddof=1) / zi.std(ddof=1)))
    # 一致性核对：本脚本复现的独立抽样窗口统计量 vs 账本里 ensure_null 已写好的 null_draws
    stored = PR.load_weekly(c, strategy, weeks, hi=end, k=k)
    if stored["weeks"] == weeks:
        own_c, null_c = PR._cum(stored)
        res["check_vs_stored"] = {"own_diff": abs(float(own_c[-1]) - stat), "null_max_diff": float(np.abs(null_c[-1] - zi).max())}
    if holding:
        hl = run_lengths(holding_sets(c, strategy, book.days))
        if len(hl):
            rng = np.random.default_rng(zlib.crc32(f"hold|{strategy}|{seed}".encode()))
            bh, ch = block_null(book, k, hl, hl, rng)
            _, zh, _ = window(book, book.own, bh)
            res["block_holding"] = describe(stat, zh) | {"len_mean": float(hl.mean()), "continuation": ch}
    return res


# ------------------------------------------------------------ 合成市场（只检验方法）
def _weekdays(start: str, n: int) -> list:
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def synth_book(rng, stocks=3000, weeks=12, per_side=100, mean_run=5.0, lim_p=0.01, vol=0.025, burn=30,
               start="2025-06-02") -> Book:
    """没有选股能力、但有持仓连续性的策略。
    日收益 = 大盘 N(0,1%) + 个股 t(4) 分布（厚尾，日波动 vol）；信号日 D 的 5 日收益 = D+1..D+5 日收益之和（相邻两天重叠 4/5）。
    买、卖两个方向各自：每天新开 Poisson(per_side/mean_run) 段，段长 ~ 几何分布(均值 mean_run)，股票均匀随机；
    当天的信号 = 所有活着的段的股票（去重）。先空跑 burn 天让段数稳定。日期都在 2025 年，早于研究截止日。"""
    T = weeks * 5
    tot = burn + T
    r = rng.standard_t(4, (tot + H + 1, stocks)) * (vol / np.sqrt(2)) + rng.normal(0, 0.01, (tot + H + 1, 1))
    cs = np.vstack([np.zeros(stocks), r.cumsum(0)])
    ret5 = cs[np.arange(tot) + 1 + H] - cs[np.arange(tot) + 1]       # 第 t 天信号：t+1..t+H 日收益之和
    lim = np.zeros((tot, stocks), dtype=int)
    u = rng.random((tot, stocks))
    lim[u < lim_p] = 1
    lim[(u >= lim_p) & (u < 2 * lim_p)] = -1
    sides = {}
    for side in ("buy", "sell"):
        live, days_sig = [], []
        for t in range(tot):
            live = [(cd, left - 1) for cd, left in live if left > 1]
            for _ in range(rng.poisson(per_side / mean_run)):
                live.append((int(rng.integers(stocks)), int(rng.geometric(1 / mean_run))))
            days_sig.append(np.array(sorted({cd for cd, _ in live}), dtype=int))
        sides[side] = days_sig[burn:]
    ex = ret5[burn:] - ret5[burn:].mean(1, keepdims=True)
    lim = lim[burn:]
    okb, oks = lim != 1, lim != -1
    buy = [b[okb[t][b]] for t, b in enumerate(sides["buy"])]
    sell = [s[oks[t][s]] for t, s in enumerate(sides["sell"])]
    own = np.array([ex[t][buy[t]].sum() - len(buy[t]) * COST - ex[t][sell[t]].sum() for t in range(T)])
    return Book("synthetic", _weekdays(start, T), [f"{i:06d}" for i in range(stocks)], ex, okb, oks, buy, sell, own)


def calibrate(runs, reps, k, seed, stocks, weeks, per_side, log=print) -> list:
    rows = []
    for mr in runs:
        stats, pi, pb, sdi, sdb, lens, cr, cb = [], [], [], [], [], [], [], []
        t0 = time.time()
        for rep in range(reps):
            rng = np.random.default_rng([seed, int(round(mr * 100)), rep])
            book = synth_book(rng, stocks=stocks, weeks=weeks, per_side=per_side, mean_run=mr)
            lb, ls = run_lengths(book.buy), run_lengths(book.sell)
            zi_d = iid_null(book, k, rng=rng)
            zb_d, cont = block_null(book, k, lb, ls, rng)
            stat, zi, _ = window(book, book.own, zi_d)
            _, zb, _ = window(book, book.own, zb_d)
            stats.append(stat); pi.append(PR._emp_p(stat, zi)); pb.append(PR._emp_p(stat, zb))
            sdi.append(zi.std(ddof=1)); sdb.append(zb.std(ddof=1))
            lens.append(np.concatenate([lb, ls]).mean())
            cr.append(continuation_share(book.buy)); cb.append(cont)
        pi, pb = np.array(pi), np.array(pb)
        true_sd = float(np.std(stats, ddof=1))
        row = {"mean_run": mr, "reps": reps, "weeks": weeks, "stocks": stocks, "per_side": per_side, "k": k,
               "true_sd": true_sd, "iid_sd": float(np.mean(sdi)), "block_sd": float(np.mean(sdb)),
               "iid_sd_over_true": float(np.mean(sdi)) / true_sd, "block_sd_over_true": float(np.mean(sdb)) / true_sd,
               "emp_run_mean": float(np.mean(lens)), "cont_real": float(np.mean(cr)), "cont_block": float(np.mean(cb)),
               "reject_iid": {str(a): float((pi <= a).mean()) for a in LEVELS},
               "reject_block": {str(a): float((pb <= a).mean()) for a in LEVELS},
               "seconds": round(time.time() - t0, 1)}
        log(json.dumps(row, ensure_ascii=False))
        rows.append(row)
    return rows


# ------------------------------------------------------------ 输出
def _pct(x):
    return "—" if x is None else f"{x:+.4%}"


def md_real(inv: dict, res: list, end: str) -> str:
    g = inv["gate"]
    lines = ["# 晋升检验：独立抽样 vs 区块抽样（真实账本）", "",
             f"研究截止日 {end}（信号日及其后第 5 个记录日都不晚于它）。", "",
             "## 样本量盘点（只计数，不读收益）", "",
             f"- 记录日 {inv['record_days']} 个（{inv['first_day']} ~ {inv['last_day']}），已走满 5 日的信号日 {inv['done5_days']} 个",
             f"- 按 today={inv['today']} 的完整交易周：{len(inv['complete_weeks'])} 周 {inv['complete_weeks']}，"
             f"包含信号日 {inv['complete_week_days']}",
             f"- 晋升门槛：≥{g['min_weeks']} 个完整周、≥{g['min_n']} 条信号", "",
             "原始信号行数（未剔除开盘一字板、未要求有行情），晋升检验实际计入的条数只会更少或相等。", "",
             "| 策略 | 完整周内 | 全部已走满 5 日的信号日内 |", "|---|---:|---:|"]
    done_n = inv["signals_in_done5_days"]
    lines += [f"| {s} | {inv['signals_in_complete_weeks'].get(s, 0)} | {n} |" for s, n in sorted(done_n.items())]
    lines += ["", "## 对比", ""]
    for r in res:
        if "iid" not in r:
            lines.append(f"- {r['strategy']}：{r.get('note')}（截止日内可用信号日 {r['days']} 个）")
            continue
        lines += [f"### {r['strategy']}（{len(r['weeks'])} 周、{r['days']} 个信号日、{r['n']} 条信号）", "",
                  f"窗口统计量（各周平均得分的平均）= {_pct(r['stat'])}", "",
                  "| 零分布 | 均值 | 标准差 | 5% 分位 | 95% 分位 | 经验 p |", "|---|---:|---:|---:|---:|---:|"]
        for key, name in (("iid", "每天独立抽样（promotion.py）"), ("block", "区块抽样（连续出信号天数）"), ("block_holding", "区块抽样（state 持有期）")):
            if key in r:
                z = r[key]
                lines.append(f"| {name} | {_pct(z['mean'])} | {z['sd']:.4%} | {_pct(z['q05'])} | {_pct(z['q95'])} | {z['p']:.4f} |")
        lines += ["", f"- 标准差之比（区块/独立）= {r['sd_ratio']:.3f}",
                  f"- 连续出信号天数：{json.dumps(r['run_len'], ensure_ascii=False)}",
                  f"- 延续比例：{json.dumps(r['continuation'], ensure_ascii=False)}"]
        if "check_vs_stored" in r:
            lines.append(f"- 与账本 null_draws 的一致性：{json.dumps(r['check_vs_stored'])}")
        lines.append("")
    return "\n".join(lines) + "\n"


def md_synth(rows: list) -> str:
    lv = [str(a) for a in LEVELS]
    lines = ["# 合成市场校准：没有选股能力的策略被判“显著”的比例", "",
             "零分布正确时，p ≤ α 的比例应约等于 α。true_sd = 同一设定下重复多次时策略窗口统计量的实际标准差。", "",
             "| 设定段长均值 | 实测段长均值 | 真实延续比例 | 区块零分布延续比例 | true_sd | 独立 sd/true | 区块 sd/true | "
             + " | ".join(f"独立 p≤{a}" for a in lv) + " | " + " | ".join(f"区块 p≤{a}" for a in lv) + " |",
             "|" + "---:|" * (7 + 2 * len(lv))]
    for r in rows:
        lines.append(f"| {r['mean_run']:g} | {r['emp_run_mean']:.2f} | {r['cont_real']:.3f} | {r['cont_block']:.3f} | {r['true_sd']:.4%} | "
                     f"{r['iid_sd_over_true']:.3f} | {r['block_sd_over_true']:.3f} | "
                     + " | ".join(f"{r['reject_iid'][a]:.3f}" for a in lv) + " | "
                     + " | ".join(f"{r['reject_block'][a]:.3f}" for a in lv) + " |")
    r0 = rows[0] if rows else {}
    lines += ["", f"每个设定重复 {r0.get('reps')} 次；{r0.get('weeks')} 个完整周、{r0.get('stocks')} 只股票、"
              f"每天买/卖各约 {r0.get('per_side')} 条信号、每个零分布 K={r0.get('k')}。"]
    return "\n".join(lines) + "\n"


def _write_new(path: Path, text: str):
    with open(path, "x", encoding="utf-8") as f:                    # 排他创建：已存在就报错，不覆盖
        f.write(text)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--ledger", default=str(L.DB))
    ap.add_argument("--strategies", default="blend2", help="逗号分隔；all = 账本里全部非对照策略")
    ap.add_argument("--end", default=CUTOFF)
    ap.add_argument("--today", default=None, help="判定“当前周”的日期，默认 end 的下一天")
    ap.add_argument("--allow-after-cutoff", action="store_true")
    ap.add_argument("--holding", action="store_true", help="另用 state 表的持有期做一次区块抽样")
    ap.add_argument("--k", type=int, default=PR.NULL_K)
    ap.add_argument("--seed", type=int, default=20261030)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--reps", type=int, default=400)
    ap.add_argument("--runs", default="1,3,5,10")
    ap.add_argument("--stocks", type=int, default=3000)
    ap.add_argument("--weeks", type=int, default=PR.PROMO_MIN_WEEKS)
    ap.add_argument("--per-side", type=int, default=100)
    a = ap.parse_args(argv)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if a.synthetic:
        rows = calibrate([float(x) for x in a.runs.split(",")], a.reps, a.k, a.seed, a.stocks, a.weeks, a.per_side)
        _write_new(out / "synthetic_calibration.json", json.dumps(rows, ensure_ascii=False, indent=1))
        _write_new(out / "synthetic_calibration.md", md_synth(rows))
        print(f"已写入 {out}/synthetic_calibration.json / .md")
        return 0

    if a.end > CUTOFF and not a.allow_after_cutoff:
        print(f"拒绝：--end {a.end} 晚于研究截止日 {CUTOFF}；确认要用截止日之后的数据请显式加 --allow-after-cutoff", file=sys.stderr)
        return 2
    today = a.today or (date.fromisoformat(a.end) + timedelta(days=1)).isoformat()
    c = open_ro(a.ledger)
    inv = inventory(c, date.today().isoformat())
    names = [s.strip() for s in a.strategies.split(",") if s.strip()]
    if names == ["all"]:
        names = [r["strategy"] for r in c.execute("SELECT DISTINCT strategy FROM signals ORDER BY strategy") if not PR.is_control(r["strategy"])]
    res = [compare_real(c, s, a.end, today, a.k, a.seed, a.holding) for s in names]
    _write_new(out / "real_compare.json", json.dumps(PR.jsonable({"inventory": inv, "results": res}), ensure_ascii=False, indent=1))
    _write_new(out / "real_compare.md", md_real(inv, res, a.end))
    print(f"已写入 {out}/real_compare.json / .md")
    for r in res:
        if "iid" in r:
            print(f"{r['strategy']}: p 独立={r['iid']['p']:.4f} 区块={r['block']['p']:.4f} sd 比={r['sd_ratio']:.3f}")
        else:
            print(f"{r['strategy']}: {r.get('note')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
