"""每日汇总：各策略明天的目标仓位、信号变化、原因、观察列表、自动点评。"""
from __future__ import annotations

from src import display_price
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import registry
from .datasource import CACHE, load_watchlist
from .indicators import realized_vol, rsi, sma

DEFAULT_STRATS = ["blend2", "trend_ens", "donchian_s1", "rsi2", "dual_momentum", "buy_hold"]


def _action(now: dict, nxt: dict) -> str:
    a, b = sum(now.values()), sum(nxt.values())
    if a < 1e-9 and b < 1e-9:
        return "空仓观望"
    if a < 1e-9:
        return "买入"
    if b < 1e-9:
        return "卖出"
    if set(now) != set(nxt):
        return "换仓"
    if b - a > 0.1:
        return "加仓"
    if a - b > 0.1:
        return "减仓"
    return "持有"


def _fmt_pos(pos: dict, names: dict) -> str:
    if not pos:
        return "空仓"
    return "、".join(f"{names.get(k, k)} {v * 100:.0f}%" for k, v in pos.items())


def _state(market: str) -> dict:
    p = CACHE / "state.json"
    st = json.loads(p.read_text()) if p.exists() else {}
    return st


def _save_state(st: dict):
    p = CACHE / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    check = json.loads(tmp.read_text(encoding="utf-8"))
    if not isinstance(check, dict):
        tmp.unlink(missing_ok=True)
        raise IOError("日报状态临时文件校验失败")
    tmp.replace(p)


def _staleness(market: str, now_market: datetime):
    """D60：返回 behind(最后日期) -> 比「应有的最新交易日」落后几个交易日（A 股；取不到交易日历返回 None）。
    应有的最新交易日 = 北京时间 15:30 起算今天、否则昨天，再按交易日历退到交易日（market_clock.last_usable_bar_date）。
    美股没有本地交易日历，返回 None，沿用原来「超过 4 个自然日」的规则。"""
    if market != "cn":
        return None
    try:
        from . import allmarket, market_clock
        cal = sorted(allmarket.trade_calendar())
        expected = market_clock.last_usable_bar_date(now_market, cal)
    except Exception:
        return None

    def behind(last) -> int:
        d = pd.Timestamp(last).date()
        return sum(1 for x in cal if d < x <= expected)
    behind.expected = expected
    return behind


