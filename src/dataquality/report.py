"""每日数据质量日报:只读扫描日线 parquet,复用 consistency 的检查,输出 Markdown + JSON。

约束:
  - 对数据目录只读;输出目录若位于数据目录之内则拒绝写入。
  - 不联网;逐只 try/except,坏文件记入 unreadable 不中断;整体失败只记录,不向调用方抛。
  - 北京时间 15:30 前,当天的 K 线是盘中半天数据,一律忽略并在报告里注明。
  - 抽样(--sample)时,报告明确写出抽样数量与种子,不把抽样结果当成全市场。
"""
from __future__ import annotations

import json
import random
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .. import market_clock
from .consistency import SEVERITY_ORDER, Thresholds, check_series
from .status_table import board_of, parse_date

BEIJING = ZoneInfo("Asia/Shanghai")
CUTOFF_HOUR, CUTOFF_MINUTE = market_clock.CLOSE_READY.hour, market_clock.CLOSE_READY.minute   # 规则见 src/market_clock.py
REPORT_MD = "data_quality.md"
REPORT_JSON = "data_quality.json"
HISTORY_DIR = "history"


@dataclass
class ScanResult:
    """一次扫描的结果(可 JSON 序列化,供次日对比)。"""
    day: str                                   # 报告日期 YYYY-MM-DD
    data_dir: str = ""
    files_total: int = 0                       # 目录里 parquet 总数
    scanned: int = 0                           # 实际成功检查的只数
    sample: int | None = None
    seed: int = 0
    unreadable: list = field(default_factory=list)       # [(文件名, 原因)]
    empty: list = field(default_factory=list)            # 读到但没有有效行的代码
    list_count: int | None = None              # cn_all_list.csv 里的在市数(找不到则 None)
    calendar_used: bool = False
    latest_day: str | None = None              # 所有股票最后一根 K 线的最大日期
    last_date_dist: dict = field(default_factory=dict)   # 最后一根日期 -> 只数
    stale: int = 0                             # 最后一根日期早于 latest_day 的只数
    intraday_dropped: bool = False
    intraday_dropped_count: int = 0
    by_kind: dict = field(default_factory=dict)          # kind -> 有该问题的股票数
    by_severity: dict = field(default_factory=dict)      # severity -> 问题条数
    stocks: dict = field(default_factory=dict)           # code -> {error,warn,info,top:[...]}
    elapsed: float = 0.0
    timed_out: bool = False
    status: str = "ok"                         # ok / no_data / error
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


# ---------------------------------------------------------------- 辅助

def beijing_now() -> datetime:
    return datetime.now(BEIJING)


def _resolve_dir(data_dir) -> Path:
    """data_dir 可以是 parquet 所在目录,也可以是它的上级(含 cn_all 子目录)。"""
    p = Path(data_dir)
    sub = p / "cn_all"
    if sub.is_dir() and not any(p.glob("*.parquet")):
        return sub
    return p


def _read_calendar(path: Path):
    try:
        if not path.is_file():
            return None
        df = pd.read_csv(path, dtype=str)
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        days = sorted({d for d in (parse_date(v) for v in df[col]) if d})
        return days or None
    except Exception:  # noqa: BLE001
        return None


def _read_list(path: Path) -> dict:
    """code -> {'ipo': date|None, 'board': str|None};读不到返回空字典。"""
    out: dict = {}
    try:
        if not path.is_file():
            return out
        df = pd.read_csv(path, dtype=str)
        if "code" not in df.columns:
            return out
        for _, r in df.iterrows():
            code = str(r["code"]).strip().zfill(6)
            out[code] = {"ipo": parse_date(r.get("ipo")) if "ipo" in df.columns else None,
                         "board": board_of(code)}
    except Exception:  # noqa: BLE001
        return {}
    return out


def _read_suspended(path: Path):
    """{日期: set(代码)};读不到返回 None。"""
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        out = {}
        for k, v in raw.items():
            d = parse_date(k)
            if d:
                out[d] = {str(c).zfill(6) for c in v}
        return out or None
    except Exception:  # noqa: BLE001
        return None


