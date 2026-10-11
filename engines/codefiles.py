"""网页「代码」页：列出、读取、保存策略代码，保存前检查语法，每次保存都留历史版本，可以一键恢复。

安全：内置文件只允许读写下面白名单里的（不能通过网页读写任意路径）。
「我的策略」是例外：用户可以在网页上新建任意多个，存在 engines/custom/ 目录下，
每个文件是独立的一个 strategy(df) 函数，key 统一是 "custom_<文件名>"。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "daily" / "code_history"
CUSTOM_DIR = ROOT / "engines" / "custom"
MAX_SIZE = 400_000

# key → (路径, 能否修改, 说明)
FILES = {
    "bt_strategies": ("engines/bt_strategies.py", True, "backtrader 版 7 个策略（国外引擎）"),
    "rq_strategies": ("engines/rq_strategies.py", True, "RQAlpha 米筐版 7 个策略（国内引擎）"),
    "strategies": ("src/strategies.py", True, "自研引擎的策略（每日任务、全市场扫描也用它，改动影响全局）"),
    "bt_base": ("engines/bt_base.py", True, "backtrader 框架：A 股手续费、次日开盘成交、涨跌停、整手"),
    "rq_base": ("engines/rq_base.py", True, "RQAlpha 框架：流式指标、集合竞价下单"),
    "bt_engine": ("engines/bt_engine.py", False, "backtrader 运行入口"),
    "rq_engine": ("engines/rq_engine.py", False, "RQAlpha 运行入口 + 自定义数据源 Mod"),
    "inhouse": ("engines/inhouse.py", False, "自研引擎的包装（和外部引擎同样的输出格式）"),
    "custom_engine": ("engines/custom_engine.py", False, "「我的策略」的运行入口"),
    "common": ("engines/common.py", False, "三个引擎共用：数据、成本、统计"),
    "optimize": ("engines/optimize.py", False, "参数优化：网格搜索 + 样本内/样本外"),
    "portfolio": ("src/portfolio.py", False, "自研回测引擎核心"),
    "indicators": ("src/indicators.py", False, "自研引擎的技术指标"),
    "registry": ("src/registry.py", False, "策略登记表：网页上的策略和参数范围"),
}

CUSTOM_TEMPLATE = '''"""在这里写你自己的策略。保存后，填一个股票代码，点"保存并运行回测"，
就会用真实的历史数据跑一遍（自研引擎口径：次日开盘成交，A 股手续费和涨跌停规则）。

只需要写一个函数：

    def strategy(df: pd.DataFrame) -> pd.Series

- df：这只股票的日线数据，按日期升序排列，列有 Open / High / Low / Close / Volume（都是后复权价格）
- 返回值：一个和 df.index 长度一样的 Series，表示"当天收盘后决定的目标仓位"：
    0   = 空仓
    1   = 满仓
    0.5 = 半仓（可以是 0~1 之间任意小数）
- 规则和内置的 7 个策略一样：只能用截至当天收盘的数据，不能提前用到未来的价格
  （比如用 df["Close"].shift(-1) 就是偷看未来，回测结果不可信，实盘不可能做到）
- 不用自己管成交价格、手续费、涨跌停——这些回测引擎会统一处理，你只管"信号"

