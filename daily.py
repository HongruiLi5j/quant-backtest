"""每日任务：更新数据 → 跑策略 → 生成汇总（网页读取）。定时任务每天自动调用它。

  python daily.py              # 两个市场都更新
  python daily.py --market cn  # 只更新 A 股
  python daily.py --offline    # 不联网，只用已有缓存重新计算
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import random
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import warnings
warnings.filterwarnings("ignore")

from config import ENABLED, MARKETS
from src import allmarket, daily_report, datasource, notify, scan, tracking

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "logs" / "daily.log"


def setup_logging():
    LOG.parent.mkdir(exist_ok=True)
    class UtcFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            return datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds")
    formatter = UtcFormatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(LOG, encoding="utf-8")
    stream_handler = logging.StreamHandler(sys.stdout)
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
    for handler in logging.root.handlers:
        handler.setFormatter(formatter)


def run_market(market: str, offline: bool = False) -> dict:
    cfg = MARKETS[market]
    logging.info("开始更新 %s 市场数据", cfg["name"])
    errors = {}
    if not offline:
        for i, s in enumerate(cfg["universe"]):
            if i and cfg["source"] in ("akshare", "sina"):
                time.sleep(random.uniform(1.2, 1.8))
            df, err, used_fallback = datasource.update(market, s, cfg, retries=3)
            if used_fallback:
                logging.info("%s %s: 主数据源失败，已切换备用源", market, s)
            errors[s] = err
            source_name = "Yahoo Finance 备用源" if used_fallback and cfg["source"] == "sina" else cfg["source"]
            logging.info("%s %s: %s", market, s,
                         f"失败 {err}" if err else f"{len(df)} 行，最新 {df.index[-1].date() if len(df) else '-'}")
            logging.info("%s %s 行情来源：%s", market, s, source_name)
        if market == "cn":   # A 股自选：保留东方财富/新浪双源
            wl = datasource.load_watchlist(market)
            for code in list(wl):
                time.sleep(random.uniform(2.5, 4.5))
                df, err, source_used = datasource.update_stock(market, code, cfg)
                errors[code] = err
                if not wl.get(code):
                    wl[code] = datasource.stock_name(code) or code
                logging.info("%s %s %s: %s", market, code, wl.get(code, ""),
                             f"失败 {err}" if err else f"{len(df)} 行，最新 {df.index[-1].date() if len(df) else '-'}"
                             + (f"（来源：{source_used}）" if source_used else ""))
            datasource.save_watchlist(market, wl)
        else:   # 美股个股自选使用已配置的 Yahoo Finance 日线源
            wl = datasource.load_watchlist(market)
            for code in list(wl):
                df, err, _ = datasource.update(market, code, cfg)
                errors[code] = err
                logging.info("%s %s %s: %s", market, code, wl.get(code, ""),
                             f"失败 {err}" if err else f"{len(df)} 行，最新 {df.index[-1].date() if len(df) else '-'}")
    if market == "cn" and cfg.get("all_market"):   # 沪深全市场
        try:
            if not offline:
                r = allmarket.daily_update(max_repair_minutes=cfg.get("repair_minutes", 30))
                logging.info("全市场数据更新：%s", r)
            unfinished = not allmarket.load_progress().get("complete", True)
            if unfinished and scan.load_scan()[1]:
                logging.info("全市场数据还没更新完，先不重新回测（续传完成后再跑）")
            elif allmarket.cached_codes():
                logging.info("正在进行 A 股全市场回测（%d 只股票）", len(allmarket.cached_codes()))
                m = scan.run()
                logging.info("全市场回测：%d 只，%d 秒", m["stocks"], m["seconds"])
        except Exception as e:
            logging.exception("全市场部分失败：%s", e)
    prices = datasource.load_market(market, cfg)
    if cfg["main"] not in prices:
        raise RuntimeError(f"{cfg['name']} 主品种 {cfg['main']} 没有数据，先联网运行一次")
    summary = daily_report.build(market, cfg, prices, errors)
    daily_report.save(market, summary)
    daily_report.log_signals(market, summary)
    logging.info("%s 汇总完成，数据日期 %s", cfg["name"], summary["data_date"])
    try:
        signals_and_alerts(market, cfg, prices, summary, send=not offline)
    except Exception as e:
        logging.exception("信号记录/提醒失败：%s", e)
    return summary


def signals_and_alerts(market: str, cfg: dict, prices: dict, summary: dict, send: bool = True):
    """记录今天的信号（之后统计准不准），并给自选股和主 ETF 发 Mac 通知。"""
    rows, alerts = [], []
    alert_strats = set(cfg.get("alert_strategies", ["blend2"]))
    main, main_name = summary["main"], summary["main_name"]
    for s in summary["strategies"]:
        if s.get("action") in tracking.ACTIONS and s["key"] != "buy_hold":
            code = main
            rows.append({"code": code, "name": main_name, "strategy": s["key"], "action": s["action"],
                         "next": s.get("next_total")})
            if s["key"] in alert_strats:
                alerts.append(f"{main_name}：{s['label']} {s['action']}（明日目标 {s['next_total'] * 100:.0f}%）")
    scan_counts = ""
    if market == "cn":
        watch = datasource.load_watchlist(market)
        df, meta = scan.load_scan()
        if df is not None and meta:
            fresh = df[(df["last_date"] == meta["data_date"]) & (df["action"].isin(["买入", "卖出"]))]
            for r in fresh.itertuples():
                rows.append({"code": r.code, "name": r.name, "strategy": r.strategy, "action": r.action,
                             "next": float(r.next)})
            for r in fresh[fresh["code"].isin(watch.keys()) & fresh["strategy"].isin(alert_strats)].itertuples():
                alerts.append(f"{r.name}({r.code})：{r.action}（明日目标 {r.next * 100:.0f}%）")
            b = meta["strategies"].get("blend2", {})
            scan_counts = f"全市场（组合策略）：买入 {b.get('buy', 0)} 只，卖出 {b.get('sell', 0)} 只。"
            tracking.record(market, meta["data_date"], [r for r in rows if r["code"] != main])
    tracking.record(market, summary["data_date"], [r for r in rows if r["code"] == main])
    ev = tracking.evaluate(market, cfg)
    tracking.save_eval(market, ev)
    try:
        rep = tracking.evaluate(market, cfg, hist=tracking.build_replay(market, cfg, prices))
        tracking.save_eval(market, rep, replay=True)
    except Exception as e:
        logging.exception("历史回放失败（不影响实盘记录）：%s", e)
    logging.info("%s 记录信号 %d 条", cfg["name"], len(rows))
    if send and cfg.get("notify", True):
        body = "；".join(alerts) if alerts else "自选股和主要 ETF 今天没有新信号。"
        if scan_counts:
            body += " " + scan_counts
        label = "北京时间" if market == "cn" else "美东时间"
        notify.send(f"量化日报 · {cfg['name']} {summary['data_date']}", body,
                    timezone=cfg["tz"], timezone_label=label)


def needs_update(market: str) -> bool:
    """最近一个已收盘的交易日，是否已经处理过。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    s = daily_report.load(market)
    if not s:
        return True
    done = s.get("data_date", "")
    cfg = MARKETS[market]
    now = datetime.now(ZoneInfo(cfg["tz"]))
    hh, mm = map(int, cfg["close_ready"].split(":"))
    day = now.date() if (now.hour, now.minute) >= (hh, mm) else now.date() - timedelta(days=1)
    if market == "cn":
        try:
            cal = allmarket.trade_calendar()
            day = max(d for d in cal if d <= day)
        except Exception:
            pass
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    if market == "cn" and cfg.get("all_market") and allmarket.cached_codes():
        prog = allmarket.load_progress()
        if prog.get("trade_day") == day.isoformat() and not prog.get("complete", True):
            return True   # 上次下载到一半断了，接着下
        _, meta = scan.load_scan()
        if not meta or meta.get("data_date", "") < day.isoformat():
            return True
    return done < day.isoformat()


