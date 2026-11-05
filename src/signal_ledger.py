"""信号核对账本：每天收盘后记录「今日买卖」的全部信号，之后用真实行情核对它们对不对、漏了什么。

和 AI 模拟账户无关，不模拟资金，只统计信号本身，所以样本是清单里的全部信号（每天一两百条），不是少数成交。
- 入场口径：信号日 D 的下一个交易日开盘价；出场口径：D+h 交易日收盘价（h = 1、5、20）。买入/加仓看涨，卖出/减仓看跌。
- 对比基准：同一天、同一口径下全市场（有行情的股票）的平均涨跌，避免大盘整体上涨时所有买入都显得“对”。
- 开盘一字涨停（买不进）、开盘跌停（卖不出）的信号单独标出，不计入准确率。
- 漏掉的机会：D 日之后 5 日涨幅位于全市场前 5% 且不低于 10% 的股票（剔除开盘涨停和连板短炒），
  看它们当时是 已推荐 / 已持有 / 被过滤规则挡掉 / 完全没有信号。
- 样本不足 30 条、或不足 5 个信号日的统计只展示（同一天的信号共享同一个大盘行情，不是独立样本），不作为改动依据；改策略的建议永远只是建议，要用户同意才改。
纯标准库；行情由 provider 注入（默认用本地日线缓存）。
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import threading
from bisect import bisect_right
from datetime import datetime
from pathlib import Path

from . import costs

ROOT =Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "signal_ledger.db"
OUT = ROOT / "data" / "daily" / "ledger"
HORIZONS = (1, 5, 10, 20)      # 次日只是噪声；趋势类策略的持仓以周计，重点看 5/10/20 日
MIN_SAMPLE = 30
MIN_DAYS = 5       # 同一天的信号受同一个大盘行情影响，不是独立样本，至少要跨 5 个信号日
WIN_PCT = 0.95
WIN_MIN_RET = 0.10
T_SIG = 2.0         # 按信号日聚合后的 t 值至少到 2，才说“显著”
ROUND_TRIP_BP = costs.CN_STOCK_ROUND_TRIP_BP  # 个股单边 8bp（config.stock_cost_bp）×2，买入 + 卖出两边 = 16
ACTIONS = ("买入", "卖出", "加仓", "减仓")
BUYS = ("买入", "加仓")
_lock = threading.RLock()


def limit_pct(code: str) -> float:
    """统一委托 src/limits.py。账本的信号不含 ST 股（scan 已排除），所以只按板块（最新规则）。"""
    from .limits import limit_pct as _lp
    return _lp(code, "")


def connect(path=None):
    p = Path(path) if path else DB
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY, has_state INTEGER, recorded_at TEXT);
    CREATE TABLE IF NOT EXISTS signals(day TEXT, strategy TEXT, code TEXT, name TEXT, action TEXT, now REAL, next REAL, close REAL,
        liquidity TEXT, small INTEGER, flags TEXT, PRIMARY KEY(day, strategy, code));
    CREATE TABLE IF NOT EXISTS state(day TEXT, strategy TEXT, code TEXT, now REAL, next REAL, PRIMARY KEY(day, strategy, code));
    CREATE TABLE IF NOT EXISTS moves(day TEXT, code TEXT, h INTEGER, ret REAL, gap REAL, lim INTEGER, pump INTEGER,
        PRIMARY KEY(day, code, h));
    CREATE TABLE IF NOT EXISTS done(day TEXT, h INTEGER, n INTEGER, PRIMARY KEY(day, h));
    CREATE TABLE IF NOT EXISTS day_params(day TEXT PRIMARY KEY, thr REAL, mt REAL);
    CREATE TABLE IF NOT EXISTS strategy_registry(strategy TEXT PRIMARY KEY, first_day TEXT);
    CREATE TABLE IF NOT EXISTS fwd_features(day TEXT, code TEXT, name TEXT, ratio REAL, max20 REAL, abvol5_60 REAL, yizi INTEGER,
        computed_at TEXT, PRIMARY KEY(day, code));
    CREATE TABLE IF NOT EXISTS limit_close(day TEXT, code TEXT, up_close INTEGER, yizi INTEGER, PRIMARY KEY(day, code));
    CREATE TABLE IF NOT EXISTS null_draws(day TEXT, strategy TEXT, n INTEGER, nb INTEGER, ns INTEGER, own_sum REAL, own_hits INTEGER,
        k INTEGER, sums BLOB, hits BLOB, PRIMARY KEY(day, strategy));
    -- D101：precision / scored 按「策略 + 动作」查 signals，主键 (day, strategy, code) 用不上，原来每次扫整张表。
    -- 只加索引、不改任何数据；已存在就跳过，第一次建约 0.1 秒（11.6 万行）。核对报告 7.8 秒 → 1.0 秒，结果逐字节相同。
    CREATE INDEX IF NOT EXISTS signals_sa ON signals(strategy, action);
    """)
    return c


# ------------------------------------------------------------ 记录
def record_day(c, payload: dict, state_rows=None) -> int:
    """记录一个信号日；重复调用不会重复写入。state_rows = [(strategy, code, now, next)]（全市场当时的持仓状态，召回率要用）。"""
    day = payload["data_date"]
    c.execute("INSERT OR IGNORE INTO days VALUES(?,?,?)", (day, 0, datetime.now().isoformat(timespec="seconds")))
    if payload.get("threshold") is not None and payload.get("min_target") is not None:      # 当天正式清单用的阈值，候选要用同一组才可比
        c.execute("INSERT OR IGNORE INTO day_params VALUES(?,?,?)", (day, payload["threshold"], payload["min_target"]))
    n = 0
    for strat, pack in payload.get("strategies", {}).items():
        for r in pack.get("rows", []):
            if r.get("action") not in ACTIONS or not r.get("close"):
                continue
            cur = c.execute("INSERT OR IGNORE INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (day, strat, r["code"], r.get("name"), r["action"], r.get("now"), r.get("next"), r["close"],
                             r.get("liquidity"), 1 if r.get("small_target") else 0, json.dumps(r.get("flags") or [], ensure_ascii=False)))
            n += cur.rowcount
    if state_rows:      # 主键去重，所以后来补充的行（比如新增的影子候选）也能写进已经记录过状态的日子
        c.executemany("INSERT OR IGNORE INTO state VALUES(?,?,?,?,?)", [(day, *r) for r in state_rows])
        c.execute("UPDATE days SET has_state=1 WHERE day=?", (day,))
    c.commit()
    return n


def backfill_archive(c, folder: Path) -> list[str]:
    """把已归档的历史清单也记进来（没有当时的持仓状态，所以这些天只能算准确率，不能算召回率）。"""
    added = []
    for f in sorted(Path(folder).glob("????-??-??.json")):
        if c.execute("SELECT 1 FROM days WHERE day=?", (f.stem,)).fetchone():
            continue
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
            if payload.get("data_date") == f.stem and record_day(c, payload) >= 0:
                added.append(f.stem)
        except Exception:
            continue
    return added


# ------------------------------------------------------------ 结果计算
def nth_day(cal: list[str], day: str, n: int):
    i = bisect_right(cal, day) - 1
    return cal[i + n] if i >= 0 and i + n < len(cal) else None


