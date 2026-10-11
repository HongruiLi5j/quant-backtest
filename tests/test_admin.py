"""后台（src/admin.py）的安全和逻辑检查：python tests/test_admin.py  （只用标准库，不需要 pandas）"""
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import admin  # noqa: E402

HOSTS = {"127.0.0.1:8765", "localhost:8765"}


def fresh_root():
    root = Path(tempfile.mkdtemp())
    for d in ("data/daily", "logs", "export", ".secret", "dashboard", "src"):
        (root / d).mkdir(parents=True)
    (root / "config.py").write_text("X = 1\n")
    admin.set_root(root)
    admin._bad_logins.clear()
    return root


def test_local_request_rules():
    ok = {"Host": "127.0.0.1:8765", "User-Agent": "x"}
    assert admin.is_local_request(ok, HOSTS)
    assert admin.is_local_request({"Host": "localhost:8765"}, HOSTS)
    assert not admin.is_local_request({"Host": "evil.trycloudflare.com"}, HOSTS)          # 分享链接的 Host
    assert not admin.is_local_request({"Host": "127.0.0.1:9999"}, HOSTS)
    assert not admin.is_local_request({}, HOSTS)
    for h in ("CF-Connecting-IP", "cf-ray", "X-Forwarded-For", "X-Real-IP", "Forwarded", "Via"):   # 就算 Host 伪装成本机，隧道头也会被拒
        assert not admin.is_local_request({"Host": "127.0.0.1:8765", h: "1.2.3.4"}, HOSTS), h


def test_origin_check():
    assert admin.origin_ok({}, HOSTS)
    assert admin.origin_ok({"Origin": "http://127.0.0.1:8765"}, HOSTS)
    assert not admin.origin_ok({"Origin": "http://evil.com"}, HOSTS)
    assert not admin.origin_ok({"Origin": "https://127.0.0.1:8765"}, HOSTS)
    assert not admin.origin_ok({"Origin": "http://127.0.0.1:8765.evil.com"}, HOSTS)


def test_session_sign_expire_tamper():
    tok = "secret-token"
    s = admin.make_session(tok, now=1000)
    assert admin.check_session(s, tok, now=1000 + 60)
    assert not admin.check_session(s, tok, now=1000 + admin.SESSION_SECONDS + 1)        # 过期
    assert not admin.check_session(s, "other-token", now=1100)                           # 令牌不同
    ts, sig = s.split(".")
    assert not admin.check_session(f"{int(ts) + 5000}.{sig}", tok, now=1100)             # 改时间戳
    assert not admin.check_session(admin.make_session(tok, now=5000), tok, now=1000)     # 来自未来
    for bad in ("", "abc", "1.2.3", None):
        assert not admin.check_session(bad, tok, now=1100)


def test_login_rate_limit():
    fresh_root()
    for i in range(4):
        admin.record_bad_login(now=1000 + i)
    assert not admin.login_blocked(now=1010)
    admin.record_bad_login(now=1005)
    assert admin.login_blocked(now=1010)
    assert not admin.login_blocked(now=1010 + 700)       # 10 分钟后解除


def test_tokens_are_separate_and_private():
    root = fresh_root()
    (root / ".secret" / "token").write_text("main-token")
    a = admin.get_admin_token()
    assert a != "main-token" and len(a) >= 40
    assert admin.get_admin_token() == a                   # 重复读取不变
    assert oct((root / ".secret" / "admin_token").stat().st_mode & 0o777) == "0o600"
    b = admin.rotate_admin_token()
    assert b != a and admin.get_admin_token() == b
    m = admin.rotate_main_token()
    assert m != "main-token" and (root / ".secret" / "token").read_text() == m
    assert oct((root / ".secret" / "token").stat().st_mode & 0o777) == "0o600"


def test_confirm_required_and_audit():
    fresh_root()
    ctx = {"share_start": lambda: {"url": None}}
    for action in admin.CONFIRM_ACTIONS:
        try:
            admin.dispatch(action, {}, ctx)
            assert False, action
        except admin.AdminError as e:
            assert "确认" in str(e)
    admin.dispatch("share_start", {"confirm": True}, ctx)
    items = admin.audit_tail(10)
    assert items[0]["action"] == "share_start" and items[0]["ok"]


