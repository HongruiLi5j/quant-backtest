"""D23：信号表按文件指纹缓存，命中结果缓存前不再每次读整张 parquet。临时目录 + 假行情，不联网、不碰 data/。
python tests/test_trade_signals_memo_d23.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd  # noqa: E402

from src import trade_signals as T  # noqa: E402


def table(codes, strategy="blend2"):
    return pd.DataFrame({"market": "cn", "strategy": strategy, "source": "trade_list",
                         "code": codes, "signal_date": "2026-11-03"})


def write(path, df):
    tmp = path.with_suffix(".tmp")
    df.to_parquet(tmp)
    tmp.replace(path)          # 与真实写法一样：临时文件 + 原子替换


def test_hist_read_once_and_invalidated_on_change():
    reads = []
    real = pd.read_parquet
    T.pd.read_parquet = lambda p, *a, **k: (reads.append(p), real(p, *a, **k))[1]
    T._HIST_MEMO.clear()
    try:
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "trade_signals_history.parquet"
            write(path, pd.concat([table(["600000", "600001"]), table(["000001"], "rsi2")]))
            h1, c1 = T._read_hist(path, "blend2")
            h2, c2 = T._read_hist(path, "blend2")
            assert h1 is h2 and c1 == frozenset({"600000", "600001"}) and len(reads) == 1   # 第二次不读文件
            assert T._read_hist(path, "rsi2")[1] == frozenset({"000001"}) and len(reads) == 2   # 不同策略分开缓存
            write(path, table(["600000", "600001", "600002"]))                                 # 文件变了
            h3, c3 = T._read_hist(path, "blend2")
            assert len(reads) == 3 and len(h3) == 3 and "600002" in c3                        # 指纹变了就重读
            assert T._read_hist(path, "nope")[0].empty                                        # 没有这个策略：空表
    finally:
        T.pd.read_parquet = real
        T._HIST_MEMO.clear()


def test_memo_bounded():
    T._HIST_MEMO.clear()
    with tempfile.TemporaryDirectory() as t:
        path = Path(t) / "trade_signals_history.parquet"
        write(path, table(["600000"]))
        for i in range(20):
            T._read_hist(path, f"s{i}")
        assert len(T._HIST_MEMO) <= 8
    T._HIST_MEMO.clear()


def test_evaluate_with_stub_loader_unchanged_semantics():
    with tempfile.TemporaryDirectory() as t:
        assert T.evaluate("blend2", cache=t)["empty"] is True                                  # 没有文件
        write(Path(t) / "trade_signals_history.parquet", table(["600000"], "rsi2"))
        assert T.evaluate("blend2", cache=t)["empty"] is True                                  # 文件里没有本策略
    T._HIST_MEMO.clear()


if __name__ == "__main__":
    test_hist_read_once_and_invalidated_on_change()
    test_memo_bounded()
    test_evaluate_with_stub_loader_unchanged_semantics()
    print("ok: 3 项通过")
