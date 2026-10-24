"""受限的追加式日志：只接收业务字段，不接收 HTTP 请求或环境。

不自动清理日志。写入失败向调用者报错，不能把没有留下记录的结果当作已确认。
"""
import datetime
import fcntl
import ipaddress
import json
import os
import re

from . import paths

_MODEL_FIELDS = ("version", "adapter_path", "adapter_sha256", "base_model_sha256", "schema_sha256")
_PRIVATE = re.compile(r"authorization|cookie|password|secret|token|client_ip|headers", re.I)
_CREDENTIAL = re.compile(r"(?i)\b(?:authorization|cookie|password|secret|token|api[_-]?key)\b[^\n,;]*")
_IP = re.compile(r"(?<![\w])(?:\d{1,3}\.){3}\d{1,3}(?![\w])|(?<![\w])(?:[0-9a-fA-F]*:){2,}[0-9a-fA-F:.]*(?![\w])")


def _redact_ip(match):
    try:
        ipaddress.ip_address(match.group())
        return "[redacted]"
    except ValueError:
        return match.group()


def _safe(value):
    """防止自由文本意外夹带凭据或地址；正常业务字段保持结构。"""
    if isinstance(value, str):
        return _IP.sub(_redact_ip, _CREDENTIAL.sub("[redacted]", value))
    if isinstance(value, dict):
        return {_safe(k): _safe(v) for k, v in value.items() if isinstance(k, str)
                and (not _PRIVATE.search(k) or (k in ("input_tokens", "output_tokens") and type(v) is int))}
    if isinstance(value, list):
        return [_safe(v) for v in value]
    return value


class LogStore:
    def __init__(self, root=None, *, now=None):
        self.directory = paths.state_dir(root)
        self.root = root
        self._now = now or (lambda: datetime.datetime.now().astimezone())

    def _append(self, path, event):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        payload = (json.dumps(_safe(event), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), 0o600)
            os.fsync(stream.fileno())
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def translate(self, *, request_id, backend_id, model, idea, precheck, raw_output,
                  parse_mode, config, gate, flags, status, latency_ms, usage=None):
        now = self._now()
        self._append(paths.log_path(now.strftime("%Y%m"), self.root), {
            "event": "translate", "ts": now.isoformat(), "request_id": request_id,
            "backend_id": backend_id, "model": {k: model.get(k) for k in _MODEL_FIELDS},
            "idea": idea, "precheck": precheck, "raw_output": _safe(raw_output)[:4000],
            "parse_mode": parse_mode, "config": config, "gate": gate,
            "flags": flags, "status": status, "latency_ms": latency_ms,
            **({"usage": usage} if usage is not None else {})})

    def confirm(self, *, request_id, ack_flags):
        now = self._now()
        self._append(paths.log_path(now.strftime("%Y%m"), self.root), {
            "event": "confirm", "ts": now.isoformat(), "request_id": request_id, "ack_flags": ack_flags is True})

    def feedback(self, *, request_id, note):
        self._append(self.directory / "feedback.jsonl", {
            "event": "feedback", "ts": self._now().isoformat(), "request_id": request_id,
            "verdict": "wrong", "note": _safe(note)[:200], "status": "pending_review"})

    def contains(self, request_id):
        """只认翻译事件；确认和反馈不能凭空建立一个有效 request_id。"""
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{16}", request_id):
            return False
        for path in sorted(self.directory.glob("log_[0-9][0-9][0-9][0-9][0-9][0-9].jsonl")):
            with open(path, "r", encoding="utf-8") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
                for line in stream:
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(row, dict) and row.get("event") == "translate" and row.get("request_id") == request_id:
                        return True
        return False
