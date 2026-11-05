#!/usr/bin/env python3
"""公开站演示数据生成器（D26）：用【同一个】合成市场，一次生成 今日 / 全市场 / 信号 三份 mock。

为什么要有它：
  以前三份 JSON 是分别手写的，同一天的数字互相矛盾（例：10-23 今日页半导体 +2.68%，全市场页 −2.76%；
  涨跌家数、站上 20 日线占比、触发说明的中英文数字也对不上）。现在所有数字都从一份模拟价格推出来，
  只要生成器不改，页面之间就不可能再矛盾；scripts/build_public_site.py 构建时还会再核对一遍。

合成方式（全部虚构，不对应任何真实股票）：
  - 5312 只虚构股票，代码 990001 起（交易所不会分配 99 开头的 A 股代码），名字是「<行业>示例 NN」。
  - 日收益 = 市场因子 + 行业因子 + 个股噪声（单因子模型），按 A 股主板 ±10% 截断；共 300 个交易日。
  - 信号用个人站真实存在的 6 个单票策略的【真实规则】在合成价格上算（规则见 src/strategies.py，
    与 revquant 审计 overnight/reviews/revquant_numbers.md Part C 一致）；触发说明中英文用同一个数生成。
  - 只用当根收盘及以前的数据（和真实系统一样没有未来函数）。

用法（项目根目录）：
  .venv/bin/python scripts/public_demo_snapshot.py          写入 dashboard/mockups/mock-data/pub-{today,market,signals}.json
  .venv/bin/python scripts/public_demo_snapshot.py --check  只生成到内存并和磁盘上的文件比较（不一致就非 0 退出）
写完之后要重新钉哈希：.venv/bin/python scripts/build_public_site.py --pin-report，人工核对后改 scripts/public_allowlist.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MOCK = ROOT / "dashboard" / "mockups" / "mock-data"
SEED = 20261023                       # 固定随机种子：同一个种子永远生成同一份数据
N_DAYS = 300                          # 交易日数（MA250、一年波动分位都够用）
CAL = [str(d) for d in np.busday_offset("2026-10-23", np.arange(-(300 - 1), 1), roll="backward")]   # 合成交易日历（周一到周五，不含节假日）
DATES = (CAL[-2], CAL[-1])             # 最后两个交易日 2026-10-22 / 2026-10-23：游客看 [0]，注册用户看 [1]
LIMIT = 0.10                          # 主板涨跌停 ±10%（演示数据不区分板块）
FLAT_EPS = 0.05 / 100                 # 平盘阈值：|涨跌幅| < 0.05% 算平盘（不是费率；写成百分数换算，免得被当成费率字面量）

# 28 个行业（中英文）和各自的股票数；合计 5312
SECTORS = [
    ("交运", "Transport", 139), ("传媒", "Media", 168), ("保险", "Insurance", 12), ("军工", "Defense", 143),
    ("农业", "Agriculture", 102), ("化工", "Chemicals", 402), ("医疗器械", "Med devices", 150), ("医药", "Pharma", 330),
    ("半导体", "Semiconductors", 160), ("地产", "Real estate", 103), ("家电", "Appliances", 92), ("建材", "Materials", 71),
    ("建筑", "Construction", 171), ("有色", "Metals", 141), ("机械", "Machinery", 520), ("汽车", "Autos", 268),
    ("煤炭", "Coal", 38), ("环保", "Environment", 124), ("电力", "Utilities", 133), ("电力设备", "Power equipment", 361),
    ("电子", "Electronics", 482), ("白酒", "Liquor", 20), ("石油", "Oil", 47), ("证券", "Brokers", 50),
    ("软件", "Software", 290), ("通信", "Telecom", 124), ("银行", "Banks", 42), ("食品饮料", "Food & beverage", 119),
]

# 用到的策略 id 必须在 pub-strategies.json 里存在（策略百科的锚点）
STRAT_NAMES: dict = {}
STRATS = ("ma_adx_original", "ma_adx_plus", "trend_vol", "donchian_s1", "rsi2", "trend_ens")


# ---------------------------------------------------------------- 指标（全部只用 t 及以前）
def sma(x: np.ndarray, n: int) -> np.ndarray:
    """简单均线，按列（每只股票一列）；前 n-1 天为 nan。"""
    c = np.cumsum(np.vstack([np.zeros((1, x.shape[1])), x]), axis=0)
    out = np.full_like(x, np.nan)
    out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def wilder(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder 平滑（RSI/ATR/ADX 用），第 n 天起有值。"""
    out = np.full_like(x, np.nan)
    out[n - 1] = np.nanmean(x[:n], axis=0)
    for t in range(n, len(x)):
        out[t] = out[t - 1] + (x[t] - out[t - 1]) / n
    return out


