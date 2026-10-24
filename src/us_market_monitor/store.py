"""美股盯盘快照存储：目录 data/us_market_monitor/，权限 0700，拒绝符号链接。

复用 A 股盯盘已验收的安全实现（逐层 dirfd + O_NOFOLLOW、0600 文件、独占创建、fsync），
只换默认根目录，不复制安全代码，避免两份实现日后漂移。
存储不使用 SQL，故不存在"参数化查询"问题；API 也不接受任何路径，只有本机可信代码能注入 root。
"""
from pathlib import Path
import uuid  # noqa: F401  测试需要能 patch 本模块的 uuid4 以构造文件名冲突

from ..market_monitor.store import Store as _BaseStore, StorageError, directory, empty_state  # noqa: F401

STORE_ROOT = Path(__file__).resolve().parents[2] / 'data' / 'us_market_monitor'


class Store(_BaseStore):
    """构造不创建目录；默认根目录是美股专用目录，与 A 股 data/market_monitor 完全分开。"""
    def __init__(self, *, root=None):
        super().__init__(root=Path(root) if root is not None else STORE_ROOT)
