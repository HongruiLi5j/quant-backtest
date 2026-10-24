"""主人通道（src/owner_access.py）的模块级测试：用写死的小 RSA 测试密钥签 JWT，公钥下载全部 mock，不联网。"""
import base64
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import owner_access as oa  # noqa: E402

# 仅用于测试的 1024 位 RSA 密钥（不是任何真实密钥）
N = 124209778198467125635416287608684278039264366994779047891563326963205640517148873291252701120843154461154833439303030053123598934980900579140603372671444716384994776212170886389220096893712980950899596932781584803126666017562977180993369258785869037088873003871022390984026554293873908556464363388470056340229
D = 29787831056430837749848754022089671451899202832856894513201715212486123136671328280492221241684725554510742285510867499960993094909666515134242690514931966010473134481827026195005094184889587529938904649513564287760805959187155729082562349860741699031413943665220638308622587936650834904481980099472343273217
E = 65537
KID = "test-kid"
TEAM = "team.cloudflareaccess.com"
HOST = "me.accessiblequant.com"
AUD, ADMIN_AUD = "aud-normal", "aud-admin"
EMAIL = "ctawcac@gmail.com"

CFG = {"enabled": True, "host": HOST, "emails": [EMAIL], "team_domain": TEAM, "aud": AUD, "admin_aud": ADMIN_AUD}


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwt(alg="RS256", kid=KID, aud=AUD, iss="https://" + TEAM, email=EMAIL, exp=None, aud_str=False):
    header = b64(json.dumps({"alg": alg, "kid": kid}).encode())
    claims = {"aud": aud if aud_str else [aud], "iss": iss, "email": email,
              "exp": int(time.time()) + 600 if exp is None else exp}
    payload = b64(json.dumps(claims).encode())
    k = (N.bit_length() + 7) // 8
    t = oa.SHA256_PREFIX + hashlib.sha256(f"{header}.{payload}".encode()).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    sig = pow(int.from_bytes(em, "big"), D, N).to_bytes(k, "big")
    return f"{header}.{payload}.{b64(sig)}"


def hdr(token=None, host=HOST):
    h = {"Host": host}
    if token is not None:
        h["Cf-Access-Jwt-Assertion"] = token
    return h


@pytest.fixture(autouse=True)
def fake_certs(monkeypatch):
    oa._cache.update(team=None, keys={}, t=0.0)
    monkeypatch.setattr(oa, "_fetch_certs", lambda team: {KID: (N, E)})
    yield
    oa._cache.update(team=None, keys={}, t=0.0)


def test_pass_normal_path():
    assert oa.is_owner(hdr(make_jwt()), "/", CFG)
    assert oa.is_owner(hdr(make_jwt()), "/api/summary", CFG)


def test_email_case_insensitive():
    assert oa.is_owner(hdr(make_jwt(email=EMAIL.upper())), "/", CFG)


def test_aud_as_string():
    # aud 也可能是单个字符串
    assert oa.verify(make_jwt(aud_str=True), AUD, CFG)


def test_admin_needs_admin_aud():
    for path in ("/admin", "/admin.js", "/api/admin/status"):
        assert oa.is_owner(hdr(make_jwt(aud=ADMIN_AUD)), path, CFG)
        assert not oa.is_owner(hdr(make_jwt(aud=AUD)), path, CFG)
    # 管理员凭证不能用于普通路径
    assert not oa.is_owner(hdr(make_jwt(aud=ADMIN_AUD)), "/", CFG)


def test_reject_missing_header():
    assert not oa.is_owner(hdr(), "/", CFG)


def test_reject_expired():
    assert not oa.is_owner(hdr(make_jwt(exp=int(time.time()) - 3600)), "/", CFG)


def test_expiry_skew_allowed():
    assert oa.is_owner(hdr(make_jwt(exp=int(time.time()) - 30)), "/", CFG)


def test_reject_tampered_signature():
    h, p, s = make_jwt().split(".")
    bad = bytearray(oa._b64d(s))
    bad[-1] ^= 1
    assert not oa.is_owner(hdr(f"{h}.{p}.{b64(bytes(bad))}"), "/", CFG)


def test_reject_tampered_payload():
    h, p, s = make_jwt(email="evil@x.com").split(".")
    good_p = make_jwt().split(".")[1]
    assert not oa.is_owner(hdr(f"{h}.{good_p}.{s}"), "/", CFG)


