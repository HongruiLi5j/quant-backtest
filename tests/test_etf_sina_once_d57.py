"""D57：ETF 增量更新只请求新浪一次，结果与改动前完全相同。假数据源 + 临时目录，不联网。
改动前的实现从 git 取（commit 0bc4474 的 src/datasource.py）做对照；没有 git 时只跑调用次数检查。
python tests/test_etf_sina_once_d57.py"""
import importlib.util
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src import datasource as NEW  # noqa: E402

CFG = {"source": "sina", "start": "2020-01-01", "tz": "Asia/Shanghai", "close_ready": "15:30"}


def bars(start, n, base=1.0, scale=1.0):
    idx = pd.bdate_range(start, periods=n)
    c = pd.Series([(base + i * 0.01) * scale for i in range(n)], index=idx)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1000.0}, index=idx)


def load_old():
    try:
        src = subprocess.run(["git", "show", "0bc4474:src/datasource.py"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except Exception:
        return None
    spec = importlib.util.spec_from_loader("src.datasource_before_d57", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "src"
    mod.__file__ = str(ROOT / "src" / "datasource.py")
    exec(compile(src, "datasource_before_d57.py", "exec"), mod.__dict__)
    return mod


OLD_CACHE = bars("2026-09-01", 40)
SCENARIOS = {
    "normal":         lambda: bars("2026-09-01", 46, scale=0.5),          # 原始价是后复权的一半，多 6 天
    "no_new_rows":    lambda: bars("2026-09-01", 40, scale=0.5),
    "gap_over_10d":   lambda: bars("2026-09-01", 25, scale=0.5).iloc[:20].pipe(lambda d: pd.concat([d, bars("2026-11-20", 3, 9.0, 0.5)])),
    "raw_stale":      lambda: bars("2026-08-01", 10, scale=0.5),           # 原始数据早于 start，截取后为空
    "raw_empty":      lambda: pd.DataFrame(columns=NEW.COLS),
    "raises":         None,
}


def run(mod, name):
    calls = []

    def fake_raw(sym):
        calls.append(sym)
        if SCENARIOS[name] is None:
            raise ConnectionError("injected")
        return SCENARIOS[name]().copy()
    with tempfile.TemporaryDirectory() as t:
        saved = (mod.CACHE, mod._fetch_sina_raw, mod.time, mod._fetch_yahoo)
        mod.CACHE = Path(t)
        mod._fetch_sina_raw = fake_raw
        mod.FETCHERS["sina"] = mod._fetch_sina          # _fetch_sina 在模块里按名字取 _fetch_sina_raw，会用到假的
        mod.time = types.SimpleNamespace(sleep=lambda s: None)
        mod._fetch_yahoo = lambda *a: (_ for _ in ()).throw(ConnectionError("yahoo off"))
        try:
            mod._save_cache_csv(OLD_CACHE, mod.cache_path("cn", "510300"))
            df, err, fb = mod.update("cn", "510300", CFG)
        finally:
            mod.CACHE, mod._fetch_sina_raw, mod.time, mod._fetch_yahoo = saved
    return df, err, fb, len(calls)


def test_one_request_on_normal_update():
    assert run(NEW, "normal")[3] == 1
    assert run(NEW, "no_new_rows")[3] == 1


def test_identical_to_before():
    old = load_old()
    if old is None:
        print("跳过：取不到 git 里的旧实现")
        return
    for name in SCENARIOS:
        a, b = run(old, name), run(NEW, name)
        assert a[1] == b[1] and a[2] == b[2], (name, a[1:3], b[1:3])          # 错误信息、是否备用源
        assert a[0].index.equals(b[0].index), name
        assert np.array_equal(a[0].to_numpy(), b[0].to_numpy(), equal_nan=True), name
        assert b[3] <= a[3], (name, a[3], b[3])                               # 请求次数不多于改动前
        print(f"{name}: same result; sina requests {a[3]} -> {b[3]}")


if __name__ == "__main__":
    test_one_request_on_normal_update()
    test_identical_to_before()
    print("ok: 2 项通过")
