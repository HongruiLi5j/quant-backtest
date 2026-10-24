"""模型登记的只读校验；网站只选择 active 指针，永不修改登记文件。"""
import hashlib
import fcntl
import json
import os
import re
import threading
import tempfile
from pathlib import Path

from . import paths


class RegistryError(ValueError):
    pass


def strict_object(data):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise RegistryError("REGISTRY_INVALID")
            out[key] = value
        return out

    def constant(_):
        raise RegistryError("REGISTRY_INVALID")

    obj = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(obj, dict):
        raise RegistryError("REGISTRY_INVALID")
    json.dumps(obj, allow_nan=False)
    return obj


def file_sha256(path):
    h = hashlib.sha256()
    with paths.guard(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def relative_path(base, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise RegistryError("REGISTRY_INVALID")
    base = paths.guard(base)
    result = paths.guard(base / value)
    if base not in result.parents or ".." in Path(value).parts:
        raise RegistryError("REGISTRY_INVALID")
    return result


def adapter_sha256(directory):
    directory = paths.guard(directory)
    if not directory.is_dir():
        raise RegistryError("MODEL_UNAVAILABLE")
    h = hashlib.sha256()
    count = 0
    for p in sorted(directory.rglob("*"), key=lambda p: p.relative_to(directory).as_posix()):
        # 拒绝符号链接，避免目录树悄悄指向外部文件或漏掉链接目录。
        if p.is_symlink():
            raise RegistryError("REGISTRY_INVALID")
        if p.is_file():
            name = p.relative_to(directory).as_posix()
            h.update((name + "\0" + file_sha256(p) + "\n").encode("utf-8"))
            count += 1
    if not count:
        raise RegistryError("MODEL_UNAVAILABLE")
    return h.hexdigest()


class WeightHashes:
    """基础权重只在 mtime 或 size 改变时重新读；缓存仅存在本进程内。"""
    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def digest(self, path):
        p = paths.guard(path)
        with self._lock:
            st = p.stat()
            sig = (st.st_mtime_ns, st.st_size)
            old = self._cache.get(p)
            if old is None or old[0] != sig:
                value = file_sha256(p)
                after = p.stat()
                if (after.st_mtime_ns, after.st_size) != sig:
                    raise RegistryError("HASH_MISMATCH")
                self._cache[p] = (sig, value)
            return self._cache[p][1]


WEIGHTS = WeightHashes()


class FileLock:
    def __init__(self, path):
        self.path = paths.guard(path)
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise RegistryError("LOCKED: 请先关闭翻译开关或等待空闲回收") from None
        self.fd = fd
        return self

    def __exit__(self, *_):
        os.close(self.fd)
        self.fd = None


def atomic_write(path, obj):
    p = paths.guard(path)
    payload = (json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".model-registry-", dir=p.parent)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, p)


def evaluation_hash(base, ref):
    """只保留匹配行的字节哈希；不返回评估内容，不推断上线指标。含换行计入哈希。"""
    if not isinstance(ref, dict) or not all(isinstance(ref.get(k), str) and ref[k]
                                           for k in ("file", "run_id", "status")):
        raise RegistryError("EVAL_REF_INVALID")
    matches = []
    with relative_path(base, ref["file"]).open("rb") as stream:
        for line in stream:
            obj = strict_object(line)
            if obj.get("run_id") == ref["run_id"] and obj.get("status") == ref["status"]:
                matches.append(hashlib.sha256(line).hexdigest())
    if len(matches) != 1:
        raise RegistryError("EVAL_REF_AMBIGUOUS_OR_MISSING")
    return matches[0]


def read_registry(path):
    obj = strict_object(paths.guard(path).read_bytes())
    if type(obj.get("schema")) is not int or obj["schema"] != 1 \
            or not isinstance(obj.get("versions"), list) or not isinstance(obj.get("history"), list):
        raise RegistryError("REGISTRY_INVALID")
    names = [v.get("version") if isinstance(v, dict) else None for v in obj["versions"]]
    if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", v) for v in names) \
            or len(set(names)) != len(names) or obj.get("active_version") not in [None, *names]:
        raise RegistryError("REGISTRY_INVALID")
    return obj


def verified_version(base, obj, version, *, weights=WEIGHTS):
    record = next((v for v in obj["versions"] if v["version"] == version), None)
    if record is None:
        raise RegistryError("MODEL_UNAVAILABLE")
    for key in ("adapter_sha256", "base_model_weights_sha256"):
        if not isinstance(record.get(key), str) or not re.fullmatch("[0-9a-f]{64}", record[key]):
            raise RegistryError("REGISTRY_INVALID")
    if record.get("status") not in ("experimental", "recommended") \
            or type(record.get("meets_launch_gate")) is not bool:
        raise RegistryError("REGISTRY_INVALID")
    ref = record.get("eval_ref")
    if not isinstance(record.get("train_run_id"), str) or not record["train_run_id"] \
            or not isinstance(ref, dict) or ref.get("run_id") != record["train_run_id"] \
            or not all(isinstance(ref.get(k), str) and ref[k] for k in ("file", "status")) \
            or not isinstance(ref.get("line_sha256"), str) or not re.fullmatch("[0-9a-f]{64}", ref["line_sha256"]):
        raise RegistryError("REGISTRY_INVALID")
    adapter = relative_path(base, record.get("adapter_path"))
    model = relative_path(base, record.get("base_model"))
    weight = relative_path(base, str((model / "model.safetensors").relative_to(paths.guard(base))))
    if adapter_sha256(adapter) != record["adapter_sha256"] \
            or weights.digest(weight) != record["base_model_weights_sha256"]:
        raise RegistryError("HASH_MISMATCH")
    return {**record, "base_model_sha256": record["base_model_weights_sha256"]}


class Registry:
    def __init__(self, ai_layer_dir, expect_model_version=None):
        self.base = paths.ai_layer_dir(ai_layer_dir)
        self.expect = expect_model_version
        self.problem = None

    def __call__(self):
        try:
            if self.base is None:
                raise RegistryError("MODEL_UNAVAILABLE")
            obj = read_registry(self.base / "MODEL_REGISTRY.json")
            active = obj.get("active_version")
            if active is None:
                raise RegistryError("MODEL_UNAVAILABLE")
            if self.expect not in (None, active):
                raise RegistryError("MODEL_VERSION_MISMATCH")
            model = verified_version(self.base, obj, active)
            self.problem = None
            return model
        except RegistryError as exc:
            self.problem = str(exc)
        except FileNotFoundError:
            self.problem = "MODEL_UNAVAILABLE"
        except (OSError, ValueError, TypeError, KeyError):
            self.problem = "REGISTRY_INVALID"
        return None
