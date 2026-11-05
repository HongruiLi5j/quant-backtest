"""D58：全市场快照不完整时写明原因、重试前等待（与报错时相同的 5 秒）。假数据源，不联网。
python tests/test_snapshot_retry_d58.py"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd  # noqa: E402

from src import allmarket as am  # noqa: E402


def snap(n):
    return pd.DataFrame({"代码": [f"{600000 + i}" for i in range(n)], "名称": "x", "今开": 1, "最高": 1, "最低": 1,
                         "最新价": 1, "昨收": 1, "成交量": 1})


def run(em, sina):
    import akshare as ak
    sleeps = []
    saved = (ak.stock_zh_a_spot_em, ak.stock_zh_a_spot, am.time, sys.modules.get("src.turnover"))
    ak.stock_zh_a_spot_em, ak.stock_zh_a_spot = em, sina
    am.time = types.SimpleNamespace(sleep=sleeps.append)
    import src
    fake = types.SimpleNamespace(capture_snapshot=lambda *a, **k: None)   # 成交额旁表会写 data/，测试里换成空函数
    sys.modules["src.turnover"], src.turnover = fake, fake
    try:
        try:
            return am.spot_snapshot(), sleeps
        except ConnectionError as e:
            return str(e), sleeps
    finally:
        ak.stock_zh_a_spot_em, ak.stock_zh_a_spot, am.time, real = saved
        import src
        if real is not None:
            sys.modules["src.turnover"], src.turnover = real, real
        else:
            sys.modules.pop("src.turnover", None)
            src.__dict__.pop("turnover", None)


def boom():
    raise ConnectionError("x")


def test_partial_snapshot_reason_and_wait():
    out, sleeps = run(lambda: snap(800), lambda: snap(800))
    assert isinstance(out, str) and out.count("只有 800 只，不完整") == 4, out
    assert sleeps == [5, 5, 5, 5], sleeps


def test_mixed_reasons_and_success_unchanged():
    out, sleeps = run(lambda: snap(800), boom)
    assert "东方财富: 只有 800 只" in out and "新浪: ConnectionError" in out, out
    calls = iter([snap(800), snap(1500)])
    out, sleeps = run(lambda: next(calls), boom)                        # 第二次完整：照常返回
    assert isinstance(out, pd.DataFrame) and len(out) == 1500 and sleeps == [5]


if __name__ == "__main__":
    test_partial_snapshot_reason_and_wait()
    test_mixed_reasons_and_success_unchanged()
    print("ok: 2 项通过")
