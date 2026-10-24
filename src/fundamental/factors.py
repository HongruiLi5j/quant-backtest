"""价值 + 基本面因子库(T014 S2):12 个纯函数因子,独立于技术面因子库(不导入 market_monitor)。

学术依据:Fama-French 五因子(2015,价值 HML / 盈利 RMW / 投资)、Barra CNE5/CNE6 风格因子
(Value、Earnings Yield、Growth、Leverage、Profitability)、Novy-Marx(2013) 毛利率盈利能力因子、
Sloan(1996) 应计异象(现金流/利润质量)。公式在 REGISTRY 里逐个写明。
这里是"打分函数",不是经过验证的 Alpha:没有在本项目里做过样本外检验,没扣成本,12 个因子同时看也有多重检验问题。

输入 fundamental_df:行=6 位代码,列=下面的标准字段(调用者已按"截至 as_of_date 已公告"准备好;
TTM 类字段由调用者用累计值差分得到,本模块不猜)。
    market_cap          总市值(元,排名日)           close_raw           未复权收盘价(元)
    net_profit_ttm      归母净利润 TTM               net_profit_ttm_prev 上年同期 TTM
    revenue_ttm         营业收入 TTM                 revenue_ttm_prev    上年同期 TTM
    operating_cost_ttm  营业成本 TTM                 operating_profit_ttm 营业利润 TTM
    equity_parent       归母净资产(MRQ)             total_assets / total_liabilities (MRQ)
    cfo_ttm             经营现金流净额 TTM            dps_ttm  过去 12 个月已实施的每股税前现金分红
    available_date      可选:该行数据的可用日;> as_of_date 的行一律当缺失(防未来函数),NaT 也当缺失

规则:缺字段 / 空数据 / 分母非正 / 非有限数 -> NaN,不填 0、不猜测;原因写入 logging 和调用者传入的 issues 列表。
每个因子返回"原始因子值",方向已统一成 direction 约定(PE/PB/PS 已取倒数成 EP/BP/SP);
需要排名时用 rank_score(),得到 0~1 分位(1=最好,已按方向调整)。
本模块不联网、不读写文件。
"""
import logging
import math
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FactorMeta:
    id: str
    name: str
    group: str            # value / quality / growth / risk
    formula: str
    source: str           # 权威来源
    direction: int        # +1:值越大越好;-1:值越小越好
    quantile: float       # 建议分位:方向调整后排名前 (1-quantile) 视为"强"
    update_freq: str      # 更新频率
    needs: tuple          # 依赖的输入字段


def _meta(id, name, group, formula, source, direction, needs, freq="季度(财报公告后);含价格的因子随价格每日变化", quantile=0.8):
    return FactorMeta(id, name, group, formula, source, direction, quantile, freq, tuple(needs))


# ---------- 计算内核:都是向量化的,输入已是数值 Series ----------

def _ratio(num, den, den_positive=True):
    """num/den;den 非正(den_positive=True)或为 0、任一非有限 -> NaN。"""
    num = num.astype(float)
    den = den.astype(float)
    ok = den > 0 if den_positive else den != 0
    out = (num / den.where(ok)).where(np.isfinite(num))
    return out.replace([np.inf, -np.inf], np.nan)


def _growth(cur, prev):
    """同比增长率 (cur-prev)/prev,要求上年同期 prev>0。

    prev<=0 时(亏损基数)增长率没有意义:用 |prev| 做分母会让"亏损收窄"看起来是正增长、"扭亏"数值爆炸,
    所以按 Barra/常规做法直接记缺失,不猜。
    """
    return _ratio(cur.astype(float) - prev.astype(float), prev, True)


def _ep(d):
    return _ratio(d["net_profit_ttm"], d["market_cap"])          # = 1/PE;亏损为负,自然排最后


