"""D55：每日任务里没写超时的请求不会永远卡住。用本机一个「接受连接但不回数据」的假服务器，不联网。
python tests/test_net_timeout_d55.py"""
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests  # noqa: E402

from src import net_timeout  # noqa: E402


def _silent_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held = []

    def loop():
        while True:
            try:
                held.append(srv.accept())     # 接受连接，永远不回
            except OSError:
                return
    threading.Thread(target=loop, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.getsockname()[1]}/"


def _run(fn, cap):
    """在线程里跑 fn，最多等 cap 秒；返回 (结果, 用时)，超时返回 ('HANG', cap)。"""
    out = {}

    def go():
        t = time.monotonic()
        try:
            fn()
            out["r"] = "returned"
        except Exception as e:
            out["r"] = type(e).__name__
        out["s"] = time.monotonic() - t
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(cap)
    return (out["r"], out["s"]) if "r" in out else ("HANG", cap)


def test_missing_timeout_gets_default_and_explicit_kept():
    srv, url = _silent_server()
    try:
        net_timeout.uninstall()
        assert _run(lambda: requests.get(url), 2.0)[0] == "HANG"            # 改动前：一直卡着
        assert net_timeout.install(default=(1, 1)) is True
        assert net_timeout.install(default=(1, 1)) is False                 # 重复安装无副作用
        r, s = _run(lambda: requests.get(url), 5.0)
        assert r == "ReadTimeout" and 0.8 <= s < 3, (r, s)                   # 补上默认超时
        r, s = _run(lambda: requests.get(url, timeout=2), 6.0)
        assert r == "ReadTimeout" and 1.8 <= s < 4, (r, s)                   # 调用方自己写的超时不变
        r, s = _run(lambda: requests.Session().get(url), 5.0)
        assert r == "ReadTimeout", r                                         # Session 也走 HTTPAdapter
    finally:
        net_timeout.uninstall()
        srv.close()


def test_daily_installs_it_and_app_does_not():
    root = Path(__file__).resolve().parent.parent
    daily = (root / "daily.py").read_text(encoding="utf-8")
    assert "net_timeout.install()" in daily
    for f in ("app.py",):                                                     # 网页服务（含盯盘）不安装
        assert "net_timeout" not in (root / f).read_text(encoding="utf-8"), f


if __name__ == "__main__":
    test_missing_timeout_gets_default_and_explicit_kept()
    test_daily_installs_it_and_app_does_not()
    print("ok: 2 项通过")
