"""请求走私防护(Codex 夜间安全审计 N1027_4)：带请求体的请求必须在响应后关闭连接。
不 import app：用 ast 取出 Handler._guard 的源码，在桩对象上运行。"""
import ast
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")


def load_guard():
    tree = ast.parse(APP)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Handler":
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name == "_guard":
                    mod = ast.Module(body=[f], type_ignores=[])
                    g = {"ALLOWED_HOSTS": {"127.0.0.1:8765", "localhost:8765"}}
                    exec(compile(mod, "app.py", "exec"), g)
                    return g["_guard"]
    raise AssertionError("找不到 Handler._guard")


class Stub:
    def __init__(self, headers):
        self.headers = headers
        self.close_connection = False
        self.sent = []

    def _owner_cfg(self):
        return None

    def _send(self, *a, **k):
        self.sent.append(a)


class DesyncTests(unittest.TestCase):
    def run_guard(self, headers):
        s = Stub(headers)
        ok = load_guard()(s)
        return ok, s.close_connection

    def test_get_with_body_closes_connection(self):
        ok, closed = self.run_guard({"Host": "127.0.0.1:8765", "Content-Length": "120"})
        self.assertTrue(ok)
        self.assertTrue(closed)

    def test_chunked_closes_connection(self):
        self.assertTrue(self.run_guard({"Host": "127.0.0.1:8765", "Transfer-Encoding": "chunked"})[1])

    def test_plain_get_keeps_alive(self):
        self.assertFalse(self.run_guard({"Host": "127.0.0.1:8765"})[1])
        self.assertFalse(self.run_guard({"Host": "127.0.0.1:8765", "Content-Length": "0"})[1])


if __name__ == "__main__":
    unittest.main()
