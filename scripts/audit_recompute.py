"""T011 / S1:回测引擎的人工可复核复算。

用法:
    python3 -B scripts/audit_recompute.py --prices FILE.csv --strategy NAME --out REPORT.md

做什么:
  1. 读调用者给的行情 CSV(Date,Open,High,Low,Close[,Volume]),默认拒绝读 data/ 下的文件。
  2. 用纯标准库独立实现信号(SMA/Wilder/RSI/ATR/ADX)和"逐笔交易表 + 逐日净值"的账户式复算
     (现金 + 持股数,逐日开盘成交)。复算里不 import 引擎的任何计算代码。
  3. 调用现有引擎(src.strategies 的信号 + src.portfolio.run_weights)得到引擎结果,逐笔对比。
  4. 差异用"消融法"归因:把复算里一个口径换成引擎口径,看这笔差异是否消失。
     全部口径对齐后仍有差异 → 疑似 bug。
  5. 输出 Markdown 报告;拒绝覆盖已有输出文件。

本脚本不改引擎,不联网,不写 data/。
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAN = float("nan")

# 复算对齐用的三个策略(参数与 src/strategies.py 默认值一致,未调参)
STRATEGIES = ("rsi2_reversion", "donchian_s1", "ma_adx_original")

# 归因用的口径(aspect):名称 → (类别, 说明)
ASPECTS = {
    "limit": ("定义差异/建模缺失", "涨跌停挡单"),
    "suspend": ("建模缺失", "停牌(Volume=0)不成交;引擎没有停牌标志"),
    "cost": ("定义差异", "费用口径(费率/印花税)"),
    "lag": ("定义差异", "信号到成交的滞后(引擎固定次日开盘)"),
    "drift_fee": ("定义差异", "费用按实际成交额计;引擎按目标仓位变化计(漂移再平衡不收费)"),
}


class InputError(Exception):
    """输入非法(文件、列、数值、参数)。"""


# ------------------------------------------------------------------ 读入与校验
def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def read_prices(path: Path, allow_data_dir: bool = False):
    """读 CSV,返回 dict(dates, open, high, low, close, volume|None)。非法输入抛 InputError。"""
    if not allow_data_dir and _is_under(path, ROOT / "data"):
        raise InputError(f"默认不读 data/ 下的文件:{path}(确需读取请加 --allow-data-dir)")
    if not path.is_file():
        raise InputError(f"行情文件不存在:{path}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        cols = {c.strip().lower(): c for c in (rd.fieldnames or [])}
        for need in ("date", "open", "high", "low", "close"):
            if need not in cols:
                raise InputError(f"缺少列 {need}(需要 Date,Open,High,Low,Close,可选 Volume)")
        rows = list(rd)
    if len(rows) < 30:
        raise InputError(f"行数太少({len(rows)}),至少 30 行")
    out = {k: [] for k in ("dates", "open", "high", "low", "close")}
    vol = [] if "volume" in cols else None
    prev = None
    for n, r in enumerate(rows, start=2):
        try:
            d = dt.date.fromisoformat(r[cols["date"]].strip()[:10])
        except ValueError:
            raise InputError(f"第 {n} 行日期无法解析:{r[cols['date']]!r}")
        if prev is not None and d <= prev:
            raise InputError(f"第 {n} 行日期 {d} 不严格递增(重复或乱序)")
        prev = d
        vals = {}
        for k in ("open", "high", "low", "close"):
            try:
                v = float(r[cols[k]])
            except (TypeError, ValueError):
                raise InputError(f"第 {n} 行 {k} 不是数字:{r[cols[k]]!r}")
            if not math.isfinite(v) or v <= 0:
                raise InputError(f"第 {n} 行 {k} 必须是正的有限数:{v}")
            vals[k] = v
        if vals["high"] < vals["low"] - 1e-9 or not (
                vals["low"] - 1e-9 <= vals["open"] <= vals["high"] + 1e-9
                and vals["low"] - 1e-9 <= vals["close"] <= vals["high"] + 1e-9):
            raise InputError(f"第 {n} 行 OHLC 不自洽(开/收不在 [低,高] 内)")
        out["dates"].append(d)
        for k in ("open", "high", "low", "close"):
            out[k].append(vals[k])
        if vol is not None:
            try:
                v = float(r[cols["volume"]])
            except (TypeError, ValueError):
                raise InputError(f"第 {n} 行 volume 不是数字:{r[cols['volume']]!r}")
            if not math.isfinite(v) or v < 0:
                raise InputError(f"第 {n} 行 volume 必须 >= 0")
            vol.append(v)
    out["volume"] = vol
    return out


# ------------------------------------------------------------------ 独立实现的指标(纯标准库)
def sma(x, n):
    out = [NAN] * len(x)
    for t in range(n - 1, len(x)):
        out[t] = sum(x[t - n + 1:t + 1]) / n
    return out


def wilder(x, n):
    """Wilder 平滑:第一个有效值起的前 n 个值取均值做种子,之后 w=w+(x-w)/n。"""
    out = [NAN] * len(x)
    valid = [i for i, v in enumerate(x) if not math.isnan(v)]
    if len(valid) < n:
        return out
    start = valid[0]
    seed = start + n - 1
    out[seed] = sum(x[start:seed + 1]) / n
    for t in range(seed + 1, len(x)):
        out[t] = out[t - 1] + (x[t] - out[t - 1]) / n
    return out


def true_range(h, l, c):
    tr = [h[0] - l[0]]
    for t in range(1, len(c)):
        tr.append(max(h[t] - l[t], abs(h[t] - c[t - 1]), abs(l[t] - c[t - 1])))
    return tr


def atr(h, l, c, n):
    return wilder(true_range(h, l, c), n)


def adx(h, l, c, n):
    m = len(c)
    pdm, mdm = [NAN], [NAN]
    for t in range(1, m):
        up, down = h[t] - h[t - 1], -(l[t] - l[t - 1])
        pdm.append(up if (up > down and up > 0) else 0.0)
        mdm.append(down if (down > up and down > 0) else 0.0)
    tr = true_range(h, l, c)
    tr[0] = NAN
    trs, ps, ms = wilder(tr, n), wilder(pdm, n), wilder(mdm, n)
    dx = []
    for t in range(m):
        if math.isnan(trs[t]) or math.isnan(ps[t]) or math.isnan(ms[t]):
            dx.append(NAN)
            continue
        pdi, mdi = 100 * ps[t] / trs[t], 100 * ms[t] / trs[t]
        den = pdi + mdi
        dx.append(NAN if den == 0 else 100 * abs(pdi - mdi) / den)
    return wilder(dx, n)


def rsi(c, n):
    gain_in, loss_in = [NAN], [NAN]
    for t in range(1, len(c)):
        d = c[t] - c[t - 1]
        gain_in.append(max(d, 0.0))
        loss_in.append(max(-d, 0.0))
    g, ls = wilder(gain_in, n), wilder(loss_in, n)
    out = []
    for t in range(len(c)):
        if math.isnan(g[t]):
            out.append(NAN)
        elif not ls[t] > 0:
            out.append(100.0)
        else:
            out.append(100 - 100 / (1 + g[t] / ls[t]))
    return out


def vol_scale(c, target_vol=0.15, n=20):
    """目标波动率仓位 = min(1, target/年化已实现波动);窗口不足为 NaN(引擎里随后 fillna(0))。"""
    ret = [NAN] + [c[t] / c[t - 1] - 1 for t in range(1, len(c))]
    out = [NAN] * len(c)
    for t in range(n, len(c)):
        w = ret[t - n + 1:t + 1]
        mu = sum(w) / n
        sd = math.sqrt(sum((v - mu) ** 2 for v in w) / (n - 1))
        vol = sd * math.sqrt(252)
        out[t] = 1.0 if vol == 0 else min(1.0, target_vol / vol)
    return out


def cross_up(a, b):
    out = [False] * len(a)
    for t in range(1, len(a)):
        out[t] = a[t] > b[t] and a[t - 1] <= b[t - 1]
    return out


# ------------------------------------------------------------------ 独立实现的三个策略信号
# 返回每天收盘后的目标仓位(0..1)。第 t 天信号只用到 t 及以前的数据;成交在后面的模拟里发生在下一根开盘。
def sig_rsi2_reversion(p, trend_len=200, rsi_len=2, entry_th=10.0, exit_ma=5, max_hold=10):
    c = p["close"]
    trend, r, ex = sma(c, trend_len), rsi(c, rsi_len), sma(c, exit_ma)
    w, on, held = [0.0] * len(c), False, 0
    for t in range(len(c)):
        if math.isnan(trend[t]) or math.isnan(r[t]):
            continue
        if not on:
            if c[t] > trend[t] and r[t] < entry_th:
                on, held = True, 0
        else:
            held += 1
            if c[t] > ex[t] or held >= max_hold:
                on = False
        w[t] = 1.0 if on else 0.0
    return w


def sig_donchian_s1(p, entry_len=20, exit_len=10, atr_len=20, atr_mult=2.0, target_vol=0.15):
    c, h, l = p["close"], p["high"], p["low"]
    m = len(c)
    hi = [max(h[t - entry_len:t]) if t >= entry_len else NAN for t in range(m)]
    lo = [min(l[t - exit_len:t]) if t >= exit_len else NAN for t in range(m)]
    a = atr(h, l, c, atr_len)
    vs = vol_scale(c, target_vol)
    w, on, peak = [0.0] * m, False, 0.0
    for t in range(m):
        if math.isnan(hi[t]) or math.isnan(a[t]):
            continue
        if not on:
            if c[t] > hi[t]:
                on, peak = True, c[t]
        else:
            peak = max(peak, c[t])
            if c[t] < lo[t] or c[t] < peak - atr_mult * a[t]:
                on = False
        if on and not math.isnan(vs[t]):
            w[t] = vs[t]
    return w


def sig_ma_adx_original(p, fast=5, slow=20, adx_len=14, adx_min=20.0):
    c = p["close"]
    mf, ms = sma(c, fast), sma(c, slow)
    ax = adx(p["high"], p["low"], c, adx_len)
    up_x, dn_x = cross_up(mf, ms), cross_up(ms, mf)
    w, on = [0.0] * len(c), False
    for t in range(len(c)):
        if not on and up_x[t] and ax[t] > adx_min:
            on = True
        elif on and dn_x[t]:
            on = False
        w[t] = 1.0 if on else 0.0
    return w


RECOMPUTE_SIGNALS = {"rsi2_reversion": sig_rsi2_reversion, "donchian_s1": sig_donchian_s1,
                     "ma_adx_original": sig_ma_adx_original}


# ------------------------------------------------------------------ 账户式复算
def round_half_up(x, nd=2):
    return math.floor(x * 10 ** nd + 0.5 + 1e-9) / 10 ** nd


def limit_hits(pc, o, pct, mode):
    """开盘价是否触及涨停/跌停。exact:涨跌停价 = 昨收×(1±幅度) 四舍五入到分;engine:跳空 ≥ 幅度-0.002。"""
    if mode == "exact":
        return (o >= round_half_up(pc * (1 + pct)) - 1e-9,
                o <= round_half_up(pc * (1 - pct)) + 1e-9)
    gap = o / pc - 1
    return gap >= pct - 0.002, gap <= -pct + 0.002


def simulate(p, weights, *, buy_cost, sell_cost, engine_cost, limit_pct, limit_mode,
             honor_susp, lag, fee_mode):
    """逐日账户模拟。

    处理规则(报告里也会原样列出):
      T+1/成交:第 t 天收盘出信号,第 t+lag 天开盘成交(lag=1 即次日开盘);同一根 K 线内不会先买后卖。
      费用:按实际成交额 × 费率;买入用 buy_cost,卖出用 sell_cost(含印花税)。
            fee_mode="target" 时改为引擎口径:|目标仓位变化| × 权益 × 费率(仅用于归因对齐)。
      涨跌停:limit_mode="exact":开盘价 ≥ 涨停价(昨收×(1+幅度),四舍五入到分)则买不进;
              开盘价 ≤ 跌停价则卖不出。"engine":引擎口径(开盘跳空 ≥ 幅度-0.002)。"off":不处理。
      停牌:Volume=0 的那天不成交,目标仓位保留,下一个可交易日再试。
      整手:不处理(连续权重),与引擎一致。
    """
    n = len(p["close"])
    O, C = p["open"], p["close"]
    susp = [False] * n
    if honor_susp and p["volume"] is not None:
        susp = [v == 0 for v in p["volume"]]
    cash, sh, wp = 1.0, 0.0, 0.0
    eq = [0.0] * n
    held_w = [0.0] * n
    events, daily = [], []
    trades, entry = [], None
    for i in range(n):
        w = weights[i - lag] if i - lag >= 0 else 0.0
        v_open = cash + sh * O[i]
        blocked = None
        if abs(w - wp) > 1e-12:
            if susp[i]:
                blocked = "停牌"
            elif limit_mode != "off" and i > 0 and limit_pct > 0:
                up_hit, dn_hit = limit_hits(C[i - 1], O[i], limit_pct, limit_mode)
                if w > wp and up_hit:
                    blocked = "涨停买不进"
                elif w < wp and dn_hit:
                    blocked = "跌停卖不出"
        if blocked:
            events.append((p["dates"][i], blocked))
        elif not susp[i] and (abs(w - wp) > 1e-12 or sh > 0):
            tgt_val = w * v_open
            notional = abs(tgt_val - sh * O[i])
            buying = tgt_val > sh * O[i]
            rate = buy_cost if buying else sell_cost
            if fee_mode == "target":
                fee = (buy_cost if w > wp else sell_cost) * abs(w - wp) * v_open
            elif notional < 1e-9 * v_open:
                fee = 0.0
            else:
                fee = rate * notional
            e_post = v_open - fee
            sh = w * e_post / O[i]
            cash = e_post - sh * O[i]
            wp = w
        eq[i] = cash + sh * C[i]
        held_w[i] = wp
        if sh > 0 and entry is None:
            entry = i
        elif sh == 0 and entry is not None:
            trades.append(_trade(p, eq, entry, i, False))
            entry = None
        daily.append((p["dates"][i], eq[i], sh, blocked))
    if entry is not None:
        trades.append(_trade(p, eq, entry, n - 1, True))
    return {"equity": eq, "trades": trades, "events": events, "weights": held_w}


def _trade(p, eq, i0, i1, is_open):
    base = eq[i0 - 1] if i0 > 0 else 1.0
    return {"entry": p["dates"][i0], "exit": p["dates"][i1], "bars": i1 - i0,
            "entry_px": p["open"][i0], "exit_px": p["open"][i1],
            "ret": eq[i1] / base - 1, "open": is_open}


def find_gaps(dates):
    """缺行检测:相邻两行之间缺了几个工作日。没有交易日历,节假日和停牌缺行无法区分,只能列出让人复核。"""
    out = []
    for a, b in zip(dates, dates[1:]):
        miss = [k for k in range(1, (b - a).days) if (a + dt.timedelta(days=k)).weekday() < 5]
        if miss:
            out.append((a, b, len(miss)))
    return out


def check_fill_after_signal(p, weights, trades, lag=1):
    """T+1 核对,对任意一方的交易表都适用:进场成交日往前 lag 天的收盘信号必须是持仓信号
    (成交不能早于信号),且出场成交日必须晚于进场成交日(期末强平除外)。返回违规的进场日。"""
    pos = {d: i for i, d in enumerate(p["dates"])}
    bad = []
    for t in trades:
        i = pos.get(t["entry"])
        same_bar = t["exit"] <= t["entry"] and t.get("reason") != "end_of_data"
        if i is None or i - lag < 0 or weights[i - lag] <= 0 or same_bar:
            bad.append(t["entry"])
    return bad


def recompute_backtest_py(p, cash0=100_000.0, comm=0.0005, fast=5, slow=20, adx_len=14,
                          adx_min=20.0, atr_len=14, atr_mult=2.0):
    """独立复算 src/backtest.py::run(ma_cross_adx_atr:整股 + ATR 止损 + 对称佣金)。
    规则:昨收信号 → 今开成交;股数 = floor(现金 / (开盘价×(1+佣金)));止损价 = 进场价 - 倍数×昨日 ATR;
    进场当根不查止损;开盘跳空破止损价按开盘价出;盘中触及按止损价出;期末按最后收盘价平仓。"""
    O, H, L, C = p["open"], p["high"], p["low"], p["close"]
    n = len(C)
    mf, ms = sma(C, fast), sma(C, slow)
    ax, a = adx(H, L, C, adx_len), atr(H, L, C, atr_len)
    up_x, dn_x = cross_up(mf, ms), cross_up(ms, mf)
    buy = [bool(up_x[t] and ax[t] > adx_min) for t in range(n)]
    cash, sh, e_i, e_px, stop = cash0, 0, -1, 0.0, 0.0
    eq, trades = [0.0] * n, []

    def close(i, px, why):
        nonlocal cash, sh
        proceeds = sh * px
        fee = proceeds * comm
        cash += proceeds - fee
        cost = sh * e_px * (1 + comm)
        trades.append({"entry": p["dates"][e_i], "exit": p["dates"][i], "shares": sh,
                       "pnl": proceeds - fee - cost, "reason": why})
        sh = 0

    for i in range(n):
        if i > 0:
            if sh > 0:
                if dn_x[i - 1]:
                    close(i, O[i], "signal")
                elif i > e_i:
                    if O[i] <= stop:
                        close(i, O[i], "stop_gap")
                    elif L[i] <= stop:
                        close(i, stop, "stop")
            elif buy[i - 1] and not math.isnan(a[i - 1]):
                q = int(cash // (O[i] * (1 + comm)))
                if q > 0:
                    cash -= q * O[i] * (1 + comm)
                    sh, e_i, e_px = q, i, O[i]
                    stop = O[i] - atr_mult * a[i - 1]
        eq[i] = cash + sh * C[i]
    if sh > 0:
        close(n - 1, C[-1], "end_of_data")
        eq[-1] = cash
    return {"equity": eq, "trades": trades, "buy": buy}


def run_backtest_py(p):
    """调用现有 src/backtest.py::run,返回与复算同格式的结果。"""
    sys.path.insert(0, str(ROOT))
    import pandas as pd
    from src.backtest import run
    from src.strategy import Params, generate_signals

    idx = pd.DatetimeIndex([dt.datetime.combine(d, dt.time()) for d in p["dates"]], name="Date")
    df = pd.DataFrame({"Open": p["open"], "High": p["high"], "Low": p["low"], "Close": p["close"],
                       "Volume": p["volume"] if p["volume"] is not None else 1.0}, index=idx)
    res = run(generate_signals(df, Params()), Params())
    trades = [{"entry": r.entry_date.date(), "exit": r.exit_date.date(), "shares": int(r.shares),
               "pnl": float(r.pnl), "reason": r.exit_reason} for r in res.trades.itertuples()]
    return {"equity": [float(x) for x in res.equity], "trades": trades}


def compare_backtest_py(p, args):
    """backtest.run 对账。两侧规则一致(都没有涨跌停/停牌),所以任何差异都记疑似 bug;
    另统计"成交落在涨跌停/停牌日"的次数,说明 backtest.py 在这批数据上的建模缺失暴露。"""
    eng, mine = run_backtest_py(p), recompute_backtest_py(p)
    e_t = {t["entry"]: t for t in eng["trades"]}
    m_t = {t["entry"]: t for t in mine["trades"]}
    rows = []
    for d in sorted(set(e_t) | set(m_t)):
        e, m = e_t.get(d), m_t.get(d)
        ok = (e is not None and m is not None and e["exit"] == m["exit"]
              and e["shares"] == m["shares"] and e["reason"] == m["reason"]
              and abs(e["pnl"] - m["pnl"]) <= 1e-6)
        rows.append({"entry": d, "engine": e, "recompute": m, "diff": not ok})
    pos = {d: i for i, d in enumerate(p["dates"])}
    limit_exp = susp_exp = 0
    for t in mine["trades"]:
        for d, side in ((t["entry"], 0), (t["exit"], 1)):
            i = pos[d]
            if i > 0 and args.limit > 0:
                hit = limit_hits(p["close"][i - 1], p["open"][i], args.limit, "exact")
                limit_exp += int(hit[side])
            if p["volume"] is not None and p["volume"][i] == 0:
                susp_exp += 1
    sig_w = [1.0 if b else 0.0 for b in mine["buy"]]
    return {"engine": eng, "mine": mine, "rows": rows, "limit_exposed": limit_exp,
            "susp_exposed": susp_exp,
            "d_eq": max(abs(a - b) for a, b in zip(eng["equity"], mine["equity"])),
            "t1_bad_engine": check_fill_after_signal(p, sig_w, eng["trades"]),
            "t1_bad_mine": check_fill_after_signal(p, sig_w, mine["trades"])}


# ------------------------------------------------------------------ 引擎侧
def run_engine(p, strategy, symbol, cost, limit_pct, engine_limit):
    """调用现有引擎;返回 {"equity": [...], "trades": [...]},格式与复算一致。"""
    sys.path.insert(0, str(ROOT))
    import pandas as pd
    from src import strategies
    from src.portfolio import run_weights

    idx = pd.DatetimeIndex([dt.datetime.combine(d, dt.time()) for d in p["dates"]], name="Date")
    df = pd.DataFrame({"Open": p["open"], "High": p["high"], "Low": p["low"], "Close": p["close"],
                       "Volume": p["volume"] if p["volume"] is not None else 1.0}, index=idx)
    target = getattr(strategies, strategy)(df)
    limits = {symbol: limit_pct} if (engine_limit and limit_pct > 0) else None
    res = run_weights({symbol: df}, target, cost=cost, limits=limits)
    trades = []
    for r in res.trades.itertuples():
        i1 = list(idx).index(r.exit_date)
        trades.append({"entry": r.entry_date.date(), "exit": r.exit_date.date(), "bars": r.bars,
                       "ret": float(r.ret), "open": bool(i1 == len(idx) - 1 and res.weights.iloc[-1, 0] > 0)})
    return {"equity": [float(x) for x in res.equity], "trades": trades}


# ------------------------------------------------------------------ 对比与归因
def same_trade(a, b, tol):
    if a is None or b is None:
        return a is None and b is None
    return a["exit"] == b["exit"] and abs(a["ret"] - b["ret"]) <= tol


def compare(p, strategy, args, symbol):
    weights = RECOMPUTE_SIGNALS[strategy](p)
    engine_limit = args.engine_limit == "on"
    lim = args.limit
    base = dict(buy_cost=args.recompute_cost, sell_cost=args.recompute_cost + args.stamp_tax,
                engine_cost=args.cost, limit_pct=lim,
                limit_mode="exact" if lim > 0 else "off",
                honor_susp=not args.no_suspend, lag=args.lag, fee_mode="notional")
    eng_limit_mode = "engine" if (engine_limit and lim > 0) else "off"
    aligners = {
        "limit": lambda c: c.update(limit_mode=eng_limit_mode),
        "suspend": lambda c: c.update(honor_susp=False),
        "cost": lambda c: c.update(buy_cost=args.cost, sell_cost=args.cost),
        "lag": lambda c: c.update(lag=1),
        "drift_fee": lambda c: c.update(fee_mode="target"),
    }
    eng = run_engine(p, strategy, symbol, args.cost, lim, engine_limit)
    sim = simulate(p, weights, **base)
    singles, all_cfg = {}, dict(base)
    for name, fn in aligners.items():
        cfg = dict(base)
        fn(cfg)
        singles[name] = simulate(p, weights, **cfg)
        fn(all_cfg)
    aligned = simulate(p, weights, **all_cfg)

    keyed = lambda ts: {t["entry"]: t for t in ts}
    e_t, s_t, a_t = keyed(eng["trades"]), keyed(sim["trades"]), keyed(aligned["trades"])
    singles_t = {k: keyed(v["trades"]) for k, v in singles.items()}
    rows = []
    for d in sorted(set(e_t) | set(s_t) | set(a_t)):
        e, s, a = e_t.get(d), s_t.get(d), a_t.get(d)
        diff = not same_trade(e, s, args.tol)
        causes, residual = [], False
        if diff:
            for name, tr in singles_t.items():
                if not same_trade(tr.get(d), s, args.tol):
                    causes.append(name)
            residual = not same_trade(e, a, args.tol)
        rows.append({"entry": d, "engine": e, "recompute": s, "diff": diff,
                     "causes": causes, "residual": residual})
    n = len(p["close"])
    d_eq = [abs(eng["equity"][i] - sim["equity"][i]) for i in range(n)]
    d_eq_al = [abs(eng["equity"][i] - aligned["equity"][i]) for i in range(n)]
    effect = {k: singles[k]["equity"][-1] - sim["equity"][-1] for k in singles}
    # 涨跌停的类别取决于引擎有没有传 limits:没传 → 建模缺失;传了但近似判定不同 → 定义差异
    limit_kind = "定义差异" if engine_limit else "建模缺失"
    return {"strategy": strategy, "weights": weights, "engine": eng, "sim": sim, "aligned": aligned,
            "rows": rows, "d_eq": d_eq, "d_eq_aligned": d_eq_al, "effect": effect, "base": base,
            "eng_limit_mode": eng_limit_mode, "limit_kind": limit_kind,
            "t1_bad_engine": check_fill_after_signal(p, weights, eng["trades"], 1),
            "t1_bad_sim": check_fill_after_signal(p, weights, sim["trades"], args.lag)}


def check_t1(trades):
    """出场成交日必须晚于进场成交日。返回违规的进场日列表(只查同根 K 线先买后卖)。"""
    return [t["entry"] for t in trades if t["exit"] <= t["entry"]]


def classify(row, limit_kind="定义差异/建模缺失"):
    if not row["diff"]:
        return "一致", ""
    cat = lambda c: limit_kind if c == "limit" else ASPECTS[c][0]
    parts = []
    if row["residual"]:
        parts.append("疑似 bug(所有已知口径对齐后仍不同)")
    for c in row["causes"]:
        parts.append(f"{cat(c)}:{ASPECTS[c][1]}")
    if not parts:
        parts.append("疑似 bug(找不到单一口径解释)")
    kind = "疑似bug" if row["residual"] or not row["causes"] else "/".join(
        sorted({cat(c) for c in row["causes"]}))
    return kind, ";".join(parts)


# ------------------------------------------------------------------ 报告
def fmt_pct(x):
    return "-" if x is None else f"{x * 100:.4f}%"


def build_report(p, results, args, symbol, prices_path, bt=None):
    L = []
    w = L.append
    w("# T011 / S1 复算与引擎对账报告")
    w("")
    w(f"- 行情文件:`{prices_path}`({len(p['close'])} 根,{p['dates'][0]} ~ {p['dates'][-1]})")
    w(f"- 引擎费率(单边):{args.cost};复算费率:{args.recompute_cost};复算卖出印花税:{args.stamp_tax}")
    w(f"- 涨跌停幅度:{args.limit};引擎是否传入涨跌停:{args.engine_limit};成交滞后 lag={args.lag}")
    w(f"- 停牌处理:{'关闭' if args.no_suspend else ('Volume=0 视为停牌' if p['volume'] is not None else '无 Volume 列,无法识别')}")
    w(f"- 比较容差:{args.tol}")
    w("")
    w("## 复算口径(逐项列出)")
    w("")
    w("| 规则 | 复算的处理 | 引擎(src/portfolio.py::run_weights)的处理 |")
    w("|---|---|---|")
    w("| T+1 / 成交时点 | 第 t 天收盘出信号,第 t+lag 天开盘成交;买入当天不可能卖出 | 目标仓位 shift(1),次日开盘成交 |")
    w("| 费用 | 实际成交额 × 费率;卖出另加印花税;买卖分开 | 费率 × 目标仓位变化,对称,不含印花税/最低佣金 |")
    w("| 涨跌停 | 开盘价触及涨停价买不进、触及跌停价卖不出(精确到分);受阻则保留原仓位下次再试 | 开盘跳空 ≥ 幅度−0.002 近似判定,同样保留原仓位 |")
    w("| 停牌 | Volume=0 当天不成交 | 无停牌标志(靠数据层删行) |")
    w("| 整手 | 不处理(连续权重) | 不处理 |")
    w("| 现金利息 | 0 | 0(cash_ret=None) |")
    w("| 复权 | 使用传入价格原样,不做复权 | 同 |")
    w("")
    w("## 预注册的预期差异来源(先于运行写下)")
    w("")
    w("1. 费用口径:复算与引擎费率/印花税不同时,每次进出场的差异。")
    w("2. 漂移再平衡费用:连续仓位策略(donchian_s1)每天把仓位调回目标,复算按实际成交额收费,引擎按目标变化收费。0/1 仓位策略无此项。")
    w("3. 涨跌停:引擎用 0.002 容差近似;复算用精确涨跌停价;引擎未传 limits 时为建模缺失。")
    w("4. 停牌:引擎无标志,复算 Volume=0 不成交。")
    w("5. 成交滞后:lag≠1 时与引擎的固定次日开盘不同。")
    w("6. 缺行:两套实现都没有交易日历,缺的那天被当成不存在,信号与成交按相邻两行计算;停牌缺行与节假日无法区分(见下方数据缺口)。")
    w("7. backtest.py(ma_cross_adx_atr,整股 + ATR 止损)与 run_weights 是两套不同的撮合口径,不互相对比;它与复算规则一致,任何差异记疑似 bug。")
    w("以上以外的差异视为疑似 bug。归因用消融法:把复算里单个口径换成引擎口径,看该笔差异是否消失;全部对齐后仍有差异才算疑似 bug。")
    w("")
    gaps = find_gaps(p["dates"])
    w("## 数据缺口(缺行检测)")
    w("")
    if gaps:
        w("相邻两行之间缺少工作日(可能是节假日,也可能是停牌缺行;脚本没有交易日历,无法区分,需人工核对)。两套实现都对缺行视而不见,属于建模缺失:")
        w("")
        w("| 前一行 | 后一行 | 缺的工作日数 |")
        w("|---|---|---|")
        for a_, b_, k in gaps:
            w(f"| {a_} | {b_} | {k} |")
    else:
        w("无(相邻行之间没有缺失的工作日)。")
    w("")
    for res in results:
        w(f"## 策略 `{res['strategy']}`")
        w("")
        eng, sim, al = res["engine"], res["sim"], res["aligned"]
        diff_rows = [r for r in res["rows"] if r["diff"]]
        n_bug = sum(1 for r in diff_rows if classify(r, res["limit_kind"])[0] == "疑似bug")
        w(f"- 引擎交易笔数:{len(eng['trades'])};复算交易笔数:{len(sim['trades'])};对齐口径后复算:{len(al['trades'])}")
        w(f"- 有差异的笔数:{len(diff_rows)}(其中疑似 bug:{n_bug})")
        w(f"- 期末净值:引擎 {eng['equity'][-1]:.8f};复算 {sim['equity'][-1]:.8f};口径对齐后复算 {al['equity'][-1]:.8f}")
        w(f"- 逐日净值最大绝对差:复算 vs 引擎 {max(res['d_eq']):.3e};口径对齐后 vs 引擎 {max(res['d_eq_aligned']):.3e}")
        w(f"- 复算触发的受阻成交事件:{len(sim['events'])} 次")
        for who, key, trs in (("引擎", "t1_bad_engine", eng["trades"]), ("复算", "t1_bad_sim", sim["trades"])):
            bad = sorted(set(res[key]) | set(check_t1(trs)))
            w(f"- T+1 核对({who}的 {len(trs)} 笔:进场成交日前一天必须是持仓信号,出场成交日晚于进场成交日):"
              f"{'通过' if not bad else '违规 ' + ','.join(map(str, bad))}")
        if not any(res["weights"]):
            w("- 注意:该样本上策略全程没有出信号(样本可能过短,例如 rsi2_reversion 需要 ≥ 260 根),对账没有检验力。")
        w("")
        w("### 各口径对期末净值的影响(把该口径单独换成引擎口径,期末净值的变化)")
        w("")
        w("| 口径 | 说明 | 期末净值变化 |")
        w("|---|---|---|")
        for k, (cat, desc) in ASPECTS.items():
            w(f"| {k} | {desc} | {res['effect'][k]:+.3e} |")
        w("")
        w("### 逐笔交易对比(引擎值 vs 复算值 vs 差值 vs 原因)")
        w("")
        w("| 进场日 | 引擎出场 | 复算出场 | 引擎收益 | 复算收益 | 差值 | 分类 | 原因 |")
        w("|---|---|---|---|---|---|---|---|")
        for r in res["rows"]:
            e, s = r["engine"], r["recompute"]
            kind, why = classify(r, res["limit_kind"])
            dv = None if (e is None or s is None) else s["ret"] - e["ret"]
            w(f"| {r['entry']} | {e['exit'] if e else '-'} | {s['exit'] if s else '-'} | "
              f"{fmt_pct(e['ret'] if e else None)} | {fmt_pct(s['ret'] if s else None)} | "
              f"{'-' if dv is None else f'{dv * 100:+.4f}%'} | {kind} | {why} |")
        if not res["rows"]:
            w("| (无交易) | | | | | | | |")
        w("")
        w("### 复算的逐笔交易表")
        w("")
        w("| 进场日(成交) | 出场日(成交) | 持有根数 | 进场价(开盘) | 出场价(开盘) | 账户收益 | 是否未平仓 |")
        w("|---|---|---|---|---|---|---|")
        for t in sim["trades"]:
            w(f"| {t['entry']} | {t['exit']} | {t['bars']} | {t['entry_px']:.4f} | {t['exit_px']:.4f} | "
              f"{fmt_pct(t['ret'])} | {'是' if t['open'] else '否'} |")
        if not sim["trades"]:
            w("| (无交易) | | | | | | |")
        w("")
        w("### 受阻成交事件")
        w("")
        if sim["events"]:
            w("| 日期 | 事件 |")
            w("|---|---|")
            for d, ev in sim["events"]:
                w(f"| {d} | {ev} |")
        else:
            w("无。")
        w("")
        w("### 逐日净值(引擎 vs 复算)")
        w("")
        w("| 日期 | 目标仓位(信号) | 引擎净值 | 复算净值 | 差值 |")
        w("|---|---|---|---|---|")
        n = len(p["close"])
        show = range(n) if n <= args.max_nav_rows else list(range(args.max_nav_rows // 2)) + \
            list(range(n - args.max_nav_rows // 2, n))
        for i in show:
            w(f"| {p['dates'][i]} | {res['weights'][i]:.4f} | {eng['equity'][i]:.8f} | "
              f"{sim['equity'][i]:.8f} | {sim['equity'][i] - eng['equity'][i]:+.3e} |")
        if n > args.max_nav_rows:
            w(f"\n(共 {n} 行,只显示首尾各 {args.max_nav_rows // 2} 行)")
        w("")
    if bt is not None:
        w("## backtest.py 对账(src/backtest.py::run,ma_cross_adx_atr:整股 + ATR 止损 + 期末强平)")
        w("")
        n_bug = sum(1 for r in bt["rows"] if r["diff"])
        w(f"- 引擎交易笔数:{len(bt['engine']['trades'])};复算交易笔数:{len(bt['mine']['trades'])};有差异的笔数(疑似 bug):{n_bug}")
        w(f"- 逐日净值最大绝对差:{bt['d_eq']:.3e};期末净值:引擎 {bt['engine']['equity'][-1]:.4f};复算 {bt['mine']['equity'][-1]:.4f}")
        for who, key in (("引擎", "t1_bad_engine"), ("复算", "t1_bad_mine")):
            bad = bt[key]
            w(f"- T+1 核对({who}:进场成交日前一天必须是买入信号,出场晚于进场):{'通过' if not bad else '违规 ' + ','.join(map(str, bad))}")
        w(f"- 建模缺失暴露:成交落在涨跌停开盘的次数 {bt['limit_exposed']};落在停牌(Volume=0)日的次数 {bt['susp_exposed']}。"
          "backtest.py 两者都不处理,这些成交在真实市场可能成交不了。")
        w("- 与 run_weights 的定义差异(不是 bug):整股 vs 连续权重;ATR 止损 vs 无止损;期末按收盘强平 vs 保持持仓;佣金按成交额收取。")
        w("")
        w("| 进场日 | 引擎出场 | 复算出场 | 引擎原因 | 复算原因 | 引擎股数 | 复算股数 | 引擎盈亏 | 复算盈亏 | 分类 |")
        w("|---|---|---|---|---|---|---|---|---|---|")
        for r in bt["rows"]:
            e, m = r["engine"], r["recompute"]
            g = lambda t, k, f="{}": "-" if t is None else f.format(t[k])
            w(f"| {r['entry']} | {g(e, 'exit')} | {g(m, 'exit')} | {g(e, 'reason')} | {g(m, 'reason')} | "
              f"{g(e, 'shares')} | {g(m, 'shares')} | {g(e, 'pnl', '{:.4f}')} | {g(m, 'pnl', '{:.4f}')} | "
              f"{'疑似bug' if r['diff'] else '一致'} |")
        if not bt["rows"]:
            w("| (无交易) | | | | | | | | | |")
        w("")
    w("## 局限")
    w("")
    w("- 缺行(停牌缺行)无法识别,只能列出缺失的工作日让人复核;漂移再平衡日(目标仓位未变、持仓随价格漂移)复算不检查涨跌停,等于允许在涨停开盘时加仓。")
    w("- 样本小、合成或调用者指定,结果只说明两套实现在这批数据上是否一致,不说明策略有效,也不能外推到真实行情。")
    w("- 信号复算与引擎共享同一套指标定义(Wilder 平滑、种子取法),这是对照 AmiBroker 的约定;若定义本身有误,本脚本发现不了。")
    w("- 涨跌停仅看开盘价;盘中触板后成交、一字板判断、整手、最低佣金、过户费、滑点均未建模。")
    w("- 分类是机械规则给出的线索,疑似 bug 需要人工复核。")
    w("")
    return "\n".join(L)


# ------------------------------------------------------------------ 命令行
def parse_args(argv):
    ap = argparse.ArgumentParser(description="T011/S1:回测引擎复算与对账")
    ap.add_argument("--prices", required=True, help="行情 CSV(Date,Open,High,Low,Close[,Volume])")
    ap.add_argument("--strategy", required=True,
                    help="策略名,逗号分隔多个,或 all。可选:" + ",".join(STRATEGIES))
    ap.add_argument("--out", required=True, help="输出 Markdown 路径;已存在则拒绝覆盖")
    ap.add_argument("--symbol", default="SAMPLE")
    ap.add_argument("--cost", type=float, default=0.0005, help="引擎单边费率(默认 0.0005)")
    ap.add_argument("--recompute-cost", type=float, default=None, help="复算单边费率,默认同 --cost")
    ap.add_argument("--stamp-tax", type=float, default=0.0, help="复算卖出印花税,默认 0")
    ap.add_argument("--limit", type=float, default=0.10, help="涨跌停幅度,0 表示不处理(默认 0.10)")
    ap.add_argument("--engine-limit", choices=("on", "off"), default="on",
                    help="是否给引擎传入涨跌停(默认 on)")
    ap.add_argument("--lag", type=int, choices=(1, 2), default=1, help="复算的信号到成交滞后(默认 1=次日开盘)")
    ap.add_argument("--no-suspend", action="store_true", help="复算不识别停牌")
    ap.add_argument("--tol", type=float, default=1e-9)
    ap.add_argument("--max-nav-rows", type=int, default=400)
    ap.add_argument("--allow-data-dir", action="store_true", help="允许读 data/ 下的文件(默认拒绝)")
    a = ap.parse_args(argv)
    if a.recompute_cost is None:
        a.recompute_cost = a.cost
    for name in ("cost", "recompute_cost", "stamp_tax"):
        v = getattr(a, name)
        if not math.isfinite(v) or v < 0 or v >= 0.1:
            raise InputError(f"--{name.replace('_', '-')} 必须在 [0, 0.1) 内:{v}")
    if not (math.isfinite(a.limit) and 0 <= a.limit < 1):
        raise InputError(f"--limit 必须在 [0, 1) 内:{a.limit}")
    if not (a.tol > 0):
        raise InputError("--tol 必须为正")
    names = list(STRATEGIES) if a.strategy.strip() == "all" else [s.strip() for s in a.strategy.split(",")]
    for s in names:
        if s not in RECOMPUTE_SIGNALS:
            raise InputError(f"未知策略 {s!r};可选:{', '.join(STRATEGIES)}")
    a.strategies = names
    return a


def main(argv=None):
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        out = Path(args.out)
        if out.exists():
            raise InputError(f"拒绝覆盖已有输出:{out}")
        if not args.allow_data_dir and _is_under(out, ROOT / "data"):
            raise InputError(f"默认不写 data/ 下的路径:{out}(确需写入请加 --allow-data-dir)")
        if not out.parent.is_dir():
            raise InputError(f"输出目录不存在:{out.parent}")
        prices_path = Path(args.prices)
        p = read_prices(prices_path, allow_data_dir=args.allow_data_dir)
        results = [compare(p, s, args, args.symbol) for s in args.strategies]
        bt = compare_backtest_py(p, args) if "ma_adx_original" in args.strategies else None
        text = build_report(p, results, args, args.symbol, prices_path, bt)
        with open(out, "x", encoding="utf-8") as f:     # "x":并发下也不会覆盖
            f.write(text)
    except InputError as e:
        print(f"错误:{e}", file=sys.stderr)
        return 2
    except FileExistsError as e:
        print(f"错误:拒绝覆盖已有输出:{e}", file=sys.stderr)
        return 2
    print(f"报告已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
