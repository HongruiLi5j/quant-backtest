"""extdata 测试:全部用合成的 zip/CSV 与假 opener,不联网,不读写真实 data/。"""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import extdata as X  # noqa: E402


def french_text(cols, n=1200, start=20000101, extra_footer=True, missing_at=None, header_cols=None):
    head = "This file was created by using the 202608 CRSP database.\n\n," + ",".join(header_cols or cols) + "\n"
    rows = []
    d = start
    for i in range(n):
        vals = [f"{(i % 7 - 3) * 0.1:8.2f}" for _ in cols]
        if missing_at == i:
            vals[0] = "-99.99"
        day = date(2000, 1, 1) + timedelta(days=i)                      # 真实存在的日历日
        rows.append(f"{day:%Y%m%d}," + ",".join(vals))
    return head + "\n".join(rows) + ("\n\nCopyright 2026 Eugene F. Fama and Kenneth R. French\n" if extra_footer else "\n")


def mkzip(text, name="x.csv"):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(name, text)
    return b.getvalue()


def fred_text(n=1500, sid="DTB3"):
    return f"observation_date,{sid}\n" + "\n".join(f"{date(2000, 1, 1) + timedelta(days=i):%Y-%m-%d},{'.' if i == 5 else '1.5'}" for i in range(n)) + "\n"


class FakeResp:
    def __init__(self, data): self.data = data
    def read(self, n=-1): return self.data if n < 0 else self.data[:n]
    def __enter__(self): return self
    def __exit__(self, *a): return False


def opener_for(mapping):
    def op(req, timeout=None):
        url = req.full_url
        if url not in mapping:
            raise AssertionError(f"unexpected url {url}")
        return FakeResp(mapping[url])
    return op


class ParseTests(unittest.TestCase):
    def test_french_ok_percent_to_decimal_and_footer_stop(self):
        rows = X.parse_french(french_text(["a", "b"]), ["a", "b"])
        self.assertEqual(len(rows), 1200)
        self.assertEqual(rows[0]["date"], "2000-01-01")
        self.assertAlmostEqual(rows[0]["a"], -0.003)                                  # -0.30% → -0.003
        self.assertEqual(list(rows[0]), ["date", "a", "b"])

    def test_french_missing_marker_is_none_not_zero(self):
        rows = X.parse_french(french_text(["a", "b"], missing_at=10), ["a", "b"])
        self.assertIsNone(rows[10]["a"])
        self.assertIsNotNone(rows[10]["b"])

    def test_french_rejects(self):
        with self.assertRaises(X.ExtDataError):
            X.parse_french(french_text(["a", "b"], header_cols=["a", "c"]), ["a", "b"])          # 列名不符
        with self.assertRaises(X.ExtDataError):
            X.parse_french(french_text(["a", "b"], n=50), ["a", "b"])                              # 行数异常偏少
        with self.assertRaises(X.ExtDataError):
            X.parse_french("no header here\n20000101, 1, 2\n", ["a", "b"])
        bad = french_text(["a", "b"]).replace("20000103", "20000101", 1)                          # 日期重复
        with self.assertRaises(X.ExtDataError):
            X.parse_french(bad, ["a", "b"])
        bad2 = french_text(["a", "b"]).replace("   -0.30", "  abc", 1)
        with self.assertRaises(X.ExtDataError):
            X.parse_french(bad2, ["a", "b"])

    def test_fred(self):
        rows = X.parse_fred(fred_text(), "DTB3")
        self.assertEqual(len(rows), 1500)
        self.assertIsNone(rows[5]["dtb3"])                                           # "." 是缺失
        self.assertEqual(rows[0]["dtb3"], 1.5)
        with self.assertRaises(X.ExtDataError):
            X.parse_fred(fred_text(sid="XXX"), "DTB3")
        with self.assertRaises(X.ExtDataError):
            X.parse_fred("observation_date,DTB3\n2000-01-02,1\n2000-01-01,1\n" * 600, "DTB3")


class SafetyTests(unittest.TestCase):
    def test_url_whitelist_and_size(self):
        with self.assertRaises(X.ExtDataError):
            X._http("https://example.com/x.zip", opener_for({}))
        with self.assertRaises(X.ExtDataError):
            X._http(X.SOURCES["dtb3"], opener_for({X.SOURCES["dtb3"]: b"x" * (X.MAX_BYTES + 5)}))
        with self.assertRaises(X.ExtDataError):
            X._http(X.SOURCES["dtb3"], opener_for({X.SOURCES["dtb3"]: b""}))

    def test_only_https_whitelisted(self):
        self.assertTrue(all(u.startswith("https://") for u in X.SOURCES.values()))

    def test_zip_checks(self):
        with self.assertRaises(X.ExtDataError):
            X._unzip_csv(b"not a zip")
        with self.assertRaises(X.ExtDataError):
            X._unzip_csv(mkzip("a", name="../evil.csv"))
        with self.assertRaises(X.ExtDataError):
            X._unzip_csv(mkzip("a", name="sub/x.csv"))
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("a.csv", "1")
            z.writestr("b.csv", "2")
        with self.assertRaises(X.ExtDataError):
            X._unzip_csv(b.getvalue())


