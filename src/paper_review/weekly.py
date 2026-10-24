"""纸面实盘周报(T012 / S2):把账本统计、随机对照、模拟盘日志和三条硬规则拼成一份 markdown。

设计原则:
  只读:不写账本、不写日志、不弹通知、不联网、不读 data/。调用者传入已打开的连接/数据。
  复用:精确率/召回率/准确率曲线 -> signal_ledger;随机对照/Holm/晋升 -> promotion;
        滑点/漏单/净值 -> validation.paper.PaperJournal.report;回撤 -> validation.stats(缺失时回落简版并标注);
        三条硬规则 -> paper_review.rules。
  诚实:数据不足的章节只写 {"status": "数据不足", "reason": ...},不编造数字。
  口径区分:账本的"跑赢全市场"衡量选股能力;停止规则的"跑输大盘"衡量赚不赚钱,两者不混用。
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping, Sequence

from .. import promotion as PR
from .. import signal_ledger as L
from . import rules

try:                                    # T007 的统计模块;取不到就用内置简版并在报告里标注
    from ..validation import stats as _STATS
except Exception:                       # pragma: no cover
    _STATS = None

INSUFFICIENT = "数据不足"
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
MIN_MONTH_DAYS = 10                     # 月内共同交易日少于此数的月份视为不完整,不参与停止规则
SIDE_OF = {"买入": "buy", "加仓": "buy", "卖出": "sell", "减仓": "sell"}
FOOTER = [
    "样本量:前 12 周 / 500 条信号之前,所有统计都只是观察,不要据此改策略或加仓。",
    "多重检验:候选策略有很多个,其中最好的那个大概率只是运气;只有通过 Holm 校正并连续达标才算数。",
    "成本:精确率口径已扣买入类 16bp 来回费用;模拟盘净值已含费用与滑点。",
    "过拟合:回测/历史好看不代表未来有效;前 6–12 个月收益约等于大盘是正常的。",
    "本报告为个人研究记录,不构成任何投资建议。",
]


# ---------------------------------------------------------------- 小工具
def _no(reason: str) -> dict:
    return {"status": INSUFFICIENT, "reason": reason}


def week_range(week: str) -> tuple[str, str]:
    """'YYYY-Www' -> (周一, 周日) 的 ISO 日期;与 promotion.week_key 同口径。"""
    m = _WEEK_RE.match(week) if isinstance(week, str) else None
    if not m:
        raise ValueError(f"周格式必须是 YYYY-Www,收到 {week!r}")
    mon = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    return mon.isoformat(), (mon + timedelta(days=6)).isoformat()


def _compound(rs) -> float:
    p = 1.0
    for r in rs:
        p *= 1 + r
    return p - 1


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def _perf(rets: list[float], dates: list[str]) -> dict:
    """累计收益 / 最大回撤 / 年化波动。优先 validation.stats,失败回落简版。"""
    if _STATS is not None:
        try:
            s = _STATS.perf_stats(rets, dates=dates)
            return {"source": "validation.stats", "n": s["n_obs"], "cum_return": s["cum_return"],
                    "max_drawdown": s["max_drawdown"], "ann_vol": s["ann_vol"], "sharpe": s["sharpe"]}
        except Exception:
            pass
    eq = peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    vol = None
    if len(rets) >= 2:
        mu = sum(rets) / len(rets)
        vol = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)
    return {"source": "内置简版(未使用 validation.stats)", "n": len(rets), "cum_return": eq - 1,
            "max_drawdown": mdd, "ann_vol": vol, "sharpe": None}


# ---------------------------------------------------------------- 账本部分(只读)
def _precision_section(c, main, week_days, we):
    if not week_days:
        return _no("本周账本没有信号日")
    rows = [p for p in L.precision(c, main) if p["h"] in (5, 20)]
    if not rows:
        return _no("主策略信号尚未走满 5 日,无法核对")
    keep = ("action", "h", "n", "days", "hit_excess", "hit_excess_lo", "hit_excess_hi",
            "mean_excess", "net_excess", "t_day", "enough", "verdict")
    board = L.strategy_board(c).get(main, {}).get("h5", {})
    daily = [d for d in board.get("daily", []) if d["day"] <= we]
    return {"status": "ok", "strategy": main, "scope": "账本内全部已核对信号(累计,不按周截断)",
            "rows": [{k: p[k] for k in keep} for p in rows],
            "week_curve": [d for d in daily if d["day"] in set(week_days)],
            "curve_tail": daily[-10:],
            "board_h5": {k: board.get(k) for k in ("n", "days", "hit", "hit_lo", "hit_hi", "score", "enough")}}


def _controls(c):
    return [r["strategy"] for r in c.execute("SELECT DISTINCT strategy FROM signals ORDER BY strategy")
            if PR.is_control(r["strategy"])]


def _controls_section(c, week_days):
    names = _controls(c)
    if not names:
        return _no("账本里没有 ctrl_ 开头的对照策略(随机对照见晋升节的零分布)")
    out = {}
    for s in names:
        out[s] = [{k: p[k] for k in ("action", "h", "n", "hit_excess", "hit_excess_lo", "hit_excess_hi", "mean_excess", "enough")}
                  for p in L.precision(c, s) if p["h"] in (5, 20) and p["action"] == "买入"]
    return {"status": "ok", "controls": out}


def _recall_block(c, strategy, days):
    per = []
    for d in days:
        r = L.recall(c, d, strategy)
        if r is None or not r["winners"]:
            continue
        per.append(r)
    return per


def _recall_section(c, main, week_days):
    if not week_days:
        return _no("本周账本没有信号日")
    per = _recall_block(c, main, week_days)
    if not per:
        return _no("本周信号日没有全市场状态,或走满 5 日后的上涨股样本不足")
    latest = per[-1]
    ctrl = {}
    for s in _controls(c):
        pc = _recall_block(c, s, week_days)
        if pc:
            ctrl[s] = {"mean_recall": _mean(r["recall"] for r in pc if r["recall"] is not None),
                       "n_days": len(pc)}
    lifts = [r["lift"] for r in per if r["lift"] is not None]
    return {"status": "ok", "strategy": main,
            "days": [{k: r[k] for k in ("day", "winners", "categories", "recall", "base_rate", "lift")} for r in per],
            "mean_recall": _mean(r["recall"] for r in per if r["recall"] is not None),
            "mean_lift": _mean(lifts), "latest_day": latest["day"], "missed": latest["missed"][:10],
            "controls": ctrl}


def _promotion_section(c, main, today):
    ps = PR.promotion_status(c, today)
    row = ps["strategies"].get(main)
    if row is None:
        return _no(f"晋升登记表里没有 {main}(需每日任务先登记)")
    keep = ("weeks", "n", "days", "stat", "ctrl_mean", "ctrl_lo", "ctrl_hi", "advantage", "p", "hit", "hit_lo",
            "hit_hi", "ctrl_hit", "noise", "streak", "holm_pass", "status", "weeks_left", "mde")
    return {"status": "ok", "strategy": main, "m": ps["m"], "alpha": ps["alpha"], "n_candidates": len(ps["strategies"]),
            "message": ps["message"], "gate": ps["gate"], "row": {k: row.get(k) for k in keep if k in row}}


# ---------------------------------------------------------------- 模拟盘日志部分
def _gap(c, day, code):
    r = c.execute("SELECT gap FROM moves WHERE day=? AND code=? AND gap IS NOT NULL LIMIT 1", (day, code)).fetchone()
    return None if r is None else r["gap"]


def split_deviation(side: str, theory: float, fill: float, gap):
    """总偏差 = 跳空部分 + 执行滑点,分母都是信号日收盘(theory)。正数 = 对自己不利。
    买入:总 = (F-C)/C,跳空 = +gap,执行 = 总 - gap。
    卖出:总 = (C-F)/C,跳空 = -gap(高开对卖方有利),执行 = 总 + gap。gap 缺失则无法拆分。"""
    total = ((fill - theory) if side == "buy" else (theory - fill)) / theory
    if gap is None:
        return {"total": total, "gap_part": None, "exec": None}
    gp = gap if side == "buy" else -gap
    return {"total": total, "gap_part": gp, "exec": total - gp}


def _execution_section(c, journal, rep, main, ws, we, end):
    sigs = journal.list_signals(ws, we)
    items, n_filled, n_missed = [], 0, 0
    for sg in sigs:
        fl = [f for f in journal.list_fills(sg["id"]) if f["ts"][:10] <= end]
        if fl:
            n_filled += 1
        elif journal.list_misses(sg["id"]):
            n_missed += 1
        for f in fl:
            d = split_deviation(sg["side"], sg["theory_price"], f["price"], _gap(c, sg["date"], sg["code"]))
            items.append({"date": sg["date"], "code": sg["code"], "side": sg["side"], "theory": sg["theory_price"],
                          "fill": f["price"], "shares": f["shares"], **d})
    have = {(s["date"], s["code"], s["side"]) for s in journal.list_signals(ws, we) if s["strategy_id"] == main}
    expected = {(r["day"], r["code"], SIDE_OF[r["action"]]) for r in
                c.execute("SELECT day, code, action FROM signals WHERE strategy=? AND day BETWEEN ? AND ?", (main, ws, we))}
    unreg = sorted(expected - have)
    split = [i for i in items if i["exec"] is not None]
    return {"status": "ok", "n_signals": len(sigs), "n_filled": n_filled, "n_missed": n_missed,
            "n_pending": len(sigs) - n_filled - n_missed, "n_fills": len(items),
            "mean_total": _mean(i["total"] for i in items),
            "mean_gap_part": _mean(i["gap_part"] for i in split),
            "mean_exec": _mean(i["exec"] for i in split),
            "n_unsplittable": len(items) - len(split),
            "top5": sorted(items, key=lambda i: -i["total"])[:5],
            "unregistered": [{"date": d, "code": cd, "side": s} for d, cd, s in unreg],
            "window_miss_rate": rep["miss_rate"], "window_miss_reasons": rep["miss_reasons"],
            "window_avg_slippage": rep["avg_slippage"], "window_weighted_slippage": rep["weighted_slippage"],
            "window_tracking_error": rep["tracking_error"], "window_cum_deviation": rep["cum_deviation"]}


def _positions_at(journal, rep, end):
    """从成交推出 end 日收盘持仓与估值(最近一个不晚于 end 的估值价)。"""
    qty = {}
    for sg in journal.list_signals():
        for f in journal.list_fills(sg["id"]):
            if f["ts"][:10] <= end:
                qty[sg["code"]] = qty.get(sg["code"], 0) + (f["shares"] if sg["side"] == "buy" else -f["shares"])
    out = []
    for code, q in sorted(qty.items()):
        if q <= 0:
            continue
        px = [m["price"] for m in journal.list_marks(code) if m["date"] <= end]
        out.append({"code": code, "qty": q, "price": px[-1] if px else None})
    return out


def _month_end(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - timedelta(days=1)).isoformat()


def monthly_excess(strategy_daily: Mapping[str, float], benchmark_daily: Mapping[str, float], as_of: str,
                   min_days: int = MIN_MONTH_DAYS):
    """两路日收益 -> (完整月 [(月, 策略月收益, 大盘月收益)], 不完整月说明)。只用两边都有值的日子;
    当月未结束(月末晚于 as_of)或共同交易日 < min_days 的月份不进入停止规则。"""
    common = sorted(set(strategy_daily) & set(benchmark_daily))
    by = {}
    for d in common:
        by.setdefault(d[:7], []).append(d)
    full, partial = [], []
    for m in sorted(by):
        ds = by[m]
        if _month_end(m) > as_of:
            partial.append({"month": m, "n_days": len(ds), "why": "当月未结束"})
        elif len(ds) < min_days:
            partial.append({"month": m, "n_days": len(ds), "why": f"共同交易日 < {min_days}"})
        else:
            full.append((m, _compound(strategy_daily[d] for d in ds), _compound(benchmark_daily[d] for d in ds)))
    return full, partial


def _rules_section(journal_ok, rep, journal, strategy_daily, benchmark_daily, we, end, alerts):
    out = {}
    # ---- 停止规则
    if strategy_daily is None:
        out["stop"] = _no("没有策略日收益序列(未提供模拟盘日志或 strategy_daily)")
    elif not benchmark_daily:
        out["stop"] = {"status": INSUFFICIENT, "reason": "无法判断:缺基准(不会用全市场等权平均冒充大盘)"}
    else:
        full, partial = monthly_excess(strategy_daily, benchmark_daily, we)
        res = rules.stop_rule(full) if full else {"triggered": False, "status": INSUFFICIENT, "months": [], "excess": [],
                                                   "streak": 0, "reason": "没有已结束且交易日足够的完整月份"}
        out["stop"] = {**res, "monthly": [{"month": m, "strategy": s, "benchmark": b, "excess": s - b} for m, s, b in full],
                       "partial": partial}
        if res["triggered"]:
            alerts.append({"level": "critical", "code": "stop_rule", "title": "暂停实盘",
                           "body": f"连续 {len(res['months'])} 个月跑输大盘 ≥ {rules.STOP_UNDERPERF:.0%}:{', '.join(res['months'])}"})
    # ---- 仓位与单日亏损(需要日志)
    if not journal_ok:
        out["positions"] = _no("没有可用的模拟盘日志")
        out["daily_loss"] = _no("没有可用的模拟盘日志")
        return out
    pos = _positions_at(journal, rep, end)
    eq_end = rep["equity"][end]
    if any(p["price"] is None for p in pos):
        out["positions"] = _no("有持仓缺估值价")
    else:
        try:
            viol = rules.position_limits(pos, eq_end)
            out["positions"] = {"status": "ok", "equity": eq_end, "positions": [
                {**p, "weight": p["qty"] * p["price"] / eq_end} for p in pos], "violations": viol}
            if viol:
                alerts.append({"level": "critical", "code": "position_limits", "title": "风控违规:仓位超限",
                               "body": "; ".join(f"{v['kind']} {v.get('code') or ''} 权重 {v['weight']:.2%} > {v['limit']:.0%}"
                                                 for v in viol if v["kind"] != "invalid_input") or "输入无效"})
        except ValueError as exc:
            out["positions"] = _no(f"仓位输入无效:{exc}")
    days = sorted(rep["equity"])
    halts, rows = [], []
    for i, d in enumerate(days):
        if not (we >= d >= week_range_start(we)):
            continue
        prev = rep["equity"][days[i - 1]] if i else rep["initial_cash"]
        pnl = rep["equity"][d] - prev
        h = rules.daily_loss_halt(pnl, prev)
        rows.append({"day": d, "pnl": pnl, "ret": pnl / prev, "halt": h})
        if h:
            halts.append(d)
    out["daily_loss"] = {"status": "ok", "days": rows, "halt_days": halts}
    if halts:
        alerts.append({"level": "critical", "code": "daily_loss", "title": "单日亏损停手",
                       "body": f"{', '.join(halts)} 单日亏损 ≥ {rules.DAILY_LOSS:.0%}:当日不再新开仓"})
    return out


def week_range_start(we: str) -> str:
    return (date.fromisoformat(we) - timedelta(days=6)).isoformat()


def _journal_sections(c, journal, ws, we, calendar, bt_returns, initial_cash):
    """返回 (rep 或 None, end, 说明 dict)。失败原因写进 reason,不抛异常。"""
    fills = journal.list_fills()
    if not fills:
        return None, None, "日志中还没有任何成交"
    if initial_cash is None:
        return None, None, "未提供 initial_cash"
    start = min(f["ts"][:10] for f in fills)
    cal = sorted(calendar) if calendar else sorted({m["date"] for m in journal.list_marks()})
    if not cal:
        return None, None, "没有交易日历(也没有估值价可推)"
    if start > we:
        return None, None, f"首笔成交 {start} 晚于本周末 {we}"
    inside = [d for d in cal if start <= d <= we]
    if not inside:
        return None, None, "本周没有落在记录窗口内的交易日"
    end = inside[-1]
    try:
        rep = journal.report(start, end, initial_cash, bt_returns or {}, cal, missing_mark="ffill")
    except Exception as exc:               # PaperError 等:如实写原因,不编数
        return None, None, f"模拟盘日志无法生成报告:{exc}"
    rep["initial_cash"] = initial_cash
    return rep, end, None


# ---------------------------------------------------------------- 入口
def build(ledger_conn, week: str, *, main_strategy: str = "blend2", journal=None,
          calendar: Sequence[str] | None = None, bt_returns: Mapping[str, float] | None = None,
          benchmark_daily: Mapping[str, float] | None = None, strategy_daily: Mapping[str, float] | None = None,
          reflections: Sequence[dict] | None = None, initial_cash: float | None = None,
          today: str | None = None) -> dict:
    """生成周报数据结构(不写任何文件、不写库)。ledger_conn 必须是 sqlite3.Row 连接,最好是 mode=ro。
    strategy_daily:可选,直接给策略日收益(例如来自 simtrade 净值),优先于日志推出的日收益,只用于停止规则。"""
    ws, we = week_range(week)
    c = ledger_conn
    today = today or (date.fromisoformat(we) + timedelta(days=1)).isoformat()
    week_days = [r["day"] for r in c.execute("SELECT day FROM days WHERE day BETWEEN ? AND ? ORDER BY day", (ws, we))]
    alerts: list[dict] = []
    rep = end = None
    j_reason = "未提供模拟盘日志"
    if journal is not None:
        rep, end, j_reason = _journal_sections(c, journal, ws, we, calendar, bt_returns, initial_cash)
    report = {"week": week, "week_start": ws, "week_end": we, "main_strategy": main_strategy, "today": today,
              "signal_days": week_days, "alerts": alerts}
    report["precision"] = _precision_section(c, main_strategy, week_days, we)
    report["controls"] = _controls_section(c, week_days)
    report["recall"] = _recall_section(c, main_strategy, week_days)
    report["promotion"] = _promotion_section(c, main_strategy, today)
    if rep is None:
        for k in ("performance", "turnover", "execution"):
            report[k] = _no(j_reason)
    else:
        wd = [d for d in sorted(rep["daily_returns"]) if ws <= d <= we]
        alld = sorted(rep["daily_returns"])
        perf = _perf([rep["daily_returns"][d] for d in alld], alld)
        report["performance"] = {"status": "ok", "window": [rep["start"], rep["end"]], "net_of_cost": True,
                                 "week_return": _compound(rep["daily_returns"][d] for d in wd) if wd else None,
                                 "cum_return": rep["total_return"], "perf": perf, "warnings": rep["warnings"]}
        fills = []
        for sg in journal.list_signals():
            for f in journal.list_fills(sg["id"]):
                if ws <= f["ts"][:10] <= we:
                    fills.append((sg["side"], f["price"] * f["shares"]))
        avg_eq = _mean(rep["equity"][d] for d in wd)
        if avg_eq:
            gross = sum(a for _, a in fills)
            report["turnover"] = {"status": "ok", "buy": sum(a for s, a in fills if s == "buy"),
                                  "sell": sum(a for s, a in fills if s == "sell"), "avg_equity": avg_eq,
                                  "turnover": gross / 2 / avg_eq, "note": "简版:(买入额+卖出额)/2 ÷ 本周平均净值,单边口径"}
        else:
            report["turnover"] = _no("本周没有落在记录窗口内的交易日")
        report["execution"] = _execution_section(c, journal, rep, main_strategy, ws, we, end)
    sd = strategy_daily if strategy_daily is not None else (rep["daily_returns"] if rep else None)
    report["rules"] = _rules_section(rep is not None, rep, journal, sd, benchmark_daily, we, end, alerts)
    ex = report["execution"]
    if ex.get("status") == "ok" and ex["unregistered"]:
        alerts.append({"level": "warn", "code": "unregistered", "title": "有信号未登记",
                       "body": f"账本有 {len(ex['unregistered'])} 条主策略信号在日志里既无成交也无漏单记录"})
    report["reflections"] = list(reflections or [])[-5:]
    report["footer"] = FOOTER
    return report


# ---------------------------------------------------------------- 渲染
def _p(x, d=2, sign=False):
    if x is None:
        return "—"
    return f"{x:+.{d}%}" if sign else f"{x:.{d}%}"


def _n(x, d=3):
    return "—" if x is None else f"{x:.{d}f}"


def _sec(lines, title, sec):
    lines += ["", f"## {title}", ""]
    if sec.get("status") != "ok":
        lines.append(f"**{INSUFFICIENT}**:{sec.get('reason', '')}")
        return False
    return True


def render_markdown(report: dict) -> str:
    L_ = [f"# 纸面实盘周报 {report['week']}({report['week_start']} ~ {report['week_end']})", "",
          f"主策略:`{report['main_strategy']}` · 本周信号日 {len(report['signal_days'])} 个"]
    crit = [a for a in report["alerts"] if a["level"] == "critical"]
    if crit:
        L_ += ["", "> ## ⚠ 需要立即处理"]
        for a in crit:
            L_.append(f"> - **{'⚠ 暂停实盘' if a['code'] == 'stop_rule' else '⚠ ' + a['title']}**:{a['body']}")
    for a in report["alerts"]:
        if a["level"] != "critical":
            L_ += ["", f"> 提示:**{a['title']}**:{a['body']}"]

    s = report["precision"]
    if _sec(L_, "1 主策略精确率(跑赢全市场,扣成本)", s):
        L_ += [f"范围:{s['scope']}。Wilson 95% 区间;样本不足的行只是观察。", "",
               "| 动作 | h | 条数 | 信号日 | 命中(超额) | 区间 | 平均超额 | 扣费后 | 按日t | 判定 | 样本够 |",
               "|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in s["rows"]:
            L_.append(f"| {p['action']} | {p['h']} | {p['n']} | {p['days']} | {_p(p['hit_excess'], 1)} | "
                      f"{_p(p['hit_excess_lo'], 1)}–{_p(p['hit_excess_hi'], 1)} | {_p(p['mean_excess'], sign=True)} | "
                      f"{_p(p['net_excess'], sign=True)} | {_n(p['t_day'], 2)} | {p['verdict']} | {'是' if p['enough'] else '否'} |")
        if s["curve_tail"]:
            L_ += ["", "累计准确率曲线(h=5,最近 10 个信号日):", "", "| 信号日 | 当日条数 | 当日命中 | 累计命中 | 累计得分 |", "|---|---|---|---|---|"]
            for d in s["curve_tail"]:
                L_.append(f"| {d['day']} | {d['n']} | {_p(d['hit'], 1)} | {_p(d['cum_hit'], 1)} | {_p(d['cum_score'], sign=True)} |")
    s = report["controls"]
    L_ += ["", "### 对照策略精确率(买入,h=5/20)", ""]
    if s.get("status") != "ok":
        L_.append(f"**{INSUFFICIENT}**:{s['reason']}")
    else:
        for name, rows in s["controls"].items():
            for p in rows:
                L_.append(f"- `{name}` h={p['h']}:n={p['n']},命中 {_p(p['hit_excess'], 1)}"
                          f"({_p(p['hit_excess_lo'], 1)}–{_p(p['hit_excess_hi'], 1)}),平均超额 {_p(p['mean_excess'], sign=True)}")
    s = report["recall"]
    if _sec(L_, "2 召回率与漏掉的上涨股", s):
        L_ += [f"本周均值 召回 {_p(s['mean_recall'], 1)} / lift {_n(s['mean_lift'], 2)}(lift=召回÷随手抽一只的命中基率)。", "",
               "| 信号日 | 上涨股数 | 召回 | 基率 | lift |", "|---|---|---|---|---|"]
        for d in s["days"]:
            L_.append(f"| {d['day']} | {d['winners']} | {_p(d['recall'], 1)} | {_p(d['base_rate'], 1)} | {_n(d['lift'], 2)} |")
        for name, v in s["controls"].items():
            L_.append(f"\n对照 `{name}` 本周平均召回 {_p(v['mean_recall'], 1)}({v['n_days']} 天)")
        if s["missed"]:
            L_ += ["", f"{s['latest_day']} 漏掉的上涨股(前 {len(s['missed'])}):", ""]
            L_ += [f"- {m['code']} {m.get('name') or ''} {_p(m['ret'], sign=True)}({m['category']})" for m in s["missed"]]
    s = report["promotion"]
    if _sec(L_, "3 随机对照与晋升进度", s):
        r = s["row"]
        L_ += [f"检验的假设数 m = **{s['m']}**(账本里现有 {s['n_candidates']} 个策略),Holm 校正 α={s['alpha']}。"
               "**100 个候选里最好的那个大概率是运气**,单个好看的数字不能当结论。", "",
               f"- 状态:{r.get('status')}(完整周 {r.get('weeks')},信号 {r.get('n')} 条)",
               f"- 平均得分 {_p(r.get('stat'), 3, True)};随机对照均值 {_p(r.get('ctrl_mean'), 3, True)},噪声带 "
               f"{_p(r.get('ctrl_lo'), 3, True)} ~ {_p(r.get('ctrl_hi'), 3, True)};优势 {_p(r.get('advantage'), 3, True)};p={_n(r.get('p'), 3)}",
               f"- 命中 {_p(r.get('hit'), 1)} vs 随机对照 {_p(r.get('ctrl_hit'), 1)};结论:{r.get('noise', '—')}",
               f"- Holm 通过:{'是' if r.get('holm_pass') else '否'};连续达标 {r.get('streak')}/{s['gate']['streak']} 周;"
               f"预计还需 {r.get('weeks_left') if r.get('weeks_left') is not None else '—'} 周",
               f"- 全局:{s['message']}"]
    s = report["performance"]
    if _sec(L_, "4 成本后收益与回撤", s):
        pf = s["perf"]
        L_ += [f"窗口 {s['window'][0]} ~ {s['window'][1]}(净值已含费用与滑点)。", "",
               f"- 本周收益 {_p(s['week_return'], sign=True)};累计收益 {_p(s['cum_return'], sign=True)}",
               f"- 最大回撤 {_p(pf['max_drawdown'])};年化波动 {_p(pf['ann_vol'], 1)};夏普 {_n(pf['sharpe'], 2)}",
               f"- 统计口径:{pf['source']}(样本 {pf['n']} 个交易日)"]
        L_ += [f"- 注意:{w}" for w in s["warnings"]]
    s = report["turnover"]
    if _sec(L_, "5 换手", s):
        L_.append(f"本周换手率 {_p(s['turnover'], 1)}(买 {s['buy']:.0f}、卖 {s['sell']:.0f},平均净值 {s['avg_equity']:.0f})。{s['note']}")
    s = report["execution"]
    if _sec(L_, "6 执行偏差(正数=对自己不利,分母为信号日收盘)", s):
        L_ += [f"- 本周信号 {s['n_signals']}:成交 {s['n_filled']}、漏单 {s['n_missed']}、待定 {s['n_pending']}",
               f"- 平均总偏差 {_p(s['mean_total'], 3, True)} = 隔夜跳空 {_p(s['mean_gap_part'], 3, True)} + 执行滑点 {_p(s['mean_exec'], 3, True)}"
               f"(无法拆分 {s['n_unsplittable']} 笔)",
               f"- 窗口累计:加权滑点 {_p(s['window_weighted_slippage'], 3, True)},漏单率 {_p(s['window_miss_rate'], 1)},"
               f"跟踪误差 {_p(s['window_tracking_error'], 1)},与回测累计偏离 {_p(s['window_cum_deviation'], 2, True)}"]
        if s["window_miss_reasons"].get("too_late"):
            L_.append(f"- 提醒:有 {s['window_miss_reasons']['too_late']} 笔因 too_late 漏单,属执行纪律问题")
        if s["unregistered"]:
            L_.append(f"- **未登记信号 {len(s['unregistered'])} 条**(日志里既无成交也无漏单):" +
                      "、".join(f"{u['date']} {u['code']} {u['side']}" for u in s["unregistered"][:10]))
        if s["top5"]:
            L_ += ["", "偏差最大的 5 笔:", "", "| 信号日 | 代码 | 方向 | 理论价 | 成交价 | 总偏差 | 跳空 | 执行滑点 |", "|---|---|---|---|---|---|---|---|"]
            for i in s["top5"]:
                gp = _p(i["gap_part"], 3, True) if i["gap_part"] is not None else "无法拆分"
                ex = _p(i["exec"], 3, True) if i["exec"] is not None else "无法拆分"
                L_.append(f"| {i['date']} | {i['code']} | {i['side']} | {i['theory']:.3f} | {i['fill']:.3f} | "
                          f"{_p(i['total'], 3, True)} | {gp} | {ex} |")
    rl = report["rules"]
    L_ += ["", "## 7 规则状态", "", "### 7.1 停止规则(连续 3 个月跑输大盘 3%)", ""]
    st = rl["stop"]
    if st.get("status") == INSUFFICIENT and "monthly" not in st:
        L_.append(f"**{INSUFFICIENT}**:{st['reason']}")
    else:
        L_.append(f"状态:**{st['status']}**——{st['reason']}")
        if st["monthly"]:
            L_ += ["", "| 月份 | 策略 | 大盘 | 超额 |", "|---|---|---|---|"]
            L_ += [f"| {m['month']} | {_p(m['strategy'], sign=True)} | {_p(m['benchmark'], sign=True)} | {_p(m['excess'], sign=True)} |"
                   for m in st["monthly"]]
        L_ += [f"- 未计入的月份 {p['month']}:{p['why']}(共同交易日 {p['n_days']})" for p in st["partial"]]
    L_.append("(此处是相对大盘的赚钱能力,与第 1 节相对全市场等权的选股能力是两个口径。)")
    L_ += ["", "### 7.2 仓位约束(单票 ≤10%,总仓 ≤80%)", ""]
    ps = rl["positions"]
    if ps.get("status") != "ok":
        L_.append(f"**{INSUFFICIENT}**:{ps['reason']}")
    else:
        L_ += [f"- {p['code']}:{p['qty']} 股,权重 {_p(p['weight'], 1)}" for p in ps["positions"]] or ["- 空仓"]
        L_.append("- 违规:" + ("; ".join(f"{v['kind']} {v.get('code') or ''}" for v in ps["violations"]) if ps["violations"] else "无"))
    L_ += ["", "### 7.3 单日亏损(≥2% 当日停手)", ""]
    dl = rl["daily_loss"]
    if dl.get("status") != "ok":
        L_.append(f"**{INSUFFICIENT}**:{dl['reason']}")
    else:
        L_ += [f"- {d['day']}:{_p(d['ret'], sign=True)}{' ⚠ 触发停手' if d['halt'] else ''}" for d in dl["days"]] or ["- 本周无交易日记录"]
    L_ += ["", "## 8 本周反思摘录", ""]
    if report["reflections"]:
        for r in report["reflections"]:
            for it in r.get("items", []):
                L_.append(f"- {r.get('day', '')} [{it.get('level', '')}] {it.get('text', '')}")
    else:
        L_.append("未提供。")
    L_ += ["", "## 9 注意事项", ""] + [f"- {t}" for t in report["footer"]]
    return "\n".join(L_) + "\n"


def write_report(text: str, folder: Path, week: str) -> Path:
    """写 <folder>/<week>.md;已存在则抛 FileExistsError(拒绝覆盖)。folder 不存在时创建。"""
    week_range(week)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{week}.md"
    with open(p, "x", encoding="utf-8") as f:
        f.write(text)
    return p
