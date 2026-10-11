"""沪深全市场个股数据。

思路（为什么这样就不会被封）：
  1. 历史数据由新浪和腾讯逐只回补，支持中断后续传；东方财富不参与历史回补。
  2. 以后每天只请求一次"全市场快照"（一次拿到 5000 多只股票当天的开高低收和昨收），
     用  今天后复权价 = 昨天后复权收盘 × 今天价格 / 昨收  接到历史后面。
     交易所给的"昨收"在除权除息日已经扣掉了分红送股，所以接出来的就是后复权价。
  3. 如果某只股票中间缺了一天，先用新浪和腾讯增量补齐；两者都失败时再限量使用 BaoStock。

数据存成 parquet：data/daily/cn_all/<代码>.parquet
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .datasource import CACHE, COLS

ALL = CACHE / "cn_all"
LIST_FILE = CACHE / "cn_all_list.csv"
CAL_FILE = CACHE / "cn_trade_calendar.csv"
PROGRESS_FILE = CACHE / "cn_all_progress.json"   # 每日更新进度：中断后下次自动续传
START = "2012-01-01"
A_SHARE = re.compile(r"^(60|68|00|30)\d{4}$")
log = logging.getLogger(__name__)


# ------------------------------------------------------------------ 基础
def board_of(code: str) -> str:
    if code.startswith(("688", "689")):
        return "科创板"
    if code.startswith(("300", "301")):
        return "创业板"
    return "沪市主板" if code.startswith("6") else "深市主板"


DELISTED = CACHE / "cn_delisted"          # 退市股票（只用于修正幸存者偏差，不参与每日信号）
DELISTED_LIST = CACHE / "cn_delisted_list.csv"


def _dir(delisted: bool) -> Path:
    return DELISTED if delisted else ALL


def path(code: str, delisted: bool = False) -> Path:
    return _dir(delisted) / f"{code}.parquet"


def load(code: str, delisted: bool = False) -> pd.DataFrame:
    p = path(code, delisted)
    if not p.exists():
        return pd.DataFrame(columns=COLS)
    return pd.read_parquet(p)


def save(code: str, df: pd.DataFrame, delisted: bool = False):
    _dir(delisted).mkdir(parents=True, exist_ok=True)
    target = path(code, delisted)
    tmp = target.with_suffix(".tmp")
    df[COLS].astype("float64").to_parquet(tmp)
    check = pd.read_parquet(tmp)
    if len(check) != len(df) or not set(COLS).issubset(check.columns):
        tmp.unlink(missing_ok=True)
        raise IOError(f"临时行情文件校验失败：{code}")
    tmp.replace(target)


def _atomic_csv(df: pd.DataFrame, target: Path, **kwargs):
    """表格类缓存先写同目录临时文件，再原子替换，避免中断留下半份 CSV。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    df.to_csv(tmp, **kwargs)
    check = pd.read_csv(tmp)
    if len(check) != len(df) or not set(df.columns).issubset(check.columns):
        tmp.unlink(missing_ok=True)
        raise IOError(f"临时 CSV 校验失败：{target.name}")
    tmp.replace(target)


