"""流水线体检（T027）：只读检查「数据源 → 回测扫描 → 今日买卖清单 → 模拟盘撮合 → 信号核对」每一环有没有按期跑完。

用法（项目根目录）：
  .venv/bin/python -B scripts/pipeline_check.py [--expect-day YYYY-MM-DD] [--out FILE] [--wait-minutes N]

- 只读本地文件和 sqlite（只读方式打开），不联网、不写 data/、不导入 app、不调用任何交易接口。
- --expect-day 缺省为最近已收盘交易日（北京时间 15:30 后算当天）。
- --wait-minutes N：最长等 N 分钟，直到清单日期追上 expect-day 再检查（通宵用）；超时也会出报告，并把没跑完的环节标红。
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

D = ROOT / "data" / "daily"
OK, WARN, BAD = "✅", "⚠️", "❌"


def read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


def expected_day():
    from src import allmarket
    cal = allmarket.trade_calendar()
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    cutoff = now.date() if now.hour * 60 + now.minute >= 15 * 60 + 30 else now.date().fromordinal(now.date().toordinal() - 1)
    return max(d for d in cal if d <= cutoff).isoformat()


def list_day():
    d = read_json(D / "trade_list_cn.json")
    return (d or {}).get("data_date")


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def check_data(day):
    meta = read_json(D / "scan_cn_meta.json") or {}
    prog = read_json(D / "cn_all_progress.json") or {}
    rows = []
    last = None
    try:
        import pandas as pd
        for code in ("000001", "600000", "600519"):
            p = D / "cn_all" / f"{code}.parquet"
            if p.exists():
                idx = pd.read_parquet(p).index
                last = max(last, str(idx[-1].date())) if last else str(idx[-1].date())
    except Exception as e:
        rows.append((WARN, "读取个股日线", f"失败：{e}"))
    ok = last == day
    rows.append((OK if ok else BAD, "个股日线最新日期", f"{last}（应为 {day}）"))
    rows.append((OK if prog.get("complete") is True and prog.get("trade_day") == day else BAD, "全市场下载进度",
                 f"complete={prog.get('complete')}，trade_day={prog.get('trade_day')}"))
    rows.append((OK if meta.get("data_date") == day else BAD, "全市场回测扫描", f"data_date={meta.get('data_date')}，股票数 {meta.get('stocks')}"))
    return rows


def check_list(day):
    d = read_json(D / "trade_list_cn.json")
    if not d:
        return [(BAD, "今日买卖清单", "文件缺失或无法解析")]
    n = len((d.get("strategies", {}).get("blend2", {}) or {}).get("rows", []))
    return [(OK if d.get("data_date") == day else BAD, "今日买卖清单", f"data_date={d.get('data_date')}，blend2 {n} 条，生成于 {d.get('generated_at')}")]


def check_sim(day):
    p = ROOT / "data" / "simtrade.db"
    rows = []
    if not p.exists():
        return [(BAD, "模拟盘数据库", "不存在")]
    c = ro(p)
    try:
        eq = c.execute("SELECT day, value FROM equity WHERE acct='ai_main' ORDER BY day DESC LIMIT 1").fetchone()
        rows.append((OK if eq and eq[0] == day else BAD, "模拟盘净值日期", f"最新 {eq[0] if eq else None}（应为 {day}），净值 {eq[1] if eq else None}"))
        st = c.execute("SELECT signal_day, status, COUNT(*) FROM orders WHERE acct='ai_main' GROUP BY 1,2 ORDER BY 1 DESC, 2").fetchall()
        rows.append((OK, "订单状态（ai_main）", "；".join(f"{a} {b} {n}" for a, b, n in st[:8])))
        pend_old = c.execute("SELECT COUNT(*) FROM orders WHERE acct='ai_main' AND status='pending' AND fill_day<?", (day,)).fetchone()[0]
        rows.append((OK if pend_old == 0 else BAD, "过期仍挂单", f"{pend_old} 条（应为 0）"))
        tr = c.execute("SELECT COUNT(*), COALESCE(SUM(fee),0) FROM trades WHERE acct='ai_main'").fetchone()
        pos = c.execute("SELECT COUNT(*) FROM positions WHERE acct='ai_main'").fetchone()[0]
        rows.append((OK if tr[0] > 0 else WARN, "累计成交 / 费用 / 持仓", f"{tr[0]} 笔 / {tr[1]:.2f} 元 / {pos} 只"))
        bad = c.execute("SELECT reason, COUNT(*) FROM orders WHERE acct='ai_main' AND status='rejected' GROUP BY 1 ORDER BY 2 DESC LIMIT 3").fetchall()
        if bad:
            rows.append((OK, "被拒绝原因（前 3）", "；".join(f"{r[:30]}×{n}" for r, n in bad)))
    finally:
        c.close()
    rep = read_json(D / "simtrade" / f"{day}.json")
    rows.append((OK if rep else BAD, "当日模拟盘报告", f"{'有' if rep else '没有'} data/daily/simtrade/{day}.json"))
    return rows


def check_ledger(day):
    p = D / "ledger" / f"{day}.json"
    return [(OK if p.exists() else BAD, "信号核对账本", f"{'有' if p.exists() else '没有'} ledger/{day}.json")]


def check_log(day):
    p = ROOT / "logs" / "daily.log"
    rows = []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except Exception:
        return [(WARN, "daily.log", "读不到")]
    errs = [l for l in lines if re.search(r"ERROR|Traceback|失败", l)]
    rows.append((OK if not errs else WARN, "daily.log 最近 400 行中的错误", f"{len(errs)} 条" + (f"，最后一条：{errs[-1][:160]}" if errs else "")))
    return rows


def build(day):
    out = [f"# 流水线体检 · 应有交易日 {day} · 生成于 {time.strftime('%Y-%m-%d %H:%M')}", ""]
    for title, fn in (("1. 数据源", check_data), ("2. 今日买卖清单", check_list), ("3. 模拟盘", check_sim), ("4. 信号核对账本", check_ledger), ("5. 每日任务日志", check_log)):
        out += [f"## {title}", ""]
        try:
            rows = fn(day)
        except Exception as e:
            rows = [(BAD, "检查本身出错", repr(e))]
        out += [f"- {a} **{b}**：{c}" for a, b, c in rows] + [""]
    out += ["说明：只读检查，不联网、不改数据；模拟盘不是真实交易，也不是策略有效的证据。"]
    return "\n".join(out), any("❌" in l for l in out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-day")
    ap.add_argument("--out")
    ap.add_argument("--wait-minutes", type=float, default=0)
    a = ap.parse_args(argv)
    day = a.expect_day or expected_day()
    deadline = time.time() + a.wait_minutes * 60
    waited = False
    while a.wait_minutes and list_day() != day and time.time() < deadline:
        time.sleep(300)
        waited = True
    if waited and list_day() == day:
        time.sleep(600)          # 清单刚生成，给后面的模拟盘撮合和信号核对账本留时间
    text, bad = build(day)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