def test_dispatch_validation_and_errors():
    fresh_root()
    ctx = {"watch_change": lambda a, m, c: "代码不对" if c == "bad" else None, "start_refresh": lambda m: m != "us"}
    for action, body in (("nope", {}), ("task_start", {"task": "rm -rf"}), ("watch_add", {"market": "jp", "code": "1"}), ("watch_add", {"code": "600519"})):
        try:
            admin.dispatch(action, body, ctx)
            assert False, (action, body)
        except admin.AdminError:
            pass
    try:
        admin.dispatch("watch_add", {"market": "cn", "code": "bad"}, ctx)
        assert False
    except admin.AdminError as e:
        assert "代码不对" in str(e)
    assert admin.dispatch("watch_add", {"market": "cn", "code": "600519"}, ctx)["ok"]
    assert admin.dispatch("refresh", {"market": "cn"}, ctx)["started"] is True
    assert admin.dispatch("refresh", {"market": "us"}, ctx)["started"] is False
    assert any(i["ok"] is False for i in admin.audit_tail(20))                            # 失败也记审计


def test_log_whitelist_and_tail():
    root = fresh_root()
    (root / "logs" / "daily.log").write_text("\n".join(f"line {i}" for i in range(500)))
    r = admin.log_tail("daily", 50)
    assert len(r["lines"]) == 50 and r["lines"][-1] == "line 499"
    for bad in ("../.secret/token", "/etc/passwd", "token", ""):
        try:
            admin.log_tail(bad, 10)
            assert False, bad
        except admin.AdminError:
            pass
    assert admin.log_tail("daily", 10**9)["lines"]                                         # n 被限制在 1000 以内
    assert admin.log_tail("tunnel", 10)["lines"] == []                                     # 文件不存在也不报错


def test_backup_excludes_secrets_and_download_is_whitelisted():
    root = fresh_root()
    (root / ".secret" / "token").write_text("MAIN-SECRET")
    (root / ".secret" / "admin_token").write_text("ADMIN-SECRET")
    (root / "data" / "daily" / "watchlist_cn.json").write_text("{}")
    (root / "src" / ".secret").mkdir()
    (root / "src" / ".secret" / "x.txt").write_text("NESTED-SECRET")
    (root / "src" / "a.py").write_text("print(1)")
    info = admin.backup_make()
    with zipfile.ZipFile(root / "export" / info["name"]) as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names)
    assert "config.py" in names and "src/a.py" in names and "data/daily/watchlist_cn.json" in names
    assert not any(".secret" in n for n in names)
    assert b"SECRET" not in blob
    assert admin.backup_path(info["name"]) is not None
    for bad in ("../config.py", "backup-1.zip", "backup-20260101-0000.zip/../../x", "/etc/passwd", "", None):
        assert admin.backup_path(bad) is None, bad


def test_backup_keeps_latest_10():
    root = fresh_root()
    for i in range(12):
        (root / "export" / f"backup-202601{i + 1:02d}-000000.zip").write_bytes(b"x")
    admin.backup_make()
    assert len(admin.backup_list()) == 10


def test_clear_visits_backs_up_first():
    root = fresh_root()
    (root / "data" / "share_visits.jsonl").write_text('{"a":1}\n{"a":2}\n')
    r = admin.clear_visits()
    assert r["cleared"] == 2 and (root / "data" / r["backup"]).read_text().count("\n") == 2
    assert (root / "data" / "share_visits.jsonl").read_text() == ""
    assert admin.clear_visits() == {"cleared": 0}


def test_overview_without_pandas():
    root = fresh_root()
    (root / "data" / "daily" / "summary_cn.json").write_text(json.dumps({"market_name": "A股", "data_date": "2026-10-09", "generated_at": "x"}))
    (root / "data" / "daily" / "cn_all").mkdir()
    (root / "data" / "daily" / "cn_all" / "000001.parquet").write_bytes(b"x")
    o = admin.overview({"app_status": lambda: {"share": {"running": False}}})
    assert o["markets"]["cn"]["data_date"] == "2026-10-09" and o["markets"]["cn"]["all"]["cached"] == 1
    assert o["share"] == {"running": False} and set(o["tasks"]) == set(admin.TASKS)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} 项通过")
