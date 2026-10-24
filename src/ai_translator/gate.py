"""校验器闸门：唯一依据是研究项目自己的 validate_document（空列表才算合法）。

校验在 ai_layer 的 python 里的短命子进程中运行（scripts/ai_gate_validate.py），网站进程不导入校验器，
也不导入 numpy / pandas / mlx。子进程只会调用 validate_document。

任何环节出问题（子进程起不来、超时、输出不对、导入了名单外的模块、被审计钩子拦过）都抛 TRANSLATOR_UNAVAILABLE，
绝不当作"合法"放行。
"""
import json
import os
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

from . import paths
from .backend import TranslatorError

# 偏离设计文档 4.3 / 4.5：schema 模块自己会导入 ml 和 trials，所以名单是这四个（依据见 docs/ai_translator_notes.md 第 9 节）
ALLOWED_STATARB_MODULES = frozenset({"statarb", "statarb.schema", "statarb.ml", "statarb.trials"})
SCRIPT = paths.ROOT / "scripts" / "ai_gate_validate.py"
MAX_PAYLOAD = 64 * 1024
MAX_OUTPUT = 256 * 1024
FALLBACK_CODE = "UNSUPPORTED_REQUEST"


def map_code(code, reject_codes) -> str:
    """校验器的 issue 码 -> 拒绝码。BAD_SCHEMA_VERSION 不是拒绝码，统一映射到 UNSUPPORTED_REQUEST。"""
    return code if code in reject_codes else FALLBACK_CODE


class Gate:
    def __init__(self, python, src_dir, *, script=None, timeout_s: float = 30):
        self.python = Path(python)
        self.src_dir = Path(src_dir)
        self.script = Path(script) if script else SCRIPT
        self.timeout_s = timeout_s
        self.calls = 0                      # 被调用次数（冒烟测试用来断言"闸门被调用"）
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: dict, **kw):
        """从配置构造。ai_layer_dir 或 statarb_src_dir 没配置就不可用。"""
        base, src = paths.ai_layer_dir(cfg.get("ai_layer_dir")), cfg.get("statarb_src_dir")
        if base is None or not src:
            raise TranslatorError("TRANSLATOR_UNAVAILABLE")
        return cls(base / ".venv" / "bin" / "python", Path(src).expanduser(), **kw)

    def _run(self, payload: bytes) -> dict:
        cmd = [str(self.python), "-I", "-B", str(self.script), "--src", str(self.src_dir)]
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL") if k in os.environ}      # 不继承其他环境变量
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env=env, cwd=tempfile.gettempdir(), start_new_session=True, close_fds=True)
        except OSError:
            raise TranslatorError("TRANSLATOR_UNAVAILABLE")
        try:
            out, _ = p.communicate(payload, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            self._kill(p)
            raise TranslatorError("TRANSLATOR_UNAVAILABLE")
        except BaseException:
            self._kill(p)
            raise
        if p.returncode != 0 or len(out) > MAX_OUTPUT:
            raise TranslatorError("TRANSLATOR_UNAVAILABLE")
        try:
            res = json.loads(out.decode("utf-8"))
        except Exception:
            raise TranslatorError("TRANSLATOR_UNAVAILABLE")
        if not isinstance(res, dict) or res.get("ok") is not True:
            raise TranslatorError("TRANSLATOR_UNAVAILABLE")
        return res

    @staticmethod
    def _kill(p):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.communicate(timeout=5)
        except Exception:
            pass

    def validate(self, doc) -> dict:
        """返回 {"legal": bool, "errors": [{code, path, message}], "code": 拒绝码 | None}。"""
        with self._lock:
            self.calls += 1
        try:
            payload = json.dumps({"doc": doc}, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            return {"legal": False, "code": FALLBACK_CODE,
                    "errors": [{"code": FALLBACK_CODE, "path": "", "message": "document is not JSON serializable"}]}
        if len(payload) > MAX_PAYLOAD:
            return {"legal": False, "code": FALLBACK_CODE,
                    "errors": [{"code": FALLBACK_CODE, "path": "", "message": "document too large"}]}
        res = self._run(payload)
        mods, issues, rc = res.get("modules"), res.get("issues"), res.get("reject_codes")
        if (not isinstance(mods, list) or not set(mods) <= ALLOWED_STATARB_MODULES or res.get("denied") != 0
                or not isinstance(issues, list) or not isinstance(rc, list)):
            raise TranslatorError("TRANSLATOR_UNAVAILABLE")
        errors = []
        for i in issues:
            if not (isinstance(i, dict) and all(isinstance(i.get(k), str) for k in ("code", "path", "message"))):
                raise TranslatorError("TRANSLATOR_UNAVAILABLE")
            errors.append({"code": i["code"], "path": i["path"], "message": i["message"]})
        return {"legal": not errors, "errors": errors, "code": map_code(errors[0]["code"], rc) if errors else None}