def rsi(close: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(close, axis=0, prepend=close[:1])
    up, dn = wilder(np.clip(d, 0, None), n), wilder(np.clip(-d, 0, None), n)
    return 100 - 100 / (1 + up / np.where(dn == 0, 1e-12, dn))


def atr_adx(high, low, close, n: int = 14):
    prev = np.vstack([close[:1], close[:-1]])
    tr = np.maximum(high - low, np.maximum(abs(high - prev), abs(low - prev)))
    up_move = high - np.vstack([high[:1], high[:-1]])
    dn_move = np.vstack([low[:1], low[:-1]]) - low
    pdm = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
    ndm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)
    atr = wilder(tr, n)
    pdi, ndi = 100 * wilder(pdm, n) / atr, 100 * wilder(ndm, n) / atr
    dx = 100 * abs(pdi - ndi) / np.where(pdi + ndi == 0, 1e-12, pdi + ndi)
    adx = np.full_like(dx, np.nan)
    adx[2 * n - 2:] = wilder(dx[n - 1:], n)[n - 1:]
    return wilder(tr, 20), adx, atr                  # ATR(20)、ADX(14)、ATR(14)


def rolling_max(x, n):
    out = np.full_like(x, np.nan)
    for t in range(n - 1, len(x)):
        out[t] = x[t - n + 1:t + 1].max(axis=0)
    return out


def rolling_min(x, n):
    out = np.full_like(x, np.nan)
    for t in range(n - 1, len(x)):
        out[t] = x[t - n + 1:t + 1].min(axis=0)
    return out


# ---------------------------------------------------------------- 合成市场
def simulate(rng: np.random.Generator):
    sec_idx = np.concatenate([[k] * n for k, (_, _, n) in enumerate(SECTORS)])
    n_stocks = len(sec_idx)
    mkt = rng.normal(0.0003, 0.011, N_DAYS)                           # 市场因子
    sec = rng.normal(0, 0.008, (N_DAYS, len(SECTORS)))                 # 行业因子
    beta = rng.uniform(0.6, 1.4, n_stocks)
    # 个股噪声用自由度 3 的 t 分布（肥尾），这样才会有涨停/跌停的股票，和真实 A 股的日截面更像
    idio = rng.standard_t(3, (N_DAYS, n_stocks)) * 0.012 * rng.uniform(0.6, 1.6, n_stocks)
    ret = np.clip(mkt[:, None] * beta + sec[:, sec_idx] + idio, -LIMIT, LIMIT)
    ret[0] = 0
    close = rng.uniform(4, 80, n_stocks) * np.cumprod(1 + ret, axis=0)
    rng_hl = np.abs(rng.normal(0, 0.012, (N_DAYS, n_stocks)))
    high = close * (1 + rng_hl)
    low = close * (1 - np.abs(rng.normal(0, 0.012, (N_DAYS, n_stocks))))
    turnover = rng.lognormal(0.3, 0.9, n_stocks) * (1 + 3 * np.abs(ret))  # 每只每天的成交额（亿元），涨跌越大成交越多
    pe = np.round(rng.uniform(8, 60, n_stocks), 1)
    return sec_idx, ret, close, high, low, turnover, pe, mkt


