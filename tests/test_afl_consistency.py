"""Python 版 MA+ADX+ATR 与 amibroker/ma_cross_adx_atr.afl 规则一致性的回归护栏（C10）。运行：
    .venv/bin/python tests/test_afl_consistency.py

只读 amibroker/ma_cross_adx_atr.afl 的文本，行情全部是合成的（不读写 data/、不联网）。
没有 AmiBroker 可跑，所以这里断言的是：
  1. AFL 里写死的默认参数 / 手续费 / 关键语句 和 Params / Config 的默认值一致（谁改了一边，这里就红）；
  2. 把 AFL 的 for 循环逐行翻译成 Python（scripts/afl_python_consistency.py::afl_loop）后，和 backtest.run 的交易逐笔一致；
  3. 有并列价的行情上，两边的差异只会出现在「SMA5 与 SMA20 精确相等」的 K 线附近（浮点并列敏感性，不是规则差异）。
"""
import importlib.util
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import backtest  # noqa: E402
from src.strategy import Params  # noqa: E402

# scripts/ 不是包，按路径加载（模块里的 main() 有 __main__ 保护，不会自动运行）
_spec = importlib.util.spec_from_file_location("afl_python_consistency", ROOT / "scripts" / "afl_python_consistency.py")
A = importlib.util.module_from_spec(_spec)
sys.modules["afl_python_consistency"] = A
_spec.loader.exec_module(A)

AFL_TEXT = A.AFL_PATH.read_text(encoding="utf-8")


def test_afl_defaults_match_python():
    parsed = A.parse_afl(AFL_TEXT)
    p, cfg = Params(), backtest.Config()
    assert parsed["params"] == {"Fast": p.fast, "Slow": p.slow, "ADXLen": p.adx_len, "ADXMin": p.adx_min,
                                "ATRLen": p.atr_len, "ATRMult": p.atr_mult}
    assert float(parsed["options"]["InitialEquity"]) == cfg.initial_equity
    # AFL 的 CommissionAmount 单位是「百分比」(CommissionMode=1)，Python 的 commission_pct 是小数
    assert parsed["options"]["CommissionMode"] == "1"
    assert abs(float(parsed["options"]["CommissionAmount"]) / 100 - cfg.commission_pct) < 1e-12
    assert parsed["trade_delays"] == "0,0,0,0"            # 延迟在循环里手动处理
    assert parsed["position_size"].startswith("100,")     # 满仓，和 Python 的「全部现金买整数股」对应
    assert cfg.stop_on_entry_bar is False                 # AFL 入场当根不检查止损


def test_afl_key_statements_present():
    stmts = A.afl_statements(AFL_TEXT)
    missing = [k for k, v in stmts.items() if not v]
    assert not missing, f"AFL 里找不到这些规则语句（可能被改了，需要同步 Python 版）：{missing}"


def test_afl_loop_matches_backtest_on_untied_prices():
    # 不取整的合成行情几乎不会出现均线并列，这时两边应当逐笔完全一致
    for p in (Params(), Params(fast=3, slow=10, adx_min=15, atr_mult=1.5), Params(adx_len=10, atr_len=20, atr_mult=0.5)):
        for seed in range(6):
            r = A.compare_one(A.gap_data(seed, 1500, round_px=False), p)
            assert r["tie_bars"] == 0
            assert r["signals_identical"], (seed, p)
            assert r["count_equal"] and r["mismatched"] == 0, (seed, p, r)
    # 至少走到过三种出场分支，否则这个测试没有覆盖止损逻辑
    r = A.compare_one(A.gap_data(0, 1500, round_px=False), Params(adx_len=10, atr_len=20, atr_mult=0.5))
    assert {"signal", "stop", "stop_gap"} <= set(r["reasons"])


def test_differences_only_near_ties_on_rounded_prices():
    for seed in range(12):
        r = A.compare_one(A.gap_data(seed, 1500, round_px=True), Params())
        assert r["diff_all_near_tie"], (seed, r)
        if r["tie_bars"] == 0:
            assert r["signals_identical"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