def _atomic_json(value: dict, target: Path):
    """JSON 状态也用原子替换，读者不会读到写了一半的内容。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    check = json.loads(tmp.read_text(encoding="utf-8"))
    if type(check) is not type(value):
        tmp.unlink(missing_ok=True)
        raise IOError(f"临时 JSON 校验失败：{target.name}")
    tmp.replace(target)


def cached_codes(delisted: bool = False) -> list[str]:
    d = _dir(delisted)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.parquet"))


def delisted_list(refresh: bool = False) -> pd.DataFrame:
    """2013 年以后退市的沪深 A 股（来自上交所、深交所官网）。"""
    if DELISTED_LIST.exists() and not refresh:
        return pd.read_csv(DELISTED_LIST, dtype=str).fillna("")
    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(2)

    def timed(fn, **kw):   # 交易所网站从美国访问可能很慢，最多等 90 秒
        return pool.submit(fn, **kw).result(timeout=90)

    parts = []
    try:
        sh = timed(ak.stock_info_sh_delist, symbol="全部")
        parts.append(pd.DataFrame({"code": sh["公司代码"].astype(str).str.zfill(6), "name": sh["公司简称"],
                                   "ipo": sh["上市日期"].astype(str), "delist": sh["暂停上市日期"].astype(str)}))
    except Exception as e:
        log.warning("上交所退市列表获取失败：%s", e)
    try:
        sz = timed(ak.stock_info_sz_delist, symbol="终止上市公司")
        name_col = next(c for c in sz.columns if "简称" in c)
        parts.append(pd.DataFrame({"code": sz["证券代码"].astype(str).str.zfill(6), "name": sz[name_col],
                                   "ipo": sz["上市日期"].astype(str), "delist": sz["终止上市日期"].astype(str)}))
    except Exception as e:
        log.warning("深交所退市列表获取失败：%s", e)
    pool.shutdown(wait=False, cancel_futures=True)
    if not parts:
        raise ConnectionError("退市股票列表获取失败（交易所网站连不上，过一会儿再试）")
    d = pd.concat(parts, ignore_index=True)
    d = d[d["code"].str.match(A_SHARE) & (d["delist"] >= "2013-06-01")]
    d = d.drop_duplicates("code")
    _atomic_csv(d, DELISTED_LIST, index=False)
    return d


# ------------------------------------------------------------------ BaoStock
class BaoStock:
    """BaoStock 会话（用 with 语句自动登录/登出）。它是单线程的，不要并发调用。"""

    def __enter__(self):
        import socket
        import baostock as bs
        # BaoStock 用裸 socket 通信，默认没有超时：网络卡住时会永远等下去。
        # 设 60 秒超时，卡住就抛异常，由 backfill 的重试逻辑重新登录。
        socket.setdefaulttimeout(60)
        self.bs = bs
        lg = bs.login()
        if lg.error_code != "0":
            raise ConnectionError(f"BaoStock 登录失败：{lg.error_msg}")
        return self

    def __exit__(self, *a):
        try:
            self.bs.logout()
        except Exception:
            pass

    @staticmethod
    def bs_code(code: str) -> str:
        return ("sh." if code.startswith("6") else "sz.") + code

    def _rows(self, rs) -> pd.DataFrame:
        if rs.error_code != "0":
            raise ConnectionError(f"BaoStock：{rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)

    def history(self, code: str, start: str, end: str | None = None) -> pd.DataFrame:
        """后复权日线；停牌日（tradestatus=0）去掉。"""
        rs = self.bs.query_history_k_data_plus(
            self.bs_code(code), "date,open,high,low,close,volume,tradestatus",
            start_date=start, end_date=end or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(), frequency="d", adjustflag="1")
        d = self._rows(rs)
        if d.empty:
            return pd.DataFrame(columns=COLS)
        d = d[d["tradestatus"] == "1"]
        out = pd.DataFrame({
            "Open": pd.to_numeric(d["open"]), "High": pd.to_numeric(d["high"]),
            "Low": pd.to_numeric(d["low"]), "Close": pd.to_numeric(d["close"]),
            "Volume": pd.to_numeric(d["volume"], errors="coerce").fillna(0)})
        out.index = pd.to_datetime(d["date"])
        return out[(out[["Open", "Close"]] > 0).all(axis=1)]

    def stock_list(self) -> pd.DataFrame:
        d = self._rows(self.bs.query_stock_basic())
        d = d[(d["type"] == "1") & (d["status"] == "1")]   # 股票、在市
        d["code"] = d["code"].str[3:]
        d = d[d["code"].str.match(A_SHARE)]
        ind = self._rows(self.bs.query_stock_industry())
        ind["code"] = ind["code"].str[3:]
        d = d.merge(ind[["code", "industry"]], on="code", how="left")
        return pd.DataFrame({"code": d["code"], "name": d["code_name"], "ipo": d["ipoDate"],
                             "industry": d["industry"].fillna("")})


# ------------------------------------------------------------------ 股票列表 / 交易日历
def stock_list(refresh: bool = False) -> pd.DataFrame:
    """在市的沪深 A 股列表（代码、名称、上市日期、行业、板块）。每周刷新一次。"""
    if LIST_FILE.exists() and not refresh:
        age = time.time() - LIST_FILE.stat().st_mtime
        if age < 7 * 86400:
            return pd.read_csv(LIST_FILE, dtype=str).fillna("")
    try:
        with BaoStock() as b:
            df = b.stock_list()
    except Exception as e:
        log.warning("BaoStock 股票列表失败（%s），改用行情快照", e)
        snap = spot_snapshot()
        df = pd.DataFrame({"code": snap.index, "name": snap["name"], "ipo": "", "industry": ""})
        if LIST_FILE.exists():   # 保留旧列表里的上市日期和行业
            old = pd.read_csv(LIST_FILE, dtype=str).fillna("").set_index("code")
            df["ipo"] = df["code"].map(old["ipo"]).fillna("")
            df["industry"] = df["code"].map(old["industry"]).fillna("")
    df["board"] = df["code"].map(board_of)
    CACHE.mkdir(parents=True, exist_ok=True)
    _atomic_csv(df, LIST_FILE, index=False)
    return df


def trade_calendar() -> list[date]:
    """A 股交易日历（新浪），缓存一个月；失败时退化为"周一到周五"。"""
    if CAL_FILE.exists() and time.time() - CAL_FILE.stat().st_mtime < 30 * 86400:
        return [d.date() for d in pd.to_datetime(pd.read_csv(CAL_FILE)["trade_date"])]
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        _atomic_csv(cal, CAL_FILE, index=False)
        return [d.date() for d in pd.to_datetime(cal["trade_date"])]
    except Exception as e:
        log.warning("交易日历获取失败（%s），按工作日处理", e)
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        return [d.date() for d in pd.bdate_range(today - timedelta(days=30), today + timedelta(days=30))]


def last_trading_day(now_bj: datetime, cal: list[date]) -> date:
    """北京时间 15:30 之后算今天，否则算上一个交易日。"""
    cutoff = now_bj.date() if now_bj.hour * 60 + now_bj.minute >= 15 * 60 + 30 else now_bj.date() - timedelta(days=1)
    return max(d for d in cal if d <= cutoff)


def prev_trading_day(d: date, cal: list[date]) -> date:
    return max(x for x in cal if x < d)


# ------------------------------------------------------------------ 全市场快照
def _norm_code(s: str) -> str:
    return re.sub(r"\D", "", str(s))[-6:]


def spot_snapshot() -> pd.DataFrame:
    """当天全市场行情：index=代码，列 name/open/high/low/close/prev_close/volume。
    先东方财富（一次请求），失败再新浪（分页，约 60 次请求）。"""
    import akshare as ak
    errs = []
    for name, fn, cols in [
        ("东方财富", ak.stock_zh_a_spot_em, ("名称", "今开", "最高", "最低", "最新价", "昨收", "成交量")),
        ("新浪", ak.stock_zh_a_spot, ("名称", "今开", "最高", "最低", "最新价", "昨收", "成交量")),
    ]:
        for attempt in range(2):
            try:
                raw = fn()
                df = pd.DataFrame({
                    "name": raw[cols[0]].astype(str),
                    "open": pd.to_numeric(raw[cols[1]], errors="coerce"),
                    "high": pd.to_numeric(raw[cols[2]], errors="coerce"),
                    "low": pd.to_numeric(raw[cols[3]], errors="coerce"),
                    "close": pd.to_numeric(raw[cols[4]], errors="coerce"),
                    "prev_close": pd.to_numeric(raw[cols[5]], errors="coerce"),
                    "volume": pd.to_numeric(raw[cols[6]], errors="coerce"),
                })
                df.index = raw["代码"].map(_norm_code)
                df = df[df.index.str.match(A_SHARE)]
                df = df[~df.index.duplicated()]
                if len(df) > 1000:
                    log.info("全市场快照：%s，%d 只", name, len(df))
                    return df
            except Exception as e:
                errs.append(f"{name}: {type(e).__name__}")
                time.sleep(5)
    raise ConnectionError("全市场快照获取失败：" + "；".join(errs))


# ------------------------------------------------------------------ 每日更新
def append_snapshot(trade_day: date, snap: pd.DataFrame, cal: list[date]) -> dict:
    """把当天快照接到每只股票的后复权数据后面。返回统计和需要修补的股票。"""
    prev_day = prev_trading_day(trade_day, cal)
    ts = pd.Timestamp(trade_day)
    stats = {"appended": 0, "already": 0, "suspended": 0, "gaps": [], "new": []}
    cached = set(cached_codes())
    for code in cached:
        df = load(code)
        if df.empty:
            stats["gaps"].append(code)
            continue
        last = df.index[-1].date()
        if last >= trade_day:
            stats["already"] += 1
            continue
        row = snap.loc[code] if code in snap.index else None
        ok = row is not None and row["volume"] > 0 and row["close"] > 0 and row["prev_close"] > 0 \
            and row["open"] > 0
        if not ok:
            stats["suspended"] += 1      # 停牌或数据缺失：今天不追加
            continue
        # 上一根 K 线必须正好是前一个交易日；否则中间有缺口，交给 BaoStock 修补
        # （停牌过的股票最后一根会更早，这时用"昨收"接也是对的：昨收就是停牌前的收盘价）
        if last != prev_day and not _was_suspended(code, last, prev_day, cal):
            stats["gaps"].append(code)
            continue
        k = df["Close"].iloc[-1] / row["prev_close"]
        new = pd.DataFrame({"Open": [row["open"] * k], "High": [row["high"] * k], "Low": [row["low"] * k],
                            "Close": [row["close"] * k], "Volume": [row["volume"]]}, index=[ts])
        save(code, pd.concat([df, new]))
        stats["appended"] += 1
    stats["new"] = sorted(set(snap.index) - cached)
    return stats


_SUSP_FILE = CACHE / "cn_all_suspended.json"


def _was_suspended(code: str, last: date, prev_day: date, cal: list[date]) -> bool:
    """我们每天记录停牌的股票；如果 last 之后的交易日都被记为停牌，就不算缺口。"""
    try:
        s = json.loads(_SUSP_FILE.read_text())
    except Exception:
        return False
    days = [d for d in cal if last < d <= prev_day]
    return bool(days) and all(code in s.get(d.isoformat(), []) for d in days)


def record_suspended(trade_day: date, snap: pd.DataFrame):
    try:
        s = json.loads(_SUSP_FILE.read_text())
    except Exception:
        s = {}
    susp = snap[(snap["volume"].fillna(0) <= 0) | snap["close"].isna()].index.tolist()
    s[trade_day.isoformat()] = susp
    s = dict(sorted(s.items())[-60:])     # 只保留最近 60 天
    _atomic_json(s, _SUSP_FILE)


class _AkshareFallback:
    """BaoStock 连不上时的备用：东方财富 → 新浪，逐只慢慢下（每只间隔 3 秒左右，避免被封）。"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def history(self, code: str, start: str) -> pd.DataFrame:
        import random
        from .datasource import _fetch_stock_em, _fetch_stock_sina
        time.sleep(2.5 + random.uniform(0, 1.5))
        try:
            return _fetch_stock_em(code, start)
        except Exception:
            time.sleep(2)
            return _fetch_stock_sina(code, start)