def names(sec_idx):
    out, cnt = [], {}
    for i, k in enumerate(sec_idx):
        cnt[k] = cnt.get(k, 0) + 1
        zh, en, _ = SECTORS[k]
        out.append((f"{990001 + i}", f"{zh}示例 {cnt[k]:02d}", f"Demo {en} {cnt[k]:02d}"))
    return out


# ---------------------------------------------------------------- 策略：真实规则在合成数据上的进出场
def strategy_states(close, high, low):
    """返回 {策略: (入场条件, 出场条件, 触发说明函数)}，都是 [日, 股票] 的布尔矩阵。"""
    ma5, ma20, ma200 = sma(close, 5), sma(close, 20), sma(close, 200)
    ma100, ma150, ma250 = sma(close, 100), sma(close, 150), sma(close, 250)
    atr20, adx, atr14 = atr_adx(high, low, close)
    r2 = rsi(close, 2)
    hh20 = np.vstack([np.full((1, close.shape[1]), np.nan), rolling_max(high, 20)[:-1]])    # 过去 20 日最高价（不含当日）
    ll10 = np.vstack([np.full((1, close.shape[1]), np.nan), rolling_min(low, 10)[:-1]])     # 过去 10 日最低价（不含当日）
    prev = lambda a: np.vstack([a[:1], a[:-1]])
    cross_up = (ma5 > ma20) & (prev(ma5) <= prev(ma20))
    cross_dn = (ma5 < ma20) & (prev(ma5) >= prev(ma20))
    n_above = sum((close > m * 1.01).astype(int) for m in (ma100, ma150, ma200, ma250))
    f = lambda v, d=2: f"{v:.{d}f}"
    return {
        "ma_adx_original": (cross_up & (adx > 20), cross_dn,
                            lambda t, j: (f"MA5 {f(ma5[t, j])} 上穿 MA20 {f(ma20[t, j])}，ADX14 = {adx[t, j]:.0f} > 20",
                                          f"MA5 {f(ma5[t, j])} crossed above MA20 {f(ma20[t, j])}, ADX14 = {adx[t, j]:.0f} > 20"),
                            lambda t, j: (f"MA5 {f(ma5[t, j])} 下穿 MA20 {f(ma20[t, j])}",
                                          f"MA5 {f(ma5[t, j])} crossed below MA20 {f(ma20[t, j])}")),
        "ma_adx_plus": (cross_up & (adx > 20) & (close > ma200), cross_dn | (close < ma200),
                        lambda t, j: (f"MA5 上穿 MA20，ADX14 = {adx[t, j]:.0f} > 20，收盘 {f(close[t, j])} > MA200 {f(ma200[t, j])}",
                                      f"MA5 crossed above MA20, ADX14 = {adx[t, j]:.0f} > 20, close {f(close[t, j])} > MA200 {f(ma200[t, j])}"),
                        lambda t, j: (f"MA5 下穿 MA20 或收盘 {f(close[t, j])} 跌破 MA200 {f(ma200[t, j])}",
                                      f"MA5 crossed below MA20 or close {f(close[t, j])} < MA200 {f(ma200[t, j])}")),
        "trend_vol": (close > ma200 * 1.01, close < ma200 * 0.99,
                      lambda t, j: (f"收盘 {f(close[t, j])} 高于 MA200 {f(ma200[t, j])} 1% 以上",
                                    f"close {f(close[t, j])} more than 1% above MA200 {f(ma200[t, j])}"),
                      lambda t, j: (f"收盘 {f(close[t, j])} 低于 MA200 {f(ma200[t, j])} 1% 以上",
                                    f"close {f(close[t, j])} more than 1% below MA200 {f(ma200[t, j])}")),
        "donchian_s1": (close > hh20, close < ll10,
                        lambda t, j: (f"收盘 {f(close[t, j])} 突破过去 20 日最高 {f(hh20[t, j])}",
                                      f"close {f(close[t, j])} broke the 20-day high {f(hh20[t, j])}"),
                        lambda t, j: (f"收盘 {f(close[t, j])} 跌破过去 10 日最低 {f(ll10[t, j])}",
                                      f"close {f(close[t, j])} broke the 10-day low {f(ll10[t, j])}")),
        "rsi2": ((close > ma200) & (r2 < 10), close > ma5,
                 lambda t, j: (f"收盘 {f(close[t, j])} > MA200 {f(ma200[t, j])}，RSI(2) = {r2[t, j]:.1f} < 10",
                               f"close {f(close[t, j])} > MA200 {f(ma200[t, j])}, RSI(2) = {r2[t, j]:.1f} < 10"),
                 lambda t, j: (f"收盘 {f(close[t, j])} 高于 MA5 {f(ma5[t, j])}",
                               f"close {f(close[t, j])} above MA5 {f(ma5[t, j])}")),
        "trend_ens": (n_above >= 3, n_above <= 1,
                      lambda t, j: (f"站上 {n_above[t, j]}/4 条均线（MA100/150/200/250）",
                                    f"above {n_above[t, j]}/4 MAs (100/150/200/250)"),
                      lambda t, j: (f"只站上 {n_above[t, j]}/4 条均线（MA100/150/200/250）",
                                    f"above only {n_above[t, j]}/4 MAs (100/150/200/250)")),
    }, atr20, atr14


