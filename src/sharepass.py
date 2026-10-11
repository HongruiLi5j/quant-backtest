"""分享链接的访问密码（只用标准库）。

只管"通过外网分享链接来的访客"：设了密码后，他们要先输入密码才能看到网站；本机（127.0.0.1）不受影响。
密码用 scrypt 加盐哈希存在 .secret/share_password（权限 600），不存明文。
通过验证后发一个签名 cookie（7 天）；改密码或清除密码后，旧 cookie 立刻失效。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / ".secret" / "share_password"
COOKIE_SECONDS = 7 * 86400


def _hash(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _read():
    try:
        raw = FILE.read_bytes()
        return raw[:16], raw[16:48]
    except OSError:
        return None


def is_set() -> bool:
    r = _read()
    return bool(r and len(r[0]) == 16 and len(r[1]) == 32)


def set_password(pw: str):
    if not (6 <= len(pw or "") <= 64):
        raise ValueError("密码需要 6–64 位")
    salt = secrets.token_bytes(16)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(salt + _hash(pw, salt))
    os.replace(tmp, FILE)


def clear():
    try:
        FILE.unlink()
    except OSError:
        pass


def verify(pw: str) -> bool:
    r = _read()
    if not r:
        return True
    return hmac.compare_digest(_hash(pw or "", r[0]), r[1])


def make_cookie() -> str:
    r = _read()
    ts = str(int(time.time()))
    return ts + "." + hmac.new(r[1], ts.encode(), hashlib.sha256).hexdigest()


def check_cookie(v: str | None) -> bool:
    r = _read()
    if not r:
        return True            # 没设密码：不拦
    try:
        ts, sig = (v or "").split(".", 1)
        ok = hmac.compare_digest(sig, hmac.new(r[1], ts.encode(), hashlib.sha256).hexdigest())
        return ok and time.time() - int(ts) < COOKIE_SECONDS
    except ValueError:
        return False
