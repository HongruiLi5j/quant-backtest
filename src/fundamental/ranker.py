"""月度价值排名(T014 S3):12 个基本面因子 -> 行业内 Z-score -> 等权(或配置权重)综合分 -> 前 N / 后 N。

方法(每一步都可以在面试里讲清楚):
1. 因子原始值来自 factors.score_all(已按 available_date 屏蔽排名日之后才公告的行,防未来函数)。
2. 行业中性化:每个因子在"同一行业"内做 Z-score,z = (x - 行业均值) / 行业标准差(总体标准差 ddof=0)。
   这样"银行永远便宜、软件永远贵"的结构性差异被扣掉,比较的是"在同行里便宜不便宜"。
   某行业该因子有效样本数 < min_industry_size(默认 8)时,均值/方差估计太不稳,回退到全市场 Z-score 并计数。
   行业内标准差为 0(全部相同)时 z=0,不除零。
3. 极端值:z 截尾到 [-z_clip, z_clip](默认 3),避免单个异常财务数据(如 ROE 几千%)主导综合分。
   这是固定的稳健化参数,不是调参结果。
4. 方向:direction=-1 的因子(如资产负债率)z 乘 -1,统一成"越大越好"。
5. 缺数据处理(missing_mode 可配):
   - "impute":缺失因子的 z 用同行业(样本不足则全市场)已有 z 的中位数补;有效因子数 < min_factors 的整只剔除,
     避免"几乎没数据的股票拿到一个中位数分数混进排名"。
   - "drop":任一参与权重的因子缺失,整只剔除。
6. 综合分 = 各因子 z 的加权平均(默认等权)。等权是故意的:不优化权重(优化权重 = 过拟合 + 多重检验)。
   改权重就是新的一次尝试,应登记。
7. 输出:综合分降序;并列按代码升序,保证重复运行逐位一致。前 top_n 为"做多候选",
   后 top_n(不与前 top_n 重叠)为"避开候选"。

daily_price_df(长表:code, date, close)的用途只有两个:
 - 丢弃 as_of_date 之后的行(防未来函数,即使调用者误传了);
 - require_price_on_date=True 时,排名日当天没有行情的股票(停牌 / 未上市 / 已退市)剔除。
价格不参与打分(估值用的未复权价与市值已在 fundamental_df 里)。

这是"打分函数",不是经过验证的 Alpha:没做样本外检验、没扣成本、有幸存者与重述偏差、价值陷阱风险。
本模块不联网、不读写文件、不修改输入。
"""
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from . import factors as F

log = logging.getLogger(__name__)

MISSING_MODES = ("impute", "drop")
UNKNOWN_INDUSTRY = "(未知行业)"