def decisions(entry, exit_, close, max_hold=None, stop=None):
    """逐日推出【收盘后】的持仓决定 dec[t]（0/1，只用 t 及以前的数据）和离场原因 why[t]：
       空仓时入场条件成立 → 持有；持仓时出场条件成立、持有满 max_hold 天、或跌破吊灯止损 → 空仓。
       stop = (倍数, ATR 矩阵)：收盘 < 入场以来最高收盘（含当天）− 倍数 × ATR 时止损。
       实际持仓 pos[t] = dec[t-1]（下一根成交；演示里按收盘价成交，简化）。"""
    T, n = entry.shape
    dec = np.zeros((T, n), dtype=bool)
    why = np.zeros((T, n), dtype=np.int8)          # 0 无 / 1 出场条件 / 2 持有满天数 / 3 吊灯止损
    held = np.zeros(n, dtype=int)
    peak = np.full(n, -np.inf)
    for t in range(1, T):
        prev = dec[t - 1]
        peak = np.where(prev, np.maximum(peak, close[t]), close[t])
        out_rule = exit_[t]
        out_hold = (held >= max_hold) if max_hold else np.zeros(n, bool)   # 入场后满 max_hold 天才离场（revquant：原来早 1 天）
        out_stop = (close[t] < peak - stop[0] * stop[1][t]) if stop else np.zeros(n, bool)
        stay = prev & ~out_rule & ~out_hold & ~out_stop
        dec[t] = np.where(prev, stay, entry[t])
        why[t] = np.where(prev & ~stay, np.where(out_stop, 3, np.where(out_rule, 1, 2)), 0)
        held = np.where(dec[t], np.where(prev, held + 1, 1), 0)
        peak = np.where(dec[t], peak, -np.inf)
    return dec, why


def trade_stats(pos, ret, j):
    """单只股票历史上按该策略的完整交易：胜率、次数、平均持有天数（只算已经平仓的交易）。"""
    p, r = pos[:, j], ret[:, j]
    trades, start = [], None
    for t in range(len(p)):
        if p[t] and start is None:
            start = t
        if not p[t] and start is not None:
            trades.append((np.prod(1 + r[start:t]) - 1, t - start))
            start = None
    if not trades:
        return 0.0, 0, 0
    wins = sum(1 for x, _ in trades if x > 0)
    return round(wins / len(trades), 2), len(trades), int(round(np.mean([d for _, d in trades])))


