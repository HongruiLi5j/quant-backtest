"""自我改进闭环：每天用「同一天的真实结果」并行比较一组备选参数，满足条件才自动换，换了以后继续盯，变差就淘汰、换到当前最好的备选，一直往更准的方向找。

只允许自动调两个“显示/分组层”的参数（硬上下限见 BOUNDS，只会比默认更严、不会更松）：
  thr —— 目标仓位变化超过多少个百分点才算加仓/减仓（默认 0.10）
  mt  —— 买入/加仓的目标仓位低于多少算“小仓位”、不进主清单（默认 0.05）
策略公式、流动性过滤等其他东西一律不自动改（只会在反思里提建议，要用户同意）。

怎么比（避免“事后看涨了就说有效”）：
  1. 备选清单不是另外跑策略，而是用当天全市场的 (今日仓位, 明日仓位) 按备选参数重新分类，和真实行情逐只对上。
     所以每个备选和当前参数看的是同一天、同一批股票、同一个全市场基准，只差参数。
  2. 每个信号的得分 = 方向 × (5 日收益 − 当天全市场平均)；买入类再扣来回约 16bp 费用；开盘一字涨/跌停的不算。
  3. 按「日」配对：备选每天的平均得分 − 当前参数每天的平均得分，再对“日”做 t 检验。
  4. 换参数的条件（缺一不可）：已有 ≥ MIN_DAYS 个可核对的日子、配对差的平均 ≥ MIN_GAIN、按周 t ≥ 11 个备选的 Bonferroni 临界值（T_ADOPT 仅保留作旧版参考）、
     备选保留的信号数 ≥ 当前的 MIN_COVERAGE（不能靠“少发信号”刷准确率）、不在冷却期、没被拉黑。
  5. 换了以后，只用换之后才发生的新日子盯：新旧参数的配对差连续 ROLLBACK_DAYS 天平均为负就淘汰这组参数（拉黑 BAN_DAYS 天），换到现有证据里最好的备选（不一定是旧的），继续找。
每一次决定（包括“为什么没换”）都写进 data/daily/ledger/tune_log.jsonl。

2026-10-20 起的收紧（防止对 12 组备选做数据窥探）：
  - 冻结期：可核对的完整交易周不足 FREEZE_WEEKS=20 周之前一律不调，参数固定在 DEFAULT（thr=0.10、mt=0.05）。
  - 采用判断按「周」聚合（相邻两天的 5 日收益重叠 4/5，按日当独立样本会高估显著性），t 门槛按 11 个备选做 Bonferroni 并考虑自由度。
  - 评估对象固定为 blend2（TARGET），不再跟着主策略变，证据序列不会因为换主策略而断。
"""
from __future__ import annotations

import json
import os
import statistics
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARAMS = ROOT / "data" / "auto_params.json"
LOG = ROOT / "data" / "daily" / "ledger" / "tune_log.jsonl"

DEFAULT = {"thr": 0.10, "mt": 0.05}
THR_GRID = (0.10, 0.15, 0.20, 0.30)
MT_GRID = (0.05, 0.10, 0.15)
BOUNDS = {"thr": (0.10, 0.30), "mt": (0.05, 0.15)}
H = 5
MIN_DAYS = 10
FREEZE_WEEKS = 20      # 完整交易周不足这个数就不调参
FAMILY_ALPHA = 0.05
TARGET = "blend2"
MIN_GAIN = 0.002
T_ADOPT = 2.5
MIN_COVERAGE = 0.5
COOLDOWN_DAYS = 5
ROLLBACK_DAYS = 5
CONFIRM_DAYS = 10
BAN_DAYS = 30
COST = 16 / 1e4
EPS = 1e-9
BUYS = ("买入", "加仓")


def _classify(now, nxt, thr):
    if now is None or nxt is None or now < 0 or nxt < 0:
        return None
    if now < EPS and nxt >= EPS:
        return "买入"
    if now >= EPS and nxt < EPS:
        return "卖出"
    if now >= EPS and nxt >= EPS:
        d = nxt - now
        if abs(d) > thr:
            return "加仓" if d > 0 else "减仓"
    return None


def clamp(p: dict) -> dict:
    """任何来源的参数都先过硬上下限，文件被改坏也不会出事。"""
    out = dict(DEFAULT)
    try:
        for k, (lo, hi) in BOUNDS.items():
            v = float(p.get(k, DEFAULT[k]))
            if lo <= v <= hi:
                out[k] = v
    except (TypeError, ValueError, AttributeError):
        return dict(DEFAULT)
    return out


def load_state(path=None) -> dict:
    p = Path(path) if path else PARAMS
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
        s["active"] = clamp(s.get("active") or {})
        s.setdefault("history", [])
        s.setdefault("banned", {})
        return s
    except (OSError, ValueError, AttributeError):
        return {"active": dict(DEFAULT), "history": [], "banned": {}}


def save_state(s: dict, path=None):
    p = Path(path) if path else PARAMS
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def key(p: dict) -> str:
    return f"thr{p['thr']:.2f}_mt{p['mt']:.2f}"


