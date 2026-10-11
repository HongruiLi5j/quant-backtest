"""跑用户在网页「代码」页自己写的策略（engines/custom/*.py 里的 strategy(df) 函数）。

和自研引擎（engines/inhouse.py）用完全一样的撮合口径（次日开盘成交、固定单边手续费、涨跌停不成交），
这样用户自己写的策略和内置 7 个策略的结果可以直接比较。

每个文件独立执行（用 exec，不用 import）：这样同名函数改了内容，子进程里读到的一定是最新保存的版本，
不用操心 Python 的模块缓存。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pandas as pd

from engines import common

ROOT = Path(__file__).resolve().parent.parent
CUSTOM_DIR = ROOT / "engines" / "custom"


def _load(name: str):
    slug = re.sub(r"[^A-Za-z0-9_一-鿿-]", "", str(name))[:40]
    path = CUSTOM_DIR / f"{slug}.py"
    if not path.exists():
        raise ValueError(f"没有找到这个自定义策略文件：engines/custom/{slug}.py（可能还没保存）")
    ns: dict = {}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), ns)
    fn = ns.get("strategy")
    if not callable(fn):
        raise ValueError(f"{path.name} 里必须有一个 strategy(df) 函数")
    return fn, slug


def run(code: str, name: str = "custom", params: dict | None = None, start: str | None = None,
        cash: float = common.DEFAULT_CASH, df: pd.DataFrame | None = None, info: dict | None = None,
        light: bool = False, cost_bp: float | None = None) -> dict:
    from src.portfolio import run_weights

    fn, slug = _load(name)
    t0 = time.time()
    if df is None:
        raw, info = common.load_stock(code)
        df, k, note = common.to_forward_adjusted(code, raw)
    else:
        k, note = 1.0, ""
    info = info or {"name": code}
    cost_bp = common.CN.get("stock_cost_bp", 8) if cost_bp is None else cost_bp
    cost = cost_bp / 1e4

    w = fn(df.copy())
    if not isinstance(w, pd.Series):
        raise ValueError("strategy(df) 必须返回一个 pandas.Series（每天的目标仓位，0~1）")
    w = w.reindex(df.index).astype(float).clip(0, 1).fillna(0)

    common.STRATEGIES.setdefault("custom", {"label": "自定义策略", "desc": "", "params": []})
    lim = common.limit_pct(code, info.get("name", ""))
    res = run_weights({code: df}, w.rename(code).to_frame(), cost=cost, band=0.10, cash_ret=None, limits={code: lim})
    value = res.equity * cash
    held = res.weights.iloc[:, 0]
    r_intra = df["Close"] / df["Open"] - 1
    expo = held * (1 + r_intra.reindex(held.index)) / (1 + held * r_intra.reindex(held.index))
    if light:
        return {"value": value, "index": df.index}

    fills = []
    prev, shares = 0.0, 0.0
    eq_prev = value.shift(1).fillna(cash)
    for d, wt in held.items():
        if abs(wt - prev) > 1e-12:
            o = float(df.at[d, "Open"])
            new_shares = wt * float(eq_prev.at[d]) / o
            fills.append({"date": d.strftime("%Y-%m-%d"), "qty": new_shares - shares, "price": o,
                          "fee": abs(wt - prev) * float(eq_prev.at[d]) * cost})
            prev, shares = wt, new_shares
    extra = {
        "price_scale": k, "price_note": note, "events": [], "cost_bp": cost_bp,
        "engine_notes": [
            f"运行的是你自己写的策略：engines/custom/{slug}.py",
            "撮合口径和自研引擎一样：收盘出信号，次日开盘按目标仓位成交",
            f"费用：固定单边 {cost_bp:g}bp（含佣金、平摊的印花税和滑点估计），没有最低 5 元",
            "按百分比持仓，不取整到 100 股",
            "涨跌停：开盘涨幅 ≥ 涨停幅度 - 0.2% 不买、跌幅 ≥ 跌停幅度 - 0.2% 不卖",
        ],
    }
    out = common.pack_result("custom", "custom", code, info, params or {}, df, value, expo, fills, start, cash, t0, extra)
    out["label"] = f"我的策略：{slug}"
    for t in out["fills"]:
        t["qty"] = round(t["qty"], 2)
    return out