下面是一个最简单的例子：站上 20 日均线就满仓，跌破就空仓。改掉它，写你自己的策略。
"""
from __future__ import annotations

import pandas as pd


def strategy(df: pd.DataFrame) -> pd.Series:
    ma20 = df["Close"].rolling(20).mean()
    return (df["Close"] > ma20).astype(float)
'''


def _is_custom(key: str) -> bool:
    return key.startswith("custom_")


def _slug(key: str) -> str:
    return key[len("custom_"):]


def _custom_path(slug: str) -> Path:
    return CUSTOM_DIR / f"{slug}.py"


def _custom_files() -> list[Path]:
    return sorted(CUSTOM_DIR.glob("*.py")) if CUSTOM_DIR.exists() else []


def listing() -> list[dict]:
    out = []
    for f in _custom_files():
        key = f"custom_{f.stem}"
        out.append({"key": key, "path": f"engines/custom/{f.name}", "editable": True,
                    "desc": "我的策略：填个股票代码，点「保存并运行回测」", "size": f.stat().st_size,
                    "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)),
                    "versions": len(list((HIST / key).glob("*.py"))) if (HIST / key).exists() else 0,
                    "custom": True})
    for k, (p, editable, desc) in FILES.items():
        f = ROOT / p
        out.append({"key": k, "path": p, "editable": editable, "desc": desc,
                    "size": f.stat().st_size if f.exists() else 0,
                    "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)) if f.exists() else "",
                    "versions": len(list((HIST / k).glob("*.py"))) if (HIST / k).exists() else 0,
                    "custom": False})
    return out


def create(name: str) -> dict:
    """新建一个"我的策略"文件，返回它的 key，网页拿到后直接跳转过去。"""
    slug = re.sub(r"[^A-Za-z0-9_一-鿿-]+", "_", (name or "").strip()).strip("_")[:40]
    if not slug:
        slug = "策略" + time.strftime("%H%M%S")
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    base, i, final = slug, 1, slug
    while _custom_path(final).exists():
        i += 1
        final = f"{base}_{i}"
    _custom_path(final).write_text(CUSTOM_TEMPLATE, encoding="utf-8")
    return {"key": f"custom_{final}", "path": f"engines/custom/{final}.py"}


def read(key: str) -> dict:
    if _is_custom(key):
        p = _custom_path(_slug(key))
        if not p.exists():
            raise KeyError("没有这个文件")
        return {"key": key, "path": f"engines/custom/{p.name}", "editable": True,
                "desc": "我的策略：填个股票代码，点「保存并运行回测」", "content": p.read_text(encoding="utf-8")}
    if key not in FILES:
        raise KeyError("没有这个文件")
    p, editable, desc = FILES[key]
    return {"key": key, "path": p, "editable": editable, "desc": desc,
            "content": (ROOT / p).read_text(encoding="utf-8")}


def _backup(key: str, text: str, tag: str):
    d = HIST / key
    d.mkdir(parents=True, exist_ok=True)
    orig = d / "original.py"
    if not orig.exists():                       # 第一次保存前，留一份"出厂版本"
        orig.write_text(text, encoding="utf-8")
    name = time.strftime("%Y%m%d-%H%M%S") + (f"-{tag}" if tag else "") + ".py"
    (d / name).write_text(text, encoding="utf-8")
    vs = sorted(p for p in d.glob("2*.py"))
    for old in vs[:-60]:                        # 最多留 60 个历史版本
        old.unlink()


def save(key: str, content: str) -> dict:
    if _is_custom(key):
        f = _custom_path(_slug(key))
        editable = True
    elif key in FILES:
        p, editable, _ = FILES[key]
        f = ROOT / p
    else:
        raise KeyError("没有这个文件")
    if not editable:
        raise PermissionError("这个文件只能查看，不能在网页上修改")
    if not isinstance(content, str) or len(content) > MAX_SIZE:
        raise ValueError("内容太大")
    content = content.replace("\r\n", "\n")
    try:
        compile(content, str(f), "exec")         # 先检查语法，有错就不保存
    except SyntaxError as e:
        return {"ok": False, "error": f"语法错误：第 {e.lineno} 行：{e.msg}", "line": e.lineno}
    old = f.read_text(encoding="utf-8") if f.exists() else ""
    if old == content:
        return {"ok": True, "unchanged": True}
    _backup(key, old, "before")
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(f)                              # 原子替换
    return {"ok": True}


def history(key: str) -> list[dict]:
    if not (_is_custom(key) or key in FILES):
        raise KeyError("没有这个文件")
    d = HIST / key
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.py"), reverse=True):
        label = "出厂版本（第一次修改之前）" if p.stem == "original" else \
            f"{p.stem[:4]}-{p.stem[4:6]}-{p.stem[6:8]} {p.stem[9:11]}:{p.stem[11:13]}:{p.stem[13:15]} 保存前的版本"
        out.append({"v": p.stem, "label": label, "size": p.stat().st_size})
    return out


def version(key: str, v: str) -> str:
    if not (_is_custom(key) or key in FILES) or not v.replace("-", "").replace("before", "").isalnum():
        raise KeyError("没有这个版本")
    p = HIST / key / f"{v}.py"
    if not p.exists() or p.parent != HIST / key:
        raise KeyError("没有这个版本")
    return p.read_text(encoding="utf-8")