def _bp_checked(d):
    out = _ratio(d["equity_parent"], d["market_cap"])            # = 1/PB
    return out.where(d["equity_parent"].astype(float) > 0)       # 负净资产的 PB 无意义,记缺失


def _sp(d):
    return _ratio(d["revenue_ttm"], d["market_cap"])             # = 1/PS


def _dy(d):
    return _ratio(d["dps_ttm"], d["close_raw"])                  # 分红/未复权价;dps 为负视为脏数据


def _dy_checked(d):
    return _dy(d).where(d["dps_ttm"].astype(float) >= 0)


def _roe(d):
    return _ratio(d["net_profit_ttm"], d["equity_parent"])       # 净资产<=0 -> NaN


def _roa(d):
    return _ratio(d["net_profit_ttm"], d["total_assets"])


def _gross_margin(d):
    return _ratio(d["revenue_ttm"].astype(float) - d["operating_cost_ttm"].astype(float), d["revenue_ttm"])


def _op_margin(d):
    return _ratio(d["operating_profit_ttm"], d["revenue_ttm"])


def _ni_yoy(d):
    return _growth(d["net_profit_ttm"], d["net_profit_ttm_prev"])


def _rev_yoy(d):
    return _growth(d["revenue_ttm"], d["revenue_ttm_prev"])


def _debt_ratio(d):
    return _ratio(d["total_liabilities"], d["total_assets"])


def _cfo_ni(d):
    # 利润<=0 时"现金流/利润"符号含义反转(亏损且现金流为负会得到正比值),所以要求利润>0
    return _ratio(d["cfo_ttm"], d["net_profit_ttm"])


# 数值合理性检查:返回 True 的行保留,False 的行置 NaN 并记录("数据矛盾"不猜)
def _ok_gross_margin(d):
    return d["operating_cost_ttm"].astype(float) >= 0


def _ok_debt(d):
    return d["total_liabilities"].astype(float) >= 0


FACTORS = {}   # id -> (FactorMeta, 计算函数, 额外有效性检查或 None)


def _reg(meta, fn, check=None):
    FACTORS[meta.id] = (meta, fn, check)


_reg(_meta("fv_ep", "盈利收益率(市盈率 PE 的倒数)", "value",
           "归母净利润TTM / 总市值 = 1/PE;用倒数是因为亏损股 PE 为负或无穷,排序无意义,EP 为负自然排最后",
           "Basu(1977);Fama-French(1992);Barra CNE5 Earnings Yield", +1, ["net_profit_ttm", "market_cap"]), _ep)
_reg(_meta("fv_bp", "账面市值比(市净率 PB 的倒数)", "value",
           "归母净资产 / 总市值 = 1/PB;净资产<=0 记缺失",
           "Fama-French(1993) HML;Barra CNE5 Book-to-Price", +1, ["equity_parent", "market_cap"]), _bp_checked)
_reg(_meta("fv_sp", "销售收益率(市销率 PS 的倒数)", "value",
           "营业收入TTM / 总市值 = 1/PS;亏损公司也有值,但行业间差异大,需行业内比较",
           "Barra CNE5 Value 类;O'Shaughnessy(2005) 的 Price-to-Sales 价值研究", +1, ["revenue_ttm", "market_cap"]), _sp)
_reg(_meta("fv_dy", "股息率", "value",
           "过去12个月已实施每股税前现金分红 / 未复权收盘价;股价暴跌会机械抬高股息率(价值陷阱)",
           "Barra CNE5 Dividend Yield;Fama-French(1988) 股息率预测收益", +1, ["dps_ttm", "close_raw"],
           freq="分红事件 + 价格每日变化"), _dy_checked)
_reg(_meta("fv_roe", "净资产收益率 ROE", "quality",
           "归母净利润TTM / 归母净资产;高 ROE 可能来自高杠杆,须与资产负债率联看;净资产<=0 记缺失",
           "Fama-French(2015) RMW 盈利因子;Barra CNE5 Profitability", +1, ["net_profit_ttm", "equity_parent"]), _roe)