@dataclass(frozen=True)
class RankConfig:
    weights: Optional[dict] = None          # 因子 id -> 权重(>=0);None = 12 个因子等权
    missing_mode: str = "impute"            # "impute" 或 "drop"
    min_industry_size: int = 8              # 行业内有效样本不足此数 -> 回退全市场
    min_factors: int = 6                    # impute 模式下至少要有多少个真实因子值
    z_clip: float = 3.0                     # z 截尾
    require_price_on_date: bool = True      # 排名日无行情的股票剔除(需要提供 daily_price_df)

    def resolved_weights(self) -> dict:
        w = {fid: 1.0 for fid in F.FACTOR_IDS} if self.weights is None else dict(self.weights)
        unknown = [k for k in w if k not in F.REGISTRY]
        if unknown:
            raise ValueError("unknown factor ids in weights: %s" % ",".join(sorted(map(str, unknown))))
        for k, v in w.items():
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not np.isfinite(v) or v < 0:
                raise ValueError("weight for %s must be a finite number >= 0" % k)
        if sum(w.values()) <= 0:
            raise ValueError("weights sum to zero")
        return {k: float(v) for k, v in w.items() if v > 0}

    def validate(self):
        if self.missing_mode not in MISSING_MODES:
            raise ValueError("missing_mode must be one of %s" % (MISSING_MODES,))
        if self.min_industry_size < 2:
            raise ValueError("min_industry_size must be >= 2")
        if self.z_clip <= 0:
            raise ValueError("z_clip must be > 0")
        self.resolved_weights()

    def cfg_hash(self) -> str:
        """配置指纹:同配置同指纹,便于登记"改配置 = 新尝试"。"""
        w = self.resolved_weights()
        payload = json.dumps(
            {"w": sorted(w.items()), "m": self.missing_mode, "i": self.min_industry_size,
             "f": self.min_factors, "z": self.z_clip, "p": self.require_price_on_date},
            sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass
class RankResult:
    as_of_date: Optional[pd.Timestamp]
    table: pd.DataFrame                     # 全部入选排名的股票,按综合分降序,含 rank 列
    longs: pd.DataFrame                     # 前 top_n
    avoids: pd.DataFrame                    # 后 top_n(与 longs 不重叠),最差的在最前
    excluded: pd.DataFrame                  # code / reason
    notes: list = field(default_factory=list)
    cfg_hash: str = ""
    n_input: int = 0


TABLE_COLS = ["code", "name", "industry", "rank", "score", "n_factors", "n_imputed", "n_fallback"]


def _empty_table(factor_ids=()):
    return pd.DataFrame(columns=TABLE_COLS + ["z_" + f for f in factor_ids])


def _empty_result(as_of, cfg, notes, n_input=0, excluded=None):
    t = _empty_table()
    ex = excluded if excluded is not None else pd.DataFrame(columns=["code", "reason"])
    return RankResult(as_of, t, t.copy(), t.copy(), ex, list(notes), cfg.cfg_hash(), n_input)


def _norm_codes(idx):
    return pd.Index([str(c).strip().zfill(6) if str(c).strip().isdigit() else str(c).strip() for c in idx])


def _prepare_fundamental(df):
    """统一成以 6 位代码为索引的表;重复代码属于输入错误。"""
    df = df.copy()
    if "code" in df.columns:
        df.index = _norm_codes(df["code"])
        df = df.drop(columns=["code"])
    else:
        df.index = _norm_codes(df.index)
    if df.index.has_duplicates:
        dup = sorted(set(df.index[df.index.duplicated()]))[:5]
        raise ValueError("duplicate codes in fundamental_df: %s" % ",".join(dup))
    return df


def _visible_prices(daily_price_df, as_of):
    """返回排名日及以前的行情(丢掉未来行);缺列/空返回 None。"""
    if daily_price_df is None or len(daily_price_df) == 0:
        return None
    if "code" not in daily_price_df.columns or "date" not in daily_price_df.columns:
        raise ValueError("daily_price_df needs columns code, date")
    p = daily_price_df[["code", "date"]].copy()
    p["code"] = _norm_codes(p["code"])
    p["date"] = pd.to_datetime(p["date"], errors="coerce")
    return p[p["date"].notna() & (p["date"] <= as_of)]


def _zscore_by_industry(raw: pd.Series, key: pd.Series, min_size: int):
    """返回 (z, 是否回退全市场)。z 未做方向调整与截尾。"""
    valid = raw.notna()
    grp = raw.groupby(key)
    cnt = grp.transform("count")
    mean = grp.transform("mean")
    std = grp.transform(lambda s: s.std(ddof=0))
    m_mean, m_std = raw.mean(), raw.std(ddof=0)
    use_ind = cnt >= min_size
    mu = mean.where(use_ind, m_mean)
    sd = std.where(use_ind, m_std)
    z = (raw - mu) / sd.where(sd > 0)
    z = z.where(~(sd == 0) | ~valid, 0.0)                    # 方差为 0:全部相同,z=0
    z = z.where(valid)
    fallback = valid & ~use_ind
    return z, fallback


def build_ranking(fundamental_df, daily_price_df, as_of_date, top_n=50, industry=None, names=None,
                  config: Optional[RankConfig] = None, issues: Optional[list] = None) -> RankResult:
    """对全市场算 12 因子综合分,返回前 top_n(做多候选)与后 top_n(避开候选)。

    fundamental_df: 见 factors.py 文档;可含 industry / name 列(也可用 industry= / names= 传 Series)。
    daily_price_df: 长表 code/date/close,可为 None(此时不做行情检查)。
    空输入、缺列返回空结果而不是抛异常;参数错误(top_n<1、非法日期、非法配置、重复代码)抛 ValueError。
    """
    cfg = config or RankConfig()
    cfg.validate()
    if isinstance(top_n, bool) or not isinstance(top_n, (int, np.integer)) or top_n < 1:
        raise ValueError("top_n must be an integer >= 1")
    try:
        as_of = pd.Timestamp(as_of_date)
    except (TypeError, ValueError):
        raise ValueError("bad as_of_date: %r" % (as_of_date,))
    if pd.isna(as_of):
        raise ValueError("bad as_of_date: %r" % (as_of_date,))
    as_of = as_of.normalize()

    if not isinstance(fundamental_df, pd.DataFrame) or len(fundamental_df) == 0:
        return _empty_result(as_of, cfg, ["empty_fundamental_df"])

    fdf = _prepare_fundamental(fundamental_df)
    n_input = len(fdf)
    notes = []
    weights = cfg.resolved_weights()
    fids = [f for f in F.FACTOR_IDS if f in weights]

    ind = None
    if industry is not None:
        ind = pd.Series(industry).copy()
        ind.index = _norm_codes(ind.index)
        ind = ind[~ind.index.duplicated(keep="last")].reindex(fdf.index)
    elif "industry" in fdf.columns:
        ind = fdf["industry"]
    if ind is None:
        notes.append("no_industry_info: 全部股票视为同一组(退化为全市场 Z-score)")
        ind = pd.Series(np.nan, index=fdf.index)
    ind = ind.astype(object).where(ind.notna(), UNKNOWN_INDUSTRY)
    ind = ind.map(lambda s: str(s).strip() or UNKNOWN_INDUSTRY)
    n_unknown = int((ind == UNKNOWN_INDUSTRY).sum())
    if n_unknown and n_unknown < n_input:
        notes.append("unknown_industry: %d 只缺行业,归入(未知行业)组" % n_unknown)

    if names is not None:
        nm = pd.Series(names).copy()
        nm.index = _norm_codes(nm.index)
        nm = nm[~nm.index.duplicated(keep="last")].reindex(fdf.index)
    elif "name" in fdf.columns:
        nm = fdf["name"]
    else:
        nm = pd.Series("", index=fdf.index)
    nm = nm.fillna("").astype(str)

    excluded = {}

    # 行情检查
    prices = _visible_prices(daily_price_df, as_of)
    if daily_price_df is None or len(daily_price_df) == 0:
        notes.append("no_price_table: 未做行情检查")
    elif cfg.require_price_on_date:
        have = set(prices.loc[prices["date"] == as_of, "code"])
        if not have:
            notes.append("price_table_has_no_bar_on_as_of: 排名日无任何行情(非交易日或数据未到),全部剔除")
        for c in fdf.index:
            if c not in have:
                excluded[c] = "no_price_on_date"

    # 因子原始值(factors 内部已按 available_date 屏蔽未来行)
    raw = F.score_all(fdf, as_of, issues)
    raw = raw.reindex(fdf.index)[fids]

    live = [c for c in fdf.index if c not in excluded]
    if not live:
        ex = pd.DataFrame(sorted(excluded.items()), columns=["code", "reason"])
        return _empty_result(as_of, cfg, notes, n_input, ex)

    # 缺失处理第一步:drop 模式整只剔除;两种模式都剔除"一个因子都没有"的
    n_real = raw.notna().sum(axis=1)
    for c in live:
        if n_real[c] == 0:
            excluded[c] = "no_factor_data"
        elif cfg.missing_mode == "drop" and n_real[c] < len(fids):
            excluded[c] = "missing_factors"
        elif cfg.missing_mode == "impute" and n_real[c] < cfg.min_factors:
            excluded[c] = "too_few_factors"
    live = [c for c in fdf.index if c not in excluded]
    if not live:
        ex = pd.DataFrame(sorted(excluded.items()), columns=["code", "reason"])
        return _empty_result(as_of, cfg, notes, n_input, ex)

    # 行业内 Z-score 只用保留下来的股票估计(被剔除的股票不影响他人分数)
    raw = raw.loc[live]
    key = ind.loc[live]
    z = pd.DataFrame(index=raw.index, columns=fids, dtype=float)
    n_fallback = pd.Series(0, index=raw.index)
    for fid in fids:
        zi, fb = _zscore_by_industry(raw[fid], key, cfg.min_industry_size)
        zi = (zi * F.REGISTRY[fid].direction).clip(-cfg.z_clip, cfg.z_clip)
        z[fid] = zi
        n_fallback += fb.astype(int)

    # 缺失补值:同行业(样本不足则全市场)已有 z 的中位数
    imputed = z.isna()
    n_imputed = imputed.sum(axis=1)
    if cfg.missing_mode == "impute" and imputed.values.any():
        for fid in fids:
            col = z[fid]
            ind_cnt = col.groupby(key).transform("count")
            ind_med = col.groupby(key).transform("median")
            med = ind_med.where(ind_cnt >= cfg.min_industry_size, col.median())
            z[fid] = col.fillna(med.fillna(0.0))

    w = pd.Series({f: weights[f] for f in fids})
    wmat = z.notna().astype(float).mul(w, axis=1)
    score = (z.fillna(0.0) * w).sum(axis=1) / wmat.sum(axis=1).replace(0.0, np.nan)

    tab = pd.DataFrame({
        "code": raw.index, "name": nm.loc[live].values, "industry": key.values,
        "score": score.values, "n_factors": (len(fids) - n_imputed).values,
        "n_imputed": n_imputed.values, "n_fallback": n_fallback.values,
    })
    for fid in fids:
        tab["z_" + fid] = z[fid].values
    tab["score"] = tab["score"].round(10)                    # 抹掉浮点噪声,让并列真正并列(再按代码定序)
    tab = tab[tab["score"].notna()]
    # 综合分降序,并列按代码升序:确定性
    tab = tab.sort_values(["score", "code"], ascending=[False, True], kind="mergesort").reset_index(drop=True)
    tab["rank"] = np.arange(1, len(tab) + 1)
    tab = tab[TABLE_COLS + ["z_" + f for f in fids]]

    ranked = set(tab["code"])
    for c in raw.index:
        if c not in ranked:
            excluded.setdefault(c, "no_score")

    n = int(top_n)
    longs = tab.head(n).reset_index(drop=True)
    rest = tab.iloc[len(longs):]
    avoids = rest.tail(n).iloc[::-1].reset_index(drop=True)
    if len(tab) < 2 * n:
        notes.append("short_universe: 可排名 %d 只 < 2×top_n,前后候选已按不重叠截取" % len(tab))
    if n_fallback.sum():
        notes.append("industry_fallback: %d 个(股票×因子)因行业样本不足(<%d)回退全市场 Z-score"
                     % (int(n_fallback.sum()), cfg.min_industry_size))
    if int(n_imputed.sum()) and cfg.missing_mode == "impute":
        notes.append("imputed: %d 个(股票×因子)缺值按行业中位数补" % int(n_imputed.sum()))

    ex = pd.DataFrame(sorted(excluded.items()), columns=["code", "reason"])
    return RankResult(as_of, tab, longs, avoids, ex, notes, cfg.cfg_hash(), n_input)
