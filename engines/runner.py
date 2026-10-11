"""网页点「运行 / 优化 / 对比」时，在独立的子进程里执行（python -m engines.runner <任务目录>）。

为什么用子进程：
  1. 策略代码可以在网页上改，改错了（死循环、报错）也不会把网页服务拖垮；超时会被结束
  2. 每次都是全新的 Python 进程，改过的代码一定会被重新加载
  3. RQAlpha 内部有全局状态，同一个进程里并发跑多个回测会互相干扰

任务目录里：job.json（任务参数）→ progress.json（进度）→ result.json（结果或错误信息）
"""
from __future__ import annotations

import json
import sys
import time
import traceback
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _bt(*a, **k):
    from engines import bt_engine
    return bt_engine.run(*a, **k)


def _rq(*a, **k):
    from engines import rq_engine
    return rq_engine.run(*a, **k)


def _in(*a, **k):
    from engines import inhouse
    return inhouse.run(*a, **k)


def _cu(*a, **k):
    from engines import custom_engine
    return custom_engine.run(*a, **k)


ENGINES = {"backtrader": _bt, "rqalpha": _rq, "inhouse": _in, "custom": _cu}
LABELS = {"inhouse": "自研引擎", "backtrader": "backtrader（国外）", "rqalpha": "RQAlpha 米筐（国内）", "custom": "自定义策略"}


def _finite(o):
    """JSON 不支持 NaN / 无穷大（浏览器解析会报错），统一换成 null。"""
    import math
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):     # numpy 数字
        try:
            return _finite(o.item())
        except Exception:
            return o
    return o


def _write(path: Path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_finite(obj), ensure_ascii=False, default=float, allow_nan=False), encoding="utf-8")
    tmp.replace(path)


def compare(job: dict, progress) -> dict:
    """同一只股票、同一个策略、同一组参数，三个引擎各跑一次。"""
    import numpy as np
    import pandas as pd
    from engines import common
    code, name = job["code"], job["strategy"]
    raw, info = common.load_stock(code)
    df, k, note = common.to_forward_adjusted(code, raw)
    out, errors = {}, {}
    for i, eng in enumerate(["inhouse", "backtrader", "rqalpha"]):
        progress(i, 3, f"正在运行 {LABELS[eng]}")
        try:
            out[eng] = ENGINES[eng](code, name, job.get("params"), job.get("start"),
                                    float(job.get("cash") or common.DEFAULT_CASH), df=df, info=info)
        except Exception as e:
            errors[eng] = f"{type(e).__name__}: {e}\n" + traceback.format_exc(limit=6)
    progress(3, 3, "整理对比结果")
    # 每天持仓是否一致、日收益的相关性、净值差
    agree = {}
    keys = list(out)
    for a in range(len(keys)):
        for b in range(a + 1, len(keys)):
            A, B = out[keys[a]], out[keys[b]]
            ea, eb = np.array(A["exposure"]), np.array(B["exposure"])
            n = min(len(ea), len(eb))
            same = float(np.mean((ea[:n] > 0.001) == (eb[:n] > 0.001)) * 100)
            ra = pd.Series(A["equity"]).pct_change().fillna(0)
            rb = pd.Series(B["equity"]).pct_change().fillna(0)
            corr = float(ra.corr(rb)) if ra.std() > 0 and rb.std() > 0 else None
            diffs = [A["dates"][i] for i in range(n) if abs(ea[i] - eb[i]) > 0.05][:30]
            agree[f"{keys[a]}|{keys[b]}"] = {"same_state%": round(same, 2), "ret_corr": corr,
                                            "final_gap%": round((A["equity"][-1] / B["equity"][-1] - 1) * 100, 3),
                                            "diff_days": diffs, "n_diff": int(np.sum(np.abs(ea[:n] - eb[:n]) > 0.05))}
    # 逐笔：按买入日期对齐
    entries = {e: {t["entry"]: t for t in r["trades"]} for e, r in out.items()}
    all_dates = sorted(set().union(*[set(v) for v in entries.values()]), reverse=True) if entries else []
    trade_rows = []
    for d in all_dates[:200]:
        row = {"entry": d}
        for e in out:
            t = entries[e].get(d)
            row[e] = None if t is None else {"exit": t["exit"], "ret": t["ret"], "days": t["days"], "open": t["open"]}
        trade_rows.append(row)
    return {"kind": "compare", "code": code, "name": info.get("name", code), "strategy": name,
            "label": common.STRATEGIES[name]["label"], "params": common.parse_params(name, job.get("params")),
            "price_scale": k, "price_note": note, "results": out, "errors": errors,
            "agree": agree, "trade_rows": trade_rows}


def check(job: dict, progress) -> dict:
    """保存代码后的自检：用一只股票把某个引擎的 7 个策略都跑一遍，看有没有报错。"""
    from engines import common
    eng = job.get("engine", "backtrader")
    code = job.get("code") or "600531"
    raw, info = common.load_stock(code)
    df, _, _ = common.to_forward_adjusted(code, raw)
    rows = []
    for i, s in enumerate(common.STRATS):
        progress(i, len(common.STRATS), f"检查 {s}")
        t0 = time.time()
        try:
            r = ENGINES[eng](code, s, {}, None, common.DEFAULT_CASH, df=df, info=info)
            rows.append({"strategy": s, "label": r["label"], "ok": True, "cagr": r["stats"].get("CAGR%"),
                         "sharpe": r["stats"].get("Sharpe"), "trades": r["stats"].get("Trades"),
                         "sec": round(time.time() - t0, 2)})
        except Exception as e:
            rows.append({"strategy": s, "label": common.STRATEGIES[s]["label"], "ok": False,
                         "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=8)})
    return {"kind": "check", "engine": eng, "code": code, "rows": rows}


def main(job_dir: str):
    d = Path(job_dir)
    job = json.loads((d / "job.json").read_text(encoding="utf-8"))
    t0 = time.time()

    def progress(done, total, msg=""):
        _write(d / "progress.json", {"done": done, "total": total, "msg": msg, "elapsed": round(time.time() - t0, 1)})

    try:
        kind = job.get("kind", "run")
        progress(0, 1, "准备数据")
        if kind == "run":
            res = ENGINES[job["engine"]](job["code"], job["strategy"], job.get("params"), job.get("start"),
                                         float(job.get("cash") or 100000))
            res["kind"] = "run"
        elif kind == "optimize":
            from engines import optimize
            res = optimize.run(job, progress)
        elif kind == "compare":
            res = compare(job, progress)
        elif kind == "check":
            res = check(job, progress)
        else:
            raise ValueError(f"未知任务 {kind}")
        res["job_seconds"] = round(time.time() - t0, 1)
        _write(d / "result.json", res)
    except Exception as e:
        _write(d / "result.json", {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=10)})


if __name__ == "__main__":
    main(sys.argv[1])