def grid():
    return [{"thr": t, "mt": m} for t in THR_GRID for m in MT_GRID]


# ------------------------------------------------------------ 打分
def scorable_days(c, strategy: str) -> list[str]:
    """既有当时持仓状态、又已走满 H 个交易日的信号日。"""
    return [r["day"] for r in c.execute(
        "SELECT d.day FROM days d JOIN done x ON x.day=d.day AND x.h=? WHERE d.has_state=1 AND x.n>0 ORDER BY d.day", (H,))]


def day_scores(c, day: str, strategy: str, variants: list[dict]) -> dict:
    """{变体key: (平均得分, 保留信号数)}。得分见模块说明第 2 条。"""
    base = c.execute("SELECT AVG(ret) m FROM moves WHERE day=? AND h=?", (day, H)).fetchone()["m"]
    if base is None:
        return {}
    ret = {r["code"]: (r["ret"], r["lim"]) for r in c.execute("SELECT code, ret, lim FROM moves WHERE day=? AND h=?", (day, H))}
    st = c.execute("SELECT code, now, next FROM state WHERE day=? AND strategy=?", (day, strategy)).fetchall()
    out = {}
    for v in variants:
        xs = []
        for r in st:
            a = _classify(r["now"], r["next"], v["thr"])
            if not a or r["code"] not in ret:
                continue
            if a in BUYS and r["next"] < v["mt"]:
                continue                      # 小仓位：不进主清单
            rr, lim = ret[r["code"]]
            buy = a in BUYS
            if lim == (1 if buy else -1):
                continue                      # 开盘一字涨停买不进 / 跌停卖不出
            x = (1 if buy else -1) * (rr - base) - (COST if buy else 0.0)
            xs.append(x)
        if xs:
            out[key(v)] = (sum(xs) / len(xs), len(xs))
    return out


def _week(day: str) -> str:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def complete_weeks_n(days: list[str], today: str) -> int:
    """可核对信号日覆盖到的、已经结束的交易周数（当前这一周不算）。"""
    cur = _week(today)
    return len({_week(d) for d in days if _week(d) < cur})


def t_crit(alpha: float, df: int) -> float:
    """单侧 t 临界值的近似（Cornish-Fisher）：z + (z³+z)/(4·df)。df 小时比正态更严。"""
    z = statistics.NormalDist().inv_cdf(1 - alpha)
    return z + (z ** 3 + z) / (4 * max(df, 1))


def paired(scores: dict, days: list[str], a: str, b: str, weekly: bool = False):
    """a 相对 b 的逐日配对差。返回 (平均差, t, 天数, 覆盖率)。weekly=True 时先把每周的逐日差取平均，再对「周」做 t，返回的第三项是周数。"""
    diffs, cov, wk = [], [], {}
    for d in days:
        s = scores.get(d, {})
        if a in s and b in s:
            diffs.append(s[a][0] - s[b][0])
            wk.setdefault(_week(d), []).append(s[a][0] - s[b][0])
            cov.append(s[a][1] / s[b][1] if s[b][1] else 0.0)
    if weekly:
        diffs = [sum(v) / len(v) for _, v in sorted(wk.items())]
    if not diffs:
        return None, None, 0, None
    m = sum(diffs) / len(diffs)
    t = None
    if len(diffs) >= 2:
        sd = statistics.stdev(diffs)
        t = m / (sd / len(diffs) ** 0.5) if sd > 0 else (float("inf") if m > 0 else None)
    return m, t, len(diffs), sum(cov) / len(cov)


