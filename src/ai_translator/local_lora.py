"""本地翻译子进程监督者；只用标准库，不导入模型或研究项目。

S3 通过 model_provider 注入已选模型信息；registry 的读取、哈希验证由 S7 接入。
测试命令只能在 QB_TESTING=1 且显式注入时使用，生产命令始终固定。
"""
import atexit
import ctypes
import datetime
import fcntl
import json
import math
import os
import select
import signal
import subprocess
import threading
import time
import weakref
from pathlib import Path

from . import config, paths
from .backend import TranslatorBackend, TranslatorError, make_result

MAX_LINE = 64 * 1024
SCHEMA_SHA256 = "ef6322a00fc61933e34ba95e53c5c1afd3076b6e078a22e46c53e0f51f79860c"
SCRIPT = paths.ROOT / "scripts" / "ai_translator_worker.py"
_instances = weakref.WeakSet()
_handlers = {}


def clean_environment():
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL") if k in os.environ}
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    return env


def _close_all():
    for owner in tuple(_instances):
        owner.close()


def _signal_handler(signum, frame):
    _close_all()
    previous = _handlers[signum]
    if callable(previous):
        previous(signum, frame)
    elif previous == signal.SIG_DFL:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


def _install_hooks():
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            if sig not in _handlers:
                _handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, _signal_handler)


atexit.register(_close_all)


def _command_args(pid):
    """只核对遗留 pid 的身份，不运行 ps/pgrep，不把命令行写进日志。"""
    if os.uname().sysname == "Linux":
        return Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    if os.uname().sysname == "Darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN / KERN_PROCARGS2
        buf = ctypes.create_string_buffer(256 * 1024)
        size = ctypes.c_size_t(len(buf))
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno(), "pid identity unavailable")
        data = buf.raw[:size.value]
        argc = int.from_bytes(data[:4], byteorder="little")
        rest = data[4:].split(b"\0", 1)[1].lstrip(b"\0")
        return rest.split(b"\0")[:argc]
    raise OSError("pid identity unavailable")


def _json_object(data):
    def pairs(items):
        out = {}
        for key, val in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = val
        return out

    def constant(_):
        raise ValueError("nonfinite")

    obj = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(obj, dict):
        raise ValueError("object required")
    return obj


