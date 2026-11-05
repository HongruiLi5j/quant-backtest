"""离线版（export_offline.py）输入指纹：输入没变就不必重新生成 18 MB 的离线版（D22）。

为什么不用文件修改时间：每 15 分钟的自动任务即使拿到的数据一模一样，也会重写 ETF 行情 CSV、
summary/tracking JSON（只有 generated 时间戳变了），按修改时间判断永远是"变了"。所以：
- 行情 CSV 和顶层 JSON 按内容算哈希；JSON 去掉顶层的 generated / generated_at 两个时间戳字段再算；
- 只在全量任务才会变的大目录（全市场个股、清单、账本、扫描结果）按 路径+大小+修改时间 算，便宜；
- 离线版用到的代码和网页文件也算进去，改了代码会重新生成；
- 当天日期也算进去：最少每天重新生成一次，万一漏算了某个输入，最多旧一天。
离线版不读 notifications.json（导出时写死为空列表）和信号历史 parquet（用打开文件审计核对过 export_offline.py
实际读的文件），而这几个文件每次自动任务都会重写，算进去就永远"变了"，所以排除；锁文件、.DS_Store 也不算。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

VOLATILE_KEYS = ("generated", "generated_at")       # 只是生成时间，不影响离线版里的数据
SKIP_NAMES = {"notifications.json", ".DS_Store", "signals_history.parquet", "trade_signals_history.parquet"}
CONTENT_SUBDIRS = ("cn", "us")                       # 指数/基金/自选股行情 CSV：每次都重写，按内容比
CODE_FILES = ("export_offline.py", "app.py", "config.py", "dashboard/index.html", "dashboard/app.js",
              "dashboard/i18n.js", "dashboard/analysis.js", "dashboard/offline_engine.js", "data/auto_params.json")


def _skip(p: Path) -> bool:
    return p.name in SKIP_NAMES or p.suffix == ".lock" or p.name.startswith(".")


def _json_digest(p: Path) -> str:
    raw = p.read_bytes()
    try:
        obj = json.loads(raw)
    except ValueError:
        return hashlib.sha1(raw).hexdigest()
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k not in VOLATILE_KEYS}
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _stat(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def fingerprint(root: Path, today: date | None = None) -> str:
    """返回离线版输入的指纹（十六进制字符串）。root 是项目根目录。"""
    root = Path(root)
    daily = root / "data" / "daily"
    h = hashlib.sha1()
    h.update(f"day={(today or date.today()).isoformat()}\n".encode())
    for rel in CODE_FILES:
        p = root / rel
        h.update(f"{rel}={_stat(p) if p.exists() else '-'}\n".encode())
    for p in sorted((root / "src").rglob("*.py")):
        h.update(f"{p.relative_to(root)}={_stat(p)}\n".encode())
    if daily.is_dir():
        for p in sorted(daily.rglob("*")):
            if not p.is_file() or _skip(p):
                continue
            rel = p.relative_to(daily)
            if len(rel.parts) == 1 and p.suffix == ".json":
                v = _json_digest(p)
            elif (len(rel.parts) == 1 and p.suffix == ".csv") or (len(rel.parts) == 2 and rel.parts[0] in CONTENT_SUBDIRS):
                v = hashlib.sha1(p.read_bytes()).hexdigest()
            else:
                v = _stat(p)
            h.update(f"{rel}={v}\n".encode())
    return h.hexdigest()


def unchanged(root: Path, store: Path, today: date | None = None) -> tuple[bool, str]:
    """(输入是否与上次成功生成时相同, 当前指纹)。没有记录（第一次、记录丢了）一律当作变了，强制重新生成。"""
    fp = fingerprint(root, today)
    try:
        last = Path(store).read_text().strip()
    except OSError:
        return False, fp
    return last == fp, fp


def remember(store: Path, fp: str) -> None:
    """离线版生成成功后记下指纹；先写临时文件再替换，避免写一半。"""
    store = Path(store)
    tmp = store.with_suffix(store.suffix + ".tmp")
    tmp.write_text(fp + "\n")
    tmp.replace(store)