def _load_frame(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if not isinstance(df.index, pd.DatetimeIndex):
        for c in ("date", "Date", "日期"):
            if c in df.columns:
                df = df.set_index(c)
                break
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- 扫描

def scan_dir(data_dir, *, sample: int | None = None, seed: int = 0, today: date | None = None,
             now: datetime | None = None, max_seconds: float | None = None,
             th: Thresholds = Thresholds(), use_aux: bool = True) -> ScanResult:
    """只读扫描 data_dir 下的 *.parquet。不抛异常:问题记入结果的 errors/unreadable。"""
    t0 = time.time()
    now = now or beijing_now()
    today = today or now.astimezone(BEIJING).date()
    res = ScanResult(day=today.isoformat(), sample=sample, seed=seed)
    try:
        d = _resolve_dir(data_dir)
        res.data_dir = str(d)
        files = sorted(d.glob("*.parquet")) if d.is_dir() else []
        res.files_total = len(files)
        if not files:
            res.status = "no_data"
            return res

        if sample is not None and sample > 0 and sample < len(files):
            files = sorted(random.Random(seed).sample(files, sample))
        else:
            res.sample = None

        calendar = suspended = None
        info: dict = {}
        if use_aux:
            aux = d.parent
            calendar = _read_calendar(aux / "cn_trade_calendar.csv")
            suspended = _read_suspended(aux / "cn_all_suspended.json")
            info = _read_list(aux / "cn_all_list.csv")
            res.list_count = len(info) if info else None
        res.calendar_used = calendar is not None

        # 15:30 前不用当天 K 线
        drop_today = (today == now.astimezone(BEIJING).date()
                      and not market_clock.is_close_ready(now))
        res.intraday_dropped = drop_today

        kind_stocks: dict[str, int] = {}
        sev_count: dict[str, int] = {}
        last_dates: dict[str, int] = {}
        for f in files:
            if max_seconds is not None and time.time() - t0 > max_seconds:
                res.timed_out = True
                break
            code = f.stem
            try:
                df = _load_frame(f)
                if drop_today and len(df):
                    mask = df.index.date == today
                    n = int(mask.sum())
                    if n:
                        res.intraday_dropped_count += n
                        df = df[~mask]
                if df.empty:
                    res.empty.append(code)
                    continue
                meta = info.get(code, {})
                issues = check_series(
                    code, df, calendar=calendar, suspended=suspended,
                    board=meta.get("board") or board_of(code), ipo=meta.get("ipo"), th=th)
            except Exception as e:  # noqa: BLE001
                res.unreadable.append([f.name, f"{type(e).__name__}: {e}"[:200]])
                continue
            res.scanned += 1
            ld = df.index[-1].date().isoformat()
            last_dates[ld] = last_dates.get(ld, 0) + 1
            cnt = {"error": 0, "warn": 0, "info": 0}
            for i in issues:
                cnt[i.severity] = cnt.get(i.severity, 0) + 1
                sev_count[i.severity] = sev_count.get(i.severity, 0) + 1
            for k in {i.kind for i in issues}:
                kind_stocks[k] = kind_stocks.get(k, 0) + 1
            if issues:
                res.stocks[code] = {**cnt, "top": [
                    {"kind": i.kind, "severity": i.severity, "day": i.day.isoformat() if i.day else None,
                     "detail": i.detail[:160]} for i in issues[:3]]}
        res.by_kind = dict(sorted(kind_stocks.items(), key=lambda kv: -kv[1]))
        res.by_severity = sev_count
        res.last_date_dist = dict(sorted(last_dates.items()))
        if last_dates:
            res.latest_day = max(last_dates)
            res.stale = sum(n for k, n in last_dates.items() if k < res.latest_day)
        if res.scanned == 0:
            res.status = "no_data"
    except Exception as e:  # noqa: BLE001
        res.status = "error"
        res.errors.append(f"{type(e).__name__}: {e}")
        res.errors.append(traceback.format_exc(limit=3)[-500:])
    finally:
        res.elapsed = round(time.time() - t0, 2)
    return res


# ---------------------------------------------------------------- 对比与渲染

def _flagged(stocks: dict) -> set:
    return {c for c, s in stocks.items() if s.get("error", 0) or s.get("warn", 0)}


def _rank(stocks: dict, top_n: int) -> list:
    items = [(c, s) for c, s in stocks.items() if s.get("error", 0) or s.get("warn", 0)]
    items.sort(key=lambda cs: (-cs[1].get("error", 0), -cs[1].get("warn", 0), cs[0]))
    return items[:top_n]


def _esc(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_markdown(result: ScanResult, previous: dict | None = None, top_n: int = 20) -> str:
    r = result
    L: list[str] = [f"# 数据质量日报 {r.day}", ""]
    status_txt = {"ok": "正常", "no_data": "无数据", "error": "运行失败(见下)"}.get(r.status, r.status)
    L += [f"- 运行状态:**{status_txt}**" + ("(超时,仅扫描了部分文件)" if r.timed_out else ""),
          f"- 数据目录:`{r.data_dir}`(只读)",
          f"- 耗时:{r.elapsed} 秒", ""]
    if r.errors:
        L += ["## 运行错误", *[f"- {_esc(e)}" for e in r.errors], ""]
    if r.status == "no_data":
        L += ["未找到可用的 parquet 数据(目录为空、不存在,或全部文件无有效行),因此没有统计。", ""]

    L += ["## 覆盖"]
    L.append(f"- 目录内 parquet 文件数:{r.files_total}")
    if r.sample:
        L.append(f"- **抽样**:本次只抽查 {r.scanned}/{r.files_total} 只(--sample {r.sample},seed={r.seed});"
                 "下列比例只代表样本,不代表全市场。")
    L.append(f"- 成功检查:{r.scanned} 只")
    if r.list_count is not None:
        L.append(f"- 在市清单(cn_all_list.csv)股票数:{r.list_count}")
    else:
        L.append("- 在市清单:未找到或无法读取,覆盖率无法判断")
    L.append(f"- 交易日历:{'已使用' if r.calendar_used else '未找到,跳过缺失交易日检查'}")
    L.append(f"- 空数据(无有效行):{len(r.empty)} 只;不可读文件:{len(r.unreadable)} 个")
    if r.intraday_dropped:
        L.append(f"- 北京时间 15:30 前生成:已忽略当天(盘中)K 线 {r.intraday_dropped_count} 根")
    L.append("")

    if r.latest_day:
        L += ["## 最新交易日分布",
              f"- 最新日期:{r.latest_day};最后一根停在更早日期的有 {r.stale} 只"
              f"({r.stale / max(r.scanned, 1):.1%})。若日更正在进行,这是“部分已更新”的混合态。",
              "", "| 最后一根日期 | 只数 |", "|---|---|"]
        for k in sorted(r.last_date_dist, reverse=True)[:8]:
            L.append(f"| {k} | {r.last_date_dist[k]} |")
        L.append("")

    if r.by_kind:
        L += ["## 问题类型占比(含该问题的股票数 / 成功检查数)", "",
              "| 类型 | 股票数 | 占比 |", "|---|---|---|"]
        for k, n in r.by_kind.items():
            L.append(f"| {_esc(k)} | {n} | {n / max(r.scanned, 1):.1%} |")
        L.append(f"\n问题条数(按严重级别):{r.by_severity}\n")

    ranked = _rank(r.stocks, top_n)
    if ranked:
        L += [f"## 异常 Top {len(ranked)}(按 error 数、warn 数排序)", "",
              "| 代码 | error | warn | 示例 |", "|---|---|---|---|"]
        for c, s in ranked:
            ex = "; ".join(f"{t['kind']}@{t['day'] or '-'}" for t in s.get("top", []))
            L.append(f"| {c} | {s.get('error', 0)} | {s.get('warn', 0)} | {_esc(ex)} |")
        L.append("")
    elif r.status == "ok":
        L += ["## 异常", "本次没有 error/warn 级别的异常。", ""]

    if r.unreadable:
        L += ["## 不可读文件", *[f"- `{_esc(n)}`:{_esc(why)}" for n, why in r.unreadable[:50]]]
        if len(r.unreadable) > 50:
            L.append(f"- …另有 {len(r.unreadable) - 50} 个")
        L.append("")

    L.append("## 与前一次对比")
    if not previous:
        L.append("没有找到更早的日报 JSON,无法对比。")
    else:
        same = (previous.get("sample") == r.sample) and not previous.get("timed_out") and not r.timed_out
        cur, prev = _flagged(r.stocks), set(previous.get("flagged", []))
        L.append(f"- 对比对象:{previous.get('day')};有异常股票数 {len(prev)} → {len(cur)}")
        if not same:
            L.append("- **注意**:两次的抽样设置不同或有超时,新增/消失数不可直接解读。")
        new, gone = sorted(cur - prev), sorted(prev - cur)
        L.append(f"- 新增异常股票 {len(new)} 只" + (":" + ", ".join(new[:15]) + ("…" if len(new) > 15 else "") if new else ""))
        L.append(f"- 消失异常股票 {len(gone)} 只" + (":" + ", ".join(gone[:15]) + ("…" if len(gone) > 15 else "") if gone else ""))
        pk = previous.get("by_kind", {})
        for k in sorted(set(pk) | set(r.by_kind)):
            a, b = pk.get(k, 0), r.by_kind.get(k, 0)
            if a != b:
                L.append(f"  - {_esc(k)}:{a} → {b}")
    L += ["", "---",
          "本报告只读数据目录,未修改任何行情文件。阈值与涨跌幅制度日期为凭记忆的默认值 [需核实];"
          "异常不等于数据错误,也不构成对策略有效性的任何结论。"]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 写出

def _load_previous(out_dir: Path, day: str) -> dict | None:
    """取 history/ 下日期早于 day 的最新一份 JSON。"""
    try:
        hist = out_dir / HISTORY_DIR
        cands = sorted(p for p in hist.glob("*.json") if p.stem < day)
        if not cands:
            return None
        return json.loads(cands[-1].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_report(result: ScanResult, out_dir, *, top_n: int = 20) -> dict:
    """写 data_quality.md / data_quality.json / history/<日期>.json。不抛异常,返回状态字典。"""
    info = {"ok": False, "md": None, "json": None, "error": None}
    try:
        out = Path(out_dir)
        if result.data_dir and _inside(out, Path(result.data_dir)):
            raise ValueError("输出目录位于数据目录之内,拒绝写入")
        out.mkdir(parents=True, exist_ok=True)
        (out / HISTORY_DIR).mkdir(exist_ok=True)
        prev = _load_previous(out, result.day)
        md = render_markdown(result, prev, top_n)
        payload = result.to_dict()
        payload["flagged"] = sorted(_flagged(result.stocks))
        payload.pop("stocks", None)
        js = json.dumps(payload, ensure_ascii=False, indent=1)
        _atomic_write(out / REPORT_MD, md)
        _atomic_write(out / REPORT_JSON, js)
        # 超时的不进历史,避免用残缺结果当“前一日”
        if not result.timed_out and result.status != "error":
            _atomic_write(out / HISTORY_DIR / f"{result.day}.json", js)
        info.update(ok=True, md=str(out / REPORT_MD), json=str(out / REPORT_JSON))
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def run(data_dir, out_dir, *, sample=None, seed=0, top_n=20, today=None, now=None,
        max_seconds=None) -> dict:
    """扫描 + 写报告;任何异常只记录在返回值里。"""
    try:
        res = scan_dir(data_dir, sample=sample, seed=seed, today=today, now=now,
                       max_seconds=max_seconds)
    except Exception as e:  # noqa: BLE001
        res = ScanResult(day=(today or beijing_now().date()).isoformat(), status="error",
                         errors=[f"{type(e).__name__}: {e}"])
    w = write_report(res, out_dir, top_n=top_n)
    return {"status": res.status, "scanned": res.scanned, "write": w}