def moves_for(code: str, bars: dict, day: str, cal: list[str], horizons=HORIZONS) -> list[dict]:
    """bars = {日期: (开, 高, 低, 收)}。没有下一交易日开盘价（停牌/数据缺失）就返回空。"""
    entry = nth_day(cal, day, 1)
    if not entry or entry not in bars or day not in bars:
        return []
    c0, o1 = bars[day][3], bars[entry][0]
    if not c0 or not o1:
        return []
    gap = o1 / c0 - 1
    from .limits import listed_days, no_limit
    if no_limit(code, entry, listed_days(code, entry)):     # D17：新股上市初期不设涨跌停，开盘价都能成交
        lim, lim_open = None, 0
    else:
        lim = limit_pct(code)
        lim_open = 1 if gap >= lim - 0.002 else (-1 if gap <= -lim + 0.002 else 0)
    out = []
    for h in horizons:
        dh = nth_day(cal, day, h)
        if not dh or dh not in bars:
            continue
        i0, ih = cal.index(entry), cal.index(dh)
        pumps, prev = 0, c0
        for d in cal[i0:ih + 1]:
            b = bars.get(d)
            if b and prev and lim is not None and b[3] / prev - 1 >= lim - 0.002:
                pumps += 1
            if b:
                prev = b[3]
        out.append({"h": h, "ret": bars[dh][3] / o1 - 1, "gap": gap, "lim": lim_open, "pump": 1 if pumps >= 2 else 0})
    return out


def pending(c, cal: list[str], today: str) -> dict:
    need = {}
    for r in c.execute("SELECT day FROM days"):
        for h in HORIZONS:
            dh = nth_day(cal, r["day"], h)
            if dh and dh <= today and not c.execute("SELECT 1 FROM done WHERE day=? AND h=?", (r["day"], h)).fetchone():
                need.setdefault(r["day"], []).append(h)
    return need


def evaluate(c, provider, cal: list[str], today: str) -> dict:
    """对已经走满 h 个交易日的信号日，一次遍历全部股票的日线算好结果。"""
    need = pending(c, cal, today)
    if not need:
        return {}
    rows, counts = [], {}
    for code in provider.codes():
        bars = provider.bars(code)
        if not bars:
            continue
        for day, hs in need.items():
            for m in moves_for(code, bars, day, cal, hs):
                rows.append((day, code, m["h"], m["ret"], m["gap"], m["lim"], m["pump"]))
                counts[(day, m["h"])] = counts.get((day, m["h"]), 0) + 1
    c.executemany("INSERT OR REPLACE INTO moves VALUES(?,?,?,?,?,?,?)", rows)
    for day, hs in need.items():
        for h in hs:
            c.execute("INSERT OR REPLACE INTO done VALUES(?,?,?)", (day, h, counts.get((day, h), 0)))
    c.commit()
    return {f"{d}/{h}": n for (d, h), n in counts.items()}


# ------------------------------------------------------------ 收盘涨停的买入信号（D30）
def ensure_limit_close(c, provider) -> int:
    """给每条（非对照策略的）买入/加仓信号记下：信号日收盘是不是在涨停价（收盘 ≥ 昨收 ×(1+幅度) − 0.2pp）。
    趋势、突破类策略天然会在涨停那天发买入信号，这些股票第二天开盘仍一字涨停（买不进）的概率远高于其他股票。
    幅度用 src/limits.py 的同一张规则表（按信号日、带上市天数）。已记过的不重算；返回新写入的行数。"""
    from .limits import limit_pct as _lp, listed_days
    todo = {}
    for r in c.execute("""SELECT DISTINCT s.day, s.code, s.name FROM signals s
                          LEFT JOIN limit_close l ON l.day=s.day AND l.code=s.code
                          WHERE s.action IN ('买入','加仓') AND s.strategy NOT LIKE 'ctrl_%' AND l.code IS NULL"""):
        todo.setdefault(r["code"], []).append((r["day"], r["name"] or ""))
    rows = []
    for code, items in todo.items():
        bars = provider.bars(code)
        if not bars:
            continue
        ds = sorted(bars)
        for day, name in items:
            i = bisect_right(ds, day) - 1
            if i < 1 or ds[i] != day:
                continue
            prev, (o, hi, lo, close) = bars[ds[i - 1]][3], bars[day]
            lim = _lp(code, name, day, listed_days(code, day))
            up = 1 if (lim is not None and prev and close and close / prev - 1 >= lim - 0.002) else 0
            yizi = 1 if (up and all(math.isclose(x, close, rel_tol=1e-10, abs_tol=1e-10) for x in (o, hi, lo))) else 0   # 一字板：开高低收相同
            rows.append((day, code, up, yizi))
    c.executemany("INSERT OR IGNORE INTO limit_close VALUES(?,?,?,?)", rows)
    c.commit()
    return len(rows)


FWD_FREEZE = "2026-11-09"     # D61：R2/R5 前向检验的冻结日（overnight/research/R2_forward_prereg.md）；之前的信号日不记


def ensure_fwd_features(c, provider, start: str = FWD_FREEZE) -> int:
    """D61 审计副本：从冻结日起，给每条（非对照策略的）买入/加仓信号记下当天就算好的 4 个值（数据 ≤ 信号日）。
    正式检验按 R2_forward_prereg.md 在判定时从 cn_all 重算；这张表只用来核对"当天的值"和"事后重算的值"是否一致
    （行情文件偶尔会被事后改写，revquant 实测最多差 0.24pp）。不影响任何选股、清单、页面。
      ratio     = (收盘_D / 收盘_{D-1} − 1) ÷ 当天涨跌停幅度（src/limits.py，带名称和上市天数；新股不设涨跌停 → NULL）
      max20     = 截至 D 的 20 个交易日里最大的单日收盘涨幅
      abvol5_60 = 5 日平均成交额 ÷ 60 日平均成交额（成交额 = 收盘 × 成交量；provider 没有成交量时为 NULL）
      yizi      = 信号日一字板（开高低收相同）
    已记过的不重算；返回新写入的行数。"""
    from .limits import limit_pct as _lp, listed_days
    todo = {}
    for r in c.execute("""SELECT DISTINCT s.day, s.code, s.name FROM signals s
                          LEFT JOIN fwd_features f ON f.day=s.day AND f.code=s.code
                          WHERE s.action IN ('买入','加仓') AND s.strategy NOT LIKE 'ctrl_%' AND s.day >= ? AND f.code IS NULL""", (start,)):
        todo.setdefault(r["code"], []).append((r["day"], r["name"] or ""))
    rows, now = [], datetime.now().isoformat(timespec="seconds")
    for code, items in todo.items():
        bars = provider.bars(code)
        if not bars:
            continue
        vols = provider.volume(code) if hasattr(provider, "volume") else {}
        ds = sorted(bars)
        for day, name in items:
            i = bisect_right(ds, day) - 1
            if i < 1 or ds[i] != day:
                continue
            closes = [bars[d][3] for d in ds[max(0, i - 20):i + 1]]
            rets = [b / a - 1 for a, b in zip(closes[:-1], closes[1:]) if a]
            lim = _lp(code, name, day, listed_days(code, day))
            o, hi, lo, cl = bars[day]
            ratio = (rets[-1] / lim) if (rets and lim) else None
            amt = [bars[d][3] * vols[d] for d in ds[max(0, i - 59):i + 1] if d in vols]
            ab = (sum(amt[-5:]) / 5) / (sum(amt) / len(amt)) if len(amt) >= 60 and sum(amt) > 0 else None
            yz = 1 if all(math.isclose(x, cl, rel_tol=1e-10, abs_tol=1e-10) for x in (o, hi, lo)) else 0
            rows.append((day, code, name, ratio, max(rets) if len(rets) >= 20 else None, ab, yz, now))
    c.executemany("INSERT OR IGNORE INTO fwd_features VALUES(?,?,?,?,?,?,?,?)", rows)
    c.commit()
    return len(rows)


