"""新界面「后台」按钮的本机入口：/admin?enter=1&to=...  python -m pytest tests/test_admin_enter_n17.py
不启动服务，也不导入 app.py：app 在导入时会读取 .secret 里的真实令牌，测试不能碰。
做法同 tests/test_ui_static.py：用 AST 只摘出 Handler 里要测的方法和几个常量，放进带假令牌的命名空间；
审计 / 登记换成空函数（不改真实的 src.admin 模块）。src.admin 导入时只用标准库、不读密钥，所以真实的
is_local_request / make_session 可以直接用。"""
import ast
import sys
import types
from email.message import Message
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import admin as real_admin  # noqa: E402

TREE = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
METHODS = {n.name: n for n in HANDLER.body if isinstance(n, ast.FunctionDef)}
NAMES = ("_admin_get", "_wants_new_ui")
CONSTS = ("ADMIN_COOKIE", "ADMIN_ENTER_TARGETS", "ALLOWED_HOSTS")      # 都是 app.py 顶层的普通赋值


def _noop(*a, **k):
    return None


FLAGS = {"new_ui_home": True, "admin_quick_enter": True}      # 后台两个开关的假设置（默认都开，和 admin_settings.DEFAULTS 一致）


def _build():
    consts = [n for n in TREE.body if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id in CONSTS for t in n.targets)]
    assert len(consts) == len(CONSTS), "app.py 顶层常量写法变了，测试要跟着改"
    cls = ast.ClassDef(name="Handler", bases=[], keywords=[], decorator_list=[],
                       body=[METHODS[n] for n in NAMES])
    admin = types.SimpleNamespace(
        is_local_request=real_admin.is_local_request, make_session=real_admin.make_session,
        SESSION_SECONDS=real_admin.SESSION_SECONDS,
        audit=_noop, audit_blocked=_noop, set_actor_from_request=_noop)
    ns = dict(urlparse=urlparse, SERVER_PORT=8765, ADMIN_TOKEN="synthetic-admin-token",
              UI_DIR=ROOT / "dashboard" / "ui", admin=admin,
              admin_settings=types.SimpleNamespace(register_session=_noop,
                                                   new_ui_home_on=lambda: FLAGS["new_ui_home"],
                                                   admin_quick_enter_on=lambda: FLAGS["admin_quick_enter"]))
    exec(compile(ast.fix_missing_locations(ast.Module(body=consts + [cls], type_ignores=[])), "<handler>", "exec"), ns)
    return ns


NS = _build()
Handler = NS["Handler"]


def _call(path, host=None, extra=None, owner=False, site="same-origin"):
    """site 是 Sec-Fetch-Site 的值；None 表示请求里根本没有这个头。"""
    h = Handler.__new__(Handler)
    msg = Message()
    msg["Host"] = host or next(iter(sorted(NS["ALLOWED_HOSTS"])))
    if site is not None:
        msg["Sec-Fetch-Site"] = site
    for k, v in (extra or {}).items():
        msg[k] = v
    h.headers = msg
    h.path = path
    h.client_address = ("127.0.0.1", 1)
    sent = {}
    h._send = lambda code, body, ctype, headers=None, **kw: sent.update(code=code, headers=headers or {})
    h._owner = lambda: owner
    h._admin_authed = lambda: False       # 开关关掉时请求落到普通登录检查，没有后台 cookie 就是 401
    h._local_or_owner = lambda: owner or NS["admin"].is_local_request(msg, NS["ALLOWED_HOSTS"])
    u = urlparse(path)
    h._admin_get(u, {k: v[0] for k, v in parse_qs(u.query).items()})
    return sent


def test_local_enter_sets_cookie_and_redirects():
    r = _call("/admin?enter=1&to=%2Fui%2Fw%2Fadmin.html")
    assert r["code"] == 303 and r["headers"]["Location"] == "/ui/w/admin.html"
    assert r["headers"]["Set-Cookie"].startswith(NS["ADMIN_COOKIE"] + "=") and "HttpOnly" in r["headers"]["Set-Cookie"]