# ------------------------------------------------------------ 每日决定
def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def tune(c, today: str, strategy: str = "blend2", state_path=None, log_path=None, apply: bool = True) -> dict:
    s = load_state(state_path)
    act = s["active"]
    days = scorable_days(c, strategy)
    vs = grid()
    scores = {d: day_scores(c, d, strategy, vs) for d in days}
    ak = key(act)
    n_weeks = complete_weeks_n(days, today)
    t_need = t_crit(FAMILY_ALPHA / (len(vs) - 1), n_weeks - 1)            # 11 个备选做 Bonferroni，周数少时更严
    decision = {"day": today, "active": act, "scorable_days": len(days), "weeks": n_weeks, "freeze_weeks": FREEZE_WEEKS,
                "t_required": t_need, "action": "keep", "why": "", "candidates": []}
    if n_weeks < FREEZE_WEEKS and act != DEFAULT:        # 冻结期内参数固定在默认值
        s["history"].append({"event": "freeze_reset", "day": today, "from": act, "to": dict(DEFAULT)})
        s["active"] = act = dict(DEFAULT)
        ak = key(act)
        decision["active"] = act

    for v in vs:
        k = key(v)
        m, t, n, cov = paired(scores, days, k, ak, weekly=True)         # 按周聚合：相邻两天的 5 日收益重叠，按日会高估显著性
        decision["candidates"].append({"params": v, "key": k, "gain": m, "t": t, "days": len(days), "weeks": n, "coverage": cov,
                                       "banned": k in s["banned"] and _days_between(s["banned"][k], today) < BAN_DAYS})

    last = s["history"][-1] if s["history"] else None
    # ---- 已换过参数：只用换之后的新日子盯，变差就退回
    if last and last.get("event") == "adopt" and not last.get("closed"):
        post = [d for d in days if d > last["day"]]
        prev = last["from"]
        m, t, n, _ = paired(scores, post, key(act), key(prev))
        decision["watch"] = {"since": last["day"], "post_days": n, "gain_vs_prev": m, "t": t}
        if n >= ROLLBACK_DAYS and m is not None and m < 0:
            # 新参数在新日子里不如旧的：淘汰它，换到「现有全部证据里最好的」备选（不一定是旧参数），继续找
            s["banned"][key(act)] = today
            fail = key(act)
            pool = []
            for v in vs:
                if key(v) == fail or key(v) in s["banned"]:
                    continue
                g, tt, nn, cv = paired(scores, days, key(v), fail)
                if g is not None and nn >= 2 and cv is not None and cv >= MIN_COVERAGE:
                    pool.append((g, v))
            target = clamp(max(pool, key=lambda x: x[0])[1]) if pool else clamp(prev)
            s["history"].append({"event": "switch", "day": today, "from": act, "to": target, "gain_vs_prev": m, "post_days": n})
            last["closed"] = True
            s["active"] = target
            decision.update(action="switch", why=f"换参数后的 {n} 个新信号日里，新参数比旧参数平均差 {m:+.3%}，淘汰它（{BAN_DAYS} 天内不再用），"
                            f"改用现有证据里最好的备选，继续找更好的。", to=target)
        elif n >= CONFIRM_DAYS and m is not None and m > 0:
            last["closed"] = True
            last["confirmed"] = {"day": today, "gain_vs_prev": m, "post_days": n}
            decision["why"] = f"换参数后的 {n} 个新信号日里新参数仍比旧参数好 {m:+.3%}，确认保留。"
        else:
            decision["why"] = f"刚换参数，换之后只有 {n} 个新信号日（需要 {ROLLBACK_DAYS} 天才判断是否退回）。"
    else:
        # ---- 找有没有够格的备选
        cool = last and last.get("event") in ("adopt", "switch") and _days_between(last["day"], today) < COOLDOWN_DAYS
        if n_weeks < FREEZE_WEEKS:
            decision["why"] = f"参数冻结中：可核对的完整交易周 {n_weeks} 周，不足 {FREEZE_WEEKS} 周，不动参数（固定为阈值 {DEFAULT['thr']:.0%}、小仓位线 {DEFAULT['mt']:.0%}）。"
        elif len(days) < MIN_DAYS:
            decision["why"] = f"可核对的信号日只有 {len(days)} 个，少于 {MIN_DAYS} 个，不动参数。"
        elif cool:
            decision["why"] = f"距上次调整不到 {COOLDOWN_DAYS} 天，冷却中。"
        else:
            ok = [x for x in decision["candidates"] if x["key"] != ak and not x["banned"] and x["gain"] is not None and x["t"] is not None
                  and x["gain"] >= MIN_GAIN and x["t"] >= t_need and x["coverage"] is not None and x["coverage"] >= MIN_COVERAGE]
            if ok:
                best = max(ok, key=lambda x: x["t"])
                s["history"].append({"event": "adopt", "day": today, "from": act, "to": best["params"], "gain": best["gain"],
                                     "t": best["t"], "days": best["days"], "coverage": best["coverage"]})
                s["active"] = clamp(best["params"])
                decision.update(action="adopt", why=f"备选 {best['key']} 在 {best['weeks']} 个完整周里每条信号平均比当前好 {best['gain']:+.3%}（按周 t={best['t']:.1f} ≥ {t_need:.1f}），"
                                f"保留了 {best['coverage']:.0%} 的信号，自动采用。", to=s["active"])
            else:
                decision["why"] = f"没有备选同时满足：平均好 ≥0.2%、按周 t ≥ {t_need:.1f}（11 个备选的多重检验修正）、信号数不少于当前的一半。"

    decision["active_after"] = s["active"]
    decision["series"] = [{"day": d, "active": scores[d].get(key(s["active"]), (None, 0))[0],
                           "default": scores[d].get(key(DEFAULT), (None, 0))[0],
                           "n": scores[d].get(key(s["active"]), (None, 0))[1]} for d in days]
    if apply:
        save_state(s, state_path)
        lp = Path(log_path) if log_path else LOG
        lp.parent.mkdir(parents=True, exist_ok=True)
        keep = []
        if lp.exists():
            for ln in lp.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(ln).get("day") != today:
                        keep.append(ln)
                except ValueError:
                    pass
        slim = {k: v for k, v in decision.items() if k != "series"}
        keep.append(json.dumps(slim, ensure_ascii=False))
        lp.write_text("\n".join(keep) + "\n", encoding="utf-8")
    return decision