_reg(_meta("fv_roa", "总资产收益率 ROA", "quality",
           "归母净利润TTM / 总资产(期末);不受杠杆放大影响,与 ROE 互补",
           "Barra CNE5 Profitability(ROA);Piotroski(2000) F-Score", +1, ["net_profit_ttm", "total_assets"]), _roa)
_reg(_meta("fv_gross_margin", "毛利率", "quality",
           "(营业收入TTM - 营业成本TTM) / 营业收入TTM;行业间结构性差异大,须行业内比较;金融业无此概念",
           "Novy-Marx(2013) The Other Side of Value: The Gross Profitability Premium", +1,
           ["revenue_ttm", "operating_cost_ttm"]), _gross_margin, _ok_gross_margin)
_reg(_meta("fv_op_margin", "营业利润率", "quality",
           "营业利润TTM / 营业收入TTM;含期间费用,比毛利率更接近经营盈利能力",
           "Barra CNE5 Profitability;Fama-French(2015) 营业盈利口径", +1, ["operating_profit_ttm", "revenue_ttm"]), _op_margin)
_reg(_meta("fv_ni_yoy", "净利润同比增长率", "growth",
           "(归母净利润TTM - 上年同期TTM) / 上年同期TTM;上年同期<=0 记缺失(亏损基数下增长率无意义)",
           "Barra CNE5 Growth;Lakonishok-Shleifer-Vishny(1994) 增长与价值", +1, ["net_profit_ttm", "net_profit_ttm_prev"]), _ni_yoy)
_reg(_meta("fv_rev_yoy", "营收同比增长率", "growth",
           "(营业收入TTM - 上年同期TTM) / 上年同期TTM;并购会抬高营收",
           "Barra CNE5 Growth;Lakonishok-Shleifer-Vishny(1994)", +1, ["revenue_ttm", "revenue_ttm_prev"]), _rev_yoy)
_reg(_meta("fv_debt_ratio", "资产负债率", "risk",
           "总负债 / 总资产(期末);越低越好。银行/保险/券商/地产结构性偏高,不应与其他行业同池比较",
           "Barra CNE5 Leverage;Bhandari(1988) 杠杆与期望收益", -1, ["total_liabilities", "total_assets"]), _debt_ratio, _ok_debt)
_reg(_meta("fv_cfo_ni", "经营现金流/净利润", "risk",
           "经营现金流净额TTM / 归母净利润TTM;利润<=0 记缺失;比值持续低于 1 是利润质量差(应计高)的预警",
           "Sloan(1996) Do Stock Prices Fully Reflect Information in Accruals and Cash Flows", +1,
           ["cfo_ttm", "net_profit_ttm"]), _cfo_ni)

REGISTRY = {k: v[0] for k, v in FACTORS.items()}
FACTOR_IDS = tuple(REGISTRY)


# ---------- 公共接口 ----------

def _note(issues, factor_id, reason):
    log.warning("fundamental factor %s -> NaN: %s", factor_id, reason)
    if issues is not None:
        issues.append((factor_id, reason))


def _index_of(df):
    return df.index if isinstance(df, pd.DataFrame) else pd.Index([])


def _nan_series(df, factor_id):
    return pd.Series(np.nan, index=_index_of(df), name=factor_id, dtype=float)