def _is_ban(e) -> bool:
    return "黑名单" in str(e)


def _login(allow_fallback: bool):
    """登录 BaoStock。被拉黑时等几分钟再试（通常几分钟就自动解封）；
    allow_fallback=False 时绝不改用东方财富（并行下载时保护每天要用的快照接口）。"""
    import random
    err = None
    for attempt in range(6):
        try:
            return BaoStock().__enter__()
        except Exception as e:
            err = e
            wait = (180 + random.uniform(0, 60)) if _is_ban(e) else (3 + attempt * 4 + random.uniform(0, 3))
            if _is_ban(e):
                log.warning("BaoStock 暂时拒绝访问（请求太频繁），等 %.0f 秒再试", wait)
            time.sleep(wait)
    if allow_fallback:
        log.warning("BaoStock 连不上（%s），改用东方财富/新浪逐只下载（会慢很多）", err)
        return _AkshareFallback()
    raise ConnectionError(f"BaoStock 多次登录失败：{err}")


def backfill(codes: list[str], max_minutes: float | None = None, full: bool = False,
             allow_fallback: bool = True, until: date | None = None) -> dict:
    """回补历史（新股票下载全部；已有的从最后一天往后补）。可中断、可续传。
    优先 BaoStock；allow_fallback=True 时连不上才用东方财富/新浪。
    until：已经有这一天数据的股票直接跳过（中断后续传时不用从头再下一遍）。"""
    t0 = time.time()
    done, failed, handled = 0, [], 0
    try:
        b = _login(allow_fallback)
    except ConnectionError as e:
        log.warning("%s", e)
        return {"done": 0, "failed": list(codes), "remaining": 0}
    try:
        for i, code in enumerate(codes):
            if max_minutes and time.time() - t0 > max_minutes * 60:
                break
            old = pd.DataFrame(columns=COLS) if full else load(code)
            if until is not None and not old.empty and old.index[-1].date() >= until:
                handled += 1   # 上次已经更新好了
                continue
            start = START if old.empty else (old.index[-1] - timedelta(days=10)).strftime("%Y-%m-%d")
            new = None
            for attempt in range(3):
                try:
                    new = b.history(code, start)
                    break
                except Exception as e:
                    # 被拉黑 / 掉线：退出重新登录（被拉黑时 _login 会等几分钟）
                    log.warning("%s 下载出错（%s），重新登录后重试", code, e)
                    b.__exit__(None, None, None)
                    try:
                        b = _login(allow_fallback)
                    except ConnectionError as e2:
                        log.warning("%s", e2)
                        b = None
                        return {"done": done, "failed": failed, "remaining": len(codes) - handled}
            handled += 1
            if new is None:
                failed.append(code)
                continue
            new = _drop_unfinished(new)
            if new.empty:
                continue
            if not old.empty:
                common = old.index.intersection(new.index)
                if len(common):   # 对齐复权基准
                    kk = old.loc[common[-1], "Close"] / new.loc[common[-1], "Close"]
                    new[["Open", "High", "Low", "Close"]] *= kk
                new = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
            save(code, new)
            done += 1
            if (i + 1) % 100 == 0:
                log.info("回补进度 %d/%d（%.0f 分钟）", i + 1, len(codes), (time.time() - t0) / 60)
    finally:
        if b is not None:
            b.__exit__(None, None, None)
    return {"done": done, "failed": failed, "remaining": len(codes) - handled}