# ---------------------------------------------------------------- 组装三份 JSON
def registry_labels() -> dict:
    """D77：策略中英文名的唯一来源是 src/registry.py 的 STRATEGIES[k]["label"] / ["label_en"]。
    用 ast 读字面量，不 import（不拉起回测依赖）。返回 {id: {"name": 中文名, "name_en": 英文名}}。"""
    import ast
    tree = ast.parse((ROOT / "src" / "registry.py").read_text(encoding="utf-8"))
    zh, en = {}, {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Dict):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        name = next((getattr(t, "id", None) for t in targets), None)
        if name == "STRATEGIES":                      # 中文名：STRATEGIES[k]["label"]
            for k, v in zip(node.value.keys, node.value.values):
                f = {kk.value: vv.value for kk, vv in zip(v.keys, v.values)
                     if isinstance(kk, ast.Constant) and isinstance(vv, ast.Constant)}
                zh[k.value] = f.get("label", k.value)
        elif name == "STRATEGY_EN":                   # 英文名：STRATEGY_EN[k] = (label_en, desc_en)，启动时写进 STRATEGIES[k]["label_en"]
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(v, ast.Tuple) and v.elts and isinstance(v.elts[0], ast.Constant):
                    en[k.value] = v.elts[0].value
    if not zh:
        raise RuntimeError("src/registry.py 里找不到 STRATEGIES")
    missing = sorted(set(zh) - set(en))
    if missing:                                       # 英文名缺了就直接报错，不再悄悄用 id 顶替（D77 第一版的错误）
        raise RuntimeError(f"src/registry.py 的 STRATEGY_EN 缺少英文名：{missing}")
    return {k: {"name": zh[k], "name_en": en[k]} for k in zh}


def sync_encyclopedia_names(text: str, labels: dict) -> str:
    """把 pub-strategies.json 每张卡的 name / name_en 改成 registry 里的名字（只改这两个字段，格式不变）。"""
    d = json.loads(text)
    for x in d["list"]:
        if x["id"] in labels:
            x["name"], x["name_en"] = labels[x["id"]]["name"], labels[x["id"]]["name_en"]
    return json.dumps(d, ensure_ascii=False, indent=1) + "\n"