class Supervisor(TranslatorBackend):
    """一个实例内单飞，实例间共享文件锁。status 不启动进程。

    model_provider 返回 {version, base_model, adapter_path}（路径相对 ai_layer_dir），
    或返回 None 表示不可用。生产环境调用者必须先完成 registry 校验。
    update_config 用于关闭开关；模型切换在当前请求结束后检查并回收。
    """

    def __init__(self, cfg=None, *, root=None, model_provider=None, audit=None,
                 test_command=None, shutdown_grace_s=5, term_grace_s=3,
                 worker_lock_fd=None, config_loader=None):
        if test_command is not None and os.environ.get("QB_TESTING") != "1":
            raise ValueError("test command requires QB_TESTING=1")
        self.cfg = config.validate(cfg or {})
        self.root = root
        self._dir = paths.state_dir(root)
        self._provider = model_provider or (lambda: None)
        self._audit = audit
        self._test_command = list(test_command) if test_command is not None else None
        self._worker_lock_fd = worker_lock_fd
        self._config_loader = config_loader
        self._shutdown_grace = shutdown_grace_s
        self._term_grace = term_grace_s
        self._guard = threading.RLock()
        self._flight = threading.Lock()
        self._stop = threading.Event()
        self._proc = None
        self._lock_file = None
        self._model = None
        self._buffer = b""
        self._closed = False
        self._last_used = time.monotonic()
        self._failures = []
        self._cooldown_until = 0
        self._day = None
        self._daily_count = 0
        self._serial = 0
        self._state = "idle"
        self._problem = None
        self._warning = None
        self.schema_sha256 = None
        self.backend_id = "local_lora"
        self.model_version = ""
        self.starts = 0
        self._watcher = None
        _instances.add(self)
        _install_hooks()
        if self._enabled():
            with self._guard:
                try:
                    self._acquire_file_lock()
                    self._recover_stale()
                except (OSError, ValueError, TypeError, KeyError, TranslatorError):
                    self._problem = "WORKER_LOCKED_OR_STALE"
                finally:
                    self._release_file_lock()

    def _enabled(self):
        return not self._closed and self.cfg["enabled"] and self.cfg["backend"] in ("local_lora", "fake")

    def _selected(self):
        try:
            model = self._provider()
            if not isinstance(model, dict) or not all(isinstance(model.get(k), str) and model[k]
                                                      for k in ("version", "base_model", "adapter_path")):
                self._problem = getattr(self._provider, "problem", None) or "MODEL_UNAVAILABLE"
                return None
            if self.cfg["expect_model_version"] not in (None, model["version"]):
                self._problem = "MODEL_VERSION_MISMATCH"
                return None
            self._problem = None
            self.model = dict(model)
            return dict(model)
        except Exception:
            return None

    def _acquire_file_lock(self):
        if self._worker_lock_fd is not None:
            self._lock_file = os.dup(self._worker_lock_fd)
            return
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._dir, 0o700)
        fd = os.open(paths.lock_path(self.root), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._lock_file = fd

    def _release_file_lock(self):
        if self._lock_file is not None:
            os.close(self._lock_file)
            self._lock_file = None

    def _recover_stale(self):
        pfile = paths.pid_path(self.root)
        if not pfile.exists():
            return
        try:
            if pfile.stat().st_size > MAX_LINE:
                raise ValueError("invalid pid record")
            record = _json_object(pfile.read_bytes())
            pid = record["pid"]
            if type(pid) is not int or pid <= 1 or pid == os.getpid():
                raise ValueError("invalid pid")
            os.kill(pid, 0)
        except ProcessLookupError:
            pfile.unlink(missing_ok=True)
            return
        # 锁已经在本进程手里；仍须核实脚本和独立进程组，避免 pid 复用误杀。
        script = str(SCRIPT)
        if self._test_command is not None:
            script = str(paths.ROOT / "tests" / "fake_translator_worker.py")
        if os.fsencode(script) not in _command_args(pid) or os.getpgid(pid) != pid:
            raise ValueError("unverified pid")
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + self._term_grace
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        pfile.unlink(missing_ok=True)

    def _start(self, model):
        try:
            self._acquire_file_lock()
            self._recover_stale()
            base = paths.ai_layer_dir(self.cfg["ai_layer_dir"])
            if base is None:
                raise TranslatorError("TRANSLATOR_UNAVAILABLE")
            model_dir = paths.guard(base / model["base_model"])
            adapter_dir = paths.guard(base / model["adapter_path"])
            cmd = self._test_command or [str(base / ".venv" / "bin" / "python"), "-I", str(SCRIPT),
                                        "--ai-layer-dir", str(base), "--model", str(model_dir),
                                        "--adapter", str(adapter_dir)]
            errfd = os.open(self._dir / "worker.err", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.fchmod(errfd, 0o600)
                if os.fstat(errfd).st_size > 1024 * 1024:
                    os.ftruncate(errfd, 0)
                self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                              stderr=errfd, cwd=base, env=clean_environment(),
                                              start_new_session=True, close_fds=True, bufsize=0)
            finally:
                os.close(errfd)
            self.starts += 1
            os.set_blocking(self._proc.stdin.fileno(), False)
            os.set_blocking(self._proc.stdout.fileno(), False)
            self._model = model
            self.model_version = model["version"]
            self.backend_id = "local_lora:" + self.model_version
            self._buffer = b""
            self._state = "loading"
            self._warning = None
            pidfd = os.open(paths.pid_path(self.root), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(pidfd, "w") as f:
                os.fchmod(f.fileno(), 0o600)
                json.dump({"pid": self._proc.pid}, f)
        except (OSError, ValueError):
            self._kill()
            raise TranslatorError("TRANSLATOR_UNAVAILABLE") from None
        except BaseException:
            self._kill()
            raise
        if self._watcher is None:
            self._watcher = threading.Thread(target=self._watch, name="translator-idle", daemon=True)
            self._watcher.start()

    def _send(self, p, obj, deadline):
        payload = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        if len(payload) > MAX_LINE:
            raise ValueError("line too long")
        fd = p.stdin.fileno()
        while payload:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                raise TimeoutError
            try:
                payload = payload[os.write(fd, payload):]
            except BlockingIOError:
                continue

    def _read(self, p, deadline):
        fd = p.stdout.fileno()
        while b"\n" not in self._buffer:
            if len(self._buffer) >= MAX_LINE:
                raise ValueError("line too long")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError
            try:
                chunk = os.read(fd, MAX_LINE + 1 - len(self._buffer))
            except BlockingIOError:
                continue
            if not chunk:
                raise ValueError("unexpected EOF")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        if len(line) + 1 > MAX_LINE:
            raise ValueError("line too long")
        return _json_object(line.decode("utf-8"))

    def _ready(self, p):
        ready = self._read(p, time.monotonic() + self.cfg["timeouts"]["load_s"])
        base = paths.ai_layer_dir(self.cfg["ai_layer_dir"])
        if ready.get("op") != "ready" or ready.get("model_dir") != str(paths.guard(base / self._model["base_model"])) \
                or ready.get("adapter_dir") != str(paths.guard(base / self._model["adapter_path"])):
            raise ValueError("bad ready")
        sha = ready.get("schema_sha256")
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("bad schema hash")
        load_s = ready.get("load_s")
        if type(load_s) not in (int, float) or not math.isfinite(load_s) or load_s < 0:
            raise ValueError("bad load duration")
        self.schema_sha256 = sha
        if sha != SCHEMA_SHA256:
            self._warning = "SCHEMA_CHANGED"
        self._state = "ready"

    def _reply(self, obj, rid):
        if obj.get("op") != "result" or obj.get("id") != rid or type(obj.get("ok")) is not bool:
            raise ValueError("bad result envelope")
        if not obj["ok"]:
            raise ValueError("generation failed")
        raw, mode, doc = obj.get("raw_output"), obj.get("parse_mode"), obj.get("obj")
        gen_ms = obj.get("gen_ms")
        if not isinstance(raw, str) or mode not in ("strict", "none") \
                or type(gen_ms) not in (int, float) or not math.isfinite(gen_ms) or gen_ms < 0:
            raise ValueError("bad result fields")
        if mode == "none" and doc is None:
            return None, raw, mode
        if mode != "strict" or not isinstance(doc, dict) or _json_object(raw) != doc:
            raise ValueError("bad strict object")
        return doc, raw, mode

    def _result(self, started, *, doc=None, code=None, raw="", mode="none"):
        return make_result(config=doc, refusal={"code": code} if code else None, raw_output=raw,
                           parse_mode=mode, backend_id=self.backend_id, model_version=self.model_version,
                           latency_ms=(time.monotonic() - started) * 1000)

    def _failed(self):
        with self._guard:
            self._kill()
            now = time.monotonic()
            self._failures = [t for t in self._failures if now - t < 600] + [now]
            if len(self._failures) >= 3:
                self._cooldown_until = now + 600
                self._state = "cooldown"
                self._problem = "BREAKER_OPEN"
                if self._audit is not None:
                    try:
                        self._audit("ai_translator_breaker_open", {"failures": len(self._failures)}, ok=True)
                    except Exception:
                        pass

    def translate(self, idea_text, *, task="idea_to_config"):
        started = time.monotonic()
        if not self._flight.acquire(blocking=False):
            return self._result(started, code="TRANSLATOR_BUSY")
        try:
            with self._guard:
                if not self._enabled():
                    return self._result(started, code="TRANSLATOR_DISABLED")
                if task != "idea_to_config" or not isinstance(idea_text, str) or not idea_text.strip():
                    return self._result(started, code="TRANSLATOR_INPUT_INVALID")
                if len(idea_text) > self.cfg["limits"]["max_input_chars"]:
                    return self._result(started, code="TRANSLATOR_INPUT_TOO_LONG")
                if time.monotonic() < self._cooldown_until:
                    return self._result(started, code="TRANSLATOR_UNAVAILABLE")
                today = datetime.date.today()
                if self._day != today:
                    self._day, self._daily_count = today, 0
                if self._daily_count >= self.cfg["limits"]["daily_limit"]:
                    return self._result(started, code="TRANSLATOR_DAILY_LIMIT")
                model = self._selected()
                if model is None:
                    return self._result(started, code="TRANSLATOR_UNAVAILABLE")
                if self._proc is not None and self._model != model:
                    self._kill()
                if self._proc is None:
                    self._start(model)
                p = self._proc
            if self._state == "loading":
                self._ready(p)
            self._state = "busy"
            self._daily_count += 1
            self._serial += 1
            rid = "r" + str(self._serial)
            deadline = time.monotonic() + self.cfg["timeouts"]["request_s"]
            self._send(p, {"op": "translate", "id": rid, "idea": idea_text}, deadline)
            doc, raw, mode = self._reply(self._read(p, deadline), rid)
            return self._result(started, doc=doc, code="TRANSLATOR_BAD_OUTPUT" if doc is None else None,
                                raw=raw, mode=mode)
        except TranslatorError as exc:
            return self._result(started, code=exc.code)
        except TimeoutError:
            if not self._enabled():
                return self._result(started, code="TRANSLATOR_DISABLED")
            self._failed()
            return self._result(started, code="TRANSLATOR_TIMEOUT")
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            if not self._enabled():
                return self._result(started, code="TRANSLATOR_DISABLED")
            self._failed()
            return self._result(started, code="TRANSLATOR_BACKEND_FAILED")
        except BaseException:
            self._kill()
            raise
        finally:
            with self._guard:
                self._last_used = time.monotonic()
                if self._proc is not None:
                    if not self._enabled() or self._model != self._selected():
                        self._kill()
                    else:
                        self._state = "ready"
            self._flight.release()

    def _kill(self):
        """所有回收共用：shutdown → TERM → KILL，回收直接子进程并释放资源。"""
        with self._guard:
            p = self._proc
            try:
                if p is not None:
                    try:
                        if p.poll() is None:
                            self._send(p, {"op": "shutdown"}, time.monotonic() + 0.2)
                    except (OSError, ValueError, TimeoutError):
                        pass
                    if p.stdin and not p.stdin.closed:
                        p.stdin.close()
                    try:
                        p.wait(timeout=self._shutdown_grace)
                    except subprocess.TimeoutExpired:
                        pass
                    # 即使组长已退出也清理进程组，防止其派生的子进程存活。
                    # 取舍：wait 已回收组长后存在很小的 pid/pgid 复用误杀窗口；
                    # 这里仍立即清理该独立进程组，以免正常退出的组长留下子孙进程。
                    for sig, grace in ((signal.SIGTERM, self._term_grace), (signal.SIGKILL, None)):
                        try:
                            os.killpg(p.pid, sig)
                        except ProcessLookupError:
                            break
                        if grace is not None:
                            try:
                                p.wait(timeout=grace)
                            except subprocess.TimeoutExpired:
                                continue
                        else:
                            p.wait()
                    p.wait()
            finally:
                if p is not None:
                    for pipe in (p.stdin, p.stdout):
                        if pipe is not None:
                            pipe.close()
                    paths.pid_path(self.root).unlink(missing_ok=True)
                self._proc = None
                self._model = None
                self._buffer = b""
                self._release_file_lock()
                self._state = "idle"

    def _watch(self):
        last_model_check = 0
        while not self._stop.wait(min(0.2 if self._config_loader else 5, self.cfg["timeouts"]["idle_s"] / 2)):
            if self._config_loader is not None:
                state = self._config_loader.load()
                if state.cfg != self.cfg:
                    self.update_config(state.cfg)
            if self._flight.acquire(blocking=False):
                try:
                    with self._guard:
                        if self._proc is not None:
                            changed = False
                            if time.monotonic() - last_model_check >= min(5, self.cfg["timeouts"]["idle_s"] / 2):
                                changed = self._model != self._selected()
                                last_model_check = time.monotonic()
                            if self._proc.poll() is not None:
                                self._failed()
                            elif not self._enabled() or changed \
                                    or time.monotonic() - self._last_used > self.cfg["timeouts"]["idle_s"]:
                                self._kill()
                finally:
                    self._flight.release()

    def update_config(self, cfg):
        new = config.validate(cfg)
        with self._guard:
            old_base = self.cfg["ai_layer_dir"]
            self.cfg = new
            if not self._enabled() or old_base != new["ai_layer_dir"]:
                self._kill()

    def status(self):
        with self._guard:
            if not self._enabled():
                state, problem = "disabled", None
            elif time.monotonic() < self._cooldown_until:
                state, problem = "cooldown", "BREAKER_OPEN"
            elif self._selected() is None:
                if self._proc is not None and not self._flight.locked():
                    self._kill()
                state, problem = "unavailable", self._problem or "MODEL_UNAVAILABLE"
            else:
                if self._proc is not None and not self._flight.locked() and self._model != self._selected():
                    self._kill()
                state, problem = self._state, None
                if state == "cooldown":
                    state = "idle"
            return {"state": state, "problem": problem, "warning": self._warning,
                    "backend_id": self.backend_id, "model_version": getattr(self, "model", {}).get("version", self.model_version),
                    "remaining_today": self.cfg["limits"]["daily_limit"] - (
                        self._daily_count if self._day == datetime.date.today() else 0)}

    def close(self):
        with self._guard:
            self._closed = True
            self._stop.set()
            self._kill()
        watcher = self._watcher
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=1)
        _instances.discard(self)