def limit_close_stats(c) -> dict:
    """收盘涨停的买入信号，次日开盘一字涨停（买不进）的比例。按 (信号日, 代码) 去重，只看已走满 1 日的。
    分三组（revquant D30 复核）：close_limit = 收盘涨停但不是一字板（清单上「今日收盘涨停」提示用的就是这一组的比例）；
    yizi = 一字板（清单原有的「一字涨停」提示）；other = 其余买入信号。三组不混在一起算。"""
    out = {}
    for key, where in (("close_limit", "l.up_close=1 AND l.yizi=0"), ("yizi", "l.up_close=1 AND l.yizi=1"), ("other", "l.up_close=0")):
        r = c.execute(f"""SELECT COUNT(*) n, SUM(m.lim = 1) blocked, COUNT(DISTINCT l.day) days FROM limit_close l
                          JOIN moves m ON m.day=l.day AND m.code=l.code AND m.h=1 WHERE {where}""").fetchone()
        n, b = r["n"] or 0, r["blocked"] or 0
        out[key] = {"n": n, "blocked": b, "days": r["days"] or 0, "rate": (b / n) if n else None}
    out["enough"] = out["close_limit"]["n"] >= MIN_SAMPLE and out["close_limit"]["days"] >= MIN_DAYS
    out["note"] = ("收盘在涨停价的买入/加仓信号（所有正式策略，按信号日+代码去重），次日开盘一字涨停买不进的比例。"
                   "close_limit=收盘涨停但非一字板，yizi=一字板，other=其余买入信号。")
    return out


# ------------------------------------------------------------ 统计
def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def baseline(c) -> dict:
    return {(r["day"], r["h"]): r["m"] for r in c.execute("SELECT day, h, AVG(ret) m FROM moves GROUP BY day, h")}


def _wilson(k: int, n: int, z: float = 1.96):
    """命中率的 95% 置信区间（Wilson）。样本少时区间会很宽，这是想让人看到的。"""
    if n <= 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, mid - half), min(1.0, mid + half)


def _day_t(ex_by_day: dict):
    """先把每个信号日的超额收益取平均，再对「日」做 t 检验。
    同一天的信号共享同一个大盘行情，不是独立样本；直接对几百条信号做 t 检验会把显著性高估很多倍。"""
    xs = [sum(v) / len(v) for v in ex_by_day.values() if v]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    sd = statistics.stdev(xs)
    return m / (sd / len(xs) ** 0.5) if sd > 0 else None


# 有效 / 反向 的结论至少要跨这么多个完整交易周（与 promotion.PROMO_MIN_WEEKS 的 12 周一致）。
# 不到之前一律显示「积累中」：13 个信号日的 5 日收益相邻重叠 4/5，实际只相当于两三个独立周。
VERDICT_MIN_WEEKS = 12

# 双侧 95% 的 t 分布分位数（自由度 1–30）；纯标准库，不依赖 scipy
_T975 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
         2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)


def _t975(df: int) -> float:
    if df < 1:
        return float("inf")
    return _T975[df - 1] if df <= len(_T975) else 1.96 + 2.4 / df      # df>30 的近似误差 < 0.005


def _cal_pos(c) -> dict:
    """信号日在「已记录交易日」序列里的位置。若中间漏记了交易日，位置差会偏小，下面的抽稀就更保守（只会多丢、不会重叠）。"""
    return {r["day"]: i for i, r in enumerate(c.execute("SELECT day FROM days ORDER BY day"))}


def _thin(days, pos: dict, h: int) -> list:
    """从信号日里按时间顺序挑出持有窗口互不重叠的日子：相邻两个被选中的日子至少隔 h 个交易日。
    h=1 时就是全部信号日；h=5 大约一周一天；h=10 大约两周一天。"""
    out, last = [], None
    for d in sorted(days):
        p = pos.get(d)
        if p is None:
            continue
        if last is None or p - last >= h:
            out.append(d)
            last = p
    return out


MIN_PERIODS_CI = 3      # 少于 3 期（自由度 < 2）时 t 分位数 ≥ 12.7，区间没有意义，干脆不给


def _period_ci(day_vals: dict, center, h: int, pos: dict, bounds=(None, None), floor=None):
    """按「期」做 t 区间：期 = 持有窗口互不重叠的信号日（_thin），每期一个当天平均值，自由度 = 期数 − 1。
    区间中心用全部信号逐条加权的点估计，宽度用抽稀后各期平均值的标准误（比用全部日子更宽、更保守）。
    为什么不用逐条 Wilson：同一天几百条信号共享同一个大盘行情，不是独立样本，Wilson 区间会窄约 3 倍、覆盖率只有 70% 多。
    为什么不用按天整块重抽样：只有 4–30 个日子时覆盖率只有 80–91%（revquant 的模拟，overnight/research/revquant/ci_coverage_sim.py）。
    bounds：区间截断到的取值范围（命中率 (0, 1)，比随机多多少 (-1, 1)）。
    floor：(下限, 上限)，区间至少要这么宽（命中率传逐条 Wilson）。各期完全一样（比如每条都命中）时样本标准差为 0，
    t 区间会缩成一个点；取两者的并集，保证不比“把信号当独立样本”的区间更窄。
    返回 (下限, 上限, 期数, 抽稀后的 t 值)。期数 < MIN_PERIODS_CI 时返回 (None, None, 期数, None)：页面显示“期数不足”。"""
    sel = _thin(day_vals.keys(), pos, h)
    xs = [day_vals[d] for d in sel]
    g = len(xs)
    if g < MIN_PERIODS_CI or center is None:
        return None, None, g, None
    sd = statistics.stdev(xs)
    se = sd / g ** 0.5
    half = _t975(g - 1) * se
    t = (sum(xs) / g) / se if se > 0 else None
    lo, hi = center - half, center + half
    if floor is not None and floor[0] is not None:
        lo, hi = min(lo, floor[0]), max(hi, floor[1])
    if bounds[0] is not None:
        lo = max(lo, bounds[0])
    if bounds[1] is not None:
        hi = min(hi, bounds[1])
    return lo, hi, g, t


def _week(day: str) -> str:
    y, w, _ = datetime.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def _complete_weeks(days, today: str | None = None) -> int:
    """信号日覆盖到的交易周数；today 所在的这一周还没结束，不算。"""
    cur = _week(today) if today else None
    return len({_week(d) for d in days if _week(d) != cur})


def random_share(c) -> dict:
    """{(day, h): (跑赢当天全市场平均的股票占比, 跑输的占比)}。
    这是「随机挑一只股票」的命中率基线：收益右偏，多数股票跑输等权平均，所以买入方向通常只有 45%–48%，不是 50%。"""
    out = {}
    for r in c.execute("""SELECT m.day, m.h, AVG(m.ret > b.m) up, AVG(m.ret < b.m) dn FROM moves m
                          JOIN (SELECT day, h, AVG(ret) m FROM moves GROUP BY day, h) b ON b.day=m.day AND b.h=m.h
                          GROUP BY m.day, m.h"""):
        out[(r["day"], r["h"])] = (r["up"], r["dn"])
    return out