def backfill_daily(codes: list[str], *, until: date, max_minutes: float = 30,
                   max_stocks: int = 500, baostock_max: int = 50,
                   cursor: int = 0, progress=None) -> dict:
    """每日缺口修复：新浪优先、腾讯备用，二者都失败后有限量使用 BaoStock。

    所有接口都按最近缓存日期增量请求；源之间和重试之间有固定间隔，单次数量及总时长均受限。
    返回下次续传位置，避免长期失败的少数代码挡住队列后面的股票。
    """
    order = list(dict.fromkeys(codes))
    if not order:
        return {"done": 0, "failed": [], "remaining": 0, "processed": 0, "next_cursor": 0,
                "by_source": {"sina": 0, "tencent": 0, "baostock": 0}}
    cursor %= len(order)
    order = order[cursor:] + order[:cursor]
    batch = order[:max(0, max_stocks)]
    started = time.monotonic()
    last_request = {"sina": 0.0, "tencent": 0.0, "baostock": 0.0}
    delays = {"sina": 1.2, "tencent": 0.4, "baostock": 0.6}
    by_source = {"sina": 0, "tencent": 0, "baostock": 0}
    failed, errors, done = [], [], 0
    baostock_attempted = 0
    baostock_unavailable = False
    bs = None

    def request_gap(source: str):
        wait = delays[source] - (time.monotonic() - last_request[source])
        if wait > 0:
            time.sleep(wait)
        last_request[source] = time.monotonic()

    try:
        for index, code in enumerate(batch):
            if time.monotonic() - started >= max_minutes * 60:
                break
            old = load(code)
            if not old.empty and old.index[-1].date() >= until:
                done += 1
                continue
            start = START if old.empty else (old.index[-1] - timedelta(days=10)).strftime("%Y-%m-%d")
            last_error = ""
            saved = False
            for source in ("sina", "tencent"):
                fetch = _fetch_sina if source == "sina" else _fetch_tencent
                for attempt in range(2):
                    if time.monotonic() - started >= max_minutes * 60:
                        break
                    try:
                        request_gap(source)
                        new = fetch(code, start)
                        new = _drop_unfinished(new) if not new.empty else new
                        if not new.empty:
                            new = new[new.index.date <= until]
                        if new.empty:
                            raise RuntimeError("没有返回可用日线")
                        _store(code, new)
                        by_source[source] += 1
                        log.info("历史补缺 %s：使用 %s", code, source)
                        saved = True
                        break
                    except Exception as exc:
                        last_error = f"{source} 第 {attempt + 1} 次：{type(exc).__name__}: {str(exc)[:100]}"
                        log.warning("历史补缺 %s：%s 失败（%s）", code, source, last_error)
                        if attempt == 0:
                            time.sleep(2)
                if saved or time.monotonic() - started >= max_minutes * 60:
                    break

            if not saved and time.monotonic() - started < max_minutes * 60 and baostock_attempted < baostock_max and not baostock_unavailable:
                baostock_attempted += 1
                try:
                    if bs is None:
                        bs = BaoStock().__enter__()
                    for attempt in range(2):
                        try:
                            request_gap("baostock")
                            new = _drop_unfinished(bs.history(code, start))
                            new = new[new.index.date <= until] if not new.empty else new
                            if new.empty:
                                raise RuntimeError("没有返回可用日线")
                            _store(code, new)
                            by_source["baostock"] += 1
                            log.warning("历史补缺 %s：新浪和腾讯失败，已使用 BaoStock 兜底", code)
                            saved = True
                            break
                        except Exception as exc:
                            last_error = f"BaoStock 第 {attempt + 1} 次：{type(exc).__name__}: {str(exc)[:100]}"
                            log.warning("历史补缺 %s：BaoStock 失败（%s）", code, last_error)
                            if attempt == 0:
                                time.sleep(2)
                except Exception as exc:
                    last_error = f"BaoStock 登录失败：{type(exc).__name__}: {str(exc)[:100]}"
                    log.warning("历史补缺 %s：%s", code, last_error)
                    baostock_unavailable = True
            elif not saved and baostock_unavailable:
                last_error = last_error or "BaoStock 登录失败，本次停止继续尝试兜底"
            elif not saved and baostock_attempted >= baostock_max:
                log.warning("历史补缺 %s：新浪和腾讯失败；本次 BaoStock 兜底数量已达上限 %d", code, baostock_max)

            if saved:
                done += 1
            else:
                failed.append(code)
                errors.append({"code": code, "error": last_error or "本次达到时长上限"})
            processed = index + 1
            if progress and (processed % 10 == 0 or processed == len(batch)):
                progress({"total": len(codes), "processed": processed, "done": done,
                          "remaining": len(codes) - processed, "by_source": by_source})
    finally:
        if bs is not None:
            bs.__exit__(None, None, None)

    processed = done + len(failed)
    next_cursor = (cursor + processed) % len(codes)
    remaining = len(codes) - processed
    return {"done": done, "failed": failed, "errors": errors, "remaining": remaining,
            "processed": processed, "next_cursor": next_cursor, "by_source": by_source}


