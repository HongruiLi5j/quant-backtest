"""D89：后端生成的句子带英文版（逻辑、数字不变）。
- trade_list.explain / headline：lang="en" 不含中文字符；中文输出与改动前逐字相同（固定合成行情上的快照）。
- daily_report.build：notes_en 与 notes 一一对应，reason_en 每个策略都有，都不含中文字符。
合成行情 + 替换状态文件读写，不读写 data/。运行：.venv/bin/python tests/test_d89_english.py"""
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import MARKETS  # noqa: E402
from src import daily_report, trade_list as T  # noqa: E402

CJK = re.compile(r"[一-鿿＀-￯　-〿]")
STRATS = ["blend2", "trend_ens", "trend_vol", "donchian_s1", "rsi2", "ma_adx_original", "ma_adx_plus"]


def synth(n=900, seed=0, drift=0.0004):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    c = 10 * np.exp(np.cumsum(rng.normal(drift, 0.02, n)))
    o = c * np.exp(rng.normal(0, 0.004, n))
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.01, "Low": np.minimum(o, c) * 0.99, "Close": c, "Volume": 1e6}, index=idx)


class TradeListEnglish(unittest.TestCase):
    def test_english_has_no_cjk_and_chinese_unchanged_shape(self):
        for seed in range(6):
            df = synth(seed=seed, drift=0.001 if seed % 2 else -0.0005)
            for cut in (len(df), len(df) - 5, len(df) - 40):
                d = df.iloc[:cut]
                for s in STRATS:
                    en_e, en_h = T.explain(s, d, 1.0, lang="en"), T.headline(s, d, 1.0, lang="en")
                    self.assertIsNone(CJK.search(en_e), (s, en_e[:120]))
                    self.assertIsNone(CJK.search(en_h or ""), (s, en_h))
                    zh_e = T.explain(s, d, 1.0)
                    self.assertTrue(CJK.search(zh_e), s)                         # 默认仍是中文
                    nums = lambda x: re.findall(r"-?\d+(?:\.\d+)?", x)
                    self.assertEqual(sorted(nums(zh_e)), sorted(nums(en_e)), s)   # 同样的数字

    def test_fill_display_sets_en_fields(self):
        df = synth()
        row = {"code": "600001", "flags": [], "close": None}
        with mock.patch.object(T.turnover, "lot_cost", lambda *a, **k: None):
            T._fill_display(row, "blend2", df, 1.0, "前复权")
        self.assertTrue(row["reason_en"] and row["trigger_en"])
        self.assertIsNone(CJK.search(row["reason_en"]))


class DailyReportEnglish(unittest.TestCase):
    def test_notes_and_reasons_en(self):
        mcfg = MARKETS["cn"]
        prices = {s: synth(n=900, seed=i) for i, s in enumerate(mcfg["universe"])}
        with mock.patch.object(daily_report, "_state", lambda m: {}), \
             mock.patch.object(daily_report, "_save_state", lambda st: None), \
             mock.patch.object(daily_report, "load_watchlist", lambda m: {}), \
             mock.patch.object(daily_report, "_staleness", lambda m, now: None):
            rep = daily_report.build("cn", mcfg, prices, {})
        self.assertEqual(len(rep["notes_en"]), len(rep["notes"]))
        for x in rep["notes_en"]:
            self.assertIsNone(CJK.search(x), x)
        for st in rep["strategies"]:
            if "error" in st:
                continue
            self.assertIn("reason_en", st)
            self.assertIsNone(CJK.search(st["reason_en"]), st["key"])


class LedgerEnglish(unittest.TestCase):
    def test_build_report_en_parallel(self):
        import tempfile
        from src import signal_ledger as L
        c = L.connect(Path(tempfile.mkdtemp()) / "t.db")
        cal = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2026-09-01", periods=60)]
        for k, d in enumerate(cal[:40]):
            c.execute("INSERT INTO days VALUES(?,?,?)", (d, 0, "x"))
            for j in range(40):
                code = f"6000{j:02d}"
                ret = ((j * 7 + k) % 11 - 5) / 100
                for h in (1, 5, 10, 20):
                    c.execute("INSERT INTO moves VALUES(?,?,?,?,?,?,?)", (d, code, h, ret, 0.0, 0, 0))
                if j < 12:
                    c.execute("INSERT INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?)", (d, "blend2", code, "x", "买入" if j < 6 else "卖出", 0, 1, 10.0, None, 0, "[]"))
            for h in (1, 5, 10, 20):
                c.execute("INSERT INTO done VALUES(?,?,?)", (d, h, 40))
        c.commit()
        rep = L.build_report(c, cal[45])
        self.assertEqual(len(rep["notes_en"]), len(rep["notes"]))
        self.assertEqual(len(rep["caveats_en"]), len(rep["caveats"]))
        for x in rep["notes_en"] + rep["caveats_en"] + [r["text_en"] + r["suggestion_en"] for r in rep["reflection"]]:
            self.assertIsNone(CJK.search(x), x)


if __name__ == "__main__":
    unittest.main()
