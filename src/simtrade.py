"""模拟盘（收盘后撮合）+ AI 账户。只用标准库；行情、清单、日历都由调用方注入，方便单独测试。

规则口径（只参照 A 股交易规则与券商费用口径，不是任何券商系统的复制）：
- 订单在 D 日收盘后的清单发布后产生，下一个交易日 D+1 的数据出来后统一撮合；成交价 = D+1 开盘价（FILL_PRICE）。
- 买入 100 股整数倍；卖出可零股一次清仓；T+1（当天买入的当天不能卖）。
- 开盘即涨停买不进、开盘即跌停卖不出、停牌（没有当天行情）不成交；这些都会记下原因，不会悄悄丢掉。
- 费用分项：佣金（有最低 5 元）、过户费、印花税（仅卖出股票）。费率见 FEE，FEE_VERIFIED=False 表示尚未和真实账户核对。
- 只保留一个 AI 账户：本金 5000 万、单只上限 2%、持仓数基本不设限，目的是跟上清单里的全部信号来验证（100 万/1 万账户资金太小、上限 20 只会挑掉大量信号，已停用）。
- AI 账户完全由确定性代码执行：读 #/trade 的 blend2 清单，按固定风控下单。不调用任何模型，也不自动改策略。
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "simtrade.db"
OUT = ROOT / "data" / "daily" / "simtrade"
LOT = 100
FILL_PRICE = "open"
FEE_VERIFIED = False      # 费率还没和真实账户核对；核对后改 True 并更新 FEE
FEE = {
    "stock": {"commission_rate": 0.00025, "min_commission": 5.0, "transfer_rate": 0.00001, "stamp_rate": 0.0005},
    "etf": {"commission_rate": 0.00015, "min_commission": 5.0, "transfer_rate": 0.0, "stamp_rate": 0.0},
}
STOP_LOSS = 0.15          # 持仓亏损达到成本的 15%，下一个交易日卖出（固定风控）
HALT_DD = 0.20            # 账户从最高点回撤超过 20%，暂停新买入
CASH_RESERVE = 0.05       # 至少留 5% 现金
MIN_TARGET = 0.05         # 与清单口径一致：目标仓位低于 5% 的买入不做
ACCOUNTS = {   # 资金放大到足够覆盖清单里的全部信号：策略强度(0-1) × 单只上限 × 净值 = 目标市值
    "ai_main": {"label": "AI 账户 5000 万", "initial": 50_000_000.0, "max_weight": 0.02, "max_positions": 500, "min_trade": 20000.0, "min_target": 0.0},
}
_lock = threading.RLock()


# ------------------------------------------------------------ 规则
def kind_of(code: str) -> str:
    return "etf" if str(code).startswith(("510", "511", "512", "513", "515", "516", "517", "518", "56", "588", "589", "159")) else "stock"


def fees(code: str, side: str, amount: float) -> dict:
    f = FEE[kind_of(code)]
    commission = round(max(amount * f["commission_rate"], f["min_commission"]), 2) if amount > 0 else 0.0
    transfer = round(amount * f["transfer_rate"], 2)
    stamp = round(amount * f["stamp_rate"], 2) if side == "sell" else 0.0
    return {"commission": commission, "transfer": transfer, "stamp": stamp, "total": round(commission + transfer + stamp, 2)}


def limit_pct(code: str, name: str = "") -> float:
    if "ST" in (name or "").upper():
        return 0.05
    if str(code).startswith(("30", "68")):
        return 0.20
    if str(code).startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def _r2(x: float) -> float:
    return math.floor(x * 100 + 0.5 + 1e-9) / 100


def limit_prices(code: str, name: str, prev_close: float) -> tuple[float, float]:
    p = limit_pct(code, name)
    return _r2(prev_close * (1 + p)), _r2(prev_close * (1 - p))


def next_day(day: str, calendar=None) -> str:
    d = date.fromisoformat(day)
    if calendar:
        later = [x for x in calendar if x > d]
        if later:
            return min(later).isoformat()
    d += timedelta(days=1)
    while d.weekday() >= 5:      # 没有日历时只跳过周末
        d += timedelta(days=1)
    return d.isoformat()


# ------------------------------------------------------------ 存储
def connect(path=None):
    p = Path(path) if path else DB
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, label TEXT, initial REAL, cash REAL, created TEXT,
        max_weight REAL, max_positions INTEGER, min_trade REAL);
    CREATE TABLE IF NOT EXISTS positions(acct TEXT, code TEXT, name TEXT, qty INTEGER, cost REAL, new_qty INTEGER, new_day TEXT,
        PRIMARY KEY(acct, code));
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, acct TEXT, signal_day TEXT, fill_day TEXT, code TEXT, name TEXT,
        side TEXT, qty INTEGER, status TEXT, reason TEXT, signal TEXT, UNIQUE(acct, signal_day, code, side));
    CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, acct TEXT, day TEXT, code TEXT, name TEXT, side TEXT, qty INTEGER,
        price REAL, amount REAL, commission REAL, transfer REAL, stamp REAL, fee REAL, pnl REAL, order_id INTEGER);
    CREATE TABLE IF NOT EXISTS equity(acct TEXT, day TEXT, cash REAL, mv REAL, value REAL, PRIMARY KEY(acct, day));
    CREATE TABLE IF NOT EXISTS summaries(acct TEXT, day TEXT, payload TEXT, PRIMARY KEY(acct, day));
    """)
    return c


