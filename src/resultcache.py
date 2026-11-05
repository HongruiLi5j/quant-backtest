"""接口结果的小缓存：输入没变就不重算（B07；B08 补个股详情）。

只缓存「纯函数式」的结果：同样的输入一定得到同样的输出（回测、跟踪汇总、逐只回测表、个股详情）。
带个人数据的接口（持仓、我的相关、会员、后台、AI 等）一律不用这里的缓存，见 tests/test_resultcache_b08.py 的守卫测试。
调用方负责把「所有会影响结果的输入」都放进 key（数据文件的修改时间、参数、自选股……），
key 变了就是缓存未命中，自然重算；这里不做任何猜测。

- maxsize：最多存几条，超了丢最久没用的（LRU），防止用户乱传参数把内存撑大。
- ttl：可选的最长保留秒数，当作兜底（比如结果依赖「现在几点」时，key 漏了也最多陈旧 ttl 秒）。
- keepalive：把 key 里用 id(对象) 代表的那个对象一起存着，保证它不会被回收后 id 被别的对象复用。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from pathlib import Path

from . import market_clock

_MISS = object()


def file_stamp(p):
    """文件指纹：修改时间(ns)、大小、inode；文件不存在返回 None。
    原子替换（写临时文件再 replace）会换 inode 和修改时间，所以内容变了指纹一定变。"""
    try:
        s = Path(p).stat()
        return (s.st_mtime_ns, s.st_size, s.st_ino)
    except OSError:
        return None


def bj_clock_state(now):
    """北京时间的「日期 + 是否已过 15:30」（B08）。行情读取会在 15:30 前丢掉当天的半天 K 线，
    所以这两个量也是结果的输入；缓存键里带上它，跨过 15:30 或跨天一定重算。now 由调用方传入（带时区的 datetime）。"""
    return market_clock.clock_state(now)


class Memo:
    def __init__(self, maxsize: int = 16, ttl: float | None = None, clock=time.monotonic):
        self.maxsize, self.ttl, self._clock = maxsize, ttl, clock
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            item = self._data.get(key, _MISS)
            if item is _MISS:
                return default
            stored_at, value, _keep = item
            if self.ttl is not None and self._clock() - stored_at > self.ttl:
                del self._data[key]
                return default
            self._data.move_to_end(key)
            return value

    def put(self, key, value, keepalive=None):
        with self._lock:
            self._data[key] = (self._clock(), value, keepalive)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
        return value

    def get_or_compute(self, key, compute, keepalive=None):
        """命中就直接返回；否则算一次存起来。算的时候不持锁（回测要几百毫秒，别堵住别的请求）。
        两个请求同时未命中会各算一次，结果相同，后写的覆盖先写的，无副作用。"""
        hit = self.get(key, _MISS)
        if hit is not _MISS:
            return hit
        return self.put(key, compute(), keepalive)

    def clear(self):
        with self._lock:
            self._data.clear()

    def __len__(self):
        return len(self._data)
