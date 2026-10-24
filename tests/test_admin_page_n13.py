"""后台页面（dashboard/admin.html + admin.js）重排后的静态检查：python -m pytest tests/test_admin_page_n13.py
只读源码，不启动服务；用 tempfile 放一份 node 语法检查的副本。"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "dashboard"
HTML = (DASH / "admin.html").read_text(encoding="utf-8")
JS = (DASH / "admin.js").read_text(encoding="utf-8")


def test_page_skeleton():
    # 左侧分区、顶部搜索、内容区、提示条都在，且只引用本站脚本
    for needle in ('id="tabs"', 'id="search"', 'id="view"', 'id="banner"', 'id="toast"', 'src="/admin.js"'):
        assert needle in HTML, needle
    assert "<aside" in HTML
    for url in re.findall(r'(?:src|href)="(https?:[^"]+)"', HTML):
        raise AssertionError("页面不能引用外网地址: " + url)


def test_no_unsafe_dom_apis():
    for bad in (".innerHTML", "insertAdjacentHTML(", "document.write(", "eval("):
        assert bad not in JS and bad not in HTML, bad


def test_existing_api_calls_kept():
    # 原有接口调用一个都不能少
    for path in ("/api/admin/overview", "/api/admin/data", "/api/admin/visits", "/api/admin/members",
                 "/api/admin/member?id=", "/api/admin/log?name=", "/api/admin/audit",
                 "/api/admin/action", "/api/admin/backup/download"):
        assert path in JS, path
    for action in ("share_start", "share_stop", "share_pass_set", "share_pass_clear", "clear_visits", "member_disable",
                   "member_enable", "member_delete", "restart_server", "rotate_main_token", "rotate_admin_token",
                   "backup_make", "refresh", "kick_update", "task_start", "task_stop", "scan_start", "scan_stop",
                   "watch_add", "watch_remove", "update_stock", "gap_start"):
        assert "'" + action + "'" in JS, action
    assert "confirm: true" in JS and "confirmDialog" in JS


def test_dangerous_actions_still_confirmed():
    for action in ("restart_server", "rotate_main_token", "rotate_admin_token", "member_delete", "share_start", "clear_visits"):
        m = re.search(r"act\('" + action + r"'[^\n]*", JS)
        assert m and "title:" in m.group(0), action


def test_new_sections_and_grey_switches():
    for tab in ("site", "sections", "members", "access", "tasks", "data", "system", "logs", "audit", "overview"):
        assert "['" + tab + "'," in JS, tab
    assert "需要后端改动" in JS
    # 灰色开关不带点击处理：swOff 里不能有 onclick
    m = re.search(r"const swOff = [^\n]*", JS)
    assert m and "onclick" not in m.group(0) and "aria-disabled" in m.group(0)


def test_js_syntax():
    node = shutil.which("node")
    if not node:
        return
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "admin_copy.js"
        p.write_text(JS, encoding="utf-8")
        r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
