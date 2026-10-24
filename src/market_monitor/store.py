"""独占快照存储。仅盘中模块使用，API 不接受路径。失败由服务暂停提醒。"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import threading
import uuid

STORE_ROOT = Path(__file__).resolve().parents[2] / 'data' / 'market_monitor'


class StorageError(Exception):
    pass


@contextmanager
def directory(path, create=False):
    """逐层 dirfd + O_NOFOLLOW，检查和打开无路径替换窗口。"""
    path = Path(path).absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            made = False
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    made = True
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if made:
                os.fchmod(fd, 0o700)
        info = os.fstat(fd)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise StorageError('storage_permissions')
        yield fd
    finally:
        os.close(fd)


def empty_state():
    return {'version': 1, 'watch': [], 'rules': {}, 'alerts': [], 'samples': {},
            'quality': {'requests': 0, 'valid': 0, 'invalid': 0, 'failures': 0,
                        'reasons': {}, 'age_seconds': {'count': 0, 'sum': 0, 'max': None},
                        'elapsed_seconds_sum': 0, 'max_gap_seconds': 0, 'alerts_suppressed': 0}, 'limits': {}}


class Store:
    """构造不创建目录；完整快照独占写入/fsync，旧快照保留便于核对。

    root 仅用于本机可信代码/测试注入；浏览器不能设置它。半写入快照会导致
    读取失败，绝不静默回退丢失提醒。运行状态和基线不持久化。
    """
    def __init__(self, *, root=None):
        self.root = Path(root) if root is not None else STORE_ROOT
        self._lock = threading.RLock()
        self._revision = 0

    def load(self):
        with self._lock:
            try:
                with directory(self.root) as fd:
                    names = sorted(n for n in os.listdir(fd) if n.startswith('snapshot_') and n.endswith('.json'))
                    if not names:
                        return empty_state()
                    name = names[-1]
                    self._revision = int(name.split('_')[1])
                    filefd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    with os.fdopen(filefd, 'r', encoding='utf-8') as stream:
                        info = os.fstat(stream.fileno())
                        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                            raise StorageError('storage_permissions')
                        state = json.load(stream)
                    if (not isinstance(state, dict) or state.keys() != empty_state().keys()
                            or state['version'] != 1 or not isinstance(state['watch'], list)
                            or not all(isinstance(state[k], dict) for k in ('rules', 'samples', 'quality', 'limits'))
                            or not isinstance(state['alerts'], list)):
                        raise StorageError('storage_format')
                    return state
            except FileNotFoundError:
                # 仅目录本身缺失表示首次启动；缺快照不能当作空状态。
                if not self.root.exists():
                    return empty_state()
                raise StorageError('storage_read') from None
            except (OSError, ValueError, TypeError, KeyError):
                raise StorageError('storage_read') from None

    def save(self, state):
        with self._lock:
            try:
                encoded = json.dumps(deepcopy(state), ensure_ascii=False, allow_nan=False).encode('utf-8')
                with directory(self.root, create=True) as fd:
                    self._revision += 1
                    name = f'snapshot_{self._revision:012d}_{uuid.uuid4().hex}.json'
                    filefd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=fd)
                    with os.fdopen(filefd, 'wb') as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.fsync(fd)
            except (OSError, ValueError, TypeError):
                raise StorageError('storage_write') from None
