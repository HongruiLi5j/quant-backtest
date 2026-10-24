"""T008 S1:95 个候选策略(不含 5 个随机对照)的向量化因子/打分函数。

对外接口:
  score(strategy_id, panel, day, params, industry=None) -> {code: score}
      只用 day 及以前的数据,返回 day 当天(面板里 ≤day 的最后一个交易日)每只股票的得分,越高越靠前;
      得分无效(窗口不满、除零)的股票不出现在结果里。
  factor_frame(strategy_id, panel, params, industry=None, day=None) -> DataFrame(日期 × 代码)
      一次算出整段历史的得分,S2/S3 向量化使用;day 不为空时先截断到 ≤day。
  iter_param_grid(strategy_id) -> 清单网格的全部参数组合。

输入:panel = {code: DataFrame},列 open/high/low/close/volume(大小写均可,日期在索引或 date 列),
可选 amount(成交额,缺省用 close*volume)和 turnover(换手率,缺省则换手率类因子无得分);
industry = {code: 行业名},行业类/复合 cmb_03 用。

防未来函数(说实话:真正的证据是测试,不是运行时断言):
  1. 入口用 index <= day 把每只股票的数据切掉 day 之后的行(切片本身就保证了不含未来行,
     所以不再写一个永远不会触发的"事后断言");score 还要求 day 必须是面板里的交易日,
     否则抛 ValueError,避免传错日期时静默拿旧数据当当天信号;
  2. 所有算子只用 shift(k≥0) 与向后看的滚动窗口,窗口不满一律给 NaN(min_periods=窗口长度),
     所以"截断到 T 重算"与"全量计算"在 T 日的值必须一致(测试里逐策略验证)。

约定:所有得分都是"越高越靠前"(与清单 direction=higher_better 一致),负号已在函数里处理,不得事后翻转。
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from . import registry

LIMIT_TOUCH = 0.095     # ext_05:最高价较昨收涨幅 ≥9.5% 视为触及涨停(只覆盖 10% 板块,ST/创业板的涨跌幅不同,是近似)
# 以下常数/口径清单描述里没有规定,已登记在 strategies_catalog.json 各策略的 fixed_params 里
# (进清单哈希、不计试验次数;实际取值以 catalog 为准)。改动它们必须同步改 catalog,
# test_catalog_params_logged 会核对两边一致:
SQUEEZE_Q = 0.2         # sq_05:带宽分位 ≤20% 算"收窄"
CMB07_REV_N = 5         # cmb_07:反转分项固定用 5 日收益(清单只有参数 n)
YEAR = 250              # "一年"按 250 个交易日


# ---------------------------------------------------------------- 面板整理

_REQUIRED = ("open", "high", "low", "close", "volume")


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名为小写、索引为升序的 DatetimeIndex;重复日期直接报错。"""
    d = df.copy()
    d.columns = [str(c).lower() for c in d.columns]
    if "date" in d.columns:
        d = d.set_index("date")
    d.index = pd.to_datetime(d.index)
    d = d.sort_index()
    if d.index.has_duplicates:
        raise ValueError("面板里有重复日期")
    miss = [c for c in _REQUIRED if c not in d.columns]
    if miss:
        raise ValueError(f"缺少列: {miss}")
    return d


