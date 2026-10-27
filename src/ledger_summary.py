"""前向账本展示页的只读后端（T040）：把 data/daily/ledger/latest.json 整理成页面字段。

边界：
- 只读一个固定文件，不写任何东西；文件缺失、损坏、字段缺失都返回「暂无」状态，不抛错；
- 只输出统计（登记了几个信号日、核对了几个、各持有期的样本与超额、局限说明），
  **不输出股票代码、不输出买卖清单**——页面是展示「先登记后核对」的方法，不是荐股；
- 默认关闭由调用方（app.py + 后台设置 ledger_page_on）负责。
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LATEST = ROOT / "data" / "daily" / "ledger" / "latest.json"
MAX_FILE_BYTES = 5_000_000
MAX_ROWS = 40
_ID = re.compile(r"[a-z][a-z0-9_]{0,23}")           # 策略内部代号：只允许这种形状，其余丢弃
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_ACTIONS = {"买入", "卖出", "加仓", "减仓"}
_VERDICTS = {"样本不足", "看不出差别", "有效", "无效", "反向"}          # signal_ledger.verdict 的全部取值；其他任何文字一律丢弃
CAVEATS = (                                                          # 固定说明文案：不转发账本文件里的自由文本（里面可能带代码或股数）
    "结果是统计，不是预测；样本小时波动很大。",
    "入场按次日开盘价；「相对全市场」未扣费用，扣费后超额一栏已按来回约 16bp 估算；显著性按信号日聚合后检验。",
    "连板或开盘涨停的股票已从召回统计里剔除。",
)


def _num(x):
    """有限数字才保留，其余一律 None（页面显示「—」）。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(x) else None


def summarize(data) -> dict:
    """纯函数：latest.json 的内容 → 页面字段。任何缺失都降级为空/None。"""
    if not isinstance(data, dict):
        return {"status": "na", "reason": "format"}
    signal_days = data.get("signal_days") if isinstance(data.get("signal_days"), list) else []
    evaluated = data.get("evaluated_days") if isinstance(data.get("evaluated_days"), list) else []
    main = data.get("main") if isinstance(data.get("main"), str) else None
    prec = data.get("precision") if isinstance(data.get("precision"), dict) else {}
    rows = []
    for r in (prec.get(main) or [])[:MAX_ROWS] if main else []:
        if not isinstance(r, dict) or r.get("action") not in _ACTIONS:
            continue
        rows.append({
            "action": r["action"], "h": _num(r.get("h")), "n": _num(r.get("n")), "days": _num(r.get("days")),
            "hit_excess": _num(r.get("hit_excess")), "hit_lo": _num(r.get("hit_excess_lo")), "hit_hi": _num(r.get("hit_excess_hi")),
            "mean_excess": _num(r.get("mean_excess")), "net_excess": _num(r.get("net_excess")),
            "enough": r.get("enough") is True,
            "verdict": r["verdict"] if r.get("verdict") in _VERDICTS else None,
        })
    promo = data.get("promotion") if isinstance(data.get("promotion"), dict) else {}
    return {
        "status": "ok",
        "today": data["today"] if isinstance(data.get("today"), str) and _DAY.fullmatch(data["today"]) else None,
        "signal_days": len([d for d in signal_days if isinstance(d, str) and _DAY.fullmatch(d)]),
        "evaluated_days": len([d for d in evaluated if isinstance(d, str) and _DAY.fullmatch(d)]),
        "min_sample": _num(data.get("min_sample")),
        "strategy": main if isinstance(main, str) and _ID.fullmatch(main) else None,
        "rows": rows,
        "any_enough": any(r["enough"] for r in rows),
        "promotion": {"any_passed": promo.get("any_passed") is True, "complete_weeks": _num(promo.get("complete_weeks")),
                      "min_weeks": _num((promo.get("gate") or {}).get("min_weeks")) if isinstance(promo.get("gate"), dict) else None},
        "caveats": list(CAVEATS),
    }


def load(path: Path | None = None) -> dict:
    """读固定路径；任何读失败都返回 status=na，不泄露路径和异常文字。"""
    p = path or LATEST
    try:
        if not p.is_file() or p.stat().st_size > MAX_FILE_BYTES:
            return {"status": "na", "reason": "missing"}
        return summarize(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return {"status": "na", "reason": "unreadable"}