def ensure_accounts(c, day: str):
    for aid, a in ACCOUNTS.items():
        c.execute("INSERT OR IGNORE INTO accounts VALUES(?,?,?,?,?,?,?,?)",
                  (aid, a["label"], a["initial"], a["initial"], day, a["max_weight"], a["max_positions"], a["min_trade"]))


def _positions(c, acct):
    return {r["code"]: dict(r) for r in c.execute("SELECT * FROM positions WHERE acct=?", (acct,))}


def _reject(c, o, res, out, reason, price=None):
    c.execute("UPDATE orders SET status='rejected', reason=? WHERE id=?", (reason, o["id"]))
    res.update(status="rejected", reason=reason)
    if price is not None:
        res["price"] = price
    out.append(res)


# ------------------------------------------------------------ 撮合
def settle(c, acct: str, day: str, bars_fn) -> list[dict]:
    """撮合 fill_day <= day 的挂单：先卖后买。bars_fn(code, day) -> {open,high,low,close,prev_close,volume} 或 None。"""
    out = []
    pend = list(c.execute("SELECT * FROM orders WHERE acct=? AND status='pending' AND fill_day<=? ORDER BY side='buy', id", (acct, day)))
    for o in pend:
        res = {"order": o["id"], "code": o["code"], "name": o["name"], "side": o["side"], "want": o["qty"], "day": o["fill_day"]}
        bar = bars_fn(o["code"], o["fill_day"])
        if not bar or not bar.get("open") or (bar.get("volume") is not None and bar["volume"] <= 0):
            if o["fill_day"] < day:      # 撮合日已过去但没有行情：放弃；当天行情还没出来则保持挂单
                _reject(c, o, res, out, "停牌或没有当天行情，未成交")
            continue
        if o["fill_day"] < day:      # 有行情但成交日已过：不补成交
            # 成交日已经过去才来撮合（日任务漏跑）：行情价按"最新一天为基准的前复权"换算，成交日之后若发生除权，历史价会变，
            # 事后补成交会用到成交日以后的信息，所以一律作废，不补撮合
            reason = f"过期：成交日 {o['fill_day']} 当天没有撮合，不用事后价格补成交"
            c.execute("UPDATE orders SET status='cancelled', reason=? WHERE id=?", (reason, o["id"]))
            res.update(status="cancelled", reason=reason)
            out.append(res)
            continue
        price = float(bar["open"])
        up, down = limit_prices(o["code"], o["name"], float(bar["prev_close"]))
        if o["side"] == "buy" and price >= up - 0.005:
            _reject(c, o, res, out, f"开盘涨停（{price:.2f} ≥ 涨停价 {up:.2f}），买不进", price); continue
        if o["side"] == "sell" and price <= down + 0.005:
            _reject(c, o, res, out, f"开盘跌停（{price:.2f} ≤ 跌停价 {down:.2f}），卖不出", price); continue
        acc = c.execute("SELECT * FROM accounts WHERE id=?", (acct,)).fetchone()
        pos = c.execute("SELECT * FROM positions WHERE acct=? AND code=?", (acct, o["code"])).fetchone()
        qty = int(o["qty"])
        if o["side"] == "buy":
            qty = qty // LOT * LOT
            while qty >= LOT and acc["cash"] < price * qty + fees(o["code"], "buy", price * qty)["total"]:
                qty -= LOT
            if qty < LOT:
                _reject(c, o, res, out, "现金不足一手，未成交", price); continue
            amount = round(price * qty, 2)
            fee = fees(o["code"], "buy", amount)
            oq, oc = (pos["qty"], pos["cost"]) if pos else (0, 0.0)
            nq = oq + qty
            ncost = (oq * oc + amount + fee["total"]) / nq
            keep_new = (pos["new_qty"] if pos and pos["new_day"] == o["fill_day"] else 0) + qty
            c.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?,?)", (acct, o["code"], o["name"], nq, ncost, keep_new, o["fill_day"]))
            c.execute("UPDATE accounts SET cash=cash-? WHERE id=?", (amount + fee["total"], acct))
            pnl = None
        else:
            reason = None
            if not pos:
                reason = "没有持仓，未成交"
            else:
                sellable = pos["qty"] - (pos["new_qty"] if pos["new_day"] == o["fill_day"] else 0)
                if sellable <= 0:
                    reason = "T+1：当天买入的不能卖，未成交"
                else:
                    qty = min(qty, sellable)
                    if qty % LOT and qty != pos["qty"]:
                        qty = qty // LOT * LOT
                    if qty <= 0:
                        reason = "不足一手且不是清仓，未成交"
            if reason:
                _reject(c, o, res, out, reason, price); continue
            amount = round(price * qty, 2)
            fee = fees(o["code"], "sell", amount)
            pnl = round(amount - fee["total"] - pos["cost"] * qty, 2)
            if qty == pos["qty"]:
                c.execute("DELETE FROM positions WHERE acct=? AND code=?", (acct, o["code"]))
            else:
                c.execute("UPDATE positions SET qty=qty-? WHERE acct=? AND code=?", (qty, acct, o["code"]))
            c.execute("UPDATE accounts SET cash=cash+? WHERE id=?", (amount - fee["total"], acct))
        c.execute("INSERT INTO trades(acct,day,code,name,side,qty,price,amount,commission,transfer,stamp,fee,pnl,order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (acct, o["fill_day"], o["code"], o["name"], o["side"], qty, price, amount, fee["commission"], fee["transfer"], fee["stamp"], fee["total"], pnl, o["id"]))
        c.execute("UPDATE orders SET status='filled', reason=? WHERE id=?", (f"成交 {qty} 股 @ {price:.2f}", o["id"]))
        res.update(status="filled", qty=qty, price=price, fee=fee["total"], pnl=pnl, bar=bar)
        out.append(res)
    return out


def mark(c, acct: str, day: str, bars_fn) -> dict:
    """收盘估值并写净值；没有当天行情的持仓按成本估值（不会写出虚假的涨跌）。"""
    cash = c.execute("SELECT cash FROM accounts WHERE id=?", (acct,)).fetchone()["cash"]
    mv, rows = 0.0, []
    for p in c.execute("SELECT * FROM positions WHERE acct=?", (acct,)).fetchall():
        bar = bars_fn(p["code"], day)
        px = float(bar["close"]) if bar and bar.get("close") else float(p["cost"])
        mv += px * p["qty"]
        rows.append({"code": p["code"], "name": p["name"], "qty": p["qty"], "cost": p["cost"], "close": px, "stale": not bar,
                     "prev_close": float(bar["prev_close"]) if bar and bar.get("prev_close") else None})
    value = round(cash + mv, 2)
    c.execute("INSERT OR REPLACE INTO equity VALUES(?,?,?,?,?)", (acct, day, cash, round(mv, 2), value))
    return {"cash": cash, "mv": round(mv, 2), "value": value, "positions": rows}


# ------------------------------------------------------------ AI 账户：确定性下单
def plan(c, acct: str, rows: list[dict], signal_day: str, fill_day: str, state: dict) -> list[dict]:
    """rows = #/trade 的 blend2 清单行；state = mark() 的结果。挂单写入 orders（含 skipped，用于解释为什么没下单）。"""
    a = c.execute("SELECT * FROM accounts WHERE id=?", (acct,)).fetchone()
    held = _positions(c, acct)
    equity = state["value"]
    peak = max([r["value"] for r in c.execute("SELECT value FROM equity WHERE acct=?", (acct,))] + [a["initial"]])
    halted = equity < peak * (1 - HALT_DD)
    close = {p["code"]: p["close"] for p in state["positions"]}
    made, sells_cash, full_sells = [], 0.0, set()

    def add(side, code, name, qty, status, reason, signal=""):
        try:
            c.execute("INSERT INTO orders(acct,signal_day,fill_day,code,name,side,qty,status,reason,signal) VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (acct, signal_day, fill_day, code, name, side, int(qty), status, reason, signal))
        except sqlite3.IntegrityError:
            return False
        made.append({"side": side, "code": code, "name": name, "qty": int(qty), "status": status, "reason": reason})
        return True

    for code, p in held.items():       # 固定止损优先
        px = close.get(code)
        if px and px <= p["cost"] * (1 - STOP_LOSS):
            if add("sell", code, p["name"], p["qty"], "pending", f"硬止损：收盘 {px:.2f} 比成本 {p['cost']:.2f} 低 {(1 - px / p['cost']):.1%}", "止损"):
                sells_cash += px * p["qty"]; full_sells.add(code)
    for r in rows:
        code, act = r["code"], r.get("action")
        if code not in held or code in full_sells or not act:
            continue
        p, px = held[code], r.get("close")
        if act == "卖出":
            if add("sell", code, p["name"], p["qty"], "pending", "策略信号：卖出（目标仓位 0）", act):
                sells_cash += (px or 0) * p["qty"]; full_sells.add(code)
        elif act == "减仓" and px:
            want = int(equity * a["max_weight"] * (r.get("next") or 0) / px // LOT * LOT)
            cut = p["qty"] - want
            if cut >= LOT and cut * px >= a["min_trade"]:
                if add("sell", code, p["name"], cut, "pending", f"策略信号：减仓，目标 {(r.get('next') or 0):.1%}", act):
                    sells_cash += cut * px
    min_target = ACCOUNTS.get(acct, {}).get("min_target", MIN_TARGET)
    buys = [r for r in rows if r.get("action") in ("买入", "加仓") and r.get("close") and (r.get("next") or 0) >= min_target
            and not (r["action"] == "加仓" and r["code"] not in held)]
    buys.sort(key=lambda r: (-(r.get("avg_turnover_20d") or 0), r["code"]))
    budget = a["cash"] + 0.95 * sells_cash - equity * CASH_RESERVE
    n_hold = len(held) - len(full_sells)
    for r in buys:
        code, px, act, name = r["code"], r["close"], r["action"], r["name"]
        if halted:
            add("buy", code, name, 0, "skipped", f"风控：账户回撤超过 {HALT_DD:.0%}，暂停买入", act); continue
        if r.get("liquidity") != "正常":
            add("buy", code, name, 0, "skipped", f"流动性：{r.get('liquidity')}，不买", act); continue
        if r.get("flags"):
            add("buy", code, name, 0, "skipped", "；".join(r["flags"]), act); continue
        if act == "买入" and n_hold >= a["max_positions"]:
            add("buy", code, name, 0, "skipped", f"已达持仓上限 {a['max_positions']} 只", act); continue
        want = int(equity * a["max_weight"] * r["next"] / px // LOT * LOT) - (held[code]["qty"] if code in held else 0)
        if want < LOT:
            add("buy", code, name, 0, "skipped", f"按 {a['max_weight']:.0%} 单只上限，目标仓位不够一手（一手约 {px * LOT:,.0f} 元）", act); continue
        qty = want
        while qty >= LOT and qty * px * 1.003 + 5 > budget:
            qty -= LOT
        if qty < LOT:
            add("buy", code, name, 0, "skipped", "可用现金不足（已留 5% 备用）", act); continue
        budget -= qty * px * 1.003 + 5
        if act == "买入":
            n_hold += 1
        add("buy", code, name, qty, "pending", f"策略信号：{act}，策略目标 {r['next']:.1%} × 单只上限 {a['max_weight']:.0%} = 占账户 {r['next'] * a['max_weight']:.2%}（{qty} 股）", act)
    return made


# ------------------------------------------------------------ 每日归因（只写代码能验证的事实）
def summarize(c, acct: str, day: str, results: list[dict], state: dict, prev_value) -> dict:
    if not results:     # 重复运行同一天（每日任务可能多次触发）不能把第一次写好的总结覆盖成空的
        old = c.execute("SELECT payload FROM summaries WHERE acct=? AND day=?", (acct, day)).fetchone()
        if old:
            return json.loads(old["payload"])
    lines = []
    day_pnl = None if prev_value is None else round(state["value"] - prev_value, 2)
    fees_paid = round(sum(r.get("fee", 0) for r in results if r["status"] == "filled"), 2)
    if day_pnl is not None:
        lines.append(f"当日净值 {state['value']:,.2f}，较前一日 {day_pnl:+,.2f}（{day_pnl / prev_value:+.2%}）；当日费用 {fees_paid:,.2f}。")
    for r in results:
        if r["status"] == "filled":
            b = r.get("bar") or {}
            gap = (r["price"] / b["prev_close"] - 1) if b.get("prev_close") else None
            intra = (b["close"] / r["price"] - 1) if b.get("close") else None
            t = f"{r['name']} {r['code']} {'买入' if r['side'] == 'buy' else '卖出'} {r['qty']} 股 @ {r['price']:.2f}"
            if gap is not None:
                t += f"；开盘相对前收 {gap:+.2%}"
            if intra is not None:
                t += f"，开盘到收盘 {intra:+.2%}"
            if r["side"] == "buy" and intra is not None and intra < -0.02:
                t += "。买入当天收盘低于开盘价，属于日内波动；策略信号基于日线，不预测次日走势。"
            if r["side"] == "sell" and r.get("pnl") is not None:
                t += f"；本笔盈亏 {r['pnl']:+,.2f}（已含费用）"
            lines.append(t)
        else:
            lines.append(f"未成交：{r['name']} {r['code']} {'买' if r['side'] == 'buy' else '卖'}单，{r['reason']}")
    moves = sorted(((p["close"] / p["prev_close"] - 1, p) for p in state["positions"] if p["prev_close"] and not p["stale"]), key=lambda x: x[0])
    if moves:
        lines.append(f"持仓里当日最弱：{moves[0][1]['name']} {moves[0][0]:+.2%}；最强：{moves[-1][1]['name']} {moves[-1][0]:+.2%}。")
    for p in state["positions"]:
        if p["close"] <= p["cost"] * (1 - STOP_LOSS * 0.6):
            lines.append(f"提示：{p['name']} {p['code']} 已比成本低 {(1 - p['close'] / p['cost']):.1%}，距离 {STOP_LOSS:.0%} 硬止损线不远。")
        if p["stale"]:
            lines.append(f"提示：{p['name']} {p['code']} 当天没有行情，估值按成本价。")
    payload = {"acct": acct, "day": day, "value": state["value"], "day_pnl": day_pnl, "fees": fees_paid, "lines": lines,
               "fee_verified": FEE_VERIFIED,
               "positions": [{k: p[k] for k in ("code", "name", "qty", "cost", "close", "prev_close", "stale")} for p in state["positions"]]}
    c.execute("INSERT OR REPLACE INTO summaries VALUES(?,?,?)", (acct, day, json.dumps(payload, ensure_ascii=False)))
    return payload


def weekly_report(c, acct: str, day: str) -> dict:
    """观察报告：只列数据和建议，不改任何策略或参数。"""
    since = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
    sells = c.execute("SELECT pnl FROM trades WHERE acct=? AND side='sell' AND day>?", (acct, since)).fetchall()
    rej = [r["reason"] for r in c.execute("SELECT reason FROM orders WHERE acct=? AND signal_day>? AND status='rejected'", (acct, since))]
    skp = [r["reason"] for r in c.execute("SELECT reason FROM orders WHERE acct=? AND signal_day>? AND status='skipped'", (acct, since))]
    fee = c.execute("SELECT COALESCE(SUM(fee),0) f FROM trades WHERE acct=? AND day>?", (acct, since)).fetchone()["f"]
    eq = [r["value"] for r in c.execute("SELECT value FROM equity WHERE acct=? ORDER BY day", (acct,))]
    sug = []
    limit_rej = sum(("涨停" in r) or ("跌停" in r) for r in rej)
    if limit_rej >= 3:
        sug.append(f"近 7 天有 {limit_rej} 笔因涨停/跌停没成交，可观察是否要对一字板信号更谨慎。")
    small = sum("一手约" in r for r in skp)
    if small:
        sug.append(f"有 {small} 笔信号因资金不够一手被跳过，小资金账户的实际可交易范围明显小于清单。")
    if eq and fee / max(eq[-1], 1) > 0.003:
        sug.append(f"近 7 天费用占净值 {fee / eq[-1]:.2%}，换手偏高。")
    if not sug:
        sug.append("本周没有触发需要关注的规则。")
    return {"acct": acct, "day": day, "closed_trades": len(sells), "wins": sum((s["pnl"] or 0) > 0 for s in sells), "fees": round(fee, 2),
            "rejected": len(rej), "skipped": len(skp), "suggestions": sug, "note": "仅为观察建议，不会自动修改策略或参数。"}


# ------------------------------------------------------------ 每日入口
class StaleListError(Exception):
    """清单日期不是最近已收盘交易日：旧清单不能触发撮合（价格按最新一天换算，用旧日期撮合会用到之后的信息）。"""


def expected_day(cal, now_bj):
    """最近已收盘交易日（北京时间 15:30 后算当天）。日历必须覆盖到截止日，否则无法确定，拒绝（不退回旧日期）。"""
    cutoff = now_bj.date() if now_bj.hour * 60 + now_bj.minute >= 15 * 60 + 30 else now_bj.date() - timedelta(days=1)
    if not cal or max(cal) < cutoff:
        raise StaleListError(f"交易日历只到 {max(cal) if cal else '无'}，不足以确定最近已收盘交易日（截止 {cutoff}），今天不撮合、不挂单")
    return max(d for d in cal if d <= cutoff).isoformat()


def run_day(payload: dict, *, bars_fn, calendar=None, db=None, rows_key="blend2", expect_day=None) -> dict:
    """payload = trade_list_cn.json 内容。顺序：撮合以前的挂单 → 收盘估值 → 归因 → 按新清单挂新单。可重复运行。
    expect_day：调用方给出的最近已收盘交易日；清单日期与它不一致就拒绝运行（None 不检查，仅供测试）。"""
    day = payload["data_date"]
    if expect_day is not None and day != expect_day:
        raise StaleListError(f"清单日期 {day} 不是最近已收盘交易日 {expect_day}，今天不撮合、不挂单")
    rows = payload["strategies"][rows_key]["rows"]
    with _lock:
        c = connect(db)
        try:
            ensure_accounts(c, day)
            report = {}
            for aid in ACCOUNTS:
                prev = c.execute("SELECT value FROM equity WHERE acct=? AND day<? ORDER BY day DESC LIMIT 1", (aid, day)).fetchone()
                res = settle(c, aid, day, bars_fn)
                state = mark(c, aid, day, bars_fn)
                summ = summarize(c, aid, day, res, state, prev["value"] if prev else None)
                made = plan(c, aid, rows, day, next_day(day, calendar), state)
                report[aid] = {"value": state["value"], "settled": len(res), "new_orders": len([m for m in made if m["status"] == "pending"]),
                               "skipped": len([m for m in made if m["status"] == "skipped"]), "summary": summ["lines"]}
                if date.fromisoformat(day).weekday() == 4:
                    report[aid]["weekly"] = weekly_report(c, aid, day)
            c.commit()
            return report
        finally:
            c.close()


def snapshot(db=None) -> dict:
    """网页 #/ai 用的只读快照；不触发撮合、不联网。"""
    if not Path(db or DB).exists():
        return {"empty": True, "message": "模拟盘还没有运行过，等收盘后的每日任务第一次跑完。"}
    c = connect(db)
    try:
        out = {"fee_verified": FEE_VERIFIED, "fill_price": FILL_PRICE, "fees": FEE, "accounts": []}
        for a in c.execute("SELECT * FROM accounts WHERE id IN (%s)" % ",".join("?" * len(ACCOUNTS)), list(ACCOUNTS)):
            curve = [[r["day"], r["value"]] for r in c.execute("SELECT day, value FROM equity WHERE acct=? ORDER BY day", (a["id"],))]
            last = c.execute("SELECT payload FROM summaries WHERE acct=? ORDER BY day DESC LIMIT 1", (a["id"],)).fetchone()
            last = json.loads(last["payload"]) if last else {}
            value = curve[-1][1] if curve else a["initial"]
            out["accounts"].append({
                "id": a["id"], "label": a["label"], "initial": a["initial"], "cash": a["cash"], "value": value,
                "ret_pct": (value / a["initial"] - 1) * 100, "created": a["created"], "curve": curve,
                "max_weight": a["max_weight"], "max_positions": a["max_positions"],
                "positions": last.get("positions", []),
                "pending": [dict(r) for r in c.execute("SELECT signal_day,fill_day,code,name,side,qty,reason FROM orders WHERE acct=? AND status='pending' ORDER BY side, code", (a["id"],))],
                "skipped": [dict(r) for r in c.execute("SELECT signal_day,code,name,side,reason FROM orders WHERE acct=? AND status='skipped' ORDER BY signal_day DESC, code LIMIT 200", (a["id"],))],
                "rejected": [dict(r) for r in c.execute("SELECT signal_day,fill_day,code,name,side,reason FROM orders WHERE acct=? AND status='rejected' ORDER BY id DESC LIMIT 100", (a["id"],))],
                "trades": [dict(r) for r in c.execute("SELECT day,code,name,side,qty,price,amount,commission,transfer,stamp,fee,pnl FROM trades WHERE acct=? ORDER BY id DESC LIMIT 200", (a["id"],))],
                "summaries": [json.loads(r["payload"]) for r in c.execute("SELECT payload FROM summaries WHERE acct=? ORDER BY day DESC LIMIT 15", (a["id"],))],
            })
        return out
    finally:
        c.close()


def default_bars_fn():
    """从本地日线缓存取某天的 K 线并换算成真实价（与 #/trade 同一套 display_price 口径）。"""
    from . import allmarket, display_price
    cache = {}

    def fn(code, day):
        if code not in cache:
            df = allmarket._drop_unfinished(allmarket.load(code))
            k, _ = display_price.scale_for("cn", code, df)
            days = list(df.index.strftime("%Y-%m-%d")) if df is not None and len(df) else []
            cache[code] = (df, k, days)
        df, k, days = cache[code]
        if day not in days:
            return None
        i = days.index(day)
        row = df.iloc[i]
        prev = df.iloc[i - 1] if i else row
        return {"open": float(row.Open) * k, "high": float(row.High) * k, "low": float(row.Low) * k, "close": float(row.Close) * k,
                "prev_close": float(prev.Close) * k, "volume": float(row.get("Volume", 1) or 0)}
    return fn


def main():
    payload = json.loads((ROOT / "data" / "daily" / "trade_list_cn.json").read_text(encoding="utf-8"))
    from . import turnover
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from . import allmarket
    try:
        today_exp = expected_day(allmarket.trade_calendar(), datetime.now(ZoneInfo("Asia/Shanghai")))
        rep = run_day(payload, bars_fn=default_bars_fn(), calendar=turnover.cached_calendar(), expect_day=today_exp)
    except StaleListError as e:
        print(f"模拟盘跳过：{e}")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"{payload['data_date']}.json"
    if target.exists() and not any(v["settled"] or v["new_orders"] or v["skipped"] for v in rep.values()):
        print("同一天重复运行，没有新内容，保留已有的当日报告")
        return
    target.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
