"""A股显示价格：把后复权价格换成"以最新一天为基准的前复权"，让最后一天等于真实成交价。

只用于显示。整条价格乘同一个系数 k，涨跌幅、信号、收益都不变；磁盘上的数据仍是后复权
（每日快照追加依赖它）。真实价格取自腾讯，结果缓存在 engine_price_scale.json（与引擎共用）。
"""
from __future__ import annotations
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SCALE_FILE = ROOT / "data" / "daily" / "engine_price_scale.json"
_fail_until = 0.0


def scale_for(market: str, code: str, df) -> tuple[float, str]:
    """返回 (k, 说明)。取不到真实价格时 k=1，说明里写明。"""
    global _fail_until
    if market != "cn" or df is None or len(df) == 0:
        return 1.0, ""
    last_day = df.index[-1].strftime("%Y-%m-%d")
    last = float(df["Close"].iloc[-1])
    cache = {}
    try:
        cache = json.loads(_SCALE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    hit = cache.get(code)
    if hit and hit.get("day") == last_day and hit.get("raw"):
        # 同一只股票在不同数据文件里后复权基准可能不同，所以只缓存真实价，k 按当前数据重算
        return float(hit["raw"]) / last, "前复权"
    if time.time() < _fail_until:
        return 1.0, "未取到真实价格，显示为后复权"
    from engines.common import _raw_close_tencent
    raw = _raw_close_tencent(code, last_day)
    if not raw:
        _fail_until = time.time() + 120
        return 1.0, "未取到真实价格，显示为后复权"
    k = raw / last
    cache[code] = {"day": last_day, "k": k, "raw": raw}
    try:
        tmp = _SCALE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(_SCALE_FILE)
    except Exception:
        pass
    return k, "前复权"


def apply(market: str, code: str, df):
    k, note = scale_for(market, code, df)
    if k == 1.0:
        return df, k, note
    out = df.copy()
    out[["Open", "High", "Low", "Close"]] *= k
    return out, k, note
