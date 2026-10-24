"""所有磁盘路径都从这里出：不写死绝对路径，外部项目的位置只来自配置。

防护栏（tripwire）：环境变量 QB_TESTING=1 时，只要解析后的路径落在真实仓库的 data/ 里，
或者落在仓库旁边的真实 ai_layer 目录里，就直接抛 TripwireError。
测试必须传入临时目录作为 root，写错了也不会碰到真实文件。
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TripwireError(RuntimeError):
    """测试模式下试图访问真实数据目录。"""


def _testing() -> bool:
    return os.environ.get("QB_TESTING") == "1"


def _real_forbidden() -> list[Path]:
    """测试模式下不许碰的真实位置：本仓库的 data/，以及与本仓库同级的 ai_layer（只用于防护，不用于读写）。"""
    return [(ROOT / "data").resolve(), (ROOT.parent / "ai_layer").resolve()]


def guard(path) -> Path:
    """返回解析后的路径；测试模式下落在真实位置内就抛异常。只比较路径，不访问磁盘内容。"""
    p = Path(path).expanduser().resolve()
    if _testing():
        for bad in _real_forbidden():
            if p == bad or bad in p.parents:
                raise TripwireError(f"测试模式下不允许访问真实路径：{p}")
    return p


def data_dir(root=None) -> Path:
    """root 默认是真实仓库；测试里必须显式传临时目录。"""
    return guard(Path(root) / "data" if root is not None else ROOT / "data")


def config_path(root=None) -> Path:
    return guard(data_dir(root) / "ai_translator.json")


def state_dir(root=None) -> Path:
    """日志、锁文件、pid 文件、worker 的 stderr 都放在这个目录下。"""
    return guard(data_dir(root) / "ai_translator")


def lock_path(root=None) -> Path:
    return guard(state_dir(root) / "worker.lock")


def pid_path(root=None) -> Path:
    return guard(state_dir(root) / "worker.pid")


def log_path(month: str, root=None) -> Path:
    """month 形如 202610。只接受 6 位数字，防止拼出奇怪的路径。"""
    if not (isinstance(month, str) and len(month) == 6 and month.isdigit()):
        raise ValueError("month 必须是 YYYYMM")
    return guard(state_dir(root) / f"log_{month}.jsonl")


def ai_layer_dir(configured):
    """把配置里的 ai_layer_dir 展开成路径；没配置就返回 None（功能不可用，而不是猜一个位置）。"""
    if configured is None:
        return None
    return guard(configured)
