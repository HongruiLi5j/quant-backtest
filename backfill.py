"""全市场历史数据回补（只需要跑一次，可以随时 Ctrl+C 中断，下次运行自动续传）。

  python backfill.py              # 回补所有还没下载的沪深 A 股（约 5200 只）
  python backfill.py --limit 30   # 先试 30 只
  python backfill.py --sources sina tencent   # 只用指定的数据源（默认 新浪1 + 腾讯2）

同时用多个数据源（默认新浪 1 个连接、腾讯 2 个连接），从同一个队列里取股票。
默认仅用新浪/腾讯；每日自动补缺口时，只有两者均失败才会按数量上限使用 BaoStock。
某个数据源被限流时它自己暂停，其他两个继续，所以不会整体卡住。
不使用东方财富，把它留给每天的全市场快照。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import logging
import sys
import time
import warnings

warnings.filterwarnings("ignore")

from src import allmarket


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--all", action="store_true", help="已下载的也重新下载")
    ap.add_argument("--delisted", action="store_true",
                    help="下载 2013 年以后退市的股票（修正幸存者偏差，只用于回测统计）")
    ap.add_argument("--sources", nargs="+", default=["sina", "tencent", "tencent"],
                    choices=["sina", "tencent", "baostock"],
                    help="每写一次开一个连接，例如 sina tencent tencent = 新浪 1 个 + 腾讯 2 个")
    a = ap.parse_args()
    logging.basicConfig(level=logging.ERROR)

    fixed = allmarket.strip_intraday()
    if fixed:
        print(f"已修复 {fixed} 只股票：删除了交易时间内下载的半天 K 线（不完整数据）")
    if a.delisted:
        lst = allmarket.delisted_list(refresh=True)
    else:
        with contextlib.redirect_stdout(io.StringIO()):   # 不显示 BaoStock 的 login/logout 提示
            lst = allmarket.stock_list()
    codes = list(lst["code"])
    if not a.all:
        codes = [c for c in codes if not allmarket.path(c, a.delisted).exists()]
    if a.limit:
        codes = codes[: a.limit]
    n = len(codes)
    kind = "2013 年以后退市的 A 股" if a.delisted else "沪深 A 股"
    print(f"{kind} {len(lst)} 只，已下载 {len(allmarket.cached_codes(a.delisted))} 只，本次要下载 {n} 只。")
    print(f"数据源：{'、'.join(a.sources)}（同时下载）。随时可以 Ctrl+C，下次会接着下。")
    if not n:
        return
    t0 = time.time()

    def progress(st):
        el = time.time() - t0
        rate = st["done"] / el * 60 if el else 0
        left = (st["total"] - st["processed"]) / rate if rate else 0
        src = "，".join(f"{k} {v}" for k, v in st["by_source"].items())
        cd = "；".join(f"{k} {v}" for k, v in st["cooldown"].items())
        print(f"  进度 {st['processed']}/{st['total']}，成功 {st['done']}（{src}），失败 {len(st['failed'])}，"
              f"每分钟 {rate:.0f} 只，预计还要 {left:.0f} 分钟" + (f"  [暂停中：{cd}]" if cd else ""), flush=True)

    try:
        st = allmarket.backfill_multi(codes, sources=tuple(a.sources), progress=progress, delisted=a.delisted)
    except KeyboardInterrupt:
        print("\n已中断，已下载的会保留，下次运行接着下。")
        return
    print(f"完成：成功 {st['done']}，失败 {len(st['failed'])}，用时 {(time.time() - t0) / 60:.0f} 分钟")
    if st["failed"]:
        print("失败的股票（再运行一次通常就好）：", " ".join(st["failed"][:40]))
        if a.delisted:
            print("退市股票的数据源不一定都有，下载不到的属正常，会在统计里注明覆盖率。")


if __name__ == "__main__":
    main()