def test_unknown_target_falls_back_to_admin():
    r = _call("/admin?enter=1&to=https%3A%2F%2Fevil.example%2F")
    assert r["headers"]["Location"] == "/admin"


def test_cross_site_request_refused():
    r = _call("/admin?enter=1", site="cross-site")
    assert r["code"] == 403 and "Set-Cookie" not in r["headers"]


def test_same_site_request_refused():
    r = _call("/admin?enter=1", site="same-site")
    assert r["code"] == 403 and "Set-Cookie" not in r["headers"]


def test_address_bar_entry_allowed():
    r = _call("/admin?enter=1", site="none")                       # 在地址栏直接输入 / 点书签
    assert r["code"] == 303 and "Set-Cookie" in r["headers"]


def test_missing_fetch_metadata_refused():
    """F1：请求里没有 Sec-Fetch-Site 不能当同源；带外站 Origin / Referer 也一样拒绝。"""
    for extra in ({}, {"Origin": "https://evil.example"}, {"Referer": "https://evil.example/x"}):
        r = _call("/admin?enter=1", site=None, extra=extra)
        assert r["code"] == 403 and "Set-Cookie" not in r["headers"], extra


def test_odd_fetch_metadata_values_refused():
    for site in ("", "null", "SAME-ORIGIN", "same-origin, cross-site", "cross-origin", "*"):
        r = _call("/admin?enter=1", site=site)
        assert r["code"] == 403 and "Set-Cookie" not in r["headers"], repr(site)


def test_owner_without_metadata_also_refused():
    r = _call("/admin?enter=1&to=%2Fui%2Fw%2Fadmin.html", host="me.accessiblequant.com", owner=True, site=None)
    assert r["code"] == 403


def test_quick_enter_switch_off_never_issues_cookie():
    FLAGS["admin_quick_enter"] = False
    try:
        r = _call("/admin?enter=1&to=%2Fui%2Fw%2Fadmin.html")
        assert r["code"] == 401 and "Set-Cookie" not in r["headers"]       # 落到普通登录检查，要管理令牌
    finally:
        FLAGS["admin_quick_enter"] = True


def test_tunnel_request_not_logged_in():
    r = _call("/admin?enter=1", host="abc.trycloudflare.com")
    assert r["code"] == 404


def test_proxy_header_request_not_logged_in():
    r = _call("/admin?enter=1", extra={"Cf-Connecting-Ip": "1.2.3.4"})
    assert r["code"] == 404


def test_owner_is_just_redirected():
    r = _call("/admin?enter=1&to=%2Fui%2Fw%2Fadmin.html", host="me.accessiblequant.com", owner=True)
    assert r["code"] == 303 and "Set-Cookie" not in r["headers"]


def _wants(q, referer=None):
    h = Handler.__new__(Handler)
    msg = Message()
    if referer:
        msg["Referer"] = referer
    msg["Host"] = "127.0.0.1:8765"
    h.headers = msg
    return h._wants_new_ui(q)


def test_home_goes_to_new_ui_by_default():
    assert _wants({}) is True


def test_classic_query_keeps_old_ui():
    assert _wants({"classic": "1"}) is False


def test_link_from_new_ui_keeps_old_ui():
    assert _wants({}, referer="http://127.0.0.1:8765/ui/w/today.html") is False
    assert _wants({}, referer="http://127.0.0.1:8765/ui/") is False


def test_other_referrer_goes_to_new_ui():
    assert _wants({}, referer="https://example.com/ui/x") is True


def test_home_switch_off_keeps_old_ui():
    FLAGS["new_ui_home"] = False
    try:
        assert _wants({}) is False
        assert _wants({}, referer="https://example.com/ui/x") is False
    finally:
        FLAGS["new_ui_home"] = True


def test_missing_ui_dir_keeps_old_ui():
    old = NS["UI_DIR"]
    NS["UI_DIR"] = ROOT / "no_such_ui_dir"
    try:
        assert _wants({}) is False
    finally:
        NS["UI_DIR"] = old
