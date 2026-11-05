"""三引擎（自研 / backtrader / RQAlpha）策略规则一致性的回归护栏。运行：
    PYTHONPATH=.engines_lib .venv/bin/python tests/test_engine_rules.py

和 tests/test_engines.py 的区别：那个用一只真实股票（需要 data/ 里有行情），而且只对 MA+ADX 原版比了成交；
这个用合成行情（不读 data/、不联网），7 个策略、三个引擎、默认参数下逐日比较「收盘后定下的目标仓位」，再比较底层指标。
断言的是「上界」：C09 实测（20 个种子）里最差的情况是 0.161% 的天数有差异（均线与收盘价刚好并列时，
三个实现的浮点舍入不同，严格大于/小于被翻转），这里放宽到 0.5%；以后如果有人改了某个引擎的规则、让差异变大，这里会报错。
已知且未在这里断言的差异见 reports/o44/C09 报告（bt 的 ATR 比另外两个晚一根起算、预热期 bt 状态机晚起步等）。
"""
import importlib.util
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import backtrader  # noqa: F401
except ImportError:
    print("没有 backtrader（要加 PYTHONPATH=.engines_lib），跳过")
    sys.exit(0)

# scripts/ 不是包，按路径加载（模块里的 main() 有 __main__ 保护，不会自动运行）
_spec = importlib.util.spec_from_file_location("engine_rule_diff", ROOT / "scripts" / "engine_rule_diff.py")
E = importlib.util.module_from_spec(_spec)
sys.modules["engine_rule_diff"] = E      # backtrader 的元类会按 cls.__module__ 去 sys.modules 里找，必须先登记
_spec.loader.exec_module(E)

SEEDS, N = 4, 1500
MAX_DIFF_FRAC = 0.005            # 区间内目标仓位不同的天数占比上界（实测最差 0.00161）


def test_param_defaults_consistent():
    tab = E.defaults_table()
    n = 0
    for name, rows in tab.items():
        for key, r in rows.items():
            n += 1
            assert not r["mismatch"], f"{name}.{key} 各来源默认值不一致：{ {k: v for k, v in r.items() if k != 'mismatch'} }"
    # 注册表里网页能调的每个参数，三个引擎都必须接得住（bt 会悄悄丢掉不认识的参数，所以这里显式查）
    for name in E.common.STRATS:
        for p in E.registry.STRATEGIES[name]["params"]:
            r = tab[name][p["key"]]
            assert "bt" in r and "rq" in r, f"{name}.{p['key']}：注册表有这个参数，但 backtrader / RQAlpha 没接：{r}"
            if name != "blend2":
                assert "src" in r, f"{name}.{p['key']}：自研函数没有这个参数：{r}"
    print(f"✓ 参数默认值：{n} 个（策略, 参数）项，注册表 / 自研 / backtrader / RQAlpha 的默认值全部一致，网页可调参数三个引擎都接得住")


def test_default_signals_agree():
    res = E.signal_layer(SEEDS, N)
    worst = 0.0
    for name, pairs in res.items():
        for pair, a in pairs.items():
            frac = a["max_frac_diff_pct"] / 100
            worst = max(worst, frac)
            assert frac <= MAX_DIFF_FRAC, f"{name} {pair}：{frac:.4%} 的天数目标仓位不同，超过 {MAX_DIFF_FRAC:.1%}"
    print(f"✓ 信号一致性：7 个策略 × 3 对引擎 × {SEEDS} 个种子，目标仓位差异天数占比最大 {worst:.4%}（上界 {MAX_DIFF_FRAC:.1%}）")


def test_indicators_agree():
    res = E.indicator_layer(3, N)
    for key, v in res.items():
        pair, ind = key.split(":")
        assert v["nan_mismatch_after260"] == 0, f"{key}：第 260 根之后两边有值/无值不一致"
        if pair == "src-rq":
            tol = 1e-9
        else:
            tol = 1e-5 if ind.startswith("atr") else 1e-9      # bt 的 ATR 起算晚一根，第 260 根后残差约 1e-7
        assert v["max_rel_after260"] <= tol, f"{key}：第 260 根之后相对差 {v['max_rel_after260']:.3g} 超过 {tol:g}"
        if ind.startswith("atr"):
            assert v["max_rel_first60"] < 0.1, f"{key}：起算后前 60 根相对差 {v['max_rel_first60']:.3g} 超过 10%"
    print(f"✓ 指标一致性：{len(res)} 组指标（SMA/ATR/ADX/RSI/前N日极值/波动率）第 260 根之后相对差都在上界内")


if __name__ == "__main__":
    test_param_defaults_consistent()
    test_default_signals_agree()
    test_indicators_agree()
    print("全部通过")
