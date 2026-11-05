"""离线版输入指纹（D22）：python tests/test_offline_fingerprint.py"""
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import offline_fingerprint as of  # noqa: E402

DAY = date(2026, 11, 4)


def _project(tmp: Path) -> Path:
    """搭一个最小的假项目目录：行情 CSV、summary JSON、全市场个股、代码文件。"""
    d = tmp / "data" / "daily"
    (d / "cn").mkdir(parents=True)
    (d / "cn_all").mkdir()
    (tmp / "src").mkdir()
    (tmp / "src" / "x.py").write_text("A = 1\n")
    (tmp / "export_offline.py").write_text("# export\n")
    (d / "cn" / "510300.csv").write_text("date,close\n2026-11-03,4.1\n")
    (d / "cn_all" / "600000.csv").write_text("date,close\n2026-11-03,9.9\n")
    (d / "summary_cn.json").write_text(json.dumps({"data_date": "2026-11-03", "generated": "17:00", "generated_at": "x"}))
    (d / "notifications.json").write_text("[]")
    (d / "signals_history.parquet").write_bytes(b"v1")
    return tmp


def _check(mutate, expect_same):
    with tempfile.TemporaryDirectory() as t:
        root = _project(Path(t))
        a = of.fingerprint(root, DAY)
        mutate(root)
        b = of.fingerprint(root, DAY)
        assert (a == b) is expect_same, mutate.__name__


def test_rewrite_same_content_is_unchanged():
    def rewrite_csv_same(root):    # 自动任务重写了 CSV，但内容一样（修改时间变了）
        p = root / "data/daily/cn/510300.csv"
        p.write_text(p.read_text())
    _check(rewrite_csv_same, True)


def test_only_timestamp_changed_is_unchanged():
    def new_generated(root):
        p = root / "data/daily/summary_cn.json"
        obj = json.loads(p.read_text())
        obj.update(generated="17:15", generated_at="y")
        p.write_text(json.dumps(obj))
    _check(new_generated, True)


def test_ignored_files_do_not_count():
    def touch_ignored(root):
        (root / "data/daily/notifications.json").write_text('[{"time": "now"}]')
        (root / "data/daily/signals_history.parquet").write_bytes(b"v2")
        (root / "data/daily/x.lock").write_text("")
    _check(touch_ignored, True)


def test_price_change_is_detected():
    def new_bar(root):
        with open(root / "data/daily/cn/510300.csv", "a") as f:
            f.write("2026-11-04,4.2\n")
    _check(new_bar, False)


def test_summary_data_change_is_detected():
    def new_date(root):
        p = root / "data/daily/summary_cn.json"
        obj = json.loads(p.read_text())
        obj["data_date"] = "2026-11-04"
        p.write_text(json.dumps(obj))
    _check(new_date, False)


def test_full_market_and_code_change_detected():
    def new_stock(root):
        (root / "data/daily/cn_all/600001.csv").write_text("x")
    _check(new_stock, False)

    def code_edit(root):
        (root / "src/x.py").write_text("A = 22\n")
    _check(code_edit, False)


def test_new_day_forces_rebuild():
    with tempfile.TemporaryDirectory() as t:
        root = _project(Path(t))
        assert of.fingerprint(root, DAY) != of.fingerprint(root, date(2026, 11, 5))


def test_missing_record_forces_rebuild_and_remember_roundtrip():
    with tempfile.TemporaryDirectory() as t:
        root = _project(Path(t))
        store = Path(t) / "fp.txt"
        same, fp = of.unchanged(root, store, DAY)
        assert same is False                      # 没有记录：一律重新生成
        of.remember(store, fp)
        assert of.unchanged(root, store, DAY) == (True, fp)
        store.write_text("garbage\n")
        assert of.unchanged(root, store, DAY)[0] is False


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"ok: {n} 项通过")