def build():
    global STRAT_NAMES
    # D77：策略显示名取 registry（个人站和公开站同一个名字）
    STRAT_NAMES = registry_labels()
    rng = np.random.default_rng(SEED)
    sec_idx, ret, close, high, low, turnover, pe, mkt = simulate(rng)
    ids = names(sec_idx)
    states, atr20, atr14 = strategy_states(close, high, low)
    ma20 = sma(close, 20)
    rules = {"rsi2": dict(max_hold=10), "donchian_s1": dict(stop=(2, atr20)), "ma_adx_plus": dict(stop=(3, atr14))}
    dec, why = {}, {}
    for k, (e, x, *_r) in states.items():
        dec[k], why[k] = decisions(e, x, close, **rules.get(k, {}))
    # 实际持仓：前一天收盘后的决定（用于历史胜率和 90 日夏普）
    pos = {k: np.vstack([np.zeros((1, d.shape[1]), bool), d[:-1]]) for k, d in dec.items()}
    T = {DATES[0]: N_DAYS - 2, DATES[1]: N_DAYS - 1}

    idx_close = np.cumprod(1 + mkt)                                   # 市场指数（只用于市场状态和波动分位）
    idx_ma20 = np.convolve(idx_close, np.ones(20) / 20, "valid")
    idx_ma20 = np.concatenate([np.full(19, np.nan), idx_ma20])
    vol20 = np.array([np.std(mkt[max(0, t - 19):t + 1]) for t in range(N_DAYS)])

    def stop_level(k, t, j):
        m, a = rules[k]["stop"]
        start = t - 1
        while start > 0 and dec[k][start - 1, j]:
            start -= 1
        return float(close[start:t + 1, j].max() - m * a[t, j])

    def day(date):
        t = T[date]
        r = ret[t]
        up, down = int((r > FLAT_EPS).sum()), int((r < -FLAT_EPS).sum())
        market = {
            "up": up, "down": down, "flat": int(len(r) - up - down),
            "limit_up": int((r >= LIMIT - 1e-9).sum()), "limit_down": int((r <= -LIMIT + 1e-9).sum()),
            "turnover_bn": round(float(turnover[t].sum()), 0),
            "above_ma20_pct": round(float((close[t] > ma20[t]).mean()), 2),
        }
        sec_chg = [round(float(r[sec_idx == k].mean() * 100), 2) for k in range(len(SECTORS))]
        order = np.argsort(sec_chg)
        on = idx_close[t] > idx_ma20[t]
        since_t = t
        while since_t > 20 and (idx_close[since_t - 1] > idx_ma20[since_t - 1]) == on:
            since_t -= 1
        regime = {
            "state": "risk_on" if on else "risk_off",
            "breadth": round(up / (up + down), 2),                   # 宽度 = 上涨家数 ÷ (上涨 + 下跌)
            "vol_pct": int(round((vol20[t - 249:t + 1] < vol20[t]).mean() * 100)),   # 近一年波动分位
            "since": CAL[since_t],                                     # 当前市场状态从哪天开始
        }
        # 信号 = 当天收盘后持仓决定发生变化（空仓→持有 = 买入，持有→空仓 = 卖出），按强度排序取前 40
        sigs = []
        for k, (entry, exit_, txt_in, txt_out) in states.items():
            fresh_in = dec[k][t] & ~dec[k][t - 1]
            fresh_out = ~dec[k][t] & dec[k][t - 1]
            for j in np.flatnonzero(fresh_in | fresh_out):
                action = "buy" if fresh_in[j] else "sell"
                reason = int(why[k][t, j])
                if action == "buy" or reason == 1:
                    zh, en = (txt_in if action == "buy" else txt_out)(t, j)
                elif reason == 2:
                    zh, en = "已持有满 10 个交易日", "held for 10 trading days"
                else:
                    m, a = rules[k]["stop"]
                    zh = f"收盘 {close[t, j]:.2f} 跌破吊灯止损（入场后最高收盘 − {m}×ATR = {stop_level(k, t, j):.2f}）"
                    en = f"close {close[t, j]:.2f} fell below the chandelier stop (highest close since entry − {m}×ATR = {stop_level(k, t, j):.2f})"
                w, n, hold = trade_stats(pos[k][:t + 1], ret, j)
                sigs.append({
                    "code": ids[j][0], "name": ids[j][1], "name_en": ids[j][2],
                    "sector": SECTORS[sec_idx[j]][0], "sector_en": SECTORS[sec_idx[j]][1],
                    "strategy": k, "action": action,
                    "strength": None,                                  # 下面按截面排名填
                    "_score": float(abs(r[j])),
                    "close": round(float(close[t, j]), 2), "chg_pct": round(float(r[j] * 100), 2),
                    "atr_stop": round(float(close[t, j] - 2 * atr20[t, j]), 2),
                    "target_weight": 0.05 if action == "buy" else 0,
                    "hist_winrate": w, "hist_n": n, "hold_days_avg": hold,
                    "trigger": zh, "trigger_en": en,
                })
        rng_s = np.random.default_rng(SEED + t)
        rng_s.shuffle(sigs)
        sigs.sort(key=lambda s: -s["_score"])
        sigs = sigs[:40]
        for i, s in enumerate(sigs):
            s["strength"] = round(0.98 - 0.6 * i / max(1, len(sigs) - 1), 2)
            del s["_score"]
        return t, market, regime, sec_chg, order, sigs

    days = {d: day(d) for d in DATES}

    def today_block(date, gen):
        t, market, regime, sec_chg, order, sigs = days[date]
        top = [[SECTORS[k][0], SECTORS[k][1], sec_chg[k]] for k in order[::-1][:3]]
        bot = [[SECTORS[k][0], SECTORS[k][1], sec_chg[k]] for k in order[:3]]
        strat_top = []
        for k in STRATS:                                               # 近 90 日：策略在全体股票上等权持仓的年化夏普（合成、样本内、未扣成本、无风险利率按 0）
            p = pos[k][t - 89:t + 1].astype(float)
            daily = (p * ret[t - 89:t + 1]).sum(axis=1) / np.maximum(p.sum(axis=1), 1)
            sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
            nm = STRAT_NAMES.get(k, {})
            strat_top.append({"id": k, "name": nm.get("name", k), "name_en": nm.get("name_en", k),
                              "sharpe90": round(sharpe, 2), "n_signals": sum(1 for s in sigs if s["strategy"] == k)})
        strat_top = sorted(strat_top, key=lambda s: -s["sharpe90"])[:5]
        prev_date = DATES[0] if date == DATES[1] else "2026-10-21"
        n_prev = len(days[DATES[0]][5]) if date == DATES[1] else 40
        return {
            "date": date, "generated_at": f"{date} {gen}", "regime": regime, "market": market,
            "sectors_top": top, "sectors_bottom": bot, "signals": sigs, "strategies_top": strat_top,
            "reports": [
                {"id": "eod", "line": "收盘复盘：上涨家数占比 {b}。", "line_en": "EOD review: advancers share {b}."},
                {"id": "backfill", "line": f"信号回填：{prev_date} 的 {n_prev} 条信号已按收盘价回填，无缺失。",
                 "line_en": f"Signal backfill: {n_prev} signals from {prev_date} filled at close, none missing."},
            ],
        }

    meta = lambda f, schema: {
        "file": f"mock-data/{f}.json", "version": "0.2.0", "updated": DATES[1], "author": "scripts/public_demo_snapshot.py",
        "schema": schema,
        "note": "合成数据，不是真实行情、回测或建议；所有股票代码和名字都是虚构的（99 开头、「示例」字样）。"
                "由 scripts/public_demo_snapshot.py 用同一个合成市场一次生成，今日 / 全市场 / 信号三页的数字互相一致。",
    }
    today = {"_meta": meta("pub-today", "Public 今日：today = 最新交易日，yesterday = 上一交易日（游客看到的版本）"),
             "today": today_block(DATES[1], "19:40"), "yesterday": today_block(DATES[0], "19:40")}
    signals = {"_meta": meta("pub-signals", "Public 信号：days[0] = 最新交易日，days[1] = 上一交易日；每日最多 40 条，按强度排序"),
               "days": [{"date": DATES[1], "signals": today["today"]["signals"]},
                        {"date": DATES[0], "signals": today["yesterday"]["signals"]}]}

    t, market, _, sec_chg, _, sigs = days[DATES[1]]
    sig_by_code = {s["code"]: s for s in sigs}
    sig_j = sorted(int(c) - 990001 for c in sig_by_code)              # 今天有信号的股票一定在筛选表里
    others = np.setdiff1d(np.arange(len(ids)), sig_j)
    pick = sorted(sig_j + rng.choice(others, 120 - len(sig_j), replace=False).tolist())
    row = lambda j: {
        "code": ids[j][0], "name": ids[j][1], "name_en": ids[j][2],
        "sector": SECTORS[sec_idx[j]][0], "sector_en": SECTORS[sec_idx[j]][1],
        "close": round(float(close[t, j]), 2), "chg_pct": round(float(ret[t, j] * 100), 2),
        "turnover_bn": round(float(turnover[t, j]), 1), "pe": float(pe[j]),
        "signal": sig_by_code[ids[j][0]]["action"] if ids[j][0] in sig_by_code else None,
        "strategy": sig_by_code[ids[j][0]]["strategy"] if ids[j][0] in sig_by_code else None,
    }
    rows = [row(j) for j in pick]
    wl = lambda js, start: [{"code": ids[j][0], "name": ids[j][1], "name_en": ids[j][2],
                             "close": round(float(close[t, j]), 2), "chg_pct": round(float(ret[t, j] * 100), 2),
                             "signal": sig_by_code[ids[j][0]]["action"] if ids[j][0] in sig_by_code else None,
                             "added": CAL[-60 + (i * 7 + start) % 55]}
                            for i, j in enumerate(js)]
    market_doc = {
        "_meta": meta("pub-market", "Public 全市场：收盘后扫描汇总 + 行业 + 个股筛选（120 只示例）+ 自选示例"),
        "date": DATES[1], "summary": market,
        "sectors": sorted([{"name": SECTORS[k][0], "name_en": SECTORS[k][1], "chg_pct": sec_chg[k],
                            "turnover_bn": round(float(turnover[t, sec_idx == k].sum()), 0), "n": int((sec_idx == k).sum())}
                           for k in range(len(SECTORS))], key=lambda s: s["name"]),
        "rows": rows,
        "watchlist": {"free": wl(pick[:14], 0), "pro": wl(pick[:27], 0)},
        # D51：上一交易日的汇总和行业（游客看这一份，和今日页的 yesterday 一致）
        "previous": {"date": DATES[0], "summary": dict(days[DATES[0]][1]),   # 复制一份：两份文件各自独立
                     "sectors": sorted([{"name": SECTORS[k][0], "name_en": SECTORS[k][1], "chg_pct": days[DATES[0]][3][k],
                                         "turnover_bn": round(float(turnover[days[DATES[0]][0], sec_idx == k].sum()), 0),
                                         "n": int((sec_idx == k).sum())} for k in range(len(SECTORS))], key=lambda s: s["name"])},
    }
    return {"pub-today": today, "pub-market": market_doc, "pub-signals": signals}


