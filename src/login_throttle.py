"""主页面令牌登录的失败限流（D01 第 18 条）。

旧做法：一个进程级列表，10 分钟内错 10 次就对所有人暂停登录，而且只增不减。
新做法：按「来源」分开计数——一个来源乱试，只锁它自己，不影响别人（包括站主本机）。

- 同一来源的规则和旧版完全一样：10 分钟滑动窗口内失败满 10 次就拒绝，窗口里最早的一次过期后解除；
  被拒绝的请求不再计数（和旧版一致）。
- 窗口外的记录会被清掉；整张表有大小上限，防止被大量不同来源撑爆内存。
- 只用标准库，不 import app，方便单独测试。
"""
from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque

LIMIT = 10                 # 窗口内失败多少次就锁（和旧版相同）
WINDOW = 600.0             # 滑动窗口，秒（和旧版相同）
MAX_SOURCES = 1000         # 表里最多同时记多少个来源
SWEEP_INTERVAL = 60.0      # 最多每隔这么久做一次全表清理

LOCAL_KEY = "local"        # 本机直连（Host 是本机地址、没有任何代理头）
OVERFLOW_KEY = "overflow"  # 表满了以后，新来源共用的计数桶
UNKNOWN_KEY = "proxy-unknown"   # 走了代理却拿不到合法 IP 的请求共用的计数桶（宁严勿松）


def source_key(headers, is_local: bool) -> str:
    """把一个请求归到一个「来源」。

    - 本机直连：固定一个 key。
    - 其余（分享链接 / 主人通道）：用 Cloudflare 写入的 CF-Connecting-IP。这个头由 Cloudflare 覆盖，
      访客伪造不了；本服务只监听 127.0.0.1，能直接伪造它的只有本机进程。
      X-Forwarded-For 可被客户端追加，不用。
    - IPv6 按 /64 归并（一个用户通常拥有整段 /64，否则攻击者换地址就能绕过计数）；
      IPv4 映射的 IPv6 地址还原成 IPv4。
    - 头缺失或不是合法 IP：全部落进同一个 UNKNOWN_KEY，不会各自获得新额度。
    """
    if is_local:
        return LOCAL_KEY
    raw = (headers.get("CF-Connecting-IP") or "").split(",")[0].strip()
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return UNKNOWN_KEY
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return "ip:" + str(ip.ipv4_mapped)
        return "ip:" + str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return "ip:" + str(ip)


class LoginThrottle:
    """按来源记录登录失败时间，线程安全（网站是多线程服务）。"""

    def __init__(self, limit: int = LIMIT, window: float = WINDOW, max_sources: int = MAX_SOURCES,
                 sweep_interval: float = SWEEP_INTERVAL, clock=time.time):
        self.limit = limit
        self.window = window
        self.max_sources = max_sources
        self.sweep_interval = sweep_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}       # 来源 -> 最近的失败时间（最多留 limit 个，够判断就行）
        self._last_sweep = clock()

    # ---------- 内部（调用前已持锁）
    def _live(self, dq: deque, now: float) -> int:
        cutoff = now - self.window
        while dq and dq[0] <= cutoff:           # 和旧版一致：t > now - window 才算窗口内
            dq.popleft()
        return len(dq)

    def _sweep(self, now: float):
        """清掉窗口外的记录，并把空条目从表里删掉。"""
        for k in [k for k, dq in self._hits.items() if self._live(dq, now) == 0]:
            del self._hits[k]
        self._last_sweep = now

    def _resolve(self, key: str, now: float) -> str:
        """决定这个来源实际落在哪个桶。表满且清不出空位时，新来源共用 OVERFLOW_KEY：
        不去踢掉别人的记录（否则攻击者灌入大量假来源就能洗掉自己的失败次数），也不放行无限次猜测。
        本机直连的 key 永远有位置（它只有一个，不会撑大表），站主不会被远程攻击挤出去。"""
        if key == LOCAL_KEY or key in self._hits:
            return key
        if now - self._last_sweep >= self.sweep_interval:
            self._sweep(now)
        if len(self._hits) < self.max_sources:
            return key
        self._sweep(now)
        return key if len(self._hits) < self.max_sources else OVERFLOW_KEY

    # ---------- 对外
    def blocked(self, key: str, now: float | None = None) -> bool:
        """这个来源现在是否被暂停登录。只读：不会为新来源建条目。"""
        now = self._clock() if now is None else now
        with self._lock:
            k = self._resolve(key, now)
            dq = self._hits.get(k)
            return dq is not None and self._live(dq, now) >= self.limit

    def record(self, key: str, now: float | None = None):
        """记一次失败。"""
        now = self._clock() if now is None else now
        with self._lock:
            k = self._resolve(key, now)
            dq = self._hits.get(k)
            if dq is None:
                dq = self._hits[k] = deque(maxlen=self.limit)
            self._live(dq, now)
            dq.append(now)

    def clear(self):
        """轮换主令牌后清空所有来源的记录。"""
        with self._lock:
            self._hits.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._hits)