def _prepare(df, as_of_date, needs, factor_id, issues):
    """返回清洗后的数值表,或 None(整列 NaN)。只做:取 needs 列、转数值、按 available_date 屏蔽未来行。"""
    if not isinstance(df, pd.DataFrame) or len(df) == 0:
        _note(issues, factor_id, "empty_data")
        return None
    missing = [c for c in needs if c not in df.columns]
    if missing:
        _note(issues, factor_id, "missing_columns:" + ",".join(missing))
        return None
    try:
        asof = pd.Timestamp(as_of_date)
    except (TypeError, ValueError):
        _note(issues, factor_id, "bad_as_of_date")
        return None
    if pd.isna(asof):
        _note(issues, factor_id, "bad_as_of_date")
        return None
    work = df[list(needs)].apply(pd.to_numeric, errors="coerce")
    work = work.replace([np.inf, -np.inf], np.nan)
    if "available_date" in df.columns:
        avail = pd.to_datetime(df["available_date"], errors="coerce")
        visible = avail.notna() & (avail <= asof)                # 公告日未知也不用:不猜
        hidden = int((~visible).sum())
        if hidden:
            log.info("fundamental factor %s: %d rows not visible at %s", factor_id, hidden, asof.date())
        work = work.where(visible, np.nan)
    return work


def score(factor_id, fundamental_df, as_of_date, issues: Optional[list] = None) -> pd.Series:
    """计算单个因子的原始值,返回以代码为索引的 Series(名字=因子 id)。

    纯函数:不改输入。空数据 / 缺字段 / 未知因子 id 返回全 NaN(未知 id 抛 KeyError,属于调用错误)。
    issues:可选列表,收集 (factor_id, 原因) 供调用者留痕。
    """
    if factor_id not in FACTORS:
        raise KeyError(factor_id)
    meta, fn, check = FACTORS[factor_id]
    work = _prepare(fundamental_df, as_of_date, meta.needs, factor_id, issues)
    if work is None:
        return _nan_series(fundamental_df, factor_id)
    out = fn(work)
    if check is not None:
        bad = ~check(work).fillna(False)
        n_bad = int((bad & work.notna().all(axis=1)).sum())
        if n_bad:
            _note(issues, factor_id, "inconsistent_rows:%d" % n_bad)
        out = out.where(~bad)
    out = out.astype(float)
    out.name = factor_id
    return out


def _make_fn(factor_id):
    def fn(fundamental_df, as_of_date, issues=None):
        return score(factor_id, fundamental_df, as_of_date, issues)
    fn.__name__ = factor_id
    fn.__doc__ = "%s:score(fundamental_df, as_of_date) -> pd.Series" % REGISTRY[factor_id].name
    return fn


# 每个因子一个同名纯函数,签名 (fundamental_df, as_of_date, issues=None) -> pd.Series
for _id in FACTOR_IDS:
    globals()[_id] = _make_fn(_id)
del _id


def score_all(fundamental_df, as_of_date, issues: Optional[list] = None) -> pd.DataFrame:
    """12 个因子的原始值,行=代码,列=因子 id。"""
    cols = {fid: score(fid, fundamental_df, as_of_date, issues) for fid in FACTOR_IDS}
    return pd.DataFrame(cols, index=_index_of(fundamental_df))


def rank_score(values: pd.Series, direction: int = 1) -> pd.Series:
    """截面分位 0~1(1=最好,已按 direction 调整)。NaN 不参与排名且保持 NaN;并列取平均名次。

    只有 1 个有效值时返回 0.5(没有比较对象,不给极端分)。
    """
    if direction not in (1, -1):
        raise ValueError("direction must be +1 or -1")
    v = pd.to_numeric(values, errors="coerce").astype(float) * direction
    v = v.replace([np.inf, -np.inf], np.nan)
    n = int(v.notna().sum())
    if n == 0:
        return v
    if n == 1:
        return v.where(v.isna(), 0.5)
    return (v.rank(method="average") - 1) / (n - 1)


def rank_all(fundamental_df, as_of_date, issues: Optional[list] = None) -> pd.DataFrame:
    """12 个因子各自的 0~1 分位(1=最好)。不合成综合分:权重选择留给 ranker 阶段并登记。"""
    raw = score_all(fundamental_df, as_of_date, issues)
    return pd.DataFrame({fid: rank_score(raw[fid], REGISTRY[fid].direction) for fid in FACTOR_IDS}, index=raw.index)