def _slice(df: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """切到 ≤day。过滤条件本身保证无未来行;防未来函数靠测试里的"改乱未来行"与"截断重算"。"""
    return df.loc[df.index <= day]


class Wide:
    """日期 × 代码 的宽表集合。"""

    def __init__(self, panel: dict, industry: dict | None, day=None):
        if not panel:
            raise ValueError("面板为空")
        d = pd.Timestamp(day) if day is not None else None
        frames = {}
        for code, df in panel.items():
            n = _norm(df)
            frames[code] = _slice(n, d) if d is not None else n
        cols = sorted(frames)

        def field(name, fallback=None):
            parts = {}
            for k in cols:
                f = frames[k]
                if name in f.columns:
                    parts[k] = f[name].astype(float)
                elif fallback is not None:
                    parts[k] = fallback(f)
                else:
                    parts[k] = pd.Series(np.nan, index=f.index)
            return pd.concat(parts, axis=1).sort_index()

        self.o, self.h = field("open"), field("high")
        self.l, self.c = field("low"), field("close")
        self.v = field("volume")
        self.a = field("amount", lambda f: f["close"].astype(float) * f["volume"].astype(float))
        self.t = field("turnover")
        self.idx = self.c.index
        self.pc = self.c.shift(1)
        self.ret = self.c / self.pc - 1
        self.mret = self.ret.mean(axis=1)       # 等权市场日收益
        self.industry = industry
        self.codes = cols

    # 等权市场 n 日累计收益
    def mkt_n(self, n: int) -> pd.Series:
        return np.expm1(np.log1p(self.mret).rolling(n).sum())

    def ret_n(self, n: int) -> pd.DataFrame:
        return self.c / self.c.shift(n) - 1

    def ind_mean(self, x: pd.DataFrame) -> pd.DataFrame:
        """每个交易日,行业内等权均值,股票继承所属行业的值。"""
        if not self.industry:
            raise ValueError("行业类因子需要 industry 映射")
        labels = np.array([self.industry.get(k) for k in x.columns], dtype=object)
        return x.T.groupby(labels).transform("mean").T


# ---------------------------------------------------------------- 通用算子


def _sma(x, n):
    return x.rolling(n).mean()


def _rk(x):
    """日内截面百分位排名(NaN 保持 NaN)。"""
    return x.rank(axis=1, pct=True)


def _frac(cond, valid, n):
    """窗口内 cond 为真的占比;valid 为假的位置记 NaN。"""
    return cond.astype(float).where(valid).rolling(n).mean()


def _atr(W, n):
    tr = np.maximum(np.maximum(W.h - W.l, (W.h - W.pc).abs()), (W.l - W.pc).abs())
    return tr.rolling(n).mean()


def _win(x: pd.DataFrame, n: int, fn, other: pd.Series | None = None, chunk: int = 64) -> pd.DataFrame:
    """对每个长度 n 的窗口整体套 fn(窗口数组 (T',N,n)[, other 窗口 (T',1,n)]) -> (T',N)。分块控内存。"""
    arr = x.to_numpy(float)
    T, N = arr.shape
    out = np.full((T, N), np.nan)
    if T >= n:
        ow = None
        if other is not None:
            ow = sliding_window_view(other.to_numpy(float), n)[:, None, :]
        with np.errstate(all="ignore"):
            for s in range(0, N, chunk):
                sw = sliding_window_view(arr[:, s:s + chunk], n, axis=0)
                out[n - 1:, s:s + chunk] = fn(sw) if ow is None else fn(sw, ow)
    return pd.DataFrame(out, index=x.index, columns=x.columns)


def _resid(r, m):
    """r 对 m 做带截距的一元回归的残差(窗口内),形状同 r。"""
    rm = r - r.mean(-1, keepdims=True)
    mm = m - m.mean(-1, keepdims=True)
    beta = (rm * mm).sum(-1) / (mm * mm).sum(-1)
    return rm - beta[..., None] * mm


def _resid_std(n):
    return lambda r, m: np.sqrt((_resid(r, m) ** 2).sum(-1) / (n - 1))   # 样本标准差口径(ddof=1)


def _resid_skew(r, m):
    e = _resid(r, m)
    s2 = (e ** 2).mean(-1)
    sk = (e ** 3).mean(-1) / s2 ** 1.5
    return np.where(s2 < 1e-18, np.nan, sk)


def _mdd(sw):
    peak = np.maximum.accumulate(sw, axis=-1)
    return (sw / peak - 1).min(-1)


def _rsi(c, n):
    d = c.diff()
    g = d.clip(lower=0).where(d.notna()).rolling(n).mean()
    ls = (-d).clip(lower=0).where(d.notna()).rolling(n).mean()
    den = g + ls
    return (100 * g / den).where(den > 0)


# ---------------------------------------------------------------- 各策略

F = {}


def _reg(sid):
    def deco(fn):
        F[sid] = fn
        return fn
    return deco


# --- 动量
@_reg("mom_01")
def _(W, p): return W.ret_n(p["n"])


@_reg("mom_02")
def _(W, p): return W.c.shift(p["skip"]) / W.c.shift(p["n"]) - 1


@_reg("mom_03")
def _(W, p): return W.ret_n(p["n"]) / W.ret.rolling(p["n"]).std()


@_reg("mom_04")
def _(W, p): return np.log(W.c / W.c.shift(p["n"]))


@_reg("mom_05")
def _(W, p): return W.ret_n(p["n"]) - (W.c.shift(p["n"]) / W.c.shift(2 * p["n"]) - 1)


@_reg("mom_06")
def _(W, p): return W.ret_n(p["n"]).sub(W.mkt_n(p["n"]), axis=0)


@_reg("mom_07")
def _(W, p): return _frac(W.ret > 0, W.ret.notna(), p["n"])


@_reg("mom_08")
def _(W, p): return W.ret_n(p["n"]) / W.ret.abs().rolling(p["n"]).sum()


# --- 反转
@_reg("rev_01")
def _(W, p): return -W.ret_n(p["n"])


@_reg("rev_02")
def _(W, p): return -W.ret_n(p["n"]).sub(W.mkt_n(p["n"]), axis=0)


@_reg("rev_03")
def _(W, p):
    ok = W.ret.abs().rolling(p["n"]).max() <= p["cap"]
    return (-W.ret_n(p["n"])).where(ok)


@_reg("rev_04")
def _(W, p): return -W.ret_n(p["n"])


@_reg("rev_05")
def _(W, p): return -(W.c / _sma(W.c, p["n"]) - 1)


# --- 均线
@_reg("ma_01")
def _(W, p): return W.c / _sma(W.c, p["n"]) - 1


@_reg("ma_02")
def _(W, p): return _sma(W.c, p["short"]) / _sma(W.c, p["long"]) - 1


@_reg("ma_03")
def _(W, p):
    b = p["base"]
    m1, m2, m3 = _sma(W.c, b), _sma(W.c, 2 * b), _sma(W.c, 4 * b)
    return (m1 / m2 - 1) + (m2 / m3 - 1)


@_reg("ma_04")
def _(W, p):
    m = _sma(W.c, p["n"])
    return m / m.shift(p["k"]) - 1


@_reg("ma_05")
def _(W, p):
    m = _sma(W.c, p["m"])
    return _frac(W.c > m, m.notna(), p["n"])


@_reg("ma_06")
def _(W, p):
    diff = _sma(W.c, p["short"]) - _sma(W.c, p["long"])
    pos = (diff > 0).astype(float)
    cs = pos.cumsum()
    run = cs - cs.where(pos == 0).ffill().fillna(0)      # 连续在上方的天数,金叉当天=1
    return (1.0 / run).where(run > 0, 0.0).where(diff.notna())


@_reg("ma_07")
def _(W, p):
    ef = W.c.ewm(span=p["fast"], adjust=False, min_periods=p["slow"]).mean()
    es = W.c.ewm(span=p["slow"], adjust=False, min_periods=p["slow"]).mean()
    return (ef - es) / W.c


@_reg("ma_08")
def _(W, p):
    b = p["base"]
    ms = np.stack([_sma(W.c, k * b).to_numpy() for k in (1, 2, 4)])
    with np.errstate(all="ignore"):
        disp = ms.std(axis=0) / ms.mean(axis=0)
    return -pd.DataFrame(disp, index=W.idx, columns=W.c.columns)


@_reg("ma_09")
def _(W, p): return (W.c - _sma(W.c, p["n"])) / W.c.rolling(p["n"]).std()


# --- 突破
def _prev_hi(W, n): return W.h.shift(1).rolling(n).max()
def _prev_lo(W, n): return W.l.shift(1).rolling(n).min()


@_reg("brk_01")
def _(W, p): return W.c / _prev_hi(W, p["n"]) - 1


@_reg("brk_02")
def _(W, p):
    hh, ll = W.h.rolling(p["n"]).max(), W.l.rolling(p["n"]).min()
    return ((W.c - ll) / (hh - ll)).where(hh > ll)


@_reg("brk_03")
def _(W, p): return (W.c - _prev_hi(W, p["n"])) / _atr(W, p["atr"])


@_reg("brk_04")
def _(W, p):
    ph = _prev_hi(W, p["n"])
    vm = _sma(W.v, p["n"])
    flag = (W.c > ph) & (W.v > p["k"] * vm)
    return (W.c / ph - 1).where(flag, 0.0).where(ph.notna() & vm.notna())


@_reg("brk_05")
def _(W, p):
    # 口径偏离:清单写"首次新高后的天数衰减",这里取"距最近一次新高的天数"(已登记为 brk_05 的 fixed_params: breakout_mode=last)
    n = p["n"]
    ph = _prev_hi(W, n)
    nh = (W.c > ph)
    pos = pd.DataFrame(np.tile(np.arange(len(W.idx))[:, None], (1, len(W.codes))),
                       index=W.idx, columns=W.c.columns).astype(float)
    last = pos.where(nh).ffill()
    d = pos - last                                        # 距最近一次新高的天数
    return (1 - d / n).clip(lower=0).fillna(0.0).where(ph.notna())


@_reg("brk_06")
def _(W, p):
    # 跌破幅度取正:跌破前 n 日最低 20% 得 +0.2、10% 得 +0.1,没跌破得 0,跌得越深越靠前
    return (1 - W.c / _prev_lo(W, p["n"])).clip(lower=0)


# --- 低波动
@_reg("lowvol_01")
def _(W, p): return -W.ret.rolling(p["n"]).std()


@_reg("lowvol_02")
def _(W, p): return -((W.h - W.l) / W.c).rolling(p["n"]).mean()


@_reg("lowvol_03")
def _(W, p):
    sq = (W.ret.clip(upper=0) ** 2).where(W.ret.notna())
    return -np.sqrt(sq.rolling(p["n"]).mean())


@_reg("lowvol_04")
def _(W, p): return _win(W.c, p["n"], _mdd)


@_reg("lowvol_05")
def _(W, p): return -(_atr(W, p["n"]) / W.c)


# --- 特质波动(对等权市场收益回归的残差)
@_reg("ivol_01")
def _(W, p): return -_win(W.ret, p["n"], _resid_std(p["n"]), W.mret)


@_reg("ivol_02")
def _(W, p): return -_win(W.ret, p["n"], _resid_skew, W.mret)


@_reg("ivol_03")
def _(W, p):
    s = _win(W.ret, p["short"], _resid_std(p["short"]), W.mret)
    lg = _win(W.ret, p["long"], _resid_std(p["long"]), W.mret)
    return -(s / lg)


# --- 量能/流动性
@_reg("liq_01")
def _(W, p): return W.v / _sma(W.v, p["n"])


@_reg("liq_02")
def _(W, p): return _sma(W.a, p["n"]) / _sma(W.a, p["m"])


@_reg("liq_03")
def _(W, p): return -_sma(W.t, p["n"])


@_reg("liq_04")
def _(W, p): return _sma(W.t, p["short"]) / _sma(W.t, p["long"]) - 1


@_reg("liq_05")
def _(W, p): return (W.ret.abs() / W.a).rolling(p["n"]).mean()


@_reg("liq_06")
def _(W, p):
    n = p["n"]
    w = np.arange(n) - (n - 1) / 2
    return _win(W.v, n, lambda sw: (sw * w).sum(-1) / (w * w).sum() / sw.mean(-1))


@_reg("liq_07")
def _(W, p): return 1 - W.v / _sma(W.v, p["n"])


@_reg("liq_08")
def _(W, p): return -(W.a.rolling(p["n"]).std() / _sma(W.a, p["n"]))


# --- 量价形态
@_reg("pv_01")
def _(W, p):
    vchg = (W.v / W.v.shift(1) - 1).replace([np.inf, -np.inf], np.nan)
    return W.ret.rolling(p["n"]).corr(vchg)


@_reg("pv_02")
def _(W, p):
    vm = W.v.shift(1).rolling(p["n"]).mean()
    flag = (W.ret > 0) & (W.v > p["k"] * vm)
    return _frac(flag, W.ret.notna() & vm.notna(), p["n"])


@_reg("pv_03")
def _(W, p):
    # 口径:清单写"能量潮N日变化率"。OBV 增量 = Σ sign(涨跌)·成交量,这里除以 N 日总成交量做标准化,
    # 得分在 [-1,1];不是 OBV(t)/OBV(t-N)-1(OBV 可为 0/负,比值不稳定)
    sv = (np.sign(W.ret) * W.v)
    return sv.rolling(p["n"]).sum() / W.v.rolling(p["n"]).sum()


@_reg("pv_04")
def _(W, p):
    rng = W.h - W.l
    up = (W.h - np.maximum(W.o, W.c)) / rng
    return -up.where(rng > 0).rolling(p["n"]).mean()


@_reg("pv_05")
def _(W, p):
    rng = W.h - W.l
    return ((W.c - W.l) / rng).where(rng > 0).rolling(p["n"]).mean()


@_reg("pv_06")
def _(W, p):
    n = p["n"]
    return (np.sign(W.ret) * W.a).rolling(n).sum() / W.a.rolling(n).sum()


# --- 波动收敛
def _bw_pct(W, n, pw):
    """布林带宽度(4σ/均线)在过去 pw 日里的分位(含当日)。"""
    bw = 4 * W.c.rolling(n).std() / _sma(W.c, n)
    return bw.rolling(pw).rank(pct=True)


@_reg("sq_01")
def _(W, p): return -_bw_pct(W, p["n"], p["pct_window"])


@_reg("sq_02")
def _(W, p): return -(_atr(W, p["short"]) / _atr(W, p["long"]))


@_reg("sq_03")
def _(W, p):
    amp = (W.h - W.l) / W.c
    return -(amp.rolling(p["n"]).mean() / amp.shift(p["n"]).rolling(p["m"]).mean())


@_reg("sq_04")
def _(W, p):
    amp = (W.h - W.l) / W.c
    return (amp.rolling(p["n"]).min() / amp).replace([np.inf, -np.inf], np.nan)


@_reg("sq_05")
def _(W, p):
    pr = _bw_pct(W, p["n"], p["pct_window"]).shift(1)
    ph, pl = _prev_hi(W, p["n"]), _prev_lo(W, p["n"])
    direction = np.where(W.c > ph, 1.0, np.where(W.c < pl, -1.0, 0.0))
    sc = pd.DataFrame(direction, index=W.idx, columns=W.c.columns) * (pr <= SQUEEZE_Q).astype(float)
    return sc.where(pr.notna() & ph.notna() & pl.notna())


@_reg("sq_06")
def _(W, p): return -(W.ret.rolling(p["n"]).std().rolling(YEAR).rank(pct=True))


# --- 超买超卖
@_reg("osc_01")
def _(W, p): return -_rsi(W.c, p["n"])


@_reg("osc_02")
def _(W, p): return _rsi(W.c, p["n"])


@_reg("osc_03")
def _(W, p):
    hh, ll = W.h.rolling(p["n"]).max(), W.l.rolling(p["n"]).min()
    rsv = ((W.c - ll) / (hh - ll) * 100).where(hh > ll)
    a = 1.0 / p["smooth"]
    k = rsv.ewm(alpha=a, adjust=False).mean()
    d = k.ewm(alpha=a, adjust=False).mean()
    return (-(3 * k - 2 * d)).where(rsv.notna())      # 当天 RSV 缺失(窗口内有缺失行情)则无得分,不沿用旧 K/D


@_reg("osc_04")
def _(W, p):
    hh, ll = W.h.rolling(p["n"]).max(), W.l.rolling(p["n"]).min()
    return (100 * (hh - W.c) / (hh - ll)).where(hh > ll)          # = -(威廉%R)


@_reg("osc_05")
def _(W, p):
    n = p["n"]
    tp = (W.h + W.l + W.c) / 3
    mad = _win(tp, n, lambda sw: np.abs(sw - sw.mean(-1, keepdims=True)).mean(-1))
    return -((tp - _sma(tp, n)) / (0.015 * mad)).where(mad > 0)


@_reg("osc_06")
def _(W, p):
    m, s = _sma(W.c, p["n"]), W.c.rolling(p["n"]).std()
    return -((W.c - (m - p["k"] * s)) / (2 * p["k"] * s)).where(s > 0)


# --- 行业动量
@_reg("ind_01")
def _(W, p): return W.ind_mean(W.ret_n(p["n"]))


@_reg("ind_02")
def _(W, p):
    r = W.ret_n(p["n"])
    return W.ind_mean(r).sub(r.mean(axis=1), axis=0)


@_reg("ind_03")
def _(W, p):
    r = W.ret_n(p["n"])
    return r - W.ind_mean(r)


@_reg("ind_04")
def _(W, p):
    n = p["n"]
    return W.ind_mean(W.ret_n(n) - (W.c.shift(n) / W.c.shift(2 * n) - 1))


@_reg("ind_05")
def _(W, p):
    r = W.ret_n(p["n"])
    return W.ind_mean((r > 0).astype(float).where(r.notna()))


@_reg("ind_06")
def _(W, p):
    return W.ind_mean(W.ret_n(p["n"])) / W.ind_mean(W.ret).rolling(p["vol"]).std()


# --- 隔夜
def _ovn(W): return W.o / W.pc - 1


@_reg("ovn_01")
def _(W, p): return _ovn(W).rolling(p["n"]).mean()


@_reg("ovn_02")
def _(W, p): return (W.c / W.o - 1).rolling(p["n"]).mean()


@_reg("ovn_03")
def _(W, p): return _ovn(W).rolling(p["n"]).mean() - (W.c / W.o - 1).rolling(p["n"]).mean()


@_reg("ovn_04")
def _(W, p):
    g = _ovn(W)
    return _frac(g > p["gap"], g.notna(), p["n"])


@_reg("ovn_05")
def _(W, p): return -_ovn(W).rolling(p["n"]).mean()


# --- 52 周高点
@_reg("h52_01")
def _(W, p): return W.c / W.h.rolling(p["n"]).max()


@_reg("h52_02")
def _(W, p):
    ph = _prev_hi(W, YEAR)
    return _frac(W.c > ph, ph.notna(), p["n"]) * p["n"]


@_reg("h52_03")
def _(W, p):
    hh, ll = W.h.rolling(p["n"]).max(), W.l.rolling(p["n"]).min()
    return ((W.c - ll) / (hh - ll)).where(hh > ll)


# --- 极端收益
@_reg("ext_01")
def _(W, p): return -W.ret.rolling(p["n"]).max()


@_reg("ext_02")
def _(W, p): return -W.ret.rolling(p["n"]).min()


@_reg("ext_03")
def _(W, p): return -W.ret.rolling(p["n"]).skew()


@_reg("ext_04")
def _(W, p): return -W.ret.rolling(p["n"]).kurt()


@_reg("ext_05")
def _(W, p):
    hit = (W.h / W.pc - 1) >= LIMIT_TOUCH
    return -hit.astype(float).where(W.pc.notna() & W.h.notna()).rolling(p["n"]).sum()


# --- 低成交额代理
@_reg("size_01")
def _(W, p): return -_sma(W.a, p["n"])


@_reg("size_02")
def _(W, p): return -_sma(W.c, p["n"])


@_reg("size_03")
def _(W, p):
    return p["w"] * _rk(-_sma(W.a, p["n"])) + (1 - p["w"]) * _rk(-W.ret.rolling(p["n"]).std())


@_reg("size_04")
def _(W, p): return 1 - _sma(W.a, p["n"]) / _sma(W.a, p["m"])


# --- 复合(各分项先做日内截面百分位排名再加权)
@_reg("cmb_01")
def _(W, p): return p["w"] * _rk(W.ret_n(p["n"])) + (1 - p["w"]) * _rk(-W.ret.rolling(p["n"]).std())


@_reg("cmb_02")
def _(W, p): return p["w"] * _rk(-W.ret_n(p["n"])) + (1 - p["w"]) * _rk(W.v / _sma(W.v, p["n"]))


@_reg("cmb_03")
def _(W, p):
    r = W.ret_n(p["n"])
    return p["w"] * _rk(r) + (1 - p["w"]) * _rk(W.ind_mean(r))


@_reg("cmb_04")
def _(W, p):
    brk = W.c / _prev_hi(W, p["n"]) - 1
    vr = (W.v / _sma(W.v, p["n"])).clip(upper=p["k"])        # 量比超过 k 倍后不再加分
    return 0.5 * _rk(brk) + 0.5 * _rk(vr)


@_reg("cmb_05")
def _(W, p): return 0.5 * _rk(W.c / _sma(W.c, p["n"]) - 1) + 0.5 * _rk(-_rsi(W.c, p["rsi"]))


@_reg("cmb_06")
def _(W, p): return 0.5 * _rk(-W.ret.rolling(p["n"]).std()) + 0.5 * _rk(-W.ret_n(p["m"]))


@_reg("cmb_07")
def _(W, p):
    n = p["n"]
    return 0.25 * (_rk(W.ret_n(n)) + _rk(-W.ret_n(CMB07_REV_N)) + _rk(-W.ret.rolling(n).std())
                   + _rk(W.v / _sma(W.v, n)))


@_reg("cmb_08")
def _(W, p): return W.ret_n(p["long"]) - W.ret_n(p["short"])


@_reg("cmb_09")
def _(W, p): return 0.5 * _rk(W.c / W.h.rolling(p["n"]).max()) + 0.5 * _rk(W.ret_n(p["m"]))


@_reg("cmb_10")
def _(W, p):
    return p["w"] * _rk(_ovn(W).rolling(p["n"]).mean()) + (1 - p["w"]) * _rk(-_sma(W.a, p["n"]))


# ---------------------------------------------------------------- 对外接口

_CAT = None


def _catalog():
    global _CAT
    if _CAT is None:
        _CAT = {s["id"]: s for s in registry.load_catalog()["strategies"]}
    return _CAT


def scorable_ids() -> list[str]:
    """有因子函数的策略 ID(清单里除随机对照以外的全部)。"""
    return sorted(F)


def iter_param_grid(strategy_id: str):
    """清单网格的全部参数组合(笛卡尔积,键序同清单)。"""
    grid = _catalog()[strategy_id]["params"]
    keys = list(grid)
    for vals in itertools.product(*(grid[k] for k in keys)):
        yield dict(zip(keys, vals))


def _check(strategy_id, params, _check_grid=True):
    cat = _catalog()
    if strategy_id not in cat:
        raise KeyError(f"清单里没有策略 {strategy_id}")
    if strategy_id not in F:
        raise ValueError(f"{strategy_id} 是随机对照,由回放引擎按匹配抽样生成,没有因子打分")
    want = set(cat[strategy_id]["params"])
    if set(params) != want:
        raise ValueError(f"{strategy_id} 的参数应为 {sorted(want)},收到 {sorted(params)}")
    if _check_grid:
        for k, v in params.items():
            if v not in cat[strategy_id]["params"][k]:
                raise ValueError(f"{strategy_id} 的参数 {k}={v} 不在清单网格 {cat[strategy_id]['params'][k]} 里;"
                                 "网格外的试验会绕过多重检验计数")


def factor_frame(strategy_id: str, panel: dict, params: dict, industry: dict | None = None,
                 day=None) -> pd.DataFrame:
    """整段历史的得分表(日期 × 代码)。day 不为空时先截断到 ≤day。参数值必须在清单网格里。"""
    return _factor_frame(strategy_id, panel, params, industry, day, True)


def _factor_frame(strategy_id, panel, params, industry, day, _check_grid):
    """内部实现。_check_grid=False 只给单元测试手算小窗口用,公开 API 不暴露,以免绕过试验计数。"""
    _check(strategy_id, params, _check_grid)
    d = pd.Timestamp(day) if day is not None else None
    W = Wide(panel, industry, d)
    out = F[strategy_id](W, params)
    # 当天收盘缺失(停牌/无数据)的股票一律无得分:避免 EWM、fillna(0) 类算子把缺失行情变成有效分数
    return out.replace([np.inf, -np.inf], np.nan).where(W.c.notna())


def score(strategy_id: str, panel: dict, day, params: dict, industry: dict | None = None) -> dict:
    """day 日收盘后的得分 {code: float}(越高越靠前)。只用 ≤day 的数据;无效得分不返回。
    day 必须是面板里的交易日,否则抛 ValueError(不静默退回更早的一天)。参数必须在清单网格里。"""
    return _score(strategy_id, panel, day, params, industry, True)


def _score(strategy_id, panel, day, params, industry, _check_grid):
    fr = _factor_frame(strategy_id, panel, params, industry, day, _check_grid)
    if fr.empty or fr.index[-1] != pd.Timestamp(day):
        raise ValueError(f"{day} 不是面板里的交易日(≤day 的最后一个交易日是 "
                         f"{None if fr.empty else fr.index[-1].date()})")
    last = fr.iloc[-1]
    return {k: float(v) for k, v in last.items() if np.isfinite(v)}
