"""从 BaoStock 拉季度财务数据入库,并按 asof 导出 run_fundamental_rank.py 需要的两份 CSV(T014 S5)。

用法(冒烟):
    .venv/bin/python scripts/fetch_fundamental.py --asof 2026-10-22 --limit 20

流程:
  1. 逐只股票、逐个报告期,用 src/fundamental/fetch_baostock.py 取数,经 store.ingest_reports(provider_fn 注入)写入
     FundamentalStore;已入库的 (代码, 报告期) 跳过 = 断点续跑。还要取 asof 所在年和上一年的已实施分红。
  2. 按 --asof 组装 factors.py 要的字段(只用 announce_date <= asof 的记录;TTM 由累计值差分,不足 4 季留 NaN)。
  3. 未复权收盘价单独从 baostock 取(本地 cn_all 是后复权价,不能算市值);prices CSV 直接取本地 cn_all parquet
     asof 及之前 5 个交易日(只用来判断排名日有没有行情)。
联网只发生在第 1 步和未复权价;--skip-fetch 跳过第 1 步。连续失败 5 次停下报错(退出码 3),已入库的数据保留,下次续跑。
"""
import argparse
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.fundamental.store import FundamentalError, FundamentalStore, check_path_allowed, ingest_reports  # noqa: E402
from src.fundamental import fetch_baostock as fb  # noqa: E402

BJT = timezone(timedelta(hours=8))
CLOSE_READY = (15, 30)
A_SHARE = re.compile(r"^(60|68|00|30)[0-9]{4}$")
DAILY_DIR = ROOT / "data" / "daily" / "cn_all"
PROTECTED_DIRS = ("data/daily",)

# 判断"这个报告期已入库"只看季频指标,不看分红行(分红行的 report_date 也落在季末)
QUARTERLY_METRICS = set(fb.PROFIT_FIELDS.values()) | set(fb.BALANCE_FIELDS.values()) | set(fb.CASH_FLOW_FIELDS.values())

FUND_COLUMNS = ["code", "name", "industry", "market_cap", "close_raw", "net_profit_ttm", "net_profit_ttm_prev",
                "revenue_ttm", "revenue_ttm_prev", "operating_cost_ttm", "operating_profit_ttm", "equity_parent",
                "total_assets", "total_liabilities", "cfo_ttm", "dps_ttm", "available_date", "report_date"]
PRICE_COLUMNS = ["code", "date", "close"]


class CliError(Exception):
    pass


# ---------- 输入 ----------

def load_codes(path, limit=None):
    """读股票列表(code 列必需,name/industry 可选),只留沪深 A 股;limit 只取前 N 只。"""
    p = Path(path)
    if not p.is_file():
        raise CliError("找不到股票列表:%s" % p)
    df = pd.read_csv(p, dtype=str).fillna("")
    if "code" not in df.columns:
        raise CliError("股票列表缺少 code 列")
    df["code"] = df["code"].str.strip().str.zfill(6)
    df = df[df["code"].str.match(A_SHARE)].drop_duplicates("code")
    for c in ("name", "industry"):
        if c not in df.columns:
            df[c] = ""
    if limit is not None:
        df = df.head(limit)
    return df[["code", "name", "industry"]].reset_index(drop=True)


def quarters_between(start, end):
    """报告期末落在 [start, end] 内的 (年, 季) 列表,按时间升序。"""
    out = []
    for y in range(start.year, end.year + 1):
        for q in (1, 2, 3, 4):
            qe = fb.quarter_end(y, q)
            if start <= qe <= end:
                out.append((y, q))
    return out


def check_asof(asof, now):
    """A 股:排名日不能晚于今天;是今天的话北京时间 15:30 前当天 K 线是盘中数据,不能用。"""
    now_bj = now.astimezone(BJT) if now.tzinfo else now.replace(tzinfo=BJT)
    if asof > now_bj.date():
        raise CliError("asof %s 晚于今天(北京时间 %s)" % (asof, now_bj.date()))
    if asof == now_bj.date() and (now_bj.hour, now_bj.minute) < CLOSE_READY:
        raise CliError("asof 是今天且北京时间未到 15:30,当天 K 线是盘中数据,拒绝运行")


def check_out_path(path):
    resolved = Path(path).resolve()
    for p in PROTECTED_DIRS:
        prot = (ROOT / p).resolve()
        if resolved == prot or prot in resolved.parents:
            raise CliError("拒绝写入受保护目录 %s" % p)
    if resolved.is_dir():
        raise CliError("输出路径是目录:%s" % path)