def verdict(p: dict) -> str:
    """有效 / 反向 / 看不出差别 / 积累中 / 样本不足。只用于展示与反思，不会自动改任何策略。
    t 来自持有窗口互不重叠的「期」（见 _period_ci），而且要跨满 VERDICT_MIN_WEEKS 个完整交易周才下结论。"""
    if not p["enough"]:
        return "样本不足"
    if p.get("weeks", 0) < VERDICT_MIN_WEEKS:
        return "积累中"
    t = p.get("t_day")
    sgn = 1 if p["action"] in BUYS else -1
    if t is None:
        return "样本不足"
    if sgn * t >= T_SIG:
        return "有效"
    if sgn * t <= -T_SIG:
        return "反向"
    return "看不出差别"


def precision(c, strategy: str, today: str | None = None, *, tables=None) -> list[dict]:
    """每个 (动作, 持有天数)：命中率、随机基线、比随机多多少，以及按「期」算的 95% 区间（_period_ci）。
    对照基准统一是「当天全市场（有行情的个股）等权平均」；卖出/减仓按方向翻转：股票跑输平均 = 方向正确。
    hit_excess_lo/hi、mean_excess_lo/hi、edge_lo/hi 都是 t 区间（不再是逐条 Wilson）。"""
    # D93：tables=(baseline, random_share, _cal_pos) 由调用方算一次传进来（build_report 里 29 个策略共用，原来每个策略都重算一遍整表）；
    # 不传就和原来一样自己算。三张表只读，同一次报告里数据库不变，所以结果相同。
    base, rnd, pos = tables if tables is not None else (baseline(c), random_share(c), _cal_pos(c))
    out = []
    cost = ROUND_TRIP_BP / 1e4
    for action in ACTIONS:
        for h in HORIZONS:
            rows = c.execute("""SELECT s.day, m.ret, m.lim FROM signals s JOIN moves m ON m.day=s.day AND m.code=s.code AND m.h=?
                                WHERE s.strategy=? AND s.action=?""", (h, strategy, action)).fetchall()
            if not rows:
                continue
            buy = action in BUYS
            ok = [r for r in rows if r["lim"] != (1 if buy else -1)]
            ex = [r["ret"] - base[(r["day"], h)] for r in ok]
            sgn = 1 if buy else -1
            by_day = {}
            for r, e in zip(ok, ex):
                by_day.setdefault(r["day"], []).append(e)
            k = sum(1 for r in ok if sgn * r["ret"] > 0)
            ke = sum(1 for e in ex if sgn * e > 0)
            n = len(ok)
            me = _mean(ex)
            # 逐日：方向命中占比、当天随机基线、平均超额（按方向取号前）
            day_hit = {d: sum(1 for e in v if sgn * e > 0) / len(v) for d, v in by_day.items()}
            day_rnd = {d: rnd[(d, h)][0 if buy else 1] for d in by_day}
            day_edge = {d: day_hit[d] - day_rnd[d] for d in by_day}
            day_ex = {d: sum(v) / len(v) for d, v in by_day.items()}
            hit_excess = ke / n if n else None
            hit_random = (sum(day_rnd[d] * len(v) for d, v in by_day.items()) / n) if n else None
            edge = (hit_excess - hit_random) if n else None
            w_lo, w_hi = _wilson(ke, n)
            h_lo, h_hi, periods, _ = _period_ci(day_hit, hit_excess, h, pos, (0.0, 1.0), (w_lo, w_hi))
            e_lo, e_hi, _, _ = _period_ci(day_edge, edge, h, pos, (-1.0, 1.0),
                                          (w_lo - hit_random, w_hi - hit_random) if w_lo is not None else None)
            m_lo, m_hi, _, t_per = _period_ci(day_ex, me, h, pos)
            days = sorted(by_day)
            p = {"action": action, "h": h, "n": n, "days": len(days), "untradable": len(rows) - n,
                 "tradable_rate": n / len(rows) if rows else None,      # D30 可成交率：开盘一字涨/跌停成交不了的不算准确率，单独给比例
                 "periods": periods, "weeks": _complete_weeks(days, today), "weeks_needed": VERDICT_MIN_WEEKS,
                 "benchmark": "全市场等权平均",
                 "hit_raw": k / n if n else None, "hit_excess": hit_excess,
                 "hit_excess_lo": h_lo, "hit_excess_hi": h_hi,
                 "hit_random": hit_random, "edge": edge, "edge_lo": e_lo, "edge_hi": e_hi,
                 "mean_ret": _mean([r["ret"] for r in ok]), "mean_excess": me,
                 "mean_excess_lo": m_lo, "mean_excess_hi": m_hi,
                 "median_excess": statistics.median(ex) if ex else None,
                 "net_excess": (me - cost) if (me is not None and buy) else None,     # 买入类扣掉来回费用；卖出类是“躲掉的跌幅”，不扣
                 "t_day": t_per,            # 名字沿用；现在是持有窗口互不重叠的「期」上的 t（h=1 时就是按日）
                 "enough": n >= MIN_SAMPLE and len(days) >= MIN_DAYS}
            p["verdict"] = verdict(p)
            out.append(p)
    return out


def winners(c, day: str, h: int = 5) -> list[dict]:
    rets = [r["ret"] for r in c.execute("SELECT ret FROM moves WHERE day=? AND h=? AND lim!=1 AND pump=0", (day, h))]
    if len(rets) < 100:
        return []
    cut = max(sorted(rets)[int(len(rets) * WIN_PCT)], WIN_MIN_RET)
    return [dict(r) for r in c.execute("SELECT code, ret FROM moves WHERE day=? AND h=? AND lim!=1 AND pump=0 AND ret>=? ORDER BY ret DESC",
                                       (day, h, cut))]


def recall(c, day: str, strategy: str, h: int = 5):
    has = c.execute("SELECT has_state FROM days WHERE day=?", (day,)).fetchone()
    if not has or not has["has_state"]:
        return None
    cats, missed = {"已推荐": 0, "已持有": 0, "被过滤": 0, "没有信号": 0}, []
    for w in winners(c, day, h):
        sig = c.execute("SELECT * FROM signals WHERE day=? AND strategy=? AND code=?", (day, strategy, w["code"])).fetchone()
        st = c.execute("SELECT now FROM state WHERE day=? AND strategy=? AND code=?", (day, strategy, w["code"])).fetchone()
        if sig and sig["action"] in BUYS:
            cat = "已推荐" if sig["liquidity"] == "正常" and not sig["small"] else "被过滤"
        elif (sig and (sig["now"] or 0) > 0) or (st and (st["now"] or 0) > 0):
            cat = "已持有"
        else:
            cat = "没有信号"
        cats[cat] += 1
        if cat in ("被过滤", "没有信号"):
            missed.append({"code": w["code"], "name": sig["name"] if sig else None, "ret": w["ret"], "category": cat})
    total = sum(cats.values())
    n_all = c.execute("SELECT COUNT(*) n FROM state WHERE day=? AND strategy=?", (day, strategy)).fetchone()["n"]
    n_held = c.execute("SELECT COUNT(*) n FROM state WHERE day=? AND strategy=? AND (now>0 OR next>0)", (day, strategy)).fetchone()["n"]
    base = n_held / n_all if n_all else None        # 随便抽一只股票，策略碰巧持有/推荐它的概率
    rc = (cats["已推荐"] + cats["已持有"]) / total if total else None
    return {"day": day, "h": h, "winners": total, "categories": cats, "missed": missed[:30],
            "recall": rc, "base_rate": base, "lift": (rc / base) if (rc is not None and base) else None}


ACTION_EN = {"买入": "Buy", "卖出": "Sell", "加仓": "Add", "减仓": "Reduce"}


