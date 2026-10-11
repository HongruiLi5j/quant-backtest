"""行情数据源 + 本地缓存（增量更新）。

  yahoo    美股，yfinance（免费，无需账号）
  akshare  A 股 ETF 和个股，行情来自东方财富（免费，无需账号），后复权；新浪财经做备用
  qmt      广发证券 QMT / miniQMT（预留）：需要券商开通权限，且只能在 Windows 上运行

缓存在 data/daily/<market>/<代码>.csv。每次只下载最近一段，和旧数据合并。
所有数据源都不需要任何账号密码，所以本项目里不保存任何凭证。
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "daily"
COLS = ["Open", "High", "Low", "Close", "Volume"]


# ------------------------------------------------------------------ 各数据源
def _fetch_yahoo(sym: str, start: str) -> pd.DataFrame:
    import yfinance as yf
    raw = yf.download(sym, start=start, auto_adjust=True, progress=False, threads=False)
    if raw.empty:
        return pd.DataFrame(columns=COLS)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[COLS].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def _fetch_akshare(sym: str, start: str) -> pd.DataFrame:
    import akshare as ak
    raw = ak.fund_etf_hist_em(symbol=sym, period="daily",
                              start_date=start.replace("-", ""), end_date="20500101", adjust="hfq")
    if raw is None or raw.empty:
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame({
        "Open": raw["开盘"], "High": raw["最高"], "Low": raw["最低"],
        "Close": raw["收盘"], "Volume": raw["成交量"],
    })
    df.index = pd.to_datetime(raw["日期"])
    return df.dropna()


def _fetch_sina_raw(sym: str) -> pd.DataFrame:
    """新浪的 ETF 日线（不复权），东方财富连不上时的备用源。"""
    import akshare as ak
    prefix = "sh" if sym.startswith(("5", "6")) else "sz"
    raw = ak.fund_etf_hist_sina(symbol=prefix + sym)
    if raw is None or raw.empty:
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame({"Open": raw["open"], "High": raw["high"], "Low": raw["low"],
                       "Close": raw["close"], "Volume": raw["volume"]})
    df.index = pd.to_datetime(raw["date"])
    return df.dropna()


def _fetch_sina(sym: str, start: str) -> pd.DataFrame:
    """新浪 ETF 日线接口返回原始价格，后续按本地缓存的重叠日对齐。"""
    raw = _fetch_sina_raw(sym)
    return raw[raw.index >= pd.Timestamp(start)] if not raw.empty else raw


def _sina_fallback(old: pd.DataFrame, sym: str) -> pd.DataFrame | None:
    """用新浪的不复权数据补上最近几天，并按重叠日的比例换算成后复权价。
    后复权价 = 原始价 × 复权因子，两次分红之间因子不变，所以按最后一个重叠日的比例缩放即可。
    下次东方财富能连上时，重叠部分对不上会自动整段重下，误差会被纠正。"""
    raw = _fetch_sina_raw(sym)
    common = old.index.intersection(raw.index)
    if raw.empty or not len(common):
        return None
    k = old.loc[common[-1], "Close"] / raw.loc[common[-1], "Close"]
    new = raw.loc[raw.index > common[-1]].copy()
    new[["Open", "High", "Low", "Close"]] *= k
    return new


def _fetch_qmt(sym: str, start: str) -> pd.DataFrame:
    """广发 QMT 接口（预留）。开通后在 Windows 里登录 miniQMT，再实现这里：

        from xtquant import xtdata
        code = sym + (".SH" if sym.startswith(("5", "6")) else ".SZ")
        xtdata.download_history_data(code, "1d", start.replace("-", ""))
        d = xtdata.get_market_data_ex([], [code], "1d", start.replace("-", ""),
                                      dividend_type="back")[code]
    """
    raise NotImplementedError("QMT 数据源还没有接入：需要广发开通 QMT 权限，并在 Windows 上运行")


FETCHERS = {"yahoo": _fetch_yahoo, "akshare": _fetch_akshare, "sina": _fetch_sina, "qmt": _fetch_qmt}


# ------------------------------------------------------------------ 缓存与增量更新
def cache_path(market: str, sym: str) -> Path:
    return CACHE / market / f"{sym}.csv"


def _save_cache_csv(df: pd.DataFrame, target: Path):
    """先写临时文件并读回校验，再替换行情缓存。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    df.to_csv(tmp, float_format="%.6f")
    check = pd.read_csv(tmp, index_col=0, parse_dates=True)
    if len(check) != len(df) or not set(COLS).issubset(check.columns) or check[COLS].isna().all().all():
        tmp.unlink(missing_ok=True)
        raise IOError(f"临时行情文件校验失败：{target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp.replace(target)


def load_cached(market: str, sym: str) -> pd.DataFrame:
    p = cache_path(market, sym)
    if not p.exists():
        return pd.DataFrame(columns=COLS)
    return pd.read_csv(p, index_col=0, parse_dates=True)


def drop_incomplete_bar(df: pd.DataFrame, tz: str, close_ready: str) -> pd.DataFrame:
    """盘中运行时，数据源会返回"今天到目前为止"的半根 K 线，用它算信号是错的，去掉。"""
    if df.empty:
        return df
    now = datetime.now(ZoneInfo(tz))
    hh, mm = map(int, close_ready.split(":"))
    ready = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if df.index[-1].date() == now.date() and now < ready:
        return df.iloc[:-1]
    return df


def update(market: str, sym: str, cfg: dict, retries: int = 3) -> tuple[pd.DataFrame, str | None, bool]:
    """增量更新一个品种。返回 (数据, 错误信息或 None, 是否用了备用源)。失败时返回旧缓存，不会覆盖。"""
    old = load_cached(market, sym)
    start = cfg["start"] if old.empty else (old.index[-1] - timedelta(days=10)).strftime("%Y-%m-%d")
    fetch = FETCHERS[cfg["source"]]
    err = None
    fallback = False
    new = pd.DataFrame(columns=COLS)
    for attempt in range(retries):
        try:
            new = fetch(sym, start)
            if new.empty:
                raise RuntimeError("数据源返回空行情")
            if cfg["source"] == "sina" and not old.empty:
                extra = _sina_fallback(old, sym)
                if extra is None:
                    raise RuntimeError("没有重叠日期，无法对齐新浪原始价格")
                new = pd.concat([old.iloc[-10:], extra])
            break
        except Exception as e:  # 网络问题等
            err = f"{type(e).__name__}: {str(e)[:150]}"
            # 东方财富对短时间内的连续请求会直接断开连接，等久一点再试
            if attempt < retries - 1:
                time.sleep(5 * 2 ** attempt + random.uniform(0, 3))
    else:
        if cfg["source"] == "sina" and not old.empty:
            try:
                extra = _sina_fallback(old, sym)
                if extra is not None:
                    new = pd.concat([old.iloc[-10:], extra])
                    err = None
                    fallback = True
            except Exception as e:
                err += f"；新浪复权对齐失败：{type(e).__name__}"
        elif cfg["source"] == "sina":
            try:
                suffix = ".SS" if sym.startswith(("5", "6")) else ".SZ"
                new = _fetch_yahoo(sym + suffix, start)
                if new.empty:
                    raise RuntimeError("Yahoo Finance 返回空行情")
                common = old.index.intersection(new.index)
                if len(common):
                    k = old.loc[common[-1], "Close"] / new.loc[common[-1], "Close"]
                    new = new.copy()
                    new[["Open", "High", "Low", "Close"]] *= k
                err = None
                fallback = True
            except Exception as e:
                err = f"{err}；Yahoo 备用源失败：{type(e).__name__}: {str(e)[:100]}"
        if err:
            return old, err, False

    new = drop_incomplete_bar(new, cfg["tz"], cfg["close_ready"])
    if new.empty:
        return old, (None if not old.empty else "数据源返回空数据"), fallback
    # 复权价会因为新分红整体变化：如果重叠部分对不上，就整段重新下载
    if not fallback and not old.empty:
        overlap = old.index.intersection(new.index)
        if len(overlap) and not (old.loc[overlap, "Close"] - new.loc[overlap, "Close"]).abs().lt(
                old.loc[overlap, "Close"] * 1e-4).all():
            try:
                new = drop_incomplete_bar(fetch(sym, cfg["start"]), cfg["tz"], cfg["close_ready"])
                old = pd.DataFrame(columns=COLS)
            except Exception as e:
                return old, f"复权数据需要全量重下，但失败了：{e}", fallback
    merged = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
    merged = merged[COLS].astype(float)
    p = cache_path(market, sym)
    _save_cache_csv(merged, p)   # 校验成功后原子替换，写入失败时保留旧行情
    return merged, None, fallback


def load_market(market: str, cfg: dict, with_stocks: bool = True) -> dict[str, pd.DataFrame]:
    """只读缓存，不联网（网页服务用这个，保证打开网页很快）。"""
    out = {}
    syms = list(cfg["universe"]) + (list(load_watchlist(market)) if with_stocks else [])
    for s in syms:
        df = load_cached(market, s)
        if not df.empty:
            out[s] = df
    return out


# ------------------------------------------------------------------ A 股个股（自选股）
STOCK_RE = __import__("re").compile(r"^(60|68|00|30)\d{4}$")   # 沪深主板、科创板、创业板


def is_valid_stock(code: str) -> bool:
    return bool(STOCK_RE.match(code or ""))


def limit_pct(code: str, name: str = "") -> float:
    """涨跌停幅度：科创板/创业板 20%，ST 5%，其余主板 10%。"""
    if "ST" in name.upper():
        return 0.05
    if code.startswith(("688", "689", "300", "301")):
        return 0.20
    return 0.10


def _watch_path(market: str) -> Path:
    return CACHE / f"watchlist_{market}.json"


def load_watchlist(market: str) -> dict:
    """自选股 {代码: 名称}。第一次使用时，从 config.py 的默认列表开始。"""
    import json
    p = _watch_path(market)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    from config import MARKETS
    return dict(MARKETS[market].get("stocks", {}))


def save_watchlist(market: str, wl: dict):
    import json
    p = _watch_path(market)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    check = json.loads(tmp.read_text(encoding="utf-8"))
    if check != wl:
        tmp.unlink(missing_ok=True)
        raise IOError(f"自选列表临时文件校验失败：{p.name}")
    tmp.replace(p)


def _fetch_stock_em(sym: str, start: str) -> pd.DataFrame:
    import akshare as ak
    raw = ak.stock_zh_a_hist(symbol=sym, period="daily", start_date=start.replace("-", ""),
                             end_date="20500101", adjust="hfq", timeout=15)
    if raw is None or raw.empty:
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame({"Open": raw["开盘"], "High": raw["最高"], "Low": raw["最低"],
                       "Close": raw["收盘"], "Volume": raw["成交量"]})
    df.index = pd.to_datetime(raw["日期"])
    return df.dropna()


def _fetch_stock_sina(sym: str, start: str) -> pd.DataFrame:
    import akshare as ak
    prefix = "sh" if sym.startswith("6") else "sz"
    raw = ak.stock_zh_a_daily(symbol=prefix + sym, start_date=start.replace("-", ""),
                              end_date="21000101", adjust="hfq")
    if raw is None or raw.empty:
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame({"Open": raw["open"], "High": raw["high"], "Low": raw["low"],
                       "Close": raw["close"], "Volume": raw["volume"]})
    df.index = pd.to_datetime(raw["date"])
    return df.dropna()


def stock_name(sym: str) -> str:
    """只从本地全市场清单查简称，不为显示名称额外请求东方财富。"""
    try:
        from .allmarket import LIST_FILE
        if LIST_FILE.exists():
            listing = pd.read_csv(LIST_FILE, dtype=str).fillna("")
            row = listing[listing["code"] == sym]
            if len(row):
                return str(row["name"].iloc[0])
    except Exception:
        pass
    return ""


def update_stock(market: str, sym: str, cfg: dict, try_em: bool = True) -> tuple[pd.DataFrame, str | None, str | None]:
    """更新 A 股自选个股：新浪优先、腾讯备用；东方财富仅用于每日全市场快照。

    两个源返回后复权日线，拼接时按重叠日期对齐复权基准。try_em 为兼容旧调用保留，
    不会再调用东方财富个股历史接口。返回 (数据, 错误, 实际来源)。
    """
    old = load_cached(market, sym)
    start = cfg["start"] if old.empty else (old.index[-1] - timedelta(days=15)).strftime("%Y-%m-%d")
    from .allmarket import _fetch_tencent
    sources = [("sina", _fetch_stock_sina), ("tencent", _fetch_tencent)]
    new, err, used = None, None, None
    for name, fn in sources:
        for attempt in range(2):
            try:
                new = fn(sym, start)
                if new.empty:
                    raise RuntimeError("数据源返回空行情")
                used = name
                break
            except Exception as e:
                err = f"{name} {type(e).__name__}: {str(e)[:100]}"
                if attempt < 1:
                    time.sleep(2 + random.uniform(0, 1))
        if used:
            break
    if new is None:
        return old, err, None
    new = drop_incomplete_bar(new, cfg["tz"], cfg["close_ready"])
    if new.empty:
        return old, (None if not old.empty else "数据源没有这只股票的数据（代码错了或已退市？）"), used
    if not old.empty:
        common = old.index.intersection(new.index)
        if len(common):
            k = old.loc[common[-1], "Close"] / new.loc[common[-1], "Close"]
            if abs(k - 1) > 1e-4:
                new[["Open", "High", "Low", "Close"]] *= k
    merged = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()[COLS].astype(float)
    p = cache_path(market, sym)
    _save_cache_csv(merged, p)
    return merged, None, used
