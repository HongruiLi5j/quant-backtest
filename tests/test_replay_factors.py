"""T008 S1:因子打分测试。只用合成数据,不读 data/,不联网。

三类测试:
  1. 手算核对:16 个非对照类别各至少一个策略,用几根 K 线手算出期望值;
  2. 未来函数断言:对全部 95 个策略,(a)把 day 之后的数据改得面目全非,day 的得分不变;
     (b)截断到 day 重算与全量计算在 day 的值一致;
  3. 接口:随机对照无打分、参数键与参数值(必须在清单网格内)校验、day 必须是交易日、清单覆盖。
手算核对除 16 个类别的简单策略外,还覆盖规则复杂的状态型策略:
  ma_06、brk_05、sq_05、osc_03(KDJ)、ext_05、h52_02、liq_06、pv_03。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.replay import factors as FA  # noqa: E402
from src.replay import registry  # noqa: E402


def bars(close, open_=None, high=None, low=None, vol=None, amount=None):
    """手算用:默认 开=高=低=收(平 K 线),成交量 100。"""
    n = len(close)
    idx = pd.bdate_range("2024-01-01", periods=n)
    c = pd.Series(close, index=idx, dtype=float)
    d = pd.DataFrame({
        "open": c if open_ is None else open_, "high": c if high is None else high,
        "low": c if low is None else low, "close": c,
        "volume": 100.0 if vol is None else vol,
    }, index=idx)
    if amount is not None:
        d["amount"] = amount
    return d


def sc(sid, panel, params, industry=None):
    day = max(df.index.max() for df in panel.values())
    return FA._score(sid, panel, day, params, industry, False)   # 手算用小窗口,不在网格里,走私有入口


class TestHandComputed(unittest.TestCase):
    def check(self, got, want, places=6):
        self.assertEqual(set(got), set(want))
        for k in want:
            self.assertAlmostEqual(got[k], want[k], places=places, msg=k)

    def test_momentum(self):      # 动量 mom_01:5 日收益 15/10-1、22/20-1
        p = {"A": bars([10, 11, 12, 13, 14, 15]), "B": bars([20, 20, 20, 20, 20, 22])}
        self.check(sc("mom_01", p, {"n": 5}), {"A": 0.5, "B": 0.1})

    def test_reversal(self):      # 反转 rev_01 n=1:-(11/10-1)
        p = {"A": bars([10, 11])}
        self.check(sc("rev_01", p, {"n": 1}), {"A": -0.1})

    def test_ma(self):            # 均线 ma_01:6 / mean(4,5,6) - 1
        p = {"A": bars([1, 2, 3, 4, 5, 6])}
        self.check(sc("ma_01", p, {"n": 3}), {"A": 0.2})

    def test_breakout(self):      # 突破 brk_01:前 3 日最高价 max(5,7,6)=7,8/7-1
        p = {"A": bars([5, 7, 6, 8])}
        self.check(sc("brk_01", p, {"n": 3}), {"A": 8 / 7 - 1})

    def test_lowvol(self):        # 低波动 lowvol_01 n=2:收益 +0.1、-0.1,样本标准差 √0.02
        p = {"A": bars([100, 110, 99])}
        self.check(sc("lowvol_01", p, {"n": 2}), {"A": -np.sqrt(0.02)})

    def test_idio_vol(self):
        # 两只股票:m=(x,-x,0)、d=(y,y,-2y) 且二者样本协方差为 0,
        # A 日收益=m+d、B=m-d,等权市场收益=m;回归 beta=1,残差=±d,样本标准差=√3·y
        x, y = 0.01, 0.02
        ra = np.array([x + y, -x + y, 0 - 2 * y])
        rb = np.array([x - y, -x - y, 0 + 2 * y])
        ca = 100 * np.concatenate([[1], np.cumprod(1 + ra)])
        cb = 100 * np.concatenate([[1], np.cumprod(1 + rb)])
        p = {"A": bars(ca), "B": bars(cb)}
        want = -np.sqrt(3) * y
        self.check(sc("ivol_01", p, {"n": 3}), {"A": want, "B": want}, places=6)

    def test_liquidity(self):     # 量能 liq_01:600 / mean(200,300,600)
        p = {"A": bars([10, 10, 10, 10], vol=[100, 200, 300, 600])}
        self.check(sc("liq_01", p, {"n": 3}), {"A": 600 / (1100 / 3)})

    def test_price_volume(self):  # 量价形态 pv_05:收盘位置 (2-1)/(3-1)=0.5 与 (3-1)/(3-1)=1,均值 0.75
        p = {"A": bars([2, 3], high=[3, 3], low=[1, 1])}
        self.check(sc("pv_05", p, {"n": 2}), {"A": 0.75})

    def test_squeeze(self):       # 波动收敛 sq_04:振幅 .4,.2,.3,窗口最小 .2,得分 .2/.3
        c = [10, 10, 10]
        p = {"A": bars(c, high=[12, 11, 11.5], low=[8, 9, 8.5])}
        self.check(sc("sq_04", p, {"n": 3}), {"A": 2 / 3})

    def test_oscillator(self):    # 超买超卖 osc_01:价差 +1,-1,-1 → 均涨 1/3、均跌 2/3,RSI=100/3,得分取负
        p = {"A": bars([10, 11, 10, 9])}
        self.check(sc("osc_01", p, {"n": 3}), {"A": -100 / 3})

    def test_industry(self):      # 行业动量 ind_01 n=2:X 行业 (0.2+0.1)/2、Y 行业 -0.1
        p = {"A": bars([10, 10, 12]), "B": bars([10, 10, 11]), "C": bars([10, 10, 9])}
        ind = {"A": "X", "B": "X", "C": "Y"}
        self.check(sc("ind_01", p, {"n": 2}, ind), {"A": 0.15, "B": 0.15, "C": -0.1})

    def test_overnight(self):     # 隔夜 ovn_01 n=2:(10.5/10-1)=.05 与 (9/10-1)=-.1,均值 -.025
        p = {"A": bars([10, 10, 9], open_=[10, 10.5, 9])}
        self.check(sc("ovn_01", p, {"n": 2}), {"A": -0.025})

    def test_high52(self):        # 52 周高点 h52_01 n=3:11 / max(10,12,11)
        p = {"A": bars([10, 12, 11])}
        self.check(sc("h52_01", p, {"n": 3}), {"A": 11 / 12})

    def test_extreme(self):       # 极端收益 ext_01/ext_02 n=2:日收益 +0.1、-0.1
        p = {"A": bars([100, 110, 99])}
        self.check(sc("ext_01", p, {"n": 2}), {"A": -0.1})
        self.check(sc("ext_02", p, {"n": 2}), {"A": 0.1})

    def test_size_proxy(self):    # 低成交额 size_01 n=2:-(100+300)/2
        p = {"A": bars([10, 10], amount=[100.0, 300.0])}
        self.check(sc("size_01", p, {"n": 2}), {"A": -200.0})

    def test_composite_diff(self):  # 复合 cmb_08:13/10-1 减 13/12-1
        p = {"A": bars([10, 11, 12, 13])}
        self.check(sc("cmb_08", p, {"long": 3, "short": 1}), {"A": 0.3 - (13 / 12 - 1)})

    def test_composite_rank(self):
        # cmb_01 n=2 w=0.3。动量:A .21 > B .1 > C -.01 → 排名 1,2/3,1/3
        # 低波动(收益标准差取负):A 0 > C .00707 > B .0707 → 排名 1,2/3,1/3 的顺序为 A=1、C=2/3、B=1/3
        p = {"A": bars([100, 110, 121]), "B": bars([100, 100, 110]), "C": bars([100, 100, 99])}
        want = {"A": 1.0, "B": 0.3 * 2 / 3 + 0.7 / 3, "C": 0.3 / 3 + 0.7 * 2 / 3}
        self.check(sc("cmb_01", p, {"n": 2, "w": 0.3}), want)


class TestHandComputedStateful(unittest.TestCase):
    """规则复杂、状态型的策略(审阅意见 5)。"""

    def check(self, got, want, places=6):
        self.assertEqual(set(got), set(want))
        for k in want:
            self.assertAlmostEqual(got[k], want[k], places=places, msg=k)

    def test_ma_06_cross_freshness(self):
        # short=2,long=3。A: sma2-sma3 在 idx4 转正、idx5 仍为正 → 在上方第 2 天 → 1/2
        # B: 一直在下方 → 0;C: 最后一天才上穿(idx4 为负,idx5 为正)→ 金叉当天 → 1
        p = {"A": bars([10, 10, 10, 9, 12, 13]), "B": bars([10, 10, 10, 9, 8, 7]),
             "C": bars([10, 10, 10, 9, 8, 12])}
        self.check(sc("ma_06", p, {"short": 2, "long": 3}), {"A": 0.5, "B": 0.0, "C": 1.0})

    def test_brk_05_days_since_new_high(self):
        # n=3,高=收。A: 新高出现在 idx3(13>前3日最高12),此后 idx4、idx5 都没再创新高,距今 2 天 → 1-2/3
        # B: 最后一天(13>前3日最高12)创新高 → 距今 0 天 → 1;C: 一路下跌从未新高 → 0
        p = {"A": bars([10, 11, 12, 13, 12, 12.5]), "B": bars([10, 11, 12, 11, 11, 13]),
             "C": bars([13, 12, 11, 10, 9, 8])}
        self.check(sc("brk_05", p, {"n": 3}), {"A": 1 / 3, "B": 1.0, "C": 0.0})

    def test_sq_05_breakout_after_squeeze(self):
        # n=2,pct_window=5。带宽 bw=8|a-b|/(√2(a+b)):idx2~4 的收盘对 (12,8)(8,12)(12,8) 带宽相同且大,
        # idx5 对 (8,10) 较小,idx6 对 (10,10) 为 0 → idx6 的带宽在 idx2~6 五个值里最小,分位 1/5=0.2 ≤ 0.2 → "收窄"
        # (用 shift(1):idx7 看的是前一天的分位)。前 2 日(idx5、6)最高/最低价都是 10。
        # A: idx7 收 11 > 10 向上突破 → +1;B: 收 9 < 10 向下 → -1;C: 收 10 未突破 → 0
        # D: 带宽一直很大(分位 0.6,并列取平均),即使突破到 13 也不算 → 0
        base = [10, 12, 8, 12, 8, 10, 10]
        p = {"A": bars(base + [11]), "B": bars(base + [9]), "C": bars(base + [10]),
             "D": bars([10, 12, 8, 12, 8, 12, 8, 13])}
        self.check(sc("sq_05", p, {"n": 2, "pct_window": 5}), {"A": 1.0, "B": -1.0, "C": 0.0, "D": 0.0})

    def test_osc_03_kdj(self):
        # n=2,smooth=3(alpha=1/3)。c=[10,12,11],h=[11,13,12],l=[9,11,10]
        # RSV1=(12-9)/(13-9)=75;RSV2=(11-10)/(13-10)=100/3(idx0 窗口不满为 NaN,EWM 从 RSV1 起算)
        # K1=75,K2=2/3·75+1/3·100/3=550/9;D1=75,D2=2/3·75+1/3·550/9=1900/27
        # J=3K-2D=550/3-3800/27=1150/27,得分取负
        p = {"A": bars([10, 12, 11], high=[11, 13, 12], low=[9, 11, 10])}
        self.check(sc("osc_03", p, {"n": 2, "smooth": 3}), {"A": -1150 / 27})

    def test_ext_05_limit_touch_count(self):
        # n=3。A 的最高价/昨收-1:idx1 11/10(=10%,触板)、idx2 10.9/10(9%,否)、idx3 11/10(触板)→ -2
        # B 从未接近涨停 → 0
        p = {"A": bars([10, 10, 10, 10], high=[10, 11, 10.9, 11]), "B": bars([10, 10, 10, 10])}
        self.check(sc("ext_05", p, {"n": 3}), {"A": -2.0, "B": 0.0})

    def test_h52_02_new_high_days(self):
        # YEAR=250,需要 250 根历史才有"前 250 日最高价"。n=2:
        # A: idx250 收 11 > 前 250 日最高 10(新高),idx251 收 11 不 > 前 250 日最高 11 → 窗口 2 日内 1 天
        # B: idx250=12、idx251=13 连续新高 → 2;C: 一直 10 → 0
        flat = [10.0] * 250
        p = {"A": bars(flat + [11, 11]), "B": bars(flat + [12, 13]), "C": bars(flat + [10, 10])}
        self.check(sc("h52_02", p, {"n": 2}), {"A": 1.0, "B": 2.0, "C": 0.0})

    def test_liq_06_volume_slope(self):
        # n=3,量 100,200,300:权重 (-1,0,1),斜率=(-100+300)/2=100,除以均量 200 → 0.5;反向为 -0.5
        p = {"A": bars([10, 10, 10], vol=[100, 200, 300]), "B": bars([10, 10, 10], vol=[300, 200, 100])}
        self.check(sc("liq_06", p, {"n": 3}), {"A": 0.5, "B": -0.5})

    def test_pv_03_obv_change_normalised(self):
        # 口径:N 日 OBV 增量/ N 日总成交量。n=2,收 10→11→10.5,量 100,200,300:
        # idx1 涨 +200,idx2 跌 -300 → (200-300)/(200+300)=-0.2
        p = {"A": bars([10, 11, 10.5], vol=[100, 200, 300])}
        self.check(sc("pv_03", p, {"n": 2}), {"A": -0.2})


# ---------------------------------------------------------------- 未来函数断言

def synth(n_days=560, n_stocks=24, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n_days)
    panel = {}
    for i in range(n_stocks):
        r = rng.normal(0.0003, 0.02, n_days)
        c = 20 * np.cumprod(1 + r)
        o = c * (1 + rng.normal(0, 0.005, n_days))
        h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n_days)))
        l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n_days)))
        v = rng.integers(1_000, 100_000, n_days).astype(float)
        panel[f"S{i:02d}"] = pd.DataFrame({
            "Open": o, "High": h, "Low": l, "Close": c, "Volume": v,
            "Turnover": v / 1e6 * 100}, index=idx)
    return panel, {f"S{i:02d}": f"IND{i % 4}" for i in range(n_stocks)}


def corner_params(sid):
    grid = list(FA.iter_param_grid(sid))
    return [grid[0], grid[-1]]


class TestNoLookahead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.panel, cls.ind = synth()
        cls.idx = next(iter(cls.panel.values())).index
        cls.day = cls.idx[-61]                       # 留出 60 根"未来"
        cls.mangled = {}
        for k, df in cls.panel.items():              # day 之后的行情全部改掉
            m = df.copy()
            m.iloc[-60:] = m.iloc[-60:].to_numpy() * np.random.default_rng(1).uniform(0.2, 5, (60, m.shape[1]))
            cls.mangled[k] = m

    def test_all_scorable(self):
        cat = registry.load_catalog()["strategies"]
        non_ctrl = {s["id"] for s in cat if not s["id"].startswith("ctrl_")}
        self.assertEqual(set(FA.scorable_ids()), non_ctrl)
        self.assertEqual(len(non_ctrl), 95)

    def test_future_rows_do_not_change_score(self):
        for sid in FA.scorable_ids():
            for p in corner_params(sid):
                a = FA.score(sid, self.panel, self.day, p, self.ind)
                b = FA.score(sid, self.mangled, self.day, p, self.ind)
                self.assertEqual(a.keys(), b.keys(), f"{sid} {p}")
                for k in a:
                    self.assertAlmostEqual(a[k], b[k], places=10, msg=f"{sid} {p} {k}")

    def test_truncated_equals_full(self):
        for sid in FA.scorable_ids():
            for p in corner_params(sid):
                full = FA.factor_frame(sid, self.panel, p, self.ind)
                part = FA.factor_frame(sid, self.panel, p, self.ind, day=self.day)
                f = full.loc[:self.day]
                self.assertTrue(f.index.equals(part.index), sid)
                pd.testing.assert_frame_equal(f, part, check_exact=False, rtol=1e-9, atol=1e-12,
                                              obj=f"{sid} {p}")

    def test_scores_not_trivially_empty(self):
        # 防止"全是 NaN 所以天然一致"的假通过:合成数据足够长,每个策略在最后一天应有有效得分
        for sid in FA.scorable_ids():
            for p in corner_params(sid):
                self.assertGreater(len(FA.score(sid, self.panel, self.day, p, self.ind)), 0,
                                   f"{sid} {p}")

    def test_slice_guard(self):
        df = next(iter(self.panel.values()))
        out = FA._slice(FA._norm(df), self.day)
        self.assertLessEqual(out.index.max(), self.day)


class TestInterface(unittest.TestCase):
    def test_control_has_no_score(self):
        p, _ = synth(n_days=30, n_stocks=3)
        with self.assertRaises(ValueError):
            FA.score("ctrl_rand_01", p, "2022-02-01", {})

    def test_param_keys_checked(self):
        p, _ = synth(n_days=30, n_stocks=3)
        with self.assertRaises(ValueError):
            FA.score("mom_01", p, "2022-02-01", {"m": 5})
        with self.assertRaises(KeyError):
            FA.score("nope_01", p, "2022-02-01", {})

    def test_industry_required(self):
        p, _ = synth(n_days=30, n_stocks=3)
        with self.assertRaises(ValueError):
            FA.score("ind_01", p, "2022-02-01", {"n": 5})

    def test_grid_size_matches_catalog(self):
        total = sum(len(list(FA.iter_param_grid(s))) for s in FA.scorable_ids())
        cat = registry.load_catalog()["strategies"]
        want = 0
        for s in cat:
            if s["id"] in FA.F:
                k = 1
                for v in s["params"].values():
                    k *= len(v)
                want += k
        self.assertEqual(total, want)

    def test_param_values_must_be_in_grid(self):
        # 审阅意见 2:网格外的取值会绕过多重检验计数,必须报错
        p, _ = synth(n_days=60, n_stocks=3)
        grid = FA.registry.load_catalog()["strategies"]
        n_grid = next(s for s in grid if s["id"] == "mom_01")["params"]["n"]
        self.assertNotIn(7, n_grid)
        with self.assertRaises(ValueError):
            FA.score("mom_01", p, "2022-02-01", {"n": 7})
        with self.assertRaises(ValueError):
            FA.factor_frame("mom_01", p, {"n": 7})
        FA.score("mom_01", p, "2022-03-01", {"n": n_grid[0]})       # 网格内正常

    def test_public_api_rejects_offgrid(self):
        # 审阅意见:公开 API 不能有关闭网格校验的开关;n=7 不在清单 n 的网格里,必须抛错
        p, _ = synth(n_days=60, n_stocks=3)
        with self.assertRaises(ValueError):
            FA.score("mom_01", p, "2022-02-01", {"n": 7})
        with self.assertRaises(ValueError):
            FA.factor_frame("mom_01", p, {"n": 7})
        with self.assertRaises(TypeError):
            FA.score("mom_01", p, "2022-02-01", {"n": 7}, check_grid=False)
        with self.assertRaises(TypeError):
            FA.factor_frame("mom_01", p, {"n": 7}, check_grid=False)

    def test_brk_06_direction(self):
        # 前 3 日最低 100:A 跌 20% → +0.2,B 跌 10% → +0.1,C 创新高 → 0;跌得越深分数越高
        p = {"A": bars([100, 100, 100, 80]), "B": bars([100, 100, 100, 90]),
             "C": bars([100, 100, 100, 120])}
        got = sc("brk_06", p, {"n": 3})
        self.assertAlmostEqual(got["A"], 0.2)
        self.assertAlmostEqual(got["B"], 0.1)
        self.assertEqual(got["C"], 0.0)
        self.assertGreater(got["A"], got["B"])
        self.assertGreater(got["B"], got["C"])

    def test_catalog_params_logged(self):
        # 固定常数登记在 catalog 的 fixed_params 里(进哈希、不计试验次数),且必须和代码里实际用的常数一致
        cat = {s["id"]: s for s in registry.load_catalog()["strategies"]}
        self.assertEqual(cat["sq_05"]["fixed_params"]["SQUEEZE_Q"], FA.SQUEEZE_Q)
        self.assertEqual(cat["cmb_07"]["fixed_params"]["CMB07_REV_N"], FA.CMB07_REV_N)
        self.assertEqual(cat["brk_05"]["fixed_params"]["breakout_mode"], "last")
        self.assertEqual(cat["ext_05"]["fixed_params"]["LIMIT_TOUCH"], FA.LIMIT_TOUCH)
        for sid in ("sq_06", "h52_02"):
            self.assertEqual(cat[sid]["fixed_params"]["YEAR"], FA.YEAR, sid)
        for sid in ("cmb_04", "cmb_05", "cmb_06", "cmb_09"):
            self.assertEqual(cat[sid]["fixed_params"]["equal_weight"], 0.5, sid)
        for s in cat.values():                       # 固定常数不能混进网格参数
            self.assertFalse(set(s.get("fixed_params", {})) & set(s["params"]), s["id"])

    def test_missing_bar_gives_no_score(self):
        # 审阅意见:当天行情整行缺失(停牌/无数据)时,EWM(osc_03)或 fillna(0)(brk_05/sq_05/h52_02)
        # 不能把它变成有效分数;其他股票不受影响
        panel, ind = synth(n_days=300, n_stocks=3)
        day = panel["S00"].index[-1]
        panel["S00"].iloc[-1, :] = np.nan
        for sid in ("osc_03", "brk_05", "sq_05", "h52_02", "mom_01"):
            params = next(FA.iter_param_grid(sid))
            got = FA.factor_frame(sid, panel, params, ind).loc[day]
            self.assertTrue(np.isnan(got["S00"]), sid)

    def test_day_must_be_trading_day(self):
        # 审阅意见 3:day 不在面板里(周末/晚于数据)不能静默用更早一天的数据
        p, _ = synth(n_days=60, n_stocks=3)
        n = FA.iter_param_grid("mom_01").__next__()
        with self.assertRaises(ValueError):
            FA.score("mom_01", p, "2022-02-05", n)              # 周六
        with self.assertRaises(ValueError):
            FA.score("mom_01", p, "2030-01-01", n)              # 晚于数据末日
        with self.assertRaises(ValueError):
            FA.score("mom_01", p, "2020-01-01", n)              # 早于数据起点,切完为空

    def test_duplicate_dates_rejected(self):
        df = bars([1, 2, 3])
        df = pd.concat([df, df.iloc[[-1]]])
        with self.assertRaises(ValueError):
            FA.score("mom_01", {"A": df}, "2024-01-10", {"n": 1})


if __name__ == "__main__":
    unittest.main()