def reflect(c, main: str, prec: dict, rec: list, today: str) -> list[dict]:
    """每日反思：只写“发现了什么、建议怎么改”。策略公式、阈值都不会被它自动修改。
    每条 = {level: ok/warn/info, text, suggestion, text_en, suggestion_en}（D89：英文同样的数字）。"""
    out = []

    def add(level, text, text_en, suggestion="", suggestion_en=""):
        out.append({"level": level, "text": text, "suggestion": suggestion, "text_en": text_en, "suggestion_en": suggestion_en})

    days = [r["day"] for r in c.execute("SELECT DISTINCT day FROM done WHERE n>0 ORDER BY day")]
    if len(days) < MIN_DAYS:
        add("info", f"已核对 {len(days)} 个信号日，不到 {MIN_DAYS} 个，所有结论都只是观察。",
            f"{len(days)} signal days checked, fewer than {MIN_DAYS}; every conclusion is only an observation.",
            f"再积累 {MIN_DAYS - len(days)} 个已走满的信号日再看显著性；现在不要据此改策略。",
            f"Wait for {MIN_DAYS - len(days)} more completed signal days before judging significance; do not change the strategy on this yet.")
    for p in prec.get(main, []):
        if p["h"] not in (5, 20) or not p["enough"]:
            continue
        v, tag = p["verdict"], f"{p['action']}信号 {p['h']} 日"
        tag_en = f"{ACTION_EN.get(p['action'], p['action'])} signals, {p['h']} days"
        ex = p["mean_excess"]
        if v == "有效":
            add("ok", f"{tag}：相对全市场 {ex:+.2%}（按互不重叠的期 t={p['t_day']:.1f}），方向对。",
                f"{tag_en}: {ex:+.2%} vs the market (t={p['t_day']:.1f} on non-overlapping periods), right direction.")
            if p["net_excess"] is not None and p["net_excess"] <= 0:
                add("warn", f"{tag}：扣掉来回约 {ROUND_TRIP_BP}bp 费用后超额 {p['net_excess']:+.2%}，没有净优势。",
                    f"{tag_en}: after about {ROUND_TRIP_BP}bp round-trip costs the excess is {p['net_excess']:+.2%}, no net edge.",
                    "考虑提高 TRADE_LIST_THRESHOLD（少给小幅加减仓信号），降低换手。",
                    "Consider raising TRADE_LIST_THRESHOLD (fewer small add/reduce signals) to cut turnover.")
        elif v == "反向":
            add("warn", f"{tag}：相对全市场 {ex:+.2%}（按互不重叠的期 t={p['t_day']:.1f}），方向和信号相反。",
                f"{tag_en}: {ex:+.2%} vs the market (t={p['t_day']:.1f} on non-overlapping periods), opposite to the signal.",
                "先检查该类信号是否集中在某类股票（小盘、低流动性、涨停附近）；若是，建议加过滤条件，需你同意后才改。",
                "First check whether these signals cluster in certain stocks (small caps, low liquidity, near the limit); if so, a filter may help, but only with your approval.")
        elif v == "积累中":
            add("info", f"{tag}：相对全市场 {ex:+.2%}；才 {p['weeks']} 个完整交易周，满 {p['weeks_needed']} 周前不下结论。",
                f"{tag_en}: {ex:+.2%} vs the market; only {p['weeks']} complete weeks, no conclusion before {p['weeks_needed']} weeks.")
        else:
            add("info", f"{tag}：相对全市场 {ex:+.2%}，和随机差别不显著。",
                f"{tag_en}: {ex:+.2%} vs the market, not significantly different from random.")
    if rec:
        r = rec[-1]
        if r.get("lift") is not None:
            lvl = "ok" if r["lift"] >= 1.2 else ("warn" if r["lift"] < 0.9 else "info")
            add(lvl, f"{r['day']} 之后 {r['h']} 日涨幅居前的 {r['winners']} 只，被推荐/已持有占 {r['recall']:.0%}，"
                     f"随机持有的基准是 {r['base_rate']:.0%}（提升 {r['lift']:.2f} 倍）。",
                f"Of the {r['winners']} top gainers over the {r['h']} days after {r['day']}, {r['recall']:.0%} were recommended or held; "
                f"a random holding would catch {r['base_rate']:.0%} (lift {r['lift']:.2f}x).",
                "提升倍数长期 <1 说明策略没比随机更会选涨得多的股票。" if lvl == "warn" else "",
                "A lift below 1 for long means the strategy is no better than random at catching big gainers." if lvl == "warn" else "")
        cats = r.get("categories", {})
        if cats.get("被过滤", 0) >= max(3, cats.get("已推荐", 0)):
            add("warn", f"漏掉的涨幅居前股票里 {cats['被过滤']} 只是被流动性/小目标仓位过滤挡掉的。",
                f"{cats['被过滤']} of the missed top gainers were blocked by the liquidity / small-target filters.",
                "过滤规则可能过严；可对比 TRADE_LIST_MIN_TURNOVER 放宽后的结果，需你同意后才改。",
                "The filters may be too strict; compare with a looser TRADE_LIST_MIN_TURNOVER, only with your approval.")
    add("info", "本节只是统计与建议；策略公式和阈值不会自动修改。",
        "This section is statistics and suggestions only; strategy formulas and thresholds are never changed automatically.")
    return out