def landing_market_text(today_doc: dict) -> str:
    """落地页「收盘后的市场」预览（exports-landing.json 的 market 段）：用游客看到的那一天（上一交易日），
    数字取自同一份合成市场；按原文件的紧凑手写格式输出，只替换这一段，文件其余部分不动。"""
    y = today_doc["yesterday"]
    m = y["market"]
    sec = [*y["sectors_top"], *y["sectors_bottom"][::-1]]           # 领涨 3 个（降序）+ 领跌 3 个（降序）
    j = lambda o: json.dumps(o, ensure_ascii=False)
    sig = [f'      {{"code": {j(x["code"])}, "name": {j([x["name"], x["name_en"]])}, "strategy": {j(x["strategy"])}, '
           f'"action": {j(x["action"])}, "strength": {x["strength"]}}}' for x in y["signals"][:5]]
    return ('  "market": {\n'
            f'    "today": {j(DATES[1])},\n    "yesterday": {j(DATES[0])},\n    "regime": {j(y["regime"]["state"])},\n'
            f'    "up": {m["up"]}, "down": {m["down"]}, "flat": {m["flat"]},\n'
            f'    "sectors": [{", ".join(j(x) for x in sec)}],\n'
            '    "signals": [\n' + ",\n".join(sig) + '\n    ]\n  },\n')


def splice_landing(text: str, block: str) -> str:
    start = text.index('  "market": {\n')
    end = text.index('  "funnel":', start)
    return text[:start] + block + text[end:]


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比较，不写文件")
    a = ap.parse_args()
    docs = build()
    bad = 0
    lp = MOCK / "exports-landing.json"
    outputs = {name: dump(obj) for name, obj in docs.items()}
    outputs["exports-landing"] = splice_landing(lp.read_text(encoding="utf-8"), landing_market_text(docs["pub-today"]))
    outputs["pub-strategies"] = sync_encyclopedia_names((MOCK / "pub-strategies.json").read_text(encoding="utf-8"), registry_labels())
    for name, text in outputs.items():
        p = MOCK / f"{name}.json"
        if a.check:
            same = p.exists() and p.read_text(encoding="utf-8") == text
            print(f"{name}: {'一致' if same else '与生成结果不同'}")
            bad += not same
        else:
            p.write_text(text, encoding="utf-8")
            print(f"写入 {p.relative_to(ROOT)}（{len(text.encode()) // 1024} KB）")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