def build(market: str, mcfg: dict, prices: dict, errors: dict) -> dict:
    main = mcfg["main"]
    df = prices[main]
    names = {**mcfg["universe"], **load_watchlist(market)}
    now_market = datetime.now(ZoneInfo(mcfg["tz"]))
    behind = _staleness(market, now_market)      # D60：落后几个交易日（A 股）
    c = df["Close"]
    kx = display_price.scale_for(market, main, df)[0]  # 仅用于文字里显示的价格
    last = df.index[-1]

    st = _state(market)
    launch = st.setdefault("launch", {}).setdefault(market, str(last.date()))
    _save_state(st)

    # ---- 指标快照（写原因用）
    mas = {n: sma(c, n).iloc[-1] for n in (100, 150, 200, 250)}
    above = sum(c.iloc[-1] > m for m in mas.values())
    vol = realized_vol(c, 20).iloc[-1]
    r2 = rsi(c, 2).iloc[-1]
    hi20 = df["High"].rolling(20).max().shift(1).iloc[-1]
    lo10 = df["Low"].rolling(10).min().shift(1).iloc[-1]
    dd1y = c.iloc[-1] / c.iloc[-252:].max() - 1

    reasons = {
        "trend_ens": f"价格站上 {above}/4 条均线（100/150/200/250 日）；20 日波动率 {vol * 100:.0f}%，"
                     f"波动率系数 {min(1, 0.15 / vol):.2f}",
        "donchian_s1": f"收盘 {c.iloc[-1] * kx:.2f}，前 20 日最高 {hi20 * kx:.2f}（差 {(c.iloc[-1] / hi20 - 1) * 100:+.1f}%），"
                       f"前 10 日最低 {lo10 * kx:.2f}（差 {(c.iloc[-1] / lo10 - 1) * 100:+.1f}%）",
        "rsi2": f"RSI(2) = {r2:.1f}（低于 10 为超卖），价格{'在' if c.iloc[-1] > mas[200] else '不在'} 200 日均线上方",
        "buy_hold": "一直满仓",
    }
    dm = mcfg["dual_momentum"]
    try:
        mom = {s: prices[s]["Close"].iloc[-1] / prices[s]["Close"].iloc[-253] - 1
               for s in (*dm["risky"], dm["cash"])}
        reasons["dual_momentum"] = "过去 12 个月：" + "，".join(
            f"{names.get(s, s)} {v * 100:+.1f}%" for s, v in mom.items()) + "（每月末调仓）"
    except Exception:
        reasons["dual_momentum"] = "数据不足"
    reasons["blend2"] = "趋势集成、RSI2、海龟各 1/3 仓位相加"
    # D89：同样的数字，英文措辞（前端英文模式用 reason_en / notes_en）
    reasons_en = {
        "trend_ens": f"Price is above {above}/4 moving averages (100/150/200/250-day); 20-day volatility {vol * 100:.0f}%, "
                     f"volatility scale {min(1, 0.15 / vol):.2f}",
        "donchian_s1": f"Close {c.iloc[-1] * kx:.2f}, prior 20-day high {hi20 * kx:.2f} ({(c.iloc[-1] / hi20 - 1) * 100:+.1f}%), "
                       f"prior 10-day low {lo10 * kx:.2f} ({(c.iloc[-1] / lo10 - 1) * 100:+.1f}%)",
        "rsi2": f"RSI(2) = {r2:.1f} (below 10 = oversold); price is {'above' if c.iloc[-1] > mas[200] else 'not above'} the 200-day average",
        "buy_hold": "Always fully invested",
        "blend2": "Trend ensemble, RSI2 and Turtle, 1/3 of the position each",
    }
    try:
        reasons_en["dual_momentum"] = "Past 12 months: " + ", ".join(
            f"{s} {v * 100:+.1f}%" for s, v in mom.items()) + " (rebalanced at month end)"
    except Exception:
        reasons_en["dual_momentum"] = "Not enough data"

    # ---- 各策略
    strategies = []
    for name in DEFAULT_STRATS:
        try:
            res = registry.run(name, prices, main, {}, mcfg, market=market)
        except Exception as e:
            strategies.append({"key": name, "label": registry.STRATEGIES[name]["label"],
                               "error": f"{type(e).__name__}: {e}"})
            continue
        eq = pd.Series(res["equity"], index=pd.to_datetime(res["dates"]))
        since = eq.loc[launch:]
        since_ret = (since.iloc[-1] / since.iloc[0] - 1) * 100 if len(since) > 1 else 0.0
        strategies.append({
            "key": name, "label": registry.STRATEGIES[name]["label"],
            "action": _action(res["position_now"], res["position_next"]),
            "now": _fmt_pos(res["position_now"], names),
            "next": _fmt_pos(res["position_next"], names),
            "next_total": sum(res["position_next"].values()),
            "reason": ("趋势集成、RSI2、海龟三个子策略加权后的目标仓位为 0%，所以当前建议空仓。"
                       if name == "blend2" and sum(res["position_next"].values()) <= 1e-9
                       else reasons.get(name, "")),
            "reason_en": ("The weighted target of the three sub-strategies (trend ensemble, RSI2, Turtle) is 0%, so the suggestion is to hold cash."
                          if name == "blend2" and sum(res["position_next"].values()) <= 1e-9
                          else reasons_en.get(name, "")),
            "recent": res["recent"], "since_launch": since_ret,
            "stats": {k: res["stats"].get(k) for k in ("CAGR%", "ExcessSharpe", "MaxDD%", "Vol%")},
            "cur_dd": res["drawdown"][-1],
        })

    # ---- 观察列表
    watch = []
    for s, d in prices.items():
        cc = d["Close"]
        if len(cc) < 260:
            continue
        ab = sum(cc.iloc[-1] > sma(cc, n).iloc[-1] for n in (100, 150, 200, 250))
        v = realized_vol(cc, 20).iloc[-1]
        watch.append({
            "behind": behind(d.index[-1]) if behind else None,      # D60：本地数据落后几个交易日（今日页显示「数据停在…」）
            "symbol": s, "name": names.get(s, s), "last_date": str(d.index[-1].date()),
            "close": float(cc.iloc[-1]) * display_price.scale_for(market, s, d)[0],
            "d1": float((cc.iloc[-1] / cc.iloc[-2] - 1) * 100),
            "m1": float((cc.iloc[-1] / cc.iloc[-22] - 1) * 100),
            "ytd": float((cc.iloc[-1] / cc.loc[:f"{d.index[-1].year - 1}-12-31"].iloc[-1] - 1) * 100)
            if len(cc.loc[:f"{d.index[-1].year - 1}-12-31"]) else None,
            "above": int(ab), "trend_w": float(ab / 4 * min(1, 0.15 / v)) if v > 0 else 0.0,
            "rsi2": float(rsi(cc, 2).iloc[-1]), "rsi14": float(rsi(cc, 14).iloc[-1]), "vol": float(v * 100),
            # MA300 只作为观察指标展示，不参与现有策略信号。
            "ma300_gap": float((cc.iloc[-1] / sma(cc, 300).iloc[-1] - 1) * 100)
            if len(cc) >= 300 and pd.notna(sma(cc, 300).iloc[-1]) else None,
            "dd1y": float((cc.iloc[-1] / cc.iloc[-252:].max() - 1) * 100),
        })

    # ---- 数据健康检查
    health = []
    stale = set()
    for s in names:
        if s not in prices:
            health.append(f"⚠️ {s} 没有数据")
            stale.add(s)
            continue
        last_d = prices[s].index[-1].date()
        why = f"；原因：{errors[s][:60]}" if errors.get(s) else ""
        if behind:
            # D60：和「应有的最新交易日」比，落后 1 个交易日就如实说，不再把 1–4 天的旧数据说成「仍是最近的交易日」
            n = behind(prices[s].index[-1])
            if n >= 1:
                stale.add(s)
                if s == main or s in mcfg["universe"]:
                    effect = "今天的策略信号用到了它（主品种 / 轮动策略），信号基于旧数据"
                else:
                    effect = "只影响观察列表的显示，不影响策略信号"
                health.append(f"⚠️ {names[s]}（{s}）最新数据停在 {last_d}，比应有的 {behind.expected} 落后 {n} 个交易日；{effect}{why}")
            continue
        lag = (pd.Timestamp(now_market.date()) - prices[s].index[-1]).days
        if lag > 4:   # 美股（没有本地交易日历）：超过 4 天（周末+1 天假期以上）还没新数据，才算真的过期
            stale.add(s)
            health.append(f"⚠️ {names[s]}（{s}）最新数据停在 {last_d}，已 {lag} 天没更新{why}")
    # 下载失败但本地数据仍然够新：不算问题，合并成一条提示
    soft = [s for s, e in errors.items() if e and s not in stale]
    if soft:
        health.append(f"ℹ️ 本次有 {len(soft)} 个品种没连上数据源，已沿用本地数据"
                      + ("（本地已是应有的最新交易日）" if behind else "（本地数据不超过 4 天）")
                      + "，不影响今天的信号。下次自动更新会再试。")

    # ---- 自动点评
    notes, notes_en = [], []          # D89：notes_en 与 notes 一一对应（同样的条件、同样的数字）
    act_en = {"买入": "buy", "卖出": "sell", "加仓": "add", "减仓": "reduce", "换仓": "switch", "持有": "hold", "空仓": "cash", "空仓观望": "stay in cash"}
    lab_en = lambda x: registry.STRATEGIES.get(x["key"], {}).get("label_en") or x["key"]
    blend = next((x for x in strategies if x["key"] == "blend2" and "error" not in x), None)
    if blend:
        notes.append(f"明天开盘，组合策略建议 {names[main]} 仓位 {blend['next_total'] * 100:.0f}%"
                     f"（{blend['action']}）。")
        notes_en.append(f"At tomorrow's open the blended strategy suggests a {blend['next_total'] * 100:.0f}% position in {main}"
                        f" ({act_en.get(blend['action'], blend['action'])}).")
    changes = [x for x in strategies if "error" not in x and x["action"] in ("买入", "卖出", "加仓", "减仓", "换仓")
               and x["key"] != "buy_hold"]
    notes.append("今天有信号变化：" + "；".join(f"{x['label']} → {x['action']}" for x in changes)
                 if changes else "今天所有策略都没有信号变化。")
    notes_en.append("Signal changes today: " + "; ".join(f"{lab_en(x)} → {act_en.get(x['action'], x['action'])}" for x in changes)
                    if changes else "No strategy changed its signal today.")
    notes.append({4: "价格站上全部 4 条均线，长期趋势向上。", 0: "价格在 4 条均线全部下方，长期趋势向下，趋势策略空仓。"}
                 .get(above, f"价格站上 {above}/4 条均线，趋势不明朗，趋势策略部分仓位。"))
    notes_en.append({4: "Price is above all 4 moving averages: long-term trend up.",
                     0: "Price is below all 4 moving averages: long-term trend down; trend strategies hold cash."}
                    .get(above, f"Price is above {above}/4 moving averages: trend unclear; trend strategies hold a partial position."))
    if vol > 0.15:
        notes.append(f"近 20 日年化波动率 {vol * 100:.0f}%，高于 15% 的目标，趋势类策略仓位打 {0.15 / vol:.0%}。")
        notes_en.append(f"20-day annualized volatility is {vol * 100:.0f}%, above the 15% target; trend strategies scale positions to {0.15 / vol:.0%}.")
    if r2 < 10 and c.iloc[-1] > mas[200]:
        notes.append(f"RSI(2) = {r2:.1f}，处在上升趋势中的短期超卖，均值回归策略会买入。")
        notes_en.append(f"RSI(2) = {r2:.1f}: short-term oversold inside an uptrend; the mean-reversion strategy will buy.")
    if dd1y < -0.10:
        notes.append(f"{names[main]} 距离一年内高点回撤 {dd1y * 100:.1f}%。")
        notes_en.append(f"{main} is {dd1y * 100:.1f}% below its 1-year high.")
    # revquant PD_after N3：「比任何历史回测都可信」说过头了——目前只有几周、主要是一直空仓这一个决定
    notes.append(f"上线日 {launch} 起是真正的前向记录（定规则时还没有这段数据），但目前时间还短、决定次数很少，统计上还说明不了什么。")
    notes_en.append(f"Since launch on {launch} this is a genuine forward record (the rules were set before this data existed), "
                    f"but it is still short and has few decisions, so it proves nothing statistically yet.")

    return {
        "market": market, "market_name": mcfg["name"], "main": main, "main_name": names[main],
        "data_date": str(last.date()), "generated": now_market.strftime("%Y-%m-%d %H:%M"),
        "generated_at": now_market.isoformat(timespec="seconds"),
        "timezone": mcfg["tz"], "timezone_label": "北京时间" if market == "cn" else "美东时间",
        "launch": launch, "strategies": strategies, "watch": watch, "health": health, "notes": notes, "notes_en": notes_en,
    }