def build_report(c, today: str, main_strategy: str = "blend2") -> dict:
    strategies = [r["strategy"] for r in c.execute("SELECT DISTINCT strategy FROM signals ORDER BY strategy")]
    days = [r["day"] for r in c.execute("SELECT DISTINCT day FROM done WHERE n>0 ORDER BY day")]
    rec = [x for x in (recall(c, d, main_strategy) for d in days) if x]
    notes, notes_en = [], []          # D89：notes_en 与 notes 一一对应
    tables = (baseline(c), random_share(c), _cal_pos(c))     # D93：所有策略共用一份
    prec_main = precision(c, main_strategy, today, tables=tables)
    for p in prec_main:
        if p["h"] in (5, 20) and p["action"] in BUYS + ("卖出",):
            nm = {5: "5 日", 20: "20 日"}[p["h"]]
            ae = ACTION_EN.get(p["action"], p["action"])
            verdict_en = {"有效": "significant (right direction)", "反向": "significant (wrong direction)", "看不出差别": "no clear difference", "积累中": "accumulating", "样本不足": "sample too small"}.get(p["verdict"], p["verdict"])
            if p["enough"]:
                notes.append(f"{main_strategy} {p['action']}信号{nm}：{p['n']} 条，方向正确 {p['hit_excess']:.0%}（随机挑约 {p['hit_random']:.0%}），"
                             f"相对全市场平均 {p['mean_excess']:+.2%}，结论：{p['verdict']}" + (f"（第 {p['weeks']}/{p['weeks_needed']} 周）" if p['verdict'] == '积累中' else '') + "。")
                notes_en.append(f"{main_strategy} {ae} signals, {p['h']} days: {p['n']} signals, right direction {p['hit_excess']:.0%} (random about {p['hit_random']:.0%}), "
                                f"{p['mean_excess']:+.2%} vs the market average; verdict: {verdict_en}" + (f" (week {p['weeks']}/{p['weeks_needed']})" if p['verdict'] == '积累中' else '') + ".")
            else:
                notes.append(f"{main_strategy} {p['action']}信号{nm}：{p['n']} 条、只跨 {p['days']} 个信号日，样本不足（需要至少 {MIN_SAMPLE} 条且跨 {MIN_DAYS} 个信号日），仅作观察，不据此改策略。")
                notes_en.append(f"{main_strategy} {ae} signals, {p['h']} days: {p['n']} signals over only {p['days']} signal days; sample too small "
                                f"(needs at least {MIN_SAMPLE} signals over {MIN_DAYS} signal days); observation only, no strategy change.")
    if rec:
        last = rec[-1]
        cat_en = {"已推荐": "recommended", "已持有": "held", "被过滤": "filtered", "没有信号": "no signal"}
        notes.append(f"{last['day']} 之后 5 日涨幅居前的 {last['winners']} 只里：" + "、".join(f"{k} {v}" for k, v in last["categories"].items()) + "。")
        notes_en.append(f"Of the {last['winners']} top 5-day gainers after {last['day']}: " + ", ".join(f"{cat_en.get(k, k)} {v}" for k, v in last["categories"].items()) + ".")
    elif days:
        notes.append("已有结果的信号日缺少当时的持仓状态，暂时不能算召回率；从今天起每天都会记录。")
        notes_en.append("Signal days with results lack the holdings state of that day, so recall can't be computed yet; it is recorded daily from today.")
    if not days:
        notes.append("还没有走满一个交易日的信号，等下一个交易日收盘后才会有第一批核对结果。")
        notes_en.append("No signal has completed a trading day yet; the first check comes after the next close.")
    prec_all = {s_: precision(c, s_, today, tables=tables) for s_ in strategies}
    reflection = reflect(c, main_strategy, prec_all, rec, today)
    return {"today": today, "reflection": reflection, "signal_days": [r["day"] for r in c.execute("SELECT day FROM days ORDER BY day")],
            "evaluated_days": days, "min_sample": MIN_SAMPLE, "main": main_strategy,
            "precision": prec_all, "recall": rec, "notes": notes, "notes_en": notes_en,
            "caveats_en": ["These are statistics, not forecasts; small samples swing a lot.",
                           "Entry = next day's open; 'vs the market' = that day's equal-weight average of all A-share stocks, before costs; the net column deducts about 16bp round trip.",
                           "95% ranges and significance use non-overlapping holding periods (about one per week for 5 days); random picks beat the average less than 50% of the time, so read 'vs random'.",
                           f"No 'significant' verdict (right or wrong direction) before {VERDICT_MIN_WEEKS} complete trading weeks; until then it shows 'accumulating'.",
                           "Consecutive limit-up / limit-up-open stocks are excluded from recall."],
            "caveats": ["结果是统计，不是预测；样本小时波动很大。", "入场=次日开盘价；“相对全市场”=当天全市场个股等权平均，未扣费用，净超额一栏扣了来回约 16bp。",
                        "95% 区间和显著性按持有窗口互不重叠的“期”计算（5 日约一周一期）；随机挑股的命中率本来就不到 50%，看“比随机多多少”。",
                        f"满 {VERDICT_MIN_WEEKS} 个完整交易周之前不下“有效/反向”结论，只显示“积累中”。",
                        "连板/开盘涨停的股票已从召回统计里剔除。"]}


# ------------------------------------------------------------ 各策略准确率榜 + 自动选主策略
MAIN_FILE = ROOT / "data" / "main_strategy.json"
DEFAULT_MAIN = "blend2"
DEFAULT_PROMOTED_ON = "2026-10-20"      # 晋升检验上线日：blend2 的正式成绩从这天之后的信号日起算
BOARD_H = (1, 5)


def scored(c, strategy: str, h: int, base=None):
    """[(信号日, 得分, 跑赢全市场?)]：得分 = 方向 × (收益 − 当天全市场平均)，买入类再扣来回费用；开盘一字涨/跌停的不算。"""
    base = base or baseline(c)
    cost = ROUND_TRIP_BP / 1e4
    out = []
    for r in c.execute("""SELECT s.day, s.action, m.ret, m.lim FROM signals s JOIN moves m ON m.day=s.day AND m.code=s.code AND m.h=?
                          WHERE s.strategy=?""", (h, strategy)):
        buy = r["action"] in BUYS
        if r["lim"] == (1 if buy else -1) or (r["day"], h) not in base:
            continue
        x = (1 if buy else -1) * (r["ret"] - base[(r["day"], h)]) - (cost if buy else 0.0)
        out.append((r["day"], x, x + (cost if buy else 0.0) > 0))
    return out


def strategy_board(c) -> dict:
    """每个策略：按天的准确率、95% 置信区间，以及累计准确率每天提高/降低了多少。
    准确率 = 跑赢全市场的信号占比（命中率）；得分 = 每条信号平均比全市场多赚多少（扣费后）。
    样本不足（< BOARD_MIN_N 条或 < BOARD_MIN_DAYS 个信号日）的标 enough=False，不排名。
    置信区间按持有窗口互不重叠的「期」算 t 区间（_period_ci），不把同一天的信号当独立样本；晋升仍看 promotion 里的按周检验。"""
    from . import promotion as PR
    base = baseline(c)
    pos = _cal_pos(c)
    strategies = [r["strategy"] for r in c.execute("SELECT DISTINCT strategy FROM signals ORDER BY strategy")]
    out = {}
    for st in strategies:
        per_h = {}
        for h in BOARD_H:
            rows = scored(c, st, h, base)
            days = sorted({d for d, _, _ in rows})
            daily, cn, cs, ck = [], 0, 0.0, 0
            prev_hit = prev_score = None
            for d in days:
                xs = [x for dd, x, _ in rows if dd == d]
                ks = sum(1 for dd, _, k in rows if dd == d and k)
                cn += len(xs); cs += sum(xs); ck += ks
                hit, score = ck / cn, cs / cn
                daily.append({"day": d, "n": len(xs), "hit": ks / len(xs), "score": sum(xs) / len(xs),
                              "cum_n": cn, "cum_hit": hit, "cum_score": score,
                              "d_hit": None if prev_hit is None else hit - prev_hit,
                              "d_score": None if prev_score is None else score - prev_score})
                prev_hit, prev_score = hit, score
            day_hit = {dd["day"]: dd["hit"] for dd in daily}
            lo, hi, periods, _ = _period_ci(day_hit, ck / cn if cn else None, h, pos, (0.0, 1.0), _wilson(ck, cn))      # 不再用逐条 Wilson（见 _period_ci）
            per_h[h] = {"n": cn, "days": len(days), "periods": periods, "hit": ck / cn if cn else None, "hit_lo": lo, "hit_hi": hi, "score": cs / cn if cn else None,
                        "enough": cn >= PR.BOARD_MIN_N and len(days) >= PR.BOARD_MIN_DAYS, "rank": None, "daily": daily}
        out[st] = {f"h{h}": per_h[h] for h in BOARD_H}
    for hk in ("h1", "h5"):      # 只给样本够的、非对照的排名
        ranked = sorted((k for k, v in out.items() if v[hk]["enough"] and not PR.is_control(k)), key=lambda k: -out[k][hk]["score"])
        for i, k in enumerate(ranked, start=1):
            out[k][hk]["rank"] = i
    return out


def _load_main(p: Path, board: dict) -> dict:
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
        cur = st["main"] if st.get("main") in board else DEFAULT_MAIN
        return {"main": cur, "history": st.get("history", []), "promoted_on": st.get("promoted_on") or DEFAULT_PROMOTED_ON,
                "default": bool(st.get("default", cur == DEFAULT_MAIN and not st.get("history")))}
    except (OSError, ValueError, KeyError, AttributeError):
        return {"main": DEFAULT_MAIN, "history": [], "promoted_on": DEFAULT_PROMOTED_ON, "default": True}


