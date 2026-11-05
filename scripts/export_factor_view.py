"""因子检验页（N24）的数据导出：把 T008 训练段回放的冻结结果整理成一个静态 json，给新界面「研究 › 因子检验」用。

为什么是静态文件：T008 训练段判定已冻结（一次性、不可重来），结果不会再变，所以不加后端接口。
输入（只读）：
  reports/overnight_2026-10-24/replay_500/monotonic_report.json   单因子分 5 组、IC、单调相关、多空差
  reports/overnight_2026-10-24/replay_500/train_record.json       四条淘汰规则的判定（规则 1 p 值、规则 2 换手与毛/净年化超额）
  src/replay/strategies_catalog.json                                策略中文名和类别
输出：dashboard/ui/research-data/factor_view.json（先写临时文件再改名，不删除任何文件）。
不联网、不改输入文件、不改冻结记录。用法（项目根目录）：.venv/bin/python scripts/export_factor_view.py
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "reports" / "overnight_2026-10-24" / "replay_500"
CATALOG = ROOT / "src" / "replay" / "strategies_catalog.json"
OUT = ROOT / "dashboard" / "ui" / "research-data" / "factor_view.json"
SCHEMA = "factor_view/1"
_RULE = re.compile(r"^规则([1-4])")


class ExportError(Exception):
    pass


def _num(v, digits):
    """有限数字才保留，四舍五入；其他（None、NaN、Infinity、bool、字符串）一律 None。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return round(float(v), digits)


def param_key(sid: str, params: dict) -> str:
    """和 train_record 的 key 同一写法：brk_01|n=10、cmb_02|n=3,w=0.5（按参数原顺序）。"""
    return sid + "|" + ",".join(f"{k}={v}" for k, v in params.items())


def build_view(mono: dict, train: dict, catalog: dict) -> dict:
    if train.get("segment") != "train":
        raise ExportError("train_record 不是训练段结果")
    decisions = {d["key"]: d for d in train.get("decisions", [])}
    names = {s["id"]: s for s in catalog.get("strategies", [])}
    rows, seen = [], set()
    for res in mono.get("results", []):
        sid = res["id"]
        for p in res.get("params", []):
            key = param_key(sid, p.get("params", {}))
            if key in seen:
                raise ExportError(f"重复的组合 {key}")
            seen.add(key)
            d = decisions.get(key)
            if d is None:
                raise ExportError(f"train_record 里没有 {key}")
            r1, r2 = d.get("rule1") or {}, d.get("rule2") or {}
            hits = sorted({int(m.group(1)) for s in d.get("reasons", []) if (m := _RULE.match(str(s)))})
            groups = [_num(x * 100, 3) for x in (p.get("group_mean") or [])]
            rows.append({
                "key": key, "id": sid,
                "name": names.get(sid, {}).get("name"), "category": names.get(sid, {}).get("category"),
                "decision": d.get("decision"),
                "rules_hit": hits,
                "mono_passed": bool(p.get("passed")),
                "ic": _num(p.get("ic_mean"), 4), "rho": _num(p.get("rho"), 3),
                "spread_pct": _num(None if p.get("spread_mean") is None else p["spread_mean"] * 100, 3),
                "groups_pct": groups if all(g is not None for g in groups) else [],
                "p_value": _num(r1.get("p_value"), 4),
                "turnover": _num(r2.get("monthly_turnover"), 3),
                "gross_pct": _num(None if r2.get("gross_annual_excess") is None else r2["gross_annual_excess"] * 100, 2),
                "net_pct": _num(None if r2.get("net_annual_excess") is None else r2["net_annual_excess"] * 100, 2),
            })
    missing = set(decisions) - seen
    if missing:
        raise ExportError(f"monotonic_report 少了 {len(missing)} 个组合")
    rows.sort(key=lambda r: r["key"])
    return {
        "schema": SCHEMA,
        "segment_range": train.get("segment_range"),
        "horizon_days": mono.get("horizon"), "n_groups": mono.get("n_groups"),
        "total": len(rows),
        "survivors": len(train.get("survivors") or []),
        "rules": train.get("rules"),
        "source": {"run": "reports/overnight_2026-10-24/replay_500", "record_sha256": train.get("record_sha256"),
                   "catalog_hash": train.get("catalog_hash"), "timestamp": train.get("timestamp")},
        "rows": rows,
    }


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".factor_view.", suffix=".tmp", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", type=Path, default=RUN)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    load = lambda p: json.loads(p.read_text(encoding="utf-8"))
    try:
        view = build_view(load(a.run / "monotonic_report.json"), load(a.run / "train_record.json"), load(CATALOG))
    except (OSError, ValueError, KeyError, ExportError) as e:
        print(f"导出失败：{e}", file=sys.stderr)
        return 1
    write_atomic(a.out, view)
    print(f"已写入 {a.out}（{view['total']} 个组合，幸存 {view['survivors']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