def online(timeout: float = 5) -> bool:
    """能不能连上网（试几个国内外常用站点，任意一个连得上就算联网）。"""
    import socket
    for host in ("hq.sinajs.cn", "www.baidu.com", "www.apple.com"):
        try:
            socket.create_connection((host, 443), timeout=timeout).close()
            return True
        except OSError:
            continue
    return False


def restart_server_if_requested():
    """代码更新后需要重启网页服务时，会留下 logs/.restart_server 这个标记文件。
    定时任务运行时看到它，就让 launchd 重启网页服务（KeepAlive 会立刻重新拉起），然后删掉标记。"""
    flag = ROOT / "logs" / ".restart_server"
    if not flag.exists():
        return
    import subprocess
    try:
        flag.unlink()
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.quantdaily.server"],
                       timeout=30, check=False)
        logging.info("检测到代码更新，已重启网页服务")
    except Exception as e:
        logging.warning("重启网页服务失败：%s", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=list(MARKETS), nargs="*", default=list(ENABLED))
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--auto", action="store_true",
                    help="定时任务用：只更新\"已经收盘、但还没处理过\"的市场")
    a = ap.parse_args()
    setup_logging()
    restart_server_if_requested()
    os.umask(0o077)   # 新建的文件只有你自己能读写

    lock = open(ROOT / "logs" / ".daily.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if a.auto:
            return   # 上一次自动任务还在跑（比如正在续传），这次不用重复启动
        logging.warning("另一个 daily.py 还在运行（可能是自动任务或之前没停干净的），这次跳过。"
                        "如果确定没有在跑，运行 pkill -f daily.py 后再试")
        print("另一个 daily.py 还在运行，这次跳过。可以运行 pkill -f daily.py 后再试。")
        return
    if a.auto:
        # 看门狗：自动任务最多运行 3 小时，超时就自己退出，释放锁，不挡住下一次定时任务
        import signal

        def _timeout(*_):
            logging.error("自动任务运行超过 3 小时，可能卡住了，强制退出（下次定时任务会重试）")
            os._exit(2)
        signal.signal(signal.SIGALRM, _timeout)
        signal.alarm(3 * 3600)
    markets = [m for m in a.market if needs_update(m)] if a.auto else a.market
    if a.auto:
        if not markets:
            return   # 都是最新的：静悄悄退出（每 15 分钟检查一次，不刷日志）
        if not a.offline and not online():
            logging.info("自动模式：需要更新 %s，但现在没联网，等下次检查", markets)
            return
        logging.info("自动模式：本次更新 %s", markets)
    ok = True
    for m in markets:
        try:
            run_market(m, a.offline)
        except Exception as e:
            ok = False
            logging.exception("%s 失败：%s", m, e)
    if markets:   # 数据更新过，顺便重新生成离线版单文件（失败只记日志，不影响每日更新）
        try:
            import subprocess
            r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "export_offline.py")],
                               capture_output=True, text=True, timeout=300)
            logging.info("离线版：%s", (r.stdout.strip().splitlines() or ["无输出"])[-1] if r.returncode == 0
                         else "生成失败 " + (r.stderr or "")[-200:])
        except Exception as e:
            logging.warning("离线版生成失败（不影响更新）：%s", e)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
