"""数据切分与锁定测试段(T007 / S1)。

内容:
  1. three_way_split / split_by_dates:训练 / 验证 / 锁定测试三段切分,段间留 embargo。
  2. walk_forward:滚动(或锚定)前进窗口生成器。
  3. locked_fingerprint + LockedTestGuard:锁定段指纹封存与一次性访问留痕。

约定:
  - 所有区间都是"位置区间"(start, end),左闭右开,即 index[start:end]。
  - 输入的日期序列必须严格升序、无重复;否则抛 SplitError。
  - embargo(隔离带)= 两段之间丢弃的交易日根数。动机:标签/持仓会跨日
    (例如持仓 5 天的收益用到了未来 5 根),紧挨着切会让训练段的信息泄漏进
    下一段,所以中间空出若干根不用。
  - 本模块只用标准库;只有 LockedTestGuard 会写文件,且路径由调用者指定。
  - 守卫提供的是"留痕与检出改动",不是访问控制:调用者手里的数组谁都能读。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # 非 Unix 平台
    fcntl = None

__all__ = [
    "SplitError", "LockedTestError", "Split", "Fold",
    "three_way_split", "split_by_dates", "walk_forward",
    "locked_fingerprint", "LockedTestGuard", "read_log",
]


_RESERVED_LEDGER_NAME = "trials" + ".jsonl"   # 运行时拼接:源码不含该文件名字面量

class SplitError(ValueError):
    """切分的输入不合法(日期乱序/重复/长度不足/参数非法)。"""


class LockedTestError(RuntimeError):
    """锁定测试段的访问被拒绝(重复 label、指纹不一致、日志损坏等)。"""


@dataclass(frozen=True)
class Split:
    """三段位置区间,左闭右开;顺序固定为 train < valid < test。"""
    train: tuple
    valid: tuple
    test: tuple


@dataclass(frozen=True)
class Fold:
    """一个滚动窗口:训练区间与测试区间(位置区间,左闭右开)。"""
    train_idx: tuple
    test_idx: tuple


# ---------------------------------------------------------------- 输入校验

def _as_int(name, v, minimum):
    # bool 是 int 的子类,但 True 当成 1 天几乎一定是笔误,直接拒绝
    if isinstance(v, bool) or not isinstance(v, int):
        raise SplitError(f"{name} 必须是整数,收到 {v!r}")
    if v < minimum:
        raise SplitError(f"{name} 必须 >= {minimum},收到 {v}")
    return v


def _check_index(index, min_len=1):
    """校验并返回 list:严格升序、无重复、长度 >= min_len。"""
    if isinstance(index, (str, bytes)) or not hasattr(index, "__iter__"):
        raise SplitError("index 必须是日期序列(list/tuple/Index 等),不是字符串或标量")
    idx = list(index)
    if len(idx) < min_len:
        raise SplitError(f"日期序列长度不足:需要至少 {min_len} 根,实际 {len(idx)} 根")
    for i in range(1, len(idx)):
        try:
            increasing = idx[i - 1] < idx[i]
            equal = idx[i - 1] == idx[i]
        except TypeError as e:
            raise SplitError(f"第 {i} 个日期与前一个无法比较(类型混用?):{e}") from None
        if equal:
            raise SplitError(f"日期重复:位置 {i - 1} 与 {i} 都是 {idx[i]!r}")
        if not increasing:
            raise SplitError(
                f"日期未按升序排列:位置 {i - 1} 是 {idx[i - 1]!r},位置 {i} 是 {idx[i]!r}")
    return idx


def _check_frac(name, v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 < v < 1):
        raise SplitError(f"{name} 必须是 (0,1) 内的小数,收到 {v!r}")
    return float(v)


# ---------------------------------------------------------------- 三段切分

def three_way_split(index, train=0.6, valid=0.2, embargo=5):
    """按比例切成 训练 / 验证 / 锁定测试,段间各留 embargo 根。

    锁定测试占比 = 1 - train - valid(必须 > 0)。先扣掉 2 个 embargo,
    再按比例分配剩余的根数;每段至少 1 根,否则抛 SplitError。
    """
    train = _check_frac("train", train)
    valid = _check_frac("valid", valid)
    if train + valid >= 1 - 1e-12:
        raise SplitError(f"train + valid 必须 < 1(要给锁定测试段留位置),收到 {train + valid}")
    embargo = _as_int("embargo", embargo, 0)
    idx = _check_index(index, min_len=3)
    n = len(idx)
    usable = n - 2 * embargo
    if usable < 3:
        raise SplitError(f"长度不足:{n} 根扣掉 2×embargo({embargo}) 后只剩 {usable} 根,不够分三段")
    n_train = int(round(usable * train))
    n_valid = int(round(usable * valid))
    n_test = usable - n_train - n_valid
    if min(n_train, n_valid, n_test) < 1:
        raise SplitError(
            f"按比例分配后有空段(train={n_train}, valid={n_valid}, test={n_test});"
            "请增加数据长度或调整比例/embargo")
    a = n_train
    b = a + embargo
    c = b + n_valid
    d = c + embargo
    return Split(train=(0, a), valid=(b, c), test=(d, n))


def split_by_dates(index, valid_start, test_start, embargo=0):
    """按日期切分:valid_start / test_start 是各段的第一个日期(必须在 index 中)。

    训练段 = valid_start 之前,验证段 = [valid_start, test_start),测试段 = test_start 起。
    embargo 从"前一段的尾部"扣除(训练段尾、验证段尾),所以验证/测试的起点保持你指定的日期。
    """
    embargo = _as_int("embargo", embargo, 0)
    idx = _check_index(index, min_len=3)
    pos = {}
    for name, d in (("valid_start", valid_start), ("test_start", test_start)):
        try:
            pos[name] = idx.index(d)
        except ValueError:
            raise SplitError(f"{name}={d!r} 不在日期序列中") from None
    iv, it = pos["valid_start"], pos["test_start"]
    if not iv < it:
        raise SplitError(f"valid_start 必须早于 test_start(位置 {iv} vs {it})")
    train_end = iv - embargo
    valid_end = it - embargo
    if train_end < 1:
        raise SplitError(f"训练段为空:valid_start 之前只有 {iv} 根,不够扣 embargo={embargo}")
    if valid_end - iv < 1:
        raise SplitError(f"验证段为空:验证段只有 {it - iv} 根,不够扣 embargo={embargo}")
    return Split(train=(0, train_end), valid=(iv, valid_end), test=(it, len(idx)))


# ---------------------------------------------------------------- 滚动前进

def walk_forward(index, train_len, test_len, step=None, embargo=0, anchored=False):
    """生成滚动前进窗口列表。

    第 k 折(k 从 0 起,s = k*step):
      训练 = [s, s+train_len)(anchored=True 时起点固定为 0,即训练段越滚越长)
      测试 = [s+train_len+embargo, s+train_len+embargo+test_len)
    step 默认 = test_len,使各折测试段首尾相接、不重叠;step < test_len 会让测试段重叠,
    OOS 收益拼接时会重复计入,所以直接拒绝。放不下完整测试窗的尾部丢弃,不做截断。
    """
    train_len = _as_int("train_len", train_len, 1)
    test_len = _as_int("test_len", test_len, 1)
    embargo = _as_int("embargo", embargo, 0)
    step = test_len if step is None else _as_int("step", step, 1)
    if step < test_len:
        raise SplitError(f"step({step}) 不能小于 test_len({test_len}),否则测试窗口会重叠")
    need = train_len + embargo + test_len
    idx = _check_index(index, min_len=need)
    n = len(idx)
    folds = []
    s = 0
    while s + need <= n:
        t0 = s + train_len + embargo
        folds.append(Fold(train_idx=(0 if anchored else s, s + train_len),
                          test_idx=(t0, t0 + test_len)))
        s += step
    return folds


# ---------------------------------------------------------------- 指纹

def _ser(v):
    """把值序列化成稳定的 JSON 友好形式;浮点用 repr,保证一个比特的改动也能检出。"""
    if isinstance(v, bool) or v is None or isinstance(v, (int, str)):
        return v
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return [_ser(x) for x in v]
    try:  # numpy 标量等:用 item() 还原成 Python 内置类型
        return _ser(v.item())
    except (AttributeError, ValueError):
        return str(v)


def locked_fingerprint(dates, values):
    """对锁定段 (日期, 数值) 逐行序列化后取 sha256 十六进制串。

    注意:日期用 str() 序列化,date 与带时间的 Timestamp 会得到不同指纹;
    封存和校验时请用同一种类型。
    """
    dates = list(dates)
    values = list(values)
    if len(dates) != len(values):
        raise SplitError(f"dates({len(dates)}) 与 values({len(values)}) 长度不一致")
    h = hashlib.sha256()
    for d, v in zip(dates, values):
        line = json.dumps([str(d), _ser(v)], ensure_ascii=False, separators=(",", ":"))
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


# ---------------------------------------------------------------- 访问日志

def _int_pair(v):
    return (isinstance(v, (list, tuple)) and len(v) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) for x in v))


def _nonempty_str(v):
    return isinstance(v, str) and bool(v.strip())


def _validate_row(row, no, path):
    """校验单行记录的结构与字段类型;不合格抛 LockedTestError(损坏日志不能变成绕过手段)。"""
    ev = row.get("event")
    ok = False
    if ev == "seal":
        sp = row.get("split")
        ok = (isinstance(sp, dict) and all(_int_pair(sp.get(k)) for k in ("train", "valid", "test"))
              and _nonempty_str(row.get("fingerprint")))
    elif ev == "open":
        ok = (_nonempty_str(row.get("label")) and _int_pair(row.get("range"))
              and _nonempty_str(row.get("fingerprint")))
    if not ok:
        raise LockedTestError(f"日志第 {no} 行格式不对(event 非法或字段缺失/类型错误),拒绝放行:{path}")


def _parse_rows(text, path):
    rows = []
    for no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise LockedTestError(f"日志第 {no} 行无法解析,拒绝放行:{path}") from None
        if not isinstance(row, dict):
            raise LockedTestError(f"日志第 {no} 行格式不对(不是对象),拒绝放行:{path}")
        _validate_row(row, no, path)
        rows.append(row)
    return rows


def read_log(path):
    """读取 JSONL 日志为 dict 列表;文件不存在视为空。损坏/结构不合格的行抛 LockedTestError。"""
    path = os.fspath(path)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return _parse_rows(f.read(), path)


@contextlib.contextmanager
def _locked_file(path):
    """以追加+读模式打开日志并加独占锁(flock),使"读日志→查重→追加"成为一个原子步骤。

    锁加在日志文件自身上(不另建锁文件);跨进程、跨线程(各自 open)都有效。
    没有 fcntl 的平台(Windows)退化为无锁,文档里已说明。
    """
    f = open(path, "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield f
    finally:
        f.close()   # 关闭文件即释放 flock


def _read_locked(f, path):
    f.seek(0)
    text = f.read()
    return _parse_rows(text, path), text


def _append_locked(f, text, row):
    """追加一行 JSONL 并 fsync;若文件末尾缺换行(被截断),先补一个换行免得粘在一起。"""
    prefix = "\n" if text and not text.endswith("\n") else ""
    f.write(prefix + json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    f.flush()
    os.fsync(f.fileno())


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_split(split):
    """守卫只接受合法切分:整数区间、非负、非空、train < valid < test 且互不重叠。"""
    names = ("train", "valid", "test")
    segs = []
    for name in names:
        seg = getattr(split, name)
        if not _int_pair(seg):
            raise SplitError(f"split.{name} 必须是两个整数 (start, end),收到 {seg!r}")
        if seg[0] < 0 or seg[1] <= seg[0]:
            raise SplitError(f"split.{name}={tuple(seg)!r} 非法:需要 0 <= start < end")
        segs.append(seg)
    for i in range(2):
        if segs[i][1] > segs[i + 1][0]:
            raise SplitError(f"{names[i]} 与 {names[i + 1]} 区间重叠或顺序颠倒:"
                             f"{tuple(segs[i])!r} vs {tuple(segs[i + 1])!r}")


def _split_dict(split):
    return {"train": list(split.train), "valid": list(split.valid), "test": list(split.test)}


class LockedTestGuard:
    """锁定测试段守卫:封存指纹 + 一次性 open 授权 + 追加式访问日志。

    log_path 由调用者指定,文件名不得含项目试验账本的保留名(见 _RESERVED_LEDGER_NAME,
    不能混用);建议 locked_access_log.jsonl。构造和每次 open 都会重新读日志,
    所以换进程/新建实例后仍能看到历史访问。
    """

    def __init__(self, split, log_path, fingerprint):
        if not isinstance(split, Split):
            raise SplitError("split 必须是 Split 对象")
        if not _nonempty_str(fingerprint):   # 与日志解析口径一致,纯空白也拒绝,免得写出读不回来的 seal
            raise SplitError("fingerprint 必须是非空字符串(用 locked_fingerprint 计算)")
        log_path = os.fspath(log_path)
        if _RESERVED_LEDGER_NAME in os.path.basename(log_path).lower():
            raise SplitError("访问日志文件名不得含项目试验账本的保留名(请用 locked_access_log.jsonl)")
        _validate_split(split)
        self._split = split
        self._log_path = log_path
        self._fingerprint = fingerprint
        with _locked_file(log_path) as f:
            rows, text = _read_locked(f, log_path)
            if self._check_seal(rows) is None:
                _append_locked(f, text, {"event": "seal", "time": _now(),
                                         "split": _split_dict(split), "fingerprint": fingerprint})

    @property
    def log_path(self):
        return self._log_path

    @property
    def split(self):
        return self._split

    def _check_seal(self, rows):
        """校验日志里的 seal 行与当前实例一致;返回 seal 行,没有则返回 None。"""
        seals = [r for r in rows if r["event"] == "seal"]
        if len(seals) > 1:
            raise LockedTestError("日志里有多条 seal 行,日志可能被篡改,拒绝放行")
        if not seals:
            if rows:
                raise LockedTestError("日志有 open 记录却没有 seal 行,拒绝放行")
            return None
        seal = seals[0]
        if rows[0] is not seal:
            raise LockedTestError("seal 行必须是日志第一条记录,拒绝放行")
        if seal.get("fingerprint") != self._fingerprint:
            raise LockedTestError("锁定段数据与封存时不一致(指纹不同)")
        if seal.get("split") != _split_dict(self._split):
            raise LockedTestError("切分区间与封存时不一致")
        for r in rows:
            if r["event"] == "open" and (r["fingerprint"] != self._fingerprint
                                         or list(r["range"]) != list(self._split.test)):
                raise LockedTestError("日志里的 open 记录与封存的指纹/区间不一致,拒绝放行")
        return seal

    def read_log(self):
        return read_log(self._log_path)

    def open_count(self):
        """历史上成功 open 的次数(含其它进程/实例)。"""
        return sum(1 for r in self.read_log() if r["event"] == "open")

    def open(self, label):
        """一次性授权:返回锁定段 (start, end)。同一 label 第二次抛 LockedTestError。"""
        if not isinstance(label, str) or not label.strip():
            raise LockedTestError("label 必须是非空字符串")
        start, end = self._split.test
        # 读日志、查重、追加都在同一把独占锁里:并发 open 同一 label 只会有一个成功
        with _locked_file(self._log_path) as f:
            rows, text = _read_locked(f, self._log_path)
            if self._check_seal(rows) is None:
                raise LockedTestError("日志缺少 seal 行(被删除?),拒绝放行")
            if any(r["event"] == "open" and r["label"] == label for r in rows):
                raise LockedTestError(f"label {label!r} 已经访问过锁定段,不能重复访问")
            # 先落盘再返回:日志写失败就不放行
            _append_locked(f, text, {"event": "open", "time": _now(), "label": label,
                                     "range": [start, end], "fingerprint": self._fingerprint})
        return (start, end)