def choose_main(c, board: dict, today: str, path=None, apply: bool = True, status: dict | None = None) -> dict:
    """主策略晋升。默认 blend2（“默认策略，未经晋升检验”）；只有同时满足 promotion.py 里的全部门槛才换：
      ≥12 个完整交易周、≥500 条信号、连续 4 个周检查点通过 Holm 修正后的“显著高于匹配随机对照”、
      相对现任（现任已不通过，或周配对平均多 0.3% 且 t≥2）、现任任期 ≥ 12 周。
    样本不足、没有通过的策略一律不换（没有“现任样本不足就换成得分最高者”的旁路）。只有正式的策略能当主策略。
    晋升日期写进 promoted_on；晋升之后（信号日 > promoted_on）的表现才算正式成绩。只决定今日买卖默认显示哪个策略，不改任何策略公式。"""
    from . import promotion as PR
    from . import shadow as SH
    p = Path(path) if path else MAIN_FILE
    st = _load_main(p, board)
    cur, hist = st["main"], st["history"]
    promoted_on, is_default = st["promoted_on"], st["default"]
    status = status or {"strategies": {}, "eligible": [], "m": PR.n_hypotheses(c), "message": "尚无策略通过晋升检验", "any_passed": False}
    strat = status.get("strategies", {})
    new, reason = cur, ""
    tenure = (datetime.fromisoformat(today) - datetime.fromisoformat(promoted_on)).days
    eligible = [k for k in status.get("eligible", []) if k != cur and k not in SH.CANDIDATES and k in board]
    if not eligible:
        n_ok = sum(1 for v in strat.values() if v.get("holm_pass"))
        reason = (f"{cur} 继续任主策略（{'默认策略，未经晋升检验' if is_default else '晋升于 ' + promoted_on}）。"
                  f"{status.get('message', '尚无策略通过晋升检验')}；晋升需要 ≥{PR.PROMO_MIN_WEEKS} 个完整交易周、≥{PR.PROMO_MIN_N} 条信号、"
                  f"连续 {PR.PROMO_STREAK} 个周检查点通过多重检验修正（当前 {n_ok} 个策略过了当周检查）。")
    elif tenure < PR.MIN_TENURE_DAYS:
        reason = f"{'、'.join(eligible)} 已通过检验，但现任 {cur} 晋升于 {promoted_on}，任期不满 {PR.MIN_TENURE_DAYS // 7} 周（{tenure} 天），暂不换。"
    else:
        leader = max(eligible, key=lambda k: strat[k]["stat"])
        mean, t, nw = PR.paired_weekly(c, leader, cur, today)
        cur_ok = bool(strat.get(cur, {}).get("holm_pass"))
        beats = (not cur_ok) or (mean is not None and mean >= PR.BEAT_GAIN and t is not None and t >= PR.BEAT_T)
        if beats:
            new = leader
            reason = (f"{leader} 连续 {strat[leader]['streak']} 个周检查点显著高于随机对照（按周平均得分比随机多 {strat[leader]['advantage']:+.2%}，"
                      f"经验 p={strat[leader]['p']:.4f}，m={status['m']} 个假设 Holm 修正后仍通过），" +
                      ("现任 " + cur + " 已不通过检验" if not cur_ok else f"且比现任 {cur} 每条信号平均多 {mean:+.2%}（{nw} 周配对 t={t:.1f}）") + "，晋升为主策略。")
        else:
            reason = (f"{leader} 已通过检验，但相对现任 {cur} 优势不够（周配对平均 {('—' if mean is None else format(mean, '+.2%'))}，"
                      f"需要 ≥{PR.BEAT_GAIN:.1%} 且 t≥{PR.BEAT_T:.0f}），先不换。")
    if new != cur:
        hist = hist + [{"day": today, "from": cur, "to": new, "why": reason, "promoted_on": today}]
        promoted_on, is_default = today, False
    n_cand = status.get("m", PR.n_hypotheses(c))
    res = {"main": new, "reason": reason, "history": hist[-30:], "day": today, "promoted_on": promoted_on, "default": is_default,
           "default_note": "默认策略，未经晋升检验" if is_default else "", "n_candidates": n_cand,
           "any_passed": bool(status.get("any_passed")), "message": status.get("message", ""),
           "record": {"strategy": new, "promoted_on": promoted_on,
                      "pre": PR.segment(c, new, today, None, promoted_on),          # 晋升前：选中它的依据（样本内），不算正式成绩
                      "post": PR.segment(c, new, today, promoted_on, None),         # 晋升后：正式成绩
                      "note": "正式成绩从晋升日之后的信号日起算；晋升日及以前的是选中它的依据，不能当作战绩。"}}
    res = PR.jsonable(res)
    if apply:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        import os
        os.replace(tmp, p)
    return res


# ------------------------------------------------------------ 写库前的自动备份
BACKUP_DIR = ROOT / "data" / "backups" / "signal_ledger"
BACKUP_KEEP = 7


def _snapshot_files(db: Path, out: Path, main_file: Path, params: Path) -> list[tuple[Path, str]]:
    """要备份的文件：(源路径, 快照里的相对路径)。库本身必须在第一位；其余是每日任务会整体覆盖的小文件。"""
    files = [(db, "signal_ledger.db"), (main_file, "main_strategy.json"), (params, "auto_params.json")]
    for name in ("latest.json", "tune_log.jsonl", "reflection.jsonl"):
        files.append((out / name, f"ledger/{name}"))
    for f in sorted(out.glob("????-??-??.json")):                  # 每个信号日的报告（2026-10-20.json 这类）
        files.append((f, f"ledger/{f.name}"))
    return files