def test_reject_alg_not_rs256():
    assert not oa.is_owner(hdr(make_jwt(alg="HS256")), "/", CFG)
    assert not oa.is_owner(hdr(make_jwt(alg="none")), "/", CFG)


def test_reject_wrong_kid():
    assert not oa.is_owner(hdr(make_jwt(kid="other")), "/", CFG)


def test_reject_wrong_aud():
    assert not oa.is_owner(hdr(make_jwt(aud="someone-else")), "/", CFG)


def test_reject_wrong_iss():
    assert not oa.is_owner(hdr(make_jwt(iss="https://evil.cloudflareaccess.com")), "/", CFG)


def test_reject_email_not_listed():
    assert not oa.is_owner(hdr(make_jwt(email="other@gmail.com")), "/", CFG)


def test_reject_wrong_host():
    assert not oa.is_owner(hdr(make_jwt(), host="x.trycloudflare.com"), "/", CFG)
    assert not oa.is_owner(hdr(make_jwt(), host="127.0.0.1:8765"), "/", CFG)


def test_reject_garbage_token():
    for t in ("", "a.b", "a.b.c", "...", "x" * 50):
        assert not oa.is_owner(hdr(t), "/", CFG)


def test_reject_when_certs_download_fails(monkeypatch):
    monkeypatch.setattr(oa, "_fetch_certs", lambda team: {})
    assert not oa.is_owner(hdr(make_jwt()), "/", CFG)


def test_is_owner_none_cfg():
    assert not oa.is_owner(hdr(make_jwt()), "/", None)


def test_certs_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(oa, "_fetch_certs", lambda team: calls.append(1) or {KID: (N, E)})
    for _ in range(3):
        assert oa.is_owner(hdr(make_jwt()), "/", CFG)
    assert len(calls) == 1


def write_cfg(tmp_path, **over):
    cfg = dict(CFG, **over)
    (tmp_path / "owner_access.json").write_text(json.dumps(cfg), encoding="utf-8")


def test_load_config_ok(tmp_path):
    write_cfg(tmp_path)
    assert oa.load_config(tmp_path)["host"] == HOST


def test_load_config_missing_file(tmp_path):
    assert oa.load_config(tmp_path) is None


def test_load_config_disabled(tmp_path):
    write_cfg(tmp_path, enabled=False)
    assert oa.load_config(tmp_path) is None


def test_load_config_empty_fields(tmp_path):
    write_cfg(tmp_path, team_domain="")
    assert oa.load_config(tmp_path) is None
    write_cfg(tmp_path, aud="")
    assert oa.load_config(tmp_path) is None


def test_load_config_bad_json(tmp_path):
    (tmp_path / "owner_access.json").write_text("{not json", encoding="utf-8")
    assert oa.load_config(tmp_path) is None


def test_shipped_config_is_sane():
    # 项目里的 owner_access.json：站主已启用，若启用则关键字段必须齐全；关闭时 load_config 返回 None
    cfg = oa.load_config(Path(__file__).resolve().parent.parent)
    if cfg is not None:
        assert cfg["host"] == "me.accessiblequant.com"
        assert cfg["team_domain"].endswith(".cloudflareaccess.com") and "://" not in cfg["team_domain"]
        assert cfg["aud"] and cfg["admin_aud"] and cfg["aud"] != cfg["admin_aud"]
        assert cfg["emails"]


# ---------- app.py 级测试：直接构造 Handler，不起服务
def _handler(headers):
    import app
    h = app.Handler.__new__(app.Handler)
    h.headers = headers
    h.path = "/api/status"
    return app, h


def test_app_external_true_for_cf_ray_without_config(monkeypatch):
    app, h = _handler({"Host": "127.0.0.1:8765", "cf-ray": "abc-LAX"})
    monkeypatch.setattr(app.owner_access, "load_config", lambda root=None: None)
    assert h._external() is True


def test_app_external_false_when_owner(monkeypatch):
    app, h = _handler({"Host": HOST, "cf-ray": "abc-LAX"})
    monkeypatch.setattr(app.owner_access, "load_config", lambda root=None: {"host": HOST})
    monkeypatch.setattr(app.owner_access, "is_owner", lambda headers, path, cfg: True)
    assert h._external() is False
    assert h._https() is True


def test_app_trycloudflare_never_owner(monkeypatch):
    app, h = _handler({"Host": "x.trycloudflare.com"})
    monkeypatch.setattr(app.owner_access, "load_config", lambda root=None: {"host": HOST})
    monkeypatch.setattr(app.owner_access, "is_owner", lambda headers, path, cfg: False)
    assert h._external() is True


