"""策略体检命令行（T022）：对一次详细回测做统计体检，只读本地行情缓存，不联网，不导入 app。
注意：本脚本会调用 registry.run(detail=True)，这一步会做新的计算（成本重跑、参数邻点回测等）；
health_check 模块本身只汇总判定，不做回测。

用法（在项目根目录）：
  .venv/bin/python scripts/strategy_health_check.py --market cn --strategy trend_ens --symbol 510300 --n-trials 20
  可选：--start 2018-01-01  --cost 5（单边成本，bp）  --json

--n-trials 是"为了得到这个策略，你一共试过多少个（策略×参数）组合"。请如实填写：
填少了，Deflated Sharpe 会高估；这是本报告最大的、代码无法替你把关的假设。

结果仅供研究，不构成投资建议；通过全部检查也不等于策略有效。
退出码：0 成功；2 参数或数据有问题（错误信息写到 stderr）。
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import MARKETS  # noqa: E402
from src import allmarket, datasource, health_check, registry  # noqa: E402

ICON = {"pass": "通过", "warn": "存疑", "fail": "未通过", "na": "无法计算"}


def _parser():
    ap = argparse.ArgumentParser(description="策略体检（仅供研究，不构成投资建议）")
    ap.add_argument("--market", required=True, choices=sorted(MARKETS))
    ap.add_argument("--strategy", required=True, choices=sorted(registry.STRATEGIES))
    ap.add_argument("--symbol", help="品种代码，默认该市场的主品种")
    ap.add_argument("--n-trials", type=int, required=True, help="一共试过多少个策略/参数组合（请如实填写，≥1）")
    ap.add_argument("--start", help="统计起始日 YYYY-MM-DD")
    ap.add_argument("--cost", type=float, help="单边成本（bp），默认用该市场配置")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    return ap


def _prices(market, cfg, sym):
    prices = datasource.load_market(market, cfg)
    if sym not in prices and market == "cn" and datasource.is_valid_stock(sym) and allmarket.path(sym).exists():
        prices[sym] = allmarket.load(sym)
    return prices


def _fail(msg):
    print(f"错误：{msg}", file=sys.stderr)
    return 2


def main(argv=None):
    a = _parser().parse_args(argv)
    # 先校验参数，再读数据、做回测（避免白白做昂贵计算）
    if a.n_trials < 1:
        return _fail("--n-trials 必须是 ≥ 1 的整数")
    if a.cost is not None and not (math.isfinite(a.cost) and a.cost >= 0):
        return _fail("--cost 必须是非负的有限数（单位 bp）")
    cfg = MARKETS[a.market]
    sym = a.symbol or cfg["main"]
    prices = _prices(a.market, cfg, sym)
    if sym not in prices:
        return _fail(f"{sym} 本地没有行情数据")
    try:
        res = registry.run(a.strategy, prices, sym, {}, cfg, cost_bp=a.cost, start=a.start, market=a.market, detail=True)
        rep = health_check.health_check(res, n_trials=a.n_trials)
    except (ValueError, KeyError) as e:
        return _fail(str(e))
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        return 0
    i = rep["inputs"]
    print(f"策略 {i['strategy']}  品种 {i['symbol']}  {i['start']} ~ {i['end']}  共 {i['days']} 个交易日  声明试过 {i['n_trials']} 个组合")
    for c in rep["checks"]:
        print(f"  [{ICON.get(c['status'], c['status'])}] {c['id']}: {c['zh']}")
    v = rep["verdict"]
    print(f"\n结论：{v['code']}  {v.get('zh', '')}")
    for c in rep["caveats"]:
        print(f"  · {c['zh']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
