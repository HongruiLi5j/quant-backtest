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
# 不导入 app.py：app 在导入时会读取 .secret 里的真实令牌，测试不能碰。
# 做法同 tests/test_ui_static.py：用 AST 只摘出要测的方法（及它们互相调用的方法）和几个常量，
# 放进带假令牌的命名空间。会碰数据库 / 密码文件的模块换成「一碰就报错」的替身；
# 审计相关函数换成空函数，不会写真实的 data/admin_audit.jsonl。
import ast  # noqa: E402
import gzip  # noqa: E402
import hmac  # noqa: E402
import threading  # noqa: E402
import types  # noqa: E402
from http.cookies import SimpleCookie  # noqa: E402
from http.server import BaseHTTPRequestHandler  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

from src import admin as _real_admin  # noqa: E402   导入时只用标准库，不读密钥
from src import personal_mode as _personal_mode  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_TREE = ast.parse((_ROOT / "app.py").read_text(encoding="utf-8"))
_HANDLER_NODE = next(n for n in _TREE.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
_METHODS = {n.name: n for n in _HANDLER_NODE.body if isinstance(n, ast.FunctionDef)}
_SEEDS = ("_external", "_https", "_owner", "_local_or_owner", "_authed", "_admin_authed",
          "_personal_blocked", "_share_gate")
_CONSTS = ("DASH", "COOKIE", "MEMBER_COOKIE", "ADMIN_COOKIE", "ALLOWED_HOSTS", "COMPRESSIBLE")
_FUNCS = ("_gzip_cached",)


class _Forbidden:
    """替身：这个测试不该用到的模块。一碰就报错，免得悄悄读写真实数据库或密码文件。"""

    def __init__(self, name):
        self._name = name

    def __getattr__(self, attr):
        raise AssertionError(f"测试不应该调用 {self._name}.{attr}（会碰真实状态）")


def _method_closure():
    seen, todo = set(), list(_SEEDS)
    while todo:
        n = todo.pop()
        if n in seen or n not in _METHODS:
            continue
        seen.add(n)
        for x in ast.walk(_METHODS[n]):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self":
                todo.append(x.attr)
    return sorted(seen)


def _build_app():
    top = [n for n in _TREE.body
           if (isinstance(n, ast.FunctionDef) and n.name in _FUNCS)
           or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in _CONSTS for t in n.targets))]
    cls = ast.ClassDef(name="Handler", bases=[ast.Name(id="BaseHTTPRequestHandler", ctx=ast.Load())],
                       keywords=[], decorator_list=[], body=[_METHODS[n] for n in _method_closure()])
    admin = types.SimpleNamespace(**{k: getattr(_real_admin, k) for k in dir(_real_admin) if not k.startswith("__")})
    admin.audit = admin.audit_blocked = admin.set_actor_from_request = lambda *a, **k: None
    ns = dict(BaseHTTPRequestHandler=BaseHTTPRequestHandler, urlparse=urlparse, SimpleCookie=SimpleCookie,
              hashlib=hashlib, hmac=hmac, json=json, gzip=gzip, time=time, SERVER_PORT=8765, ROOT=_ROOT,
              TOKEN="synthetic-token", ADMIN_TOKEN="synthetic-admin-token", _gz_cache={},
              _trade_session_lock=threading.Lock(), owner_access=oa, personal_mode=_personal_mode, admin=admin,
              members=_Forbidden("members"), admin_settings=_Forbidden("admin_settings"),
              sharepass=types.SimpleNamespace(is_set=lambda: False, check_cookie=lambda v: False))     # 真实的会读 .secret/share_password
    exec(compile(ast.fix_missing_locations(ast.Module(body=top + [cls], type_ignores=[])), "<handler>", "exec"), ns)
    return types.SimpleNamespace(**ns)


APP = _build_app()


def _handler(headers):
    app = APP
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


# ---------- 个人模式：后台三类路径对本机 / 主人放行，其余隐藏路由不变
ADMIN_PATHS = ("/admin", "/admin.js", "/api/admin/status", "/api/admin/action")