class FetchTests(unittest.TestCase):
    def mapping(self, **over):
        m = {X.SOURCES["ff3"]: mkzip(french_text(["Mkt-RF", "SMB", "HML", "RF"])), X.SOURCES["mom"]: mkzip(french_text(["Mom"])),
             X.SOURCES["dtb3"]: fred_text().encode()}
        m.update(over)
        return m

    def test_fetch_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            meta = X.fetch_all(Path(d), opener_for(self.mapping()))
            self.assertEqual(meta["ff3"]["rows"], 1200)
            self.assertIn("Fama and Kenneth R. French", meta["attribution"]["ken_french"])
            self.assertIn("not endorsed", meta["attribution"]["fred"])
            self.assertEqual(json.loads((Path(d) / "meta.json").read_text(encoding="utf-8"))["dtb3"]["rows"], 1500)
            f = X.load_factors(Path(d))
            self.assertEqual(set(f["2000-01-01"]), {"Mkt-RF", "SMB", "HML", "RF", "Mom"})
            self.assertAlmostEqual(f["2000-01-01"]["Mom"], -0.003)

    def test_all_or_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(X.ExtDataError):
                X.fetch_all(Path(d), opener_for(self.mapping(**{X.SOURCES["dtb3"]: b"observation_date,DTB3\n"})))
            self.assertEqual(list(Path(d).iterdir()), [])                              # 第三份失败：一份都没写

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(X.load_factors(Path(d)))

    def test_no_app_import(self):
        self.assertNotIn("app", sys.modules)


class HardeningTests(unittest.TestCase):
    def test_bad_row_midway_rejected(self):
        t = french_text(["Mom"])
        lines = t.split("\n")
        k = next(i for i, l in enumerate(lines) if l.startswith("20000301"))
        lines[k] = "garbage,line"
        with self.assertRaises(X.ExtDataError):
            X.parse_french("\n".join(lines), ["Mom"])

    def test_fake_calendar_day_rejected(self):
        t = french_text(["Mom"]).replace("20000102,", "20000100,", 1)
        with self.assertRaises(X.ExtDataError):
            X.parse_french(t, ["Mom"])
        with self.assertRaises(X.ExtDataError):
            X.parse_fred(fred_text().replace("2000-01-03", "2000-02-30", 1), "DTB3")

    def test_redirect_refused(self):
        import urllib.request
        with self.assertRaises(X.ExtDataError):
            X._NoRedirect().redirect_request(urllib.request.Request(next(iter(X.SOURCES.values()))), None, 302, "Found", {}, "http://evil.example/x")

    def test_default_http_path_refuses_redirect(self):
        """默认下载链路(不注入 opener)真的经过 _NoRedirect：模拟 HTTPS 302，只允许发生一次请求。"""
        from email.message import Message
        from unittest.mock import patch
        from urllib.request import HTTPSHandler
        from urllib.response import addinfourl
        seen = []

        def response(handler, req):
            seen.append(req.full_url)
            assert len(seen) == 1, "发生了重定向跟随"
            headers = Message()
            headers["Location"] = "https://example.invalid/redirect"
            r = addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
            r.msg = "Found"
            return r
        with patch.object(HTTPSHandler, "https_open", new=response):
            with self.assertRaises(X.ExtDataError):
                X._http(X.SOURCES["dtb3"])
        self.assertEqual(seen, [X.SOURCES["dtb3"]])

    def test_default_fetch_uses_no_redirect_opener(self):
        import inspect
        self.assertIsNone(inspect.signature(X.fetch_all).parameters["opener"].default)       # 默认走拒绝重定向的 opener

    def test_group_commit_all_or_nothing_and_sha_check(self):
        mapping = {X.SOURCES["ff3"]: mkzip(french_text(["Mkt-RF", "SMB", "HML", "RF"])), X.SOURCES["mom"]: mkzip(french_text(["Mom"])),
                   X.SOURCES["dtb3"]: fred_text().encode()}
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            X.fetch_all(base, opener_for(mapping))
            self.assertIsNotNone(X.load_factors(base))
            (base / "mom_daily.csv").write_text("date,Mom\n2000-01-01,0.5\n", encoding="utf-8")       # 与 meta 的 sha 对不上
            self.assertIsNone(X.load_factors(base))
            # 写入阶段失败:不留临时文件,已有文件不变
            before = {p.name: p.read_bytes() for p in base.iterdir()}
            orig = X._stage_csv
            calls = []
            def boom(path, rows, cols):
                calls.append(path.name)
                if len(calls) == 2:
                    raise OSError("disk full")
                return orig(path, rows, cols)
            X._stage_csv = boom
            try:
                with self.assertRaises(OSError):
                    X.fetch_all(base, opener_for(mapping))
            finally:
                X._stage_csv = orig
            self.assertEqual({p.name: p.read_bytes() for p in base.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