def write_csv_atomic(df, path):
    """先写同目录临时文件再 os.replace,中断不会留下半份 CSV。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fetch_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            df.to_csv(f, index=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------- 第 1 步:拉取入库(断点续跑) ----------

def stored_periods(store, code):
    """库里已有季频指标的报告期集合。"""
    return {date.fromisoformat(r["report_date"]) for r in store.list_reports(code)
            if r["metric"] in QUARTERLY_METRICS and r["source"] == fb.SOURCE}


def queried_dividend_years(store, code):
    return {date.fromisoformat(r["report_date"]).year
            for r in store.list_reports(code, metric=fb.DIVIDEND_YEAR_MARKER) if r["source"] == fb.SOURCE}


def fetch_one(store, client, code, quarters, div_years, today, stats):
    """拉一只股票:缺的报告期 + 需要的分红年份。单季失败计数后继续;BaostockUnavailable 向上抛。"""
    have = stored_periods(store, code)
    for y, q in quarters:
        rd = fb.quarter_end(y, q)
        if rd in have:
            stats["quarters_skipped"] += 1
            continue
        try:
            n = ingest_reports(store, [code], [rd], provider_fn=lambda c, r, q=q: client.fetch_quarterly(c, r.year, q))
        except fb.BaostockUnavailable:
            raise
        except (fb.FetchError, FundamentalError) as e:
            stats["quarters_failed"] += 1
            print("  %s %s 失败:%s" % (code, rd, getattr(e, "code", e)), file=sys.stderr)
            continue
        stats["quarters_fetched"] += 1
        if n == 0:
            stats["quarters_empty"] += 1   # 还没披露 / 全部被丢弃(缺公告日等),下次还会再试
    done = queried_dividend_years(store, code)
    for y in div_years:
        if y in done and y < today.year:   # 往年查过就不再查;今年每次都重查(可能有新实施的分红)
            stats["div_years_skipped"] += 1
            continue
        try:
            recs = client.fetch_dividends(code, y)
            groups = {}
            for r in recs:
                groups.setdefault(date.fromisoformat(r["report_date"]), []).append(r)
            ingest_reports(store, [code], sorted(groups), provider_fn=lambda c, r: groups[r])
        except fb.BaostockUnavailable:
            raise
        except (fb.FetchError, FundamentalError) as e:
            stats["div_years_failed"] += 1
            print("  %s 分红 %d 失败:%s" % (code, y, getattr(e, "code", e)), file=sys.stderr)
            continue
        stats["div_years_fetched"] += 1


# ---------- 第 2/3 步:导出 ----------

def load_recent_prices(daily_dir, code, asof, n=5):
    """本地 cn_all parquet 中 asof(含)之前最近 n 个交易日的收盘价(后复权,只用于"有没有行情")。"""
    p = Path(daily_dir) / ("%s.parquet" % code)
    if not p.is_file():
        return pd.DataFrame(columns=PRICE_COLUMNS)
    d = pd.read_parquet(p, columns=["Close"])
    d = d[d.index <= pd.Timestamp(asof)].tail(n)
    return pd.DataFrame({"code": code, "date": d.index.strftime("%Y-%m-%d"), "close": d["Close"].values})


def build_exports(store, codes_df, asof, daily_dir, close_fn, stats):
    """返回 (fundamental_df, prices_df)。close_fn(code, asof) -> 未复权收盘价或 None。"""
    rows, prices = [], []
    asof_s = asof.isoformat()
    for r in codes_df.itertuples(index=False):
        px = load_recent_prices(daily_dir, r.code, asof)
        if len(px):
            prices.append(px)
        traded = bool(len(px)) and px["date"].iloc[-1] == asof_s
        close = None
        if traded:   # 排名日停牌的股票会被排名脚本剔除,不必再请求未复权价
            try:
                close = close_fn(r.code, asof)
            except fb.BaostockUnavailable:
                raise
            except fb.FetchError as e:
                stats["close_failed"] += 1
                print("  %s 未复权价失败:%s" % (r.code, e), file=sys.stderr)
        row = fb.assemble_asof(store, r.code, asof, close_raw=close)
        if row is None:
            stats["no_fundamental"] += 1
            continue
        row["name"], row["industry"] = r.name, r.industry
        rows.append(row)
    fdf = pd.DataFrame(rows, columns=FUND_COLUMNS)
    pdf = pd.concat(prices, ignore_index=True) if prices else pd.DataFrame(columns=PRICE_COLUMNS)
    return fdf, pdf[PRICE_COLUMNS]


# ---------- 命令行 ----------

def build_parser():
    ap = argparse.ArgumentParser(description="BaoStock 季度财务数据入库 + 按 asof 导出基本面/行情 CSV。")
    ap.add_argument("--codes-file", default=str(ROOT / "data" / "daily" / "cn_all_list.csv"))
    ap.add_argument("--start", default="2023-01-01", help="最早报告期(含)")
    ap.add_argument("--db", default=str(ROOT / "data" / "fundamental.db"))
    ap.add_argument("--export-fundamental", default=str(ROOT / "data" / "exports" / "fundamental_latest.csv"))
    ap.add_argument("--export-prices", default=str(ROOT / "data" / "exports" / "prices_latest.csv"))
    ap.add_argument("--asof", required=True, help="导出日 YYYY-MM-DD(只用该日及以前已公告的数据)")
    ap.add_argument("--limit", type=int, help="调试用:只处理前 N 只")
    ap.add_argument("--skip-fetch", action="store_true", help="不拉财报,只用库里已有数据导出(仍会取未复权价)")
    return ap


def _parse_day(s, name):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise CliError("%s 日期格式错误:%s" % (name, s))


def main(argv=None, client=None, now=None, daily_dir=None):
    """client:fetch_baostock.BaostockClient 或同接口的假对象(测试注入);now/daily_dir 同理。"""
    args = build_parser().parse_args(argv)
    stats = dict(quarters_fetched=0, quarters_skipped=0, quarters_empty=0, quarters_failed=0,
                 div_years_fetched=0, div_years_skipped=0, div_years_failed=0,
                 close_failed=0, no_fundamental=0)
    t0 = time.monotonic()
    own_client = client is None
    client = client or fb.BaostockClient()
    try:
        asof = _parse_day(args.asof, "--asof")
        start = _parse_day(args.start, "--start")
        if start > asof:
            raise CliError("--start 晚于 --asof")
        if args.limit is not None and args.limit < 1:
            raise CliError("--limit 必须 >= 1")
        now = now or datetime.now(BJT)
        check_asof(asof, now)
        today = (now.astimezone(BJT) if now.tzinfo else now).date()
        for p in (args.export_fundamental, args.export_prices):
            check_out_path(p)
        check_path_allowed(args.db)
        codes = load_codes(args.codes_file, args.limit)
        store = FundamentalStore(args.db)
        quarters = quarters_between(start, asof)
        div_years = list(range(max(start.year, asof.year - 1), asof.year + 1))
        if not args.skip_fetch:
            for i, code in enumerate(codes["code"], 1):
                fetch_one(store, client, code, quarters, div_years, today, stats)
                if i % 50 == 0 or i == len(codes):
                    print("[%d/%d] 已处理到 %s,请求 %d 次,用时 %.0f 秒"
                          % (i, len(codes), code, client.stats["requests"], time.monotonic() - t0))
        fdf, pdf = build_exports(store, codes, asof, daily_dir or DAILY_DIR, client.fetch_close_raw, stats)
        write_csv_atomic(fdf, args.export_fundamental)
        write_csv_atomic(pdf, args.export_prices)
    except fb.BaostockUnavailable as e:
        print("错误:BaoStock 连续失败,已停下(已入库的数据保留,下次续跑):%s" % e, file=sys.stderr)
        return 3
    except (CliError, FundamentalError) as e:
        print("错误:%s" % getattr(e, "code", e), file=sys.stderr)
        return 2
    finally:
        if own_client:
            client.logout()
    cs = client.stats
    print("完成:股票 %d 只,导出基本面 %d 行、行情 %d 行;用时 %.0f 秒" % (len(codes), len(fdf), len(pdf), time.monotonic() - t0))
    print("  报告期:新取 %(quarters_fetched)d(其中无数据 %(quarters_empty)d),跳过 %(quarters_skipped)d,失败 %(quarters_failed)d;"
          "分红年份:新取 %(div_years_fetched)d,跳过 %(div_years_skipped)d,失败 %(div_years_failed)d;"
          "未复权价失败 %(close_failed)d;无已公告财报 %(no_fundamental)d" % stats)
    print("  请求 %d 次,失败 %d 次;丢弃:缺公告日 %d、公告日异常 %d、报告期不符 %d、分红缺除权日 %d、分红异常 %d"
          % (cs["requests"], cs["failures"], cs["dropped_no_pubdate"], cs["dropped_bad_pubdate"],
             cs["dropped_stat_mismatch"], cs["dropped_no_exdate"], cs["dropped_bad_dividend"]))
    print("  已写入 %s 和 %s" % (args.export_fundamental, args.export_prices))
    return 0


if __name__ == "__main__":
    sys.exit(main())