def load_progress() -> dict:
    try:
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_progress(**kw):
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    kw["updated"] = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")
    if not isinstance(json.loads(tmp.read_text(encoding="utf-8")), dict):
        tmp.unlink(missing_ok=True)
        raise IOError("全市场进度临时文件校验失败")
    tmp.replace(PROGRESS_FILE)


def daily_update(max_repair_minutes: float = 30) -> dict:
    """每日任务调用：快照追加 + 修补缺口 + 顺便回补新股票/没下完的股票。"""
    from zoneinfo import ZoneInfo
    from config import MARKETS
    cal = trade_calendar()
    now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))
    fixed = strip_intraday()
    if fixed:
        log.info("删除了 %d 只股票里盘中保存的半天 K 线", fixed)
    day = last_trading_day(now_bj, cal)
    result = {"trade_day": day.isoformat()}
    prev = load_progress()
    resuming = prev.get("trade_day") == day.isoformat() and not prev.get("complete")
    trading_now = now_bj.date() in cal and (9 * 60 + 15) <= now_bj.hour * 60 + now_bj.minute < 15 * 60 + 30
    if trading_now:
        # 盘中：今天还没收盘，历史数据已经是最新的上一个交易日，没什么可更新的
        result["skipped"] = "A 股交易时间，等收盘后再更新"
        return result
    # 先记下"这一天还没更新完"；如果中途断网/睡眠/被强制结束，下次定时任务看到它就会接着更新
    save_progress(trade_day=day.isoformat(), complete=False,
                  remaining=prev.get("remaining") if resuming else None)
    if resuming:
        log.info("续传：%s 上次没更新完（剩 %s 只），接着更新", day, prev.get("remaining", "?"))
    try:
        snap = spot_snapshot()
        record_suspended(day, snap)
        st = append_snapshot(day, snap, cal)
        result.update({k: (len(v) if isinstance(v, list) else v) for k, v in st.items()})
        todo = st["gaps"]
    except Exception as e:
        log.warning("全市场快照失败：%s；后续逐只尝试新浪、腾讯，二者都失败后才按上限使用 BaoStock", e)
        result["snapshot_error"] = str(e)
        todo = cached_codes()
    # 还没下载过的股票（回补没完成 / 新上市满一年）
    lst = stock_list()
    missing = [c for c in lst["code"] if not path(c).exists()]
    todo = list(dict.fromkeys(todo + missing))
    remaining = 0
    if todo:
        try:
            batch_limit = int(MARKETS["cn"].get("repair_max_stocks", 500))
            fallback_limit = int(MARKETS["cn"].get("baostock_fallback_max", 50))
            cursor = int(prev.get("repair_cursor", 0)) if resuming else int(prev.get("repair_cursor", 0))
            def record_repair_progress(st):
                handled = st["processed"]
                log.info("每日缺口修复进度 %d/%d（新浪 %d，腾讯 %d，BaoStock %d）",
                         handled, st["total"], st["by_source"]["sina"],
                         st["by_source"]["tencent"], st["by_source"]["baostock"])
                save_progress(trade_day=day.isoformat(), complete=False,
                              total=st["total"], processed=handled,
                              remaining=max(0, st["total"] - handled),
                              repair_cursor=(cursor + handled) % max(1, len(todo)))
            r = backfill_daily(todo, until=day, max_minutes=max_repair_minutes,
                               max_stocks=batch_limit, baostock_max=fallback_limit,
                               cursor=cursor, progress=record_repair_progress)
            result["repaired"] = r["done"]
            result["still_missing"] = r["remaining"] + len(r["failed"])
            remaining = result["still_missing"]
            result["repair_sources"] = r["by_source"]
            prev["repair_cursor"] = r["next_cursor"]
            if r["failed"]:
                result["failed"] = len(r["failed"])
            for failure in r.get("errors", []):
                log.warning("历史缺口修复失败：%s（%s）", failure["code"], failure["error"])
        except Exception as e:
            result["repair_error"] = str(e)
            remaining = len(todo)
    # 剩余和失败都保持未完成，下次任务会重试这些股票，不把缺口误报为已完成。
    complete = remaining == 0
    save_progress(trade_day=day.isoformat(), complete=complete, remaining=remaining,
                  repair_cursor=prev.get("repair_cursor", 0))
    if not complete:
        log.warning("全市场还有 %d 只没更新完，已保存进度，下次自动续传", remaining)
    return result