def backup_snapshot(db=None, folder=None, keep: int = BACKUP_KEEP, now: datetime | None = None, out=None, main_file=None, params=None):
    """每日任务写账本之前先做一份整体快照：目录 <folder>/YYYYMMDD/，里面是
         signal_ledger.db（sqlite 在线备份接口，一致性快照 + 完整性检查）
         ledger/latest.json、ledger/YYYY-MM-DD.json、ledger/tune_log.jsonl、ledger/reflection.jsonl、main_strategy.json、auto_params.json（原样拷贝）
         MANIFEST.json（每个文件的大小和 sha256，以及没备份成功的文件）
       每个日历日一份（当天第一次运行时的状态，同一天再运行不覆盖），只保留最近 keep 份。
       库备份失败 → 整份快照作废；小文件不存在就跳过、拷贝失败只记入清单和日志，其余照常。
       成功返回快照目录；当天已有、账本库还不存在、或失败返回 None。任何情况下都不抛异常（不能因为备份拖垮每日任务）。"""
    import hashlib
    import logging
    import os
    import re
    import shutil
    part = None
    try:
        from . import auto_tune
        db = Path(db) if db else DB
        if not db.exists():
            return None
        dest_dir = Path(folder) if folder else BACKUP_DIR
        files = _snapshot_files(db, Path(out) if out else OUT, Path(main_file) if main_file else MAIN_FILE,
                                Path(params) if params else auto_tune.PARAMS)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / (now or datetime.now()).strftime("%Y%m%d")
        if dest.exists():
            return None                       # 当天已经有了：它是当天第一次写入之前的状态，不要用后来的状态覆盖
        part = dest_dir / (dest.name + ".part")
        if part.exists():
            shutil.rmtree(part)
        part.mkdir()
        manifest = {"created_at": datetime.now().isoformat(timespec="seconds"), "files": {}, "failed": {}}
        for i, (src, rel) in enumerate(files):
            if not src.exists():
                continue
            target = part / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if i == 0:
                    con, outc = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=30), sqlite3.connect(target)
                    try:
                        con.backup(outc)
                    finally:
                        outc.close(); con.close()
                    chk = sqlite3.connect(target)
                    try:
                        if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise RuntimeError("备份文件完整性检查失败")
                    finally:
                        chk.close()
                else:
                    shutil.copy2(src, target)
                manifest["files"][rel] = {"bytes": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
            except Exception as e:
                if i == 0:
                    raise                      # 账本库没备份成功就不算快照
                logging.exception("账本快照：%s 备份失败（其余文件照常）", rel)
                manifest["failed"][rel] = f"{type(e).__name__}: {e}"
                target.unlink(missing_ok=True)
        (part / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(part, dest)
        part = None
        for old in sorted(d for d in dest_dir.iterdir() if d.is_dir() and re.fullmatch(r"\d{8}", d.name))[:-keep]:     # 目录名是日期，按名字排序就是按时间排序
            shutil.rmtree(old)
        return dest
    except Exception:
        logging.exception("信号核对账本备份失败（继续运行，但这次写入前没有新备份）")
        if part is not None:
            shutil.rmtree(part, ignore_errors=True)       # 不留半成品
        return None


# ------------------------------------------------------------ 每日入口
def run(payload: dict, *, provider, cal: list[str], state_rows=None, db=None, archive: Path | None = None, out: Path | None = None,
        tune_state=None, tune_log=None, tune_apply: bool = True, main_file=None) -> dict:
    with _lock:
        c = connect(db)
        try:
            if archive:
                backfill_archive(c, archive)
            record_day(c, payload, state_rows)
            try:
                from . import shadow
                shadow.record_signals(c, payload["data_date"])
            except Exception:
                pass
            evaluate(c, provider, cal, payload["data_date"])
            try:      # D30：收盘涨停买入信号的次日可成交率；失败不影响核对本身
                ensure_limit_close(c, provider)
            except Exception:
                pass
            try:      # D61：R2/R5 前向检验的当天值审计副本；失败不影响核对本身
                ensure_fwd_features(c, provider)
            except Exception:
                pass
            status = None
            try:   # 晋升检验：匹配的随机对照（1000 次）+ Holm 修正；失败时不晋升（主策略保持不变）
                from . import promotion
                promotion.register(c)
                promotion.ensure_null(c)
                status = promotion.promotion_status(c, payload["data_date"])
            except Exception as e:
                status = {"error": f"{type(e).__name__}: {e}", "strategies": {}, "eligible": [], "any_passed": False,
                          "message": f"晋升检验没有运行（{type(e).__name__}），主策略保持不变"}
            board = strategy_board(c)
            pick = choose_main(c, board, payload["data_date"], path=main_file, status=status)
            rep = build_report(c, payload["data_date"], pick["main"])
            rep["board"], rep["main_pick"] = board, pick
            from . import promotion as _pr
            rep["promotion"] = _pr.jsonable(status)
            try:
                rep["limit_close"] = limit_close_stats(c)
            except Exception as e:
                rep["limit_close"] = {"error": f"{type(e).__name__}: {e}"}
            try:
                from . import shadow
                rep["labels"] = shadow.LABELS
                rep["families"] = shadow.FAMILY
            except Exception:
                pass
            try:   # 自我改进闭环：任何失败都只记录，不影响核对本身
                from . import auto_tune
                tune = auto_tune.tune(c, payload["data_date"], auto_tune.TARGET, state_path=tune_state, log_path=tune_log, apply=tune_apply)
                rep["tune"] = tune
                rep["reflection"].insert(0, {"level": {"adopt": "warn", "switch": "warn"}.get(tune["action"], "info"),
                                             "text": "自动调参：" + (tune["why"] or "保持当前参数。") + f"（当前 阈值 {tune['active_after']['thr']:.0%}、小仓位线 {tune['active_after']['mt']:.0%}）",
                                             "suggestion": "", "suggestion_en": "",
                                             "text_en": f"Auto-tune: {tune['action']} (current threshold {tune['active_after']['thr']:.0%}, "
                                                        f"small-position line {tune['active_after']['mt']:.0%}); parameters stay frozen until enough complete weeks."})
            except Exception as e:
                rep["tune"] = {"error": f"{type(e).__name__}: {e}"}
        finally:
            c.close()
    folder = Path(out) if out else OUT
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "latest.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / f"{payload['data_date']}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    # 反思历史：每个信号日一行，重复运行会覆盖当天那行，不会越写越多
    hist = folder / "reflection.jsonl"
    keep = []
    if hist.exists():
        for ln in hist.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(ln).get("day") != payload["data_date"]:
                    keep.append(ln)
            except ValueError:
                pass
    keep.append(json.dumps({"day": payload["data_date"], "items": rep.get("reflection", [])}, ensure_ascii=False))
    hist.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return rep


class LocalProvider:
    """默认行情来源：本地日线缓存（只读）。"""
    def __init__(self):
        from . import allmarket
        self.am = allmarket

    def codes(self):
        return self.am.cached_codes()

    def bars(self, code):
        df = self.am._drop_unfinished(self.am.load(code))
        if df is None or len(df) == 0:
            return {}
        return {d: (o, h, l, cl) for d, o, h, l, cl in zip(df.index.strftime("%Y-%m-%d"), df.Open.astype(float), df.High.astype(float),
                                                          df.Low.astype(float), df.Close.astype(float))}

    def volume(self, code):
        """D61：成交量（只用于 fwd_features 的成交额比值）。"""
        df = self.am._drop_unfinished(self.am.load(code))
        if df is None or len(df) == 0 or "Volume" not in df:
            return {}
        return dict(zip(df.index.strftime("%Y-%m-%d"), df.Volume.astype(float)))


def read_state():
    import pandas as pd
    s = pd.read_parquet(ROOT / "data" / "daily" / "scan_cn.parquet", columns=["code", "strategy", "now", "next"])
    return [(r.strategy, r.code, float(r.now), float(r.next)) for r in s.itertuples() if r.now == r.now and r.next == r.next]


def main():
    from . import turnover
    backup_snapshot()    # 写账本（含下面 run 里的 record_day / evaluate / 晋升检验 / 报告和主策略文件）之前先做整体快照
    payload = json.loads((ROOT / "data" / "daily" / "trade_list_cn.json").read_text(encoding="utf-8"))
    cal = [d.isoformat() for d in turnover.cached_calendar()]
    try:   # 只有扫描结果的日期和清单日期一致时，才把它当作当天的持仓状态
        same = json.loads((ROOT / "data" / "daily" / "scan_cn_meta.json").read_text(encoding="utf-8")).get("data_date") == payload["data_date"]
    except Exception:
        same = False
    state = read_state() if same else None
    if state:
        try:   # 影子候选：和正式策略用同一批股票（已排除 ST 和次新股）
            from . import shadow
            codes = sorted({r[1] for r in state})
            state = state + shadow.compute_all(codes, payload["data_date"])
        except Exception as e:
            print("影子候选计算失败（不影响核对）：", e)
    rep = run(payload, provider=LocalProvider(), cal=cal, state_rows=state,
              archive=ROOT / "data" / "daily" / "trade_list")
    print("\n".join(rep["notes"]))


if __name__ == "__main__":
    main()