def log_signals(market: str, summary: dict):
    """把每天的信号追加到日志，以后可以核对"当时说了什么"。"""
    p = CACHE / f"signals_{market}.csv"
    rows = [{"data_date": summary["data_date"], "strategy": s["key"], "action": s.get("action"),
             "next": s.get("next")} for s in summary["strategies"] if "error" not in s]
    new = pd.DataFrame(rows)
    if p.exists():
        old = pd.read_csv(p)
        old = old[old.data_date != summary["data_date"]]
        new = pd.concat([old, new])
    tmp = p.with_suffix(".tmp")
    new.to_csv(tmp, index=False)
    check = pd.read_csv(tmp)
    if len(check) != len(new) or not {"data_date", "strategy", "action"}.issubset(check.columns):
        tmp.unlink(missing_ok=True)
        raise IOError("信号记录临时文件校验失败")
    tmp.replace(p)


def save(market: str, summary: dict) -> Path:
    p = CACHE / f"summary_{market}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False, default=float), encoding="utf-8")
    check = json.loads(tmp.read_text(encoding="utf-8"))
    required = {"market", "data_date", "generated", "strategies", "watch"}
    if not required.issubset(check) or len(check["strategies"]) != len(summary["strategies"]):
        tmp.unlink(missing_ok=True)
        raise IOError(f"日报临时文件校验失败：{market}")
    tmp.replace(p)
    return p


