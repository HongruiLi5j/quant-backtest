"""主人通道：校验 Cloudflare Access 签发的 JWT，判断请求是不是站主本人。

只用标准库。任何一步出问题都返回 False（宁可进不去）。
三个条件同时满足才算主人：Host 等于配置的域名、JWT 有效（RS256、aud、iss、exp）、email 在允许列表。
"""
import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.request
from pathlib import Path

CONFIG_NAME = "owner_access.json"
CERT_TTL = 3600       # 公钥缓存 1 小时
CLOCK_SKEW = 60       # 允许 60 秒时钟误差
ADMIN_PREFIXES = ("/admin", "/admin.js", "/api/admin/")
# SHA-256 的 DigestInfo 前缀（PKCS#1 v1.5 签名里 hash 前面的固定字节）
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")

_cache = {"team": None, "keys": {}, "t": 0.0}
_lock = threading.Lock()



def load_config(root=None):
    """读 owner_access.json；文件不存在、格式不对、未启用、team_domain/aud 为空都返回 None。"""
    try:
        path = Path(root or Path(__file__).resolve().parent.parent) / CONFIG_NAME
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
            return None
        for k in ("host", "team_domain", "aud"):
            if not isinstance(cfg.get(k), str) or not cfg[k].strip():
                return None
        if not isinstance(cfg.get("emails"), list) or not cfg["emails"]:
            return None
        if not isinstance(cfg.get("admin_aud", ""), str):
            return None
        return cfg
    except Exception:
        return None


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _fetch_certs(team_domain: str) -> dict:
    """下载公钥，返回 {kid: (n, e)}；失败返回空字典。"""
    try:
        url = f"https://{team_domain}/cdn-cgi/access/certs"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        keys = {}
        for k in data.get("keys", []):
            if k.get("kty") == "RSA" and k.get("kid"):
                keys[k["kid"]] = (int.from_bytes(_b64d(k["n"]), "big"), int.from_bytes(_b64d(k["e"]), "big"))
        return keys
    except Exception:
        return {}


def get_keys(team_domain: str) -> dict:
    with _lock:
        now = time.time()
        if _cache["team"] == team_domain and _cache["keys"] and now - _cache["t"] < CERT_TTL:
            return _cache["keys"]
        keys = _fetch_certs(team_domain)
        if keys:
            _cache.update(team=team_domain, keys=keys, t=now)
        return keys


def verify(token, aud, cfg):
    """JWT 有效且邮箱在名单里，返回 claims 字典；否则返回 None。"""
    try:
        if not token or not aud:
            return None
        h64, p64, s64 = token.split(".")
        header = json.loads(_b64d(h64))
        if header.get("alg") != "RS256":
            return None
        key = get_keys(cfg["team_domain"]).get(header.get("kid"))
        if not key:
            return None
        n, e = key
        sig = _b64d(s64)
        k = (n.bit_length() + 7) // 8
        if len(sig) != k or int.from_bytes(sig, "big") >= n:
            return None
        em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
        digest = hashlib.sha256(f"{h64}.{p64}".encode("ascii")).digest()
        t = SHA256_PREFIX + digest
        expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
        if not hmac.compare_digest(em, expected):
            return None
        claims = json.loads(_b64d(p64))
        auds = claims.get("aud")
        auds = [auds] if isinstance(auds, str) else auds
        if not isinstance(auds, list) or aud not in auds:
            return None
        if claims.get("iss") != "https://" + cfg["team_domain"]:
            return None
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp + CLOCK_SKEW <= time.time():
            return None
        email = claims.get("email")
        if not isinstance(email, str) or email.lower() not in {str(x).lower() for x in cfg["emails"]}:
            return None
        return claims
    except Exception:
        return None


def is_owner(headers, path, cfg) -> bool:
    try:
        if not cfg or (headers.get("Host") or "").lower() != cfg["host"].lower():
            return False
        is_admin = any(path == p.rstrip("/") or path.startswith(p) for p in ADMIN_PREFIXES)
        aud = cfg.get("admin_aud") if is_admin else cfg.get("aud")
        return verify(headers.get("Cf-Access-Jwt-Assertion"), aud, cfg) is not None
    except Exception:
        return False