def _blocked(monkeypatch, host, path, owner, command="GET"):
    """在个人模式开启的情况下，返回 (_personal_blocked() 的结果, 回复的状态码或 None)。"""
    app, h = _handler({"Host": host})
    h.path, h.command = path, command
    sent = []
    h._send = lambda code, *a, **k: sent.append(code)
    monkeypatch.setattr(app.personal_mode, "is_enabled", lambda: True)
    monkeypatch.setattr(app.owner_access, "load_config", lambda root=None: {"host": HOST})
    monkeypatch.setattr(app.owner_access, "is_owner", lambda headers, p, cfg: owner and headers.get("Host") == HOST)
    return h._personal_blocked(), (sent[0] if sent else None), h


def test_personal_mode_owner_passes_admin_paths(monkeypatch):
    for path in ADMIN_PATHS:
        assert _blocked(monkeypatch, HOST, path, True)[:2] == (False, None), path


def test_personal_mode_local_passes_admin_paths_but_still_needs_token(monkeypatch):
    for path in ADMIN_PATHS:
        blocked, code, h = _blocked(monkeypatch, "127.0.0.1:8765", path, False)
        assert (blocked, code) == (False, None), path
    # 放行只是不再被隐藏；后台自己的登录检查不变：本机没有管理令牌 cookie 就是未登录
    assert h._admin_authed() is False


def test_personal_mode_share_link_and_non_owner_still_404(monkeypatch):
    for path in ADMIN_PATHS:
        assert _blocked(monkeypatch, "x.trycloudflare.com", path, False)[:2] == (True, 404), path
        assert _blocked(monkeypatch, "x.trycloudflare.com", path, True)[:2] == (True, 404), path   # 分享链接域名不可能是主人
        assert _blocked(monkeypatch, HOST, path, False)[:2] == (True, 404), path                  # 主人域名但凭证没通过
    # 带代理 / 隧道头的“本机”请求也不算本机
    app, h = _handler({"Host": "127.0.0.1:8765", "cf-ray": "abc-LAX"})
    h.path, h.command = "/admin", "GET"
    sent = []
    h._send = lambda code, *a, **k: sent.append(code)
    monkeypatch.setattr(app.personal_mode, "is_enabled", lambda: True)
    monkeypatch.setattr(app.owner_access, "load_config", lambda root=None: None)
    assert h._personal_blocked() is True and sent == [404]


def test_personal_mode_other_hidden_routes_unchanged_even_for_owner(monkeypatch):
    for path in ("/api/sim", "/api/share", "/member", "/api/member/", "/api/code/x", "/api/ai/status",
                 "/api/offline", "/engines.js", "/admin/", "/api/eng/scan"):
        assert _blocked(monkeypatch, HOST, path, True)[:2] == (True, 404), path
        assert _blocked(monkeypatch, "127.0.0.1:8765", path, False)[:2] == (True, 404), path


def test_personal_mode_off_does_not_block(monkeypatch):
    app, h = _handler({"Host": "x.trycloudflare.com"})
    h.path, h.command = "/admin", "GET"
    monkeypatch.setattr(app.personal_mode, "is_enabled", lambda: False)
    assert h._personal_blocked() is False


# ---------- 外网没有凭证：连 /api/health 这样的普通路径也被挡（个人模式下一律 404）
def test_external_without_credentials_blocked_on_api_health(monkeypatch):
    from urllib.parse import urlparse
    app, h = _handler({"Host": HOST, "cf-ray": "abc-LAX"})   # 外网请求，没有 Cf-Access 凭证
    h.path, h.command = "/api/health", "GET"
    sent = []
    h._send = lambda code, *a, **k: sent.append(code)
    h._personal_mode = True
    monkeypatch.setattr(app.owner_access, "load_config", lambda root=None: CFG)   # 用真实的 is_owner 校验
    assert h._owner() is False and h._external() is True
    assert h._share_gate(urlparse(h.path)) is True and sent == [404]


def test_non_owner_email_credential_rejected_for_every_path():
    for path in ("/", "/api/health", "/admin", "/admin.js", "/api/admin/status"):
        for aud in (AUD, ADMIN_AUD):
            assert not oa.is_owner(hdr(make_jwt(aud=aud, email="stranger@example.com")), path, CFG), (path, aud)
