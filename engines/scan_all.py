"""全市场三引擎扫描：沪深全部 A 股（排除 ST、上市不满一年）× 7 个策略 × 3 个引擎（自研 / backtrader / RQAlpha）。

目的不是找"回测最好的股票"（那基本是运气），而是大规模验证回测引擎本身：
  同一只股票、同一个策略，三个引擎的年化收益、持仓状态是不是一致？
  不一致的比例有多大、集中在哪些策略、哪些板块？
如果几千只股票上三者都高度一致，自研引擎的结果就可以放心用。

运行：python -m engines.scan_all          （网页「引擎对比」页也有按钮）
可以随时中断，下次运行会跳过已经算完的股票（结果逐只追加到 rows.jsonl）。
全部跑完大约需要 2~3 小时（取决于电脑性能），期间用 caffeinate 防止 Mac 睡眠。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DIR = ROOT / "data" / "daily" / "engine_scan"
ROWS = DIR / "rows.jsonl"
PROGRESS = DIR / "progress.json"
SUMMARY = DIR / "summary.json"
OUT = DIR / "engine_scan.parquet"
LOCK = DIR / "running.pid"
REQUEST = DIR / "REQUEST"          # 有这个文件 = 网页服务启动时自动开始（或继续）扫描
ENGINE_ORDER = ["inhouse", "backtrader", "rqalpha"]
MIN_ROWS = 250


def _write(p: Path, obj):
    from engines.runner import _finite
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(_finite(obj), ensure_ascii=False, default=float, allow_nan=False), encoding="utf-8")
    tmp.replace(p)


def is_running() -> bool:
    try:
        pid = int(LOCK.read_text())
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def status() -> dict:
    out = {"running": is_running(), "requested": REQUEST.exists()}
    for k, p in (("progress", PROGRESS), ("summary", SUMMARY)):
        try:
            out[k] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            out[k] = None
    return out


# ---------------------------------------------------------------- 单只股票（在子进程里跑）
def _scale_df(df, raw_close):
    import pandas as pd  # noqa
    last = float(df["Close"].iloc[-1])
    if raw_close and raw_close > 0:
        k = raw_close / last
    else:
        k = 1.0 if 5 <= last <= 300 else 50.0 / last
    out = df.copy()
    out[["Open", "High", "Low", "Close"]] *= k
    return out, k


def scan_stock(item) -> list[dict]:
    import numpy as np
    import pandas as pd
    from engines import common
    from engines.runner import ENGINES
    code, name, raw_close = item
    rows = []
    try:
        raw, info = common.load_stock(code)
    except Exception as e:
        return [{"code": code, "name": name, "error": f"{type(e).__name__}: {e}"}]
    if len(raw) < MIN_ROWS + 150:
        return [{"code": code, "name": name, "skip": "数据太短"}]
    info["name"] = name or info.get("name", code)
    df, k = _scale_df(raw, raw_close)
    for s in common.STRATS:
        res = {}
        for eng in ENGINE_ORDER:
            try:
                res[eng] = ENGINES[eng](code, s, {}, None, common.DEFAULT_CASH, df=df, info=info)
            except Exception as e:
                rows.append({"code": code, "name": name, "strategy": s, "engine": eng,
                             "error": f"{type(e).__name__}: {str(e)[:200]}"})
        base = res.get("inhouse")
        for eng, r in res.items():
            st, bs = r["stats"], r["bench_stats"]
            row = {"code": code, "name": name, "board": info.get("board", ""), "strategy": s, "engine": eng,
                   "cagr": st.get("CAGR%"), "sharpe": st.get("Sharpe"), "maxdd": st.get("MaxDD%"),
                   "trades": st.get("Trades"), "win": st.get("Win%"), "exposure": st.get("Exposure%"),
                   "fees": st.get("Fees"), "b_cagr": bs.get("CAGR%"), "b_sharpe": bs.get("Sharpe"),
                   "b_maxdd": bs.get("MaxDD%"), "events": len(r.get("events", [])), "scale": k,
                   "start": r["start"], "end": r["end"]}
            if base is not None and eng != "inhouse":
                ea, eb = np.array(base["exposure"]), np.array(r["exposure"])
                n = min(len(ea), len(eb))
                row["same_state"] = float(np.mean((ea[:n] > 0.001) == (eb[:n] > 0.001)) * 100)
                ra = pd.Series(base["equity"]).pct_change().fillna(0)
                rb = pd.Series(r["equity"]).pct_change().fillna(0)
                row["ret_corr"] = float(ra.corr(rb)) if ra.std() > 0 and rb.std() > 0 else None
                row["cagr_gap"] = (st.get("CAGR%") or 0) - (base["stats"].get("CAGR%") or 0)
            rows.append(row)
    return rows


# ---------------------------------------------------------------- 汇总
def summarize() -> dict:
    import numpy as np
    import pandas as pd
    from engines import common
    if not ROWS.exists():
        return {}
    recs = [json.loads(x) for x in ROWS.read_text(encoding="utf-8").splitlines() if x.strip()]
    df = pd.DataFrame([r for r in recs if "engine" in r and "error" not in r])
    if df.empty:
        return {}
    df.to_parquet(OUT)
    out = {"stocks": int(df["code"].nunique()), "errors": sum(1 for r in recs if "error" in r),
           "updated": time.strftime("%Y-%m-%d %H:%M"), "strategies": {}}
    for s in common.STRATS:
        g = df[df.strategy == s]
        per = {}
        for eng in ENGINE_ORDER:
            e = g[g.engine == eng]
            if e.empty:
                continue
            per[eng] = {
                "n": int(len(e)),
                "median_cagr": float(e.cagr.median()),
                "median_sharpe": float(e.sharpe.median()),
                "median_maxdd": float(e.maxdd.median()),
                "beat_bh%": float((e.sharpe > e.b_sharpe).mean() * 100),
                "median_trades": float(e.trades.median()),
                "median_fees": float(e.fees.median()),
            }
            if eng != "inhouse" and "same_state" in e:
                per[eng].update({
                    "same_state%": float(e.same_state.mean()),
                    "ret_corr": float(e.ret_corr.median()),
                    "abs_cagr_gap": float(e.cagr_gap.abs().median()),
                    "gap_over_1pct%": float((e.cagr_gap.abs() > 1).mean() * 100),
                })
        # "策略是否跑赢持有"这个结论，三个引擎是否一致
        wide = g.drop_duplicates(["code", "engine"]).pivot_table(index="code", columns="engine", values="sharpe")
        bh = (g[g.engine == "inhouse"].drop_duplicates("code").set_index("code")["b_sharpe"]
              .reindex(wide.index))
        agree = {}
        for eng in ("backtrader", "rqalpha"):
            if eng in wide and "inhouse" in wide:
                # 比较运算符不像算术运算符那样自动按索引对齐，索引顺序不一致就会报错，
                # 所以上面先把 bh reindex 到 wide.index，这里三者索引就完全一致了
                both = pd.concat([wide["inhouse"] > bh, wide[eng] > bh], axis=1).dropna()
                agree[eng] = float((both.iloc[:, 0] == both.iloc[:, 1]).mean() * 100) if len(both) else None
        out["strategies"][s] = {"label": common.STRATEGIES[s]["label"], "engines": per, "verdict_agree%": agree}
    _write(SUMMARY, out)
    return out


def main(workers: int | None = None):
    from engines.runner import _finite
    from src import allmarket
    DIR.mkdir(parents=True, exist_ok=True)
    if is_running():
        print("已经在运行了")
        return
    LOCK.write_text(str(os.getpid()))
    try:
        lst = allmarket.stock_list()
        names = dict(zip(lst["code"], lst["name"]))
        boards = dict(zip(lst["code"], lst["board"])) if "board" in lst else {}
        codes = [c for c in allmarket.cached_codes() if c in names
                 and "ST" not in names[c].upper() and "退" not in names[c]]
        done = set()
        if ROWS.exists():
            for line in ROWS.read_text(encoding="utf-8").splitlines():
                try:
                    done.add(json.loads(line)["code"])
                except Exception:
                    pass
        todo = [c for c in codes if c not in done]
        # 真实股价（用来把后复权价格换算成真实价位，决定一手多少钱）：一次请求拿到全市场
        raw = {}
        try:
            snap = allmarket.spot_snapshot()
            raw = snap["close"].dropna().to_dict()
        except Exception:
            pass
        items = [(c, names[c], raw.get(c)) for c in todo]
        total = len(codes)
        t0 = time.time()
        workers = workers or max(1, (os.cpu_count() or 4) - 1)
        prog = {"total": total, "done": len(done), "started": time.strftime("%Y-%m-%d %H:%M"),
                "workers": workers, "real_price": bool(raw)}
        _write(PROGRESS, prog)
        with open(ROWS, "a", encoding="utf-8") as f, ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(scan_stock, it): it[0] for it in items}
            n = 0
            for fut in as_completed(futs):
                try:
                    rows = fut.result()
                except Exception as e:
                    rows = [{"code": futs[fut], "error": f"{type(e).__name__}: {e}"}]
                for r in rows:
                    f.write(json.dumps(_finite(r), ensure_ascii=False, default=float) + "\n")
                f.flush()
                n += 1
                el = time.time() - t0
                prog.update(done=len(done) + n, elapsed_min=round(el / 60, 1),
                            eta_min=round(el / n * (len(items) - n) / 60, 1), last=futs[fut])
                if n % 10 == 0 or n == len(items):
                    _write(PROGRESS, prog)
                if n % 200 == 0:
                    try:
                        summarize()
                    except Exception:
                        pass
        summarize()
        prog.update(finished=time.strftime("%Y-%m-%d %H:%M"))
        _write(PROGRESS, prog)
        REQUEST.unlink(missing_ok=True)
    except Exception:
        (DIR / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