# ------------------------------------------------------------------ 多数据源并行回补
def _fetch_tencent(code: str, start: str) -> pd.DataFrame:
    """腾讯证券后复权日线。接口每次最多返回 640 根 K 线，所以按约 2.4 年一段请求
    （akshare 自带的函数按 1 年一段，要多请求一倍多）。解析失败时退回 akshare 的实现。"""
    import json as _json

    import requests
    sym = ("sh" if code.startswith("6") else "sz") + code
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    rows = []
    try:
        seg_start = pd.Timestamp(start)
        today = pd.Timestamp(_now_bj().date())
        while seg_start <= today:
            seg_end = min(seg_start + pd.Timedelta(days=880), today)
            params = {"_var": "kline_dayhfq",
                      "param": f"{sym},day,{seg_start:%Y-%m-%d},{seg_end:%Y-%m-%d},640,hfq"}
            r = requests.get(url, params=params, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
            text = r.text
            data = _json.loads(text[text.find("={") + 1:])["data"][sym]
            k = data.get("hfqday") or data.get("day") or []
            rows.extend(x[:6] for x in k)
            seg_start = seg_end + pd.Timedelta(days=1)
            time.sleep(0.15)
    except (ValueError, KeyError, TypeError):
        import akshare as ak
        raw = ak.stock_zh_a_hist_tx(symbol=sym, start_date=start.replace("-", ""),
                                    end_date="20500101", adjust="hfq", timeout=15)
        rows = raw[["date", "open", "close", "high", "low", "volume"]].astype(str).values.tolist()
    if not rows:
        return pd.DataFrame(columns=COLS)
    d = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"])
    df = pd.DataFrame({"Open": pd.to_numeric(d["open"], errors="coerce"),
                       "High": pd.to_numeric(d["high"], errors="coerce"),
                       "Low": pd.to_numeric(d["low"], errors="coerce"),
                       "Close": pd.to_numeric(d["close"], errors="coerce"),
                       "Volume": pd.to_numeric(d["volume"], errors="coerce").fillna(0)})
    df.index = pd.to_datetime(d["date"])
    df = df[~df.index.duplicated()].sort_index()
    return df.dropna()


def _fetch_sina(code: str, start: str) -> pd.DataFrame:
    from .datasource import _fetch_stock_sina
    return _fetch_stock_sina(code, start)


def _now_bj() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _drop_unfinished(df: pd.DataFrame) -> pd.DataFrame:
    """去掉还没收盘的 K 线：新浪/腾讯的历史接口在交易时间会把"今天到目前为止"的半天行情也返回。
    北京时间 15:30 之前，日期是今天（或以后）的 K 线一律不要。"""
    if df.empty:
        return df
    now = _now_bj()
    cutoff = pd.Timestamp(now.date())
    if now.hour * 60 + now.minute < 15 * 60 + 30:
        return df[df.index < cutoff]
    return df[df.index <= cutoff]


def strip_intraday() -> int:
    """修复已经存下来的半天 K 线：如果文件是在某天北京时间 15:30 之前保存的，
    而最后一根 K 线的日期就是那天（或更晚），这根 K 线是盘中数据，删掉。返回修复的股票数。"""
    from zoneinfo import ZoneInfo
    bj = ZoneInfo("Asia/Shanghai")
    fixed = 0
    for code in cached_codes():
        p = path(code)
        saved = datetime.fromtimestamp(p.stat().st_mtime, bj)
        df = load(code)
        if df.empty:
            continue
        ready = pd.Timestamp(saved.date()) if saved.hour * 60 + saved.minute >= 15 * 60 + 30 \
            else pd.Timestamp(saved.date()) - pd.Timedelta(days=1)
        bad = df.index > ready
        if bad.any():
            save(code, df[~bad])
            fixed += 1
    return fixed


def _store(code: str, new: pd.DataFrame, delisted: bool = False):
    """和已有数据合并（按重叠日对齐复权基准）后保存。"""
    new = _drop_unfinished(new)
    new = new[(new[["Open", "Close"]] > 0).all(axis=1)].sort_index()
    new = new[~new.index.duplicated()]
    old = load(code, delisted)
    if not old.empty:
        common = old.index.intersection(new.index)
        if len(common):
            k = old.loc[common[-1], "Close"] / new.loc[common[-1], "Close"]
            new = new.copy()
            new[["Open", "High", "Low", "Close"]] *= k
        new = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
    save(code, new, delisted)


def backfill_multi(codes: list[str], sources=("sina", "tencent", "tencent"), progress=None,
                   delisted: bool = False, max_minutes: float | None = None,
                   until: date | None = None) -> dict:
    """三个数据源各开一个连接，同时从同一个待下载队列里取股票。
    某个数据源被限流时，只有它自己暂停一会儿，另外两个继续下载。
    不用东方财富（留给每天的全市场快照，避免被封）。"""
    import os
    import queue
    import random
    import threading
    os.environ.setdefault("TQDM_DISABLE", "1")

    q: queue.Queue = queue.Queue()
    for c in codes:
        q.put((c, 0))
    lock = threading.Lock()
    st = {"done": 0, "failed": [], "errors": [], "by_source": {s: 0 for s in dict.fromkeys(sources)}, "cooldown": {}}
    stop = threading.Event()
    started = time.monotonic()

    def finish(code, ok, src=None):
        with lock:
            if ok:
                st["done"] += 1
                st["by_source"][src] += 1
            else:
                st["failed"].append(code)
            n = st["done"] + len(st["failed"])
        if progress and n % 25 == 0:
            progress(dict(st, total=len(codes), processed=n))

    def worker(src):
        gap = {"sina": 1.2, "tencent": 0.3, "baostock": 0.6}[src]
        errors_in_row = 0
        bs = None
        while not stop.is_set():
            try:
                code, tries = q.get(timeout=1)
            except queue.Empty:
                if q.unfinished_tasks == 0:
                    break
                continue
            if max_minutes and time.monotonic() - started >= max_minutes * 60:
                q.put((code, tries))
                q.task_done()
                stop.set()
                break
            try:
                old = load(code, delisted)
                if until and not old.empty and old.index[-1].date() >= until:
                    finish(code, True, src)
                else:
                    start = START if old.empty else (old.index[-1] - timedelta(days=10)).strftime("%Y-%m-%d")
                    if src == "baostock":
                        if bs is None:
                            bs = BaoStock().__enter__()
                        df = bs.history(code, start)
                    elif src == "tencent":
                        df = _fetch_tencent(code, start)
                    else:
                        df = _fetch_sina(code, start)
                    df = _drop_unfinished(df) if not df.empty else df
                    if until and not df.empty:
                        df = df[df.index.date <= until]
                    if df.empty:
                        raise RuntimeError("数据源没有返回可用日线")
                    _store(code, df, delisted)
                    finish(code, True, src)
                    log.info("历史回补 %s：使用 %s", code, src)
                errors_in_row = 0
            except Exception as e:
                errors_in_row += 1
                if tries + 1 >= 3:
                    with lock:
                        st["failed"].append(code)
                        st["errors"].append({"code": code, "error": f"{src}: {type(e).__name__}: {str(e)[:120]}"})
                        n = st["done"] + len(st["failed"])
                    if progress and n % 25 == 0:
                        progress(dict(st, total=len(codes), processed=n))
                else:
                    q.put((code, tries + 1))
                # 被限流时只暂停当前数据源，并受每日修复时限约束。
                if _is_ban(e) or errors_in_row >= 3:
                    wait = 240 if _is_ban(e) else 90
                    with lock:
                        st["cooldown"][src] = f"{type(e).__name__}，暂停 {wait} 秒"
                    for _ in range(wait):
                        if stop.is_set() or (max_minutes and time.monotonic() - started >= max_minutes * 60):
                            break
                        time.sleep(1)
                    with lock:
                        st["cooldown"].pop(src, None)
                    errors_in_row = 0
            finally:
                q.task_done()
            if max_minutes and time.monotonic() - started >= max_minutes * 60:
                stop.set()
            if gap:
                time.sleep(gap + random.uniform(0, gap / 2))
        if bs is not None:
            bs.__exit__(None, None, None)

    threads = [threading.Thread(target=worker, args=(s,), daemon=True) for s in sources]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
            if q.unfinished_tasks == 0:
                stop.set()
    except KeyboardInterrupt:
        stop.set()
        raise
    st["remaining"] = max(0, len(codes) - st["done"] - len(st["failed"]))
    if progress:
        progress(dict(st, total=len(codes), processed=st["done"] + len(st["failed"])))
    return st
