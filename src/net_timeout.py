"""给没有设超时的 requests 调用一个默认超时（D55）。

为什么需要：akshare 里取 ETF 日线（fund_etf_hist_sina）和个股历史（stock_zh_a_daily）的函数调用 requests.get 时
没有传 timeout。对方接受连接却一直不回数据时，请求会永远卡住，整个每日任务只能等 3 小时看门狗强制退出
（2026-10-14 出现过一次）。socket.setdefaulttimeout 没用：requests 会显式传 timeout=None，覆盖掉全局默认值。

做法：在 requests 的 HTTPAdapter.send 这一层，只给 timeout 为 None 的请求补上默认值；
调用方自己写了 timeout 的（腾讯 15 秒等）保持不变。卡住变成普通的 ReadTimeout，走现有的重试和备用源流程。

只在每日任务进程里安装（daily.py 的 main；网页上的「立即更新」也是另起 daily.py 进程）。
网页服务 app.py 和盯盘不安装，盯盘的请求频率和行为完全不变。
"""
from __future__ import annotations

DEFAULT = (10, 30)       # (连接超时, 读取超时) 秒：全市场快照分页、ETF 全历史都在几秒内返回，30 秒足够宽松
_installed = False


def install(default=DEFAULT) -> bool:
    """安装一次；重复调用无副作用。返回这次是否真的安装了。"""
    global _installed
    if _installed:
        return False
    import requests.adapters as adapters
    original = adapters.HTTPAdapter.send

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = default
        return original(self, request, **kwargs)

    send._net_timeout_original = original       # 测试里用来还原
    adapters.HTTPAdapter.send = send
    _installed = True
    return True


def uninstall() -> None:
    """测试用：还原 requests 原来的 send。"""
    global _installed
    import requests.adapters as adapters
    original = getattr(adapters.HTTPAdapter.send, "_net_timeout_original", None)
    if original is not None:
        adapters.HTTPAdapter.send = original
    _installed = False