def load(market: str) -> dict | None:
    p = CACHE / f"summary_{market}.json"
    return json.loads(p.read_text()) if p.exists() else None


def table_rows(market: str, mcfg: dict, prices: dict, strategy: str, start: str | None = None) -> list:
    """逐只回测：自选股 + ETF，每只一行，同一个策略、默认参数。"""
    if registry.STRATEGIES[strategy]["kind"] != "single":
        raise ValueError("多品种策略（双动量）不能逐只回测，请换一个策略")
    wl = load_watchlist(market)
    syms = [(s, n, "个股") for s, n in wl.items()] + \
           [(s, n, "ETF") for s, n in mcfg["universe"].items() if s != mcfg["cash"]]
    rows = []
    for s, name, kind in syms:
        base = {"symbol": s, "name": name or s, "kind": kind}
        if s not in prices or len(prices[s]) < 60:
            rows.append(base | {"missing": True})
            continue
        try:
            r = registry.run(strategy, prices, s, {}, mcfg, start=start, market=market)
        except Exception as e:
            rows.append(base | {"error": f"{type(e).__name__}: {e}"})
            continue
        c = prices[s]["Close"]
        st, bs = r["stats"], r["bench_stats"]
        rows.append(base | {
            "last_date": r["end"], "start": r["start"],
            "d1": float((c.iloc[-1] / c.iloc[-2] - 1) * 100),
            "m1": float((c.iloc[-1] / c.iloc[-22] - 1) * 100) if len(c) > 22 else None,
            "cagr": st.get("CAGR%"), "sharpe": st.get("ExcessSharpe"), "maxdd": st.get("MaxDD%"),
            "total": st.get("TotalRet%"), "y1": r["recent"]["1Y"], "win": st.get("Win%"),
            "trades": st.get("Trades"), "exposure": st.get("Exposure%"),
            "b_cagr": bs.get("CAGR%"), "b_maxdd": bs.get("MaxDD%"), "b_total": bs.get("TotalRet%"),
            "b_sharpe": bs.get("ExcessSharpe"),
            "next": sum(r["position_next"].values()),
            "action": _action(r["position_now"], r["position_next"]),
        })
    return rows
