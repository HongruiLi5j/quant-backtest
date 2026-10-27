"""factor_attribution 测试:合成数据,不联网,不读 data/。"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import factor_attribution as fa  # noqa: E402


def make(n=1500, alpha=0.0, b=(1.1, 0.3, -0.2, 0.0), noise=0.004, seed=0, ar=0.0, rf0=0.0001):
    rng = np.random.default_rng(seed)
    import datetime as dt
    d0 = dt.date(2015, 1, 1)
    dates = [(d0 + dt.timedelta(days=i)).isoformat() for i in range(n)]
    mk, smb, hml, mom = (rng.normal(0.0004, 0.01, n), rng.normal(0, 0.005, n), rng.normal(0, 0.005, n), rng.normal(0, 0.006, n))
    e = rng.normal(0, noise, n)
    if ar:
        for i in range(1, n):
            e[i] += ar * e[i - 1]
    rf = np.full(n, rf0)
    y = rf + alpha + b[0] * mk + b[1] * smb + b[2] * hml + b[3] * mom + e
    f = {d: {"Mkt-RF": float(mk[i]), "SMB": float(smb[i]), "HML": float(hml[i]), "Mom": float(mom[i]), "RF": float(rf[i])} for i, d in enumerate(dates)}
    return dates, y.tolist(), f


class AttributionTests(unittest.TestCase):
    def test_recovers_betas_and_alpha(self):
        d, r, f = make(alpha=0.0005)
        out = fa.attribute(d, r, f, "ff3")
        self.assertEqual(out["status"], "ok")
        c = out["coef"]
        self.assertAlmostEqual(c["Mkt-RF"]["coef"], 1.1, delta=0.05)
        self.assertAlmostEqual(c["SMB"]["coef"], 0.3, delta=0.1)
        self.assertAlmostEqual(c["HML"]["coef"], -0.2, delta=0.1)
        self.assertAlmostEqual(c["alpha"]["coef"], 0.0005, delta=0.0004)
        self.assertGreater(c["alpha"]["t"], 2.0)
        self.assertAlmostEqual(out["alpha_annual"], c["alpha"]["coef"] * 252)
        self.assertGreater(out["r2"], 0.8)

    def test_zero_alpha_not_significant(self):
        d, r, f = make(alpha=0.0, seed=3)
        out = fa.attribute(d, r, f, "ff3")
        self.assertLess(abs(out["coef"]["alpha"]["t"]), 3.0)

    def test_ff4_has_momentum(self):
        d, r, f = make(b=(1.0, 0.0, 0.0, 0.8))
        out = fa.attribute(d, r, f, "ff4")
        self.assertAlmostEqual(out["coef"]["Mom"]["coef"], 0.8, delta=0.1)
        self.assertNotIn("Mom", fa.attribute(d, r, f, "ff3")["coef"])

    def test_rf_is_subtracted(self):
        d, r, f = make(alpha=0.0)
        shifted = [x + 0.001 for x in r]                                              # 每天多赚 0.1%：全部进 alpha
        out = fa.attribute(d, shifted, f, "ff3")
        self.assertAlmostEqual(out["coef"]["alpha"]["coef"], 0.001, delta=0.0004)

    def test_rf_subtraction_matters(self):
        d, r, f = make(alpha=0.0, rf0=0.0006, noise=0.001, n=3000)                    # 无风险利率较高：不减 RF 的话 α 会虚高约 0.0006
        self.assertAlmostEqual(fa.attribute(d, r, f, "ff3")["coef"]["alpha"]["coef"], 0.0, delta=0.0002)

    def test_insufficient_overlap_is_na_not_computed(self):
        d, r, f = make(n=1000)
        part = {k: v for k, v in list(f.items())[:100]}
        out = fa.attribute(d, r, part, "ff3")
        self.assertEqual(out["status"], "na")
        self.assertEqual(out["n_obs"], 100)
        self.assertNotIn("coef", out)

    def test_missing_factor_days_dropped(self):
        d, r, f = make()
        for k in list(f)[:300]:
            f[k]["SMB"] = None
        out = fa.attribute(d, r, f, "ff3")
        self.assertEqual(out["n_obs"], 1200)

    def test_newey_west_bigger_than_ols_under_autocorrelation(self):
        d, r, f = make(ar=0.5, seed=5, n=3000)
        wide = fa.attribute(d, r, f, "ff3", lags=10)["coef"]["alpha"]["t"]
        none = fa.attribute(d, r, f, "ff3", lags=0)["coef"]["alpha"]["t"]
        self.assertLess(abs(wide), abs(none))                                         # 自相关下 HAC 的 t 值更保守

    def test_validation(self):
        d, r, f = make(n=300)
        with self.assertRaises(fa.AttributionError):
            fa.attribute(d, r, f, "capm")
        with self.assertRaises(fa.AttributionError):
            fa.attribute(d, r, None, "ff3")
        with self.assertRaises(fa.AttributionError):
            fa.attribute(d, r[:-1], f, "ff3")

    def test_constant_factor_is_na(self):
        d, r, f = make(n=400)
        for v in f.values():
            v["SMB"] = 0.0
        self.assertEqual(fa.attribute(d, r, f, "ff3")["status"], "na")

    def test_no_app_import(self):
        self.assertNotIn("app", sys.modules)


if __name__ == "__main__":
    unittest.main()