# ---------- admin_aud 为空 / /api/admin/ 只认 admin_aud
def test_empty_admin_aud_rejects_admin_paths():
    for admin_aud in ("", None):
        cfg = dict(CFG, admin_aud=admin_aud)
        for aud in (AUD, ADMIN_AUD, ""):
            for path in ("/admin", "/admin.js", "/api/admin/action", "/api/admin/status"):
                assert not oa.is_owner(hdr(make_jwt(aud=aud)), path, cfg), (admin_aud, aud, path)
    # 普通路径不受影响
    assert oa.is_owner(hdr(make_jwt()), "/", dict(CFG, admin_aud=""))


def test_api_admin_only_accepts_admin_aud():
    for path in ("/api/admin/action", "/api/admin/status"):
        assert not oa.is_owner(hdr(make_jwt(aud=AUD)), path, CFG)
        assert oa.is_owner(hdr(make_jwt(aud=ADMIN_AUD)), path, CFG)


# ---------- _authed：主人通道通过算已登录，否则令牌规则不变
def test_app_authed_owner_without_token(monkeypatch):
    app, h = _handler({"Host": HOST})
    monkeypatch.setattr(app.owner_access, "load_config", lambda root=None: {"host": HOST})
    monkeypatch.setattr(app.owner_access, "is_owner", lambda headers, path, cfg: True)
    assert h._authed() is True


def test_app_authed_token_rules_unchanged_when_not_owner(monkeypatch):
    app, h = _handler({"Host": "127.0.0.1:8765"})
    monkeypatch.setattr(app.owner_access, "load_config", lambda root=None: None)
    assert h._authed() is False
    h.headers = {"Host": "127.0.0.1:8765", "Cookie": f"{app.COOKIE}=wrong"}
    assert h._authed() is False
    h.headers = {"Host": "127.0.0.1:8765", "Cookie": f"{app.COOKIE}={app.TOKEN}"}
    assert h._authed() is True
    # 配置开启但凭证无效：同样要令牌
    h2 = _handler({"Host": HOST})[1]
    monkeypatch.setattr(app.owner_access, "load_config", lambda root=None: {"host": HOST})
    monkeypatch.setattr(app.owner_access, "is_owner", lambda headers, path, cfg: False)
    assert h2._authed() is False


# ---------- 同一个 Handler 复用（keep-alive）时，每个请求必须重新判断
def test_app_owner_recomputed_per_request_on_reused_handler(monkeypatch):
    """先用普通 aud 访问普通路径（通过），再在同一个 Handler 上访问 /admin：必须重新用 admin_aud 校验。"""
    monkeypatch.setattr(oa, "_fetch_certs", lambda team: {KID: (N, E)})
    monkeypatch.setattr(oa, "_cache", {"team": None, "keys": {}, "t": 0.0})
    app, h = _handler({"Host": HOST, **hdr(make_jwt(aud=AUD))})
    monkeypatch.setattr(app.owner_access, "load_config", lambda root=None: CFG)
    h.path = "/api/status"
    assert h._owner() is True
    # 同一个 Handler 对象处理下一个请求：新的 headers 对象，路径是 /admin，凭证仍是普通 aud
    h.headers = {"Host": HOST, **hdr(make_jwt(aud=AUD))}
    h.path = "/admin"
    assert h._owner() is False
    # 再换成 admin_aud 的凭证访问 /api/admin/：通过
    h.headers = {"Host": HOST, **hdr(make_jwt(aud=ADMIN_AUD))}
    h.path = "/api/admin/status"
    assert h._owner() is True
    # 第一次失败后，下一个请求带对的凭证不能一直失败
    h.headers = {"Host": HOST}
    h.path = "/"
    assert h._owner() is False
    h.headers = {"Host": HOST, **hdr(make_jwt(aud=AUD))}
    assert h._owner() is True


def test_app_owner_cache_same_request_computed_once(monkeypatch):
    app, h = _handler({"Host": HOST})
    calls = []
    monkeypatch.setattr(app.owner_access, "load_config", lambda root=None: {"host": HOST})
    monkeypatch.setattr(app.owner_access, "is_owner", lambda headers, path, cfg: calls.append(1) or True)
    assert h._owner() and h._owner() and h._local_or_owner()
    assert len(calls) == 1
