"""T008 S3:逐日回放引擎。

做什么:从 start 起逐个交易日 T,
  1) 只用 ≤T 日收盘的数据给每个(策略, 参数组合)打分;
  2) 取前 top_n 只作为次日持仓(等权或风险平价);随机对照 ctrl_* 按"匹配抽样"
     (条数与真实策略相同,在 T 日有收盘价的股票里按清单固定种子随机抽);
  3) T+1 开盘价成交,T+1 开盘 → T+2 开盘为一天的持有收益(开盘到开盘),扣成本;
  4) 把"当日预测、次日真实结果、累计准确率与收益"写进调用者指定的 sqlite(.sqlite/.db)或 jsonl 文件。
支持断点续跑(同一输出文件、同一配置再跑一次,从上次完成的最后一个交易日之后接着算)
与 universe_n(训练开始前成交额前 N 只,股票池一次定下,不用未来信息)。

只处理 i+2 仍在面板内的交易日(要有 T+1 开盘和 T+2 开盘才能算出结果);面板往后延长后,
再跑一次就会补上后面的日子,已经写下的行不会改。

关键口径(面试可讲):
  - 防未来函数:默认一次性向量化算整段因子表再取 T 行(因子只用 shift(k≥0) 与向后窗口,
    S1 测试逐策略验证"截断重算 == 全量计算");strict=True 则每天调用 factors.score 截断到 ≤T 重算,
    测试里两种方式结果必须逐位一致。风险平价用的波动率也只用 ≤T 的 20 日收益。
  - 成交只用 T+1 开盘价(合约 1),不用 T+1 的最高/最低/收盘;成交量只用来判断停牌。
    可交易性由外部 tradable_mask(日期×代码 bool)决定,引擎内不再硬编码涨跌幅;没传掩码时用近似:
    T+1 开盘较 T 日收盘涨跌幅超出 ±9.9% 视为不可交易(只覆盖 10% 板块,ST/创业板/科创板是近似,CostParams 同样标注)。
    CostParams.enforce_limits=False 忽略掩码/近似里的涨跌停;enforce_suspension=False 允许成交量为 0 但有开盘价的日子成交;
    开盘价缺失/≤0 永远不能成交。买不进的部分留作现金(收益 0),卖不出的旧持仓原样保留。
    停牌的已有持仓按前向填充价(最近一次可交易日的开盘价)估值,复牌价一到收益补回(不清零)。
  - 成本与整手(Engine 类):优先用 T007 validation.costs.CostParams 的费率(佣金含最低收费、过户费、卖出印花税、滑点);
    不可用时用内置同参数简版(cost_source 字段标注)。每个策略各自记账 Portfolio(整股股数, 现金),
    先卖完再买,买入预算只用卖完后的现金并预留买入费用(含最低佣金),整手取整(买不起一手就不成交),
    现金不为负;隔夜估值 = Σ股数×价 + 现金,不用权重反推。费率默认值是占位,需所有者按实际账户费率核对。
    持仓随价格漂移,与目标的差额每天再平衡(也要付成本)。
  - 续跑:配置(含股票池、成本、清单哈希等)必须一致;每行还带 data_fp(面板第 0..T+2 行的累计哈希),
    续跑时核对最后完成日的指纹,已处理区间的行情被改动就报错,避免新旧数据混算。
  - 随机对照:strategies 里的对照可带 "match": 真实策略键,条数逐策略匹配;不带则取当天各策略最大条数。
  - 超额收益 = 策略当日净收益 − 当日全市场(股票池)等权的开盘到开盘收益。
  - 准确率两种:日度命中(超额>0 的天数占比)与个股命中(选中的股票里收益高于当日市场均值的占比)。
  - 不翻转信号;股票池里的行业类策略需传 industry,否则 factors 会报错。
  - 本模块不读不写 data/ 下的任何文件(输出路径在仓库 data/ 内会被拒绝);也不联网。
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd

from . import factors, registry

try:                                   # T007 成本模型;不可用时退回内置简版(同默认参数)
    from src.validation.costs import CostParams as _BaseCostParams, trade_fees, lot_round
    COST_SOURCE = "validation.costs(CostParams/trade_fees/lot_round)"
except Exception:                      # pragma: no cover - 只在 T007 缺失时走
    @dataclass(frozen=True)
    class _BaseCostParams:             # 内置简版:字段与默认值同 T007(占位,需所有者核对)
        commission_rate: float = 0.00025
        min_commission: float = 5.0
        transfer_rate: float = 0.00001
        stamp_rate: float = 0.0005
        slippage_bps: float = 5.0
        lot: int = 100
        enforce_limits: bool = True
        enforce_suspension: bool = True

    def lot_round(shares, lot=100):
        s = int(shares)
        return 0 if s <= 0 else (s // lot) * lot

    def trade_fees(params, side, amount):
        if amount <= 0:
            return {"total": 0.0}
        c = max(amount * params.commission_rate, params.min_commission)
        return {"total": c + amount * params.transfer_rate + (amount * params.stamp_rate if side == "sell" else 0.0)}
    COST_SOURCE = "内置简版(T007 不可用)"


@dataclass(frozen=True)
class CostParams(_BaseCostParams):
    """T007 CostParams + 本引擎的一个近似参数。
    approx_limit_pct:调用者没传 tradable_mask 时的【近似】涨跌停判断——T+1 开盘相对 T 日收盘
    涨跌幅超出 ±approx_limit_pct 视为不可交易。只覆盖 10% 板块,ST/创业板/科创板/新股不适用,
    要准确请传外部 tradable_mask。enforce_limits / enforce_suspension 的含义见 Engine 文档。"""
    approx_limit_pct: float = 0.099


__all__ = ["run_replay", "Engine", "Portfolio", "CostParams", "all_grid_strategies", "strategy_key", "load_rows", "summarize",
           "select_universe", "ReplayError"]

APPROX_LIMIT = 0.099       # 没有外部 tradable_mask 时的近似涨跌停阈值(T+1 开盘 vs T 收盘),见 CostParams
VOL_WINDOW = 20            # 风险平价用的历史波动率窗口
UNIVERSE_WINDOW = 60       # 股票池按"start 之前最后 60 个交易日"的平均成交额排名
WEIGHTINGS = ("equal", "risk_parity")
STORE_EXT = {".sqlite": "sqlite", ".db": "sqlite", ".jsonl": "jsonl"}
ROOT = Path(__file__).resolve().parents[2]


class ReplayError(ValueError):
    pass


# ---------------------------------------------------------------- 策略键与清单

def strategy_key(sid: str, params: dict | None = None) -> str:
    """(策略, 参数组合)的唯一键,如 mom_01|n=5;无参数为 ctrl_rand_01|。"""
    p = params or {}
    return sid + "|" + ",".join(f"{k}={p[k]}" for k in sorted(p))


def all_grid_strategies(ids=None, include_controls=True) -> list:
    """清单里指定策略(默认全部非对照)的全部网格组合,按 ID 与网格顺序;可附带 5 个随机对照。
    真正"存活"的名单由 S4 淘汰后传入,这里只是方便测试/小规模运行。"""
    out = []
    for sid in (ids if ids is not None else factors.scorable_ids()):
        for p in factors.iter_param_grid(sid):
            out.append({"id": sid, "params": p})
    if include_controls and ids is None:
        for s in registry.load_catalog()["strategies"]:
            if s["id"].startswith(registry.CONTROL_PREFIX):
                out.append({"id": s["id"], "params": {}})
    return out


def _norm_strategies(strategies) -> list:
    if not strategies:
        raise ReplayError("strategies 为空")
    cat = {s["id"]: s for s in registry.load_catalog()["strategies"]}
    out, seen = [], set()
    for s in strategies:
        sid, params = s["id"], dict(s.get("params") or {})
        if sid not in cat:
            raise ReplayError(f"清单里没有策略 {sid}")
        ctrl = sid.startswith(registry.CONTROL_PREFIX)
        if ctrl and params:
            raise ReplayError("随机对照没有参数")
        match = s.get("match")
        if match and not ctrl:
            raise ReplayError("只有随机对照可以指定 match")
        k = strategy_key(sid, params) + (f"match={match}" if match else "")
        if k in seen:
            raise ReplayError(f"策略重复: {k}")
        seen.add(k)
        out.append({"id": sid, "params": params, "key": k, "control": ctrl, "match": match or None,
                    "seed": cat[sid].get("seed") if ctrl else None})
    for s in out:
        if s["match"] and not any(t["key"] == s["match"] and not t["control"] for t in out):
            raise ReplayError(f"对照 {s['key']} 要匹配的真实策略 {s['match']} 不在本次策略列表里")
    return out


# ---------------------------------------------------------------- 股票池

def select_universe(panel: dict, start, n: int, window: int = UNIVERSE_WINDOW) -> list:
    """start 之前(严格小于 start)最后 window 个交易日平均成交额前 n 只(并列按代码排序)。
    只用 start 之前的数据,避免用未来信息选股;start 之前没有任何数据则报错。"""
    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
        raise ReplayError(f"universe_n 必须是正整数,收到 {n!r}")
    W = factors.Wide(panel, None)
    pre = W.a.loc[W.a.index < pd.Timestamp(start)].tail(window)
    if pre.empty:
        raise ReplayError("start 之前没有数据,无法按成交额选股票池")
    mean_a = pre.mean(axis=0, skipna=True).dropna()
    ranked = sorted(mean_a.index, key=lambda c: (-float(mean_a[c]), c))
    return sorted(ranked[:n])


# ---------------------------------------------------------------- 存储

class _SqliteStore:
    def __init__(self, path):
        self.con = sqlite3.connect(str(path))
        self.con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        self.con.execute("CREATE TABLE IF NOT EXISTS daily(key TEXT, date TEXT, row TEXT, PRIMARY KEY(key, date))")
        self.con.commit()

    def get_meta(self):
        r = self.con.execute("SELECT v FROM meta WHERE k='config'").fetchone()
        return json.loads(r[0]) if r else None

    def set_meta(self, meta):
        self.con.execute("INSERT OR REPLACE INTO meta VALUES('config', ?)", (json.dumps(meta, sort_keys=True),))
        self.con.commit()

    def last_date(self):
        r = self.con.execute("SELECT MAX(date) FROM daily").fetchone()
        return r[0]

    def last_row(self, key, date):
        r = self.con.execute("SELECT row FROM daily WHERE key=? AND date=?", (key, date)).fetchone()
        return json.loads(r[0]) if r else None

    def write_day(self, rows):                    # 一天的所有行一个事务:要么全写要么全不写
        with self.con:
            self.con.executemany("INSERT OR REPLACE INTO daily VALUES(?,?,?)",
                                 [(r["key"], r["date"], json.dumps(r, sort_keys=True)) for r in rows])

    def all_rows(self):
        cur = self.con.execute("SELECT row FROM daily ORDER BY date, key")
        return [json.loads(x[0]) for x in cur]

    def close(self):
        self.con.close()


class _JsonlStore:
    """每行一个 JSON:{"type":"meta"} / {"type":"row",...} / {"type":"done","date":...}。
    一天的行先写,最后写 done 标记;读取时只认 ≤ 最后一个 done 日期的行(崩溃时可能多出半天的行,忽略),
    半截的最后一行(没有换行/不是合法 JSON)也忽略。重复行按后写覆盖。"""

    def __init__(self, path):
        self.path = Path(path)
        self.meta = None
        self.done = None
        self.rows = {}
        if self.path.exists():
            good = 0                                    # 最后一个完整合法行之后的字节偏移
            with open(self.path, "rb") as f:
                for raw in f:
                    if not raw.endswith(b"\n"):
                        break
                    try:
                        o = json.loads(raw.decode("utf-8"))
                    except ValueError:
                        break
                    good += len(raw)
                    t = o.get("type")
                    if t == "meta":
                        self.meta = o["config"]
                    elif t == "row":
                        self.rows[(o["key"], o["date"])] = o
                    elif t == "done":
                        self.done = o["date"]
            if good < self.path.stat().st_size:         # 崩溃留下的半截行:截掉,免得和后面新写的内容粘在一起
                with open(self.path, "r+b") as f:
                    f.truncate(good)
        self.rows = {k: v for k, v in self.rows.items() if self.done is not None and k[1] <= self.done}
        self.f = open(self.path, "a", encoding="utf-8")

    def get_meta(self):
        return self.meta

    def set_meta(self, meta):
        self.meta = meta
        self.f.write(json.dumps({"type": "meta", "config": meta}, sort_keys=True) + "\n")
        self.f.flush()

    def last_date(self):
        return self.done

    def last_row(self, key, date):
        r = self.rows.get((key, date))
        return None if r is None else {k: v for k, v in r.items() if k != "type"}

    def write_day(self, rows):
        date = rows[0]["date"]
        buf = "".join(json.dumps({"type": "row", **r}, sort_keys=True) + "\n" for r in rows)
        buf += json.dumps({"type": "done", "date": date}) + "\n"
        self.f.write(buf)
        self.f.flush()
        for r in rows:
            self.rows[(r["key"], r["date"])] = {"type": "row", **r}
        self.done = date

    def all_rows(self):
        return [{k: v for k, v in r.items() if k != "type"}
                for _, r in sorted(self.rows.items(), key=lambda kv: (kv[0][1], kv[0][0]))]

    def close(self):
        self.f.close()


def _open_store(path):
    p = Path(path)
    kind = STORE_EXT.get(p.suffix.lower())
    if kind is None:
        raise ReplayError(f"输出文件后缀必须是 {sorted(STORE_EXT)} 之一,收到 {p.suffix!r}")
    rp = p.resolve()
    data_dir = (ROOT / "data").resolve()
    if rp == data_dir or data_dir in rp.parents:
        raise ReplayError("输出不能写进仓库 data/ 目录")
    p.parent.mkdir(parents=True, exist_ok=True)
    return _SqliteStore(p) if kind == "sqlite" else _JsonlStore(p)


def load_rows(path) -> list:
    """读回输出文件里的全部行(按日期、键排序)。"""
    p = Path(path)
    if not p.exists():
        raise ReplayError(f"文件不存在: {p}")
    st = _open_store(p)
    try:
        return st.all_rows()
    finally:
        st.close()


def summarize(path) -> dict:
    """每个策略键的最新累计指标(天数、复合净收益、累计超额、日度/个股命中率)。"""
    last = {}
    for r in load_rows(path):
        last[r["key"]] = r                   # 已按日期升序,后者覆盖前者
    return {k: {"date": r["date"], "days": r["days"], "cum_net": r["cum_net"], "cum_excess": r["cum_excess"],
                "acc_day": r["hit_days"] / r["days"] if r["days"] else None,
                "acc_stock": r["picks_hit"] / r["picks_total"] if r["picks_total"] else None}
            for k, r in sorted(last.items())}


# ---------------------------------------------------------------- 核心:持仓状态 + 交易引擎

@dataclass
class Portfolio:
    """持仓状态只有两个字段:整股股数 shares(每只都是整手倍数)和现金 cash。
    不保存权重,也不用"净值×权重"反推股数;市值 = Σ shares[c]×当日价 + cash(见 Engine.value)。"""
    shares: Dict[str, int] = field(default_factory=dict)
    cash: float = 0.0


class Engine:
    """交易可行性 + 估值 + 两步再平衡。对应 7 条合约:

    1. 成交只用 T+1 的开盘价;不读 T+1 的 high/low/close。成交量只用来判断停牌(合约 7)。
    2. tradable_mask(日期×代码,bool,True=可买可卖,False=不可)由调用者传入,引擎不再硬编码涨跌幅。
       没传时用"近似":T+1 开盘相对 T 日收盘涨跌幅超出 ±approx_limit_pct(默认 9.9%)视为不可交易。
       近似只覆盖 10% 板块,ST/创业板/科创板/新股不同,需要外部掩码才准确(CostParams.approx_limit_pct 同样标注)。
       掩码里缺失的日期/代码按 False(不可交易)处理;掩码为 False 时买卖都不行(不区分涨停不可买、跌停不可卖)。
    3. cp.enforce_limits=False 忽略掩码(及近似)里的涨跌停信号;成交量为 0 或开盘价缺失/≤0 无论开关都不能成交,
       持仓按前一有效价估值(不用停牌日价格);enforce_suspension=False 时只是成交量缺失(未知)的日子不再按停牌处理。
    4. 再平衡分两步:先卖(整手向下取整,收入 = 股数×开盘价 − 佣金(含最低收费)− 过户费 − 印花税 − 滑点,入现金),
       再用更新后的现金买入(每只预算 = min(现金×权重/待买权重和, 剩余现金, 目标缺口),股数整手向下取整)。现金不为负。
    5. 状态是 Portfolio(shares, cash)。
    6. 买入预留费用:可下单金额 = 预算/(1+max(buy_fee_rate, min_commission/预算)),取整后再核对"股款+费用 ≤ 预算"。
    7. 估值价 = 最近一次"可交易且有价"日的开盘价(前向填充,只用过去),停牌期间不清零收益,复牌当日用复牌价。

    滑点折成费用(成交价仍是开盘价):买卖都按 滑点bps×成交额 计入费用,与 T007 口径等价。
    """

    def __init__(self, open: pd.DataFrame, volume: pd.DataFrame, tradable_mask: pd.DataFrame | None = None,
                 cost_params=None, close: pd.DataFrame | None = None):
        o = open.astype(float)
        self.cp = cost_params if cost_params is not None else CostParams()
        self.lot = int(getattr(self.cp, "lot", 100))
        self.enforce_lim = bool(getattr(self.cp, "enforce_limits", True))
        self.enforce_susp = bool(getattr(self.cp, "enforce_suspension", True))
        v = volume.reindex(index=o.index, columns=o.columns).astype(float)
        if tradable_mask is not None:
            if not isinstance(tradable_mask, pd.DataFrame) or any(d != np.dtype(bool) for d in tradable_mask.dtypes):
                raise ReplayError("tradable_mask 必须是 bool 类型的 DataFrame(index=日期, columns=代码)")
            mask = tradable_mask.reindex(index=o.index, columns=o.columns, fill_value=False)
            self.mask_source = "external"
        else:
            if close is None:
                raise ReplayError("没有 tradable_mask 时必须提供 close(用 T 日收盘做 ±9.9% 近似)")
            lim = float(getattr(self.cp, "approx_limit_pct", APPROX_LIMIT))
            gap = o / close.reindex(index=o.index, columns=o.columns).astype(float).shift(1) - 1
            mask = ~(gap.abs() > lim)              # 缺昨收(NaN)不拦;开盘价无效由下面的 valid 统一拦
            self.mask_source = f"approx_open_vs_prev_close_{lim:.3f}"
        self.mask = mask.astype(bool)
        valid = (o > 0) & np.isfinite(o)
        valid &= ~(v <= 0)                         # 成交量为 0 永远是停牌:不交易、不用当天价估值
        if self.enforce_susp:
            valid &= v.notna()                     # 强制停牌过滤时,成交量缺失(未知)也按不可交易
        if self.enforce_lim:
            valid &= self.mask
        self.tradable = valid
        self.o = o
        # 估值价:最近一次"有价且(强制时)有成交量、且(外部掩码强制时)掩码为 True"的开盘价,只含过去与当日。
        # 没有外部掩码时,±9.9% 近似只用于判断能否成交,不用于估值(近似不该让正常的大涨日估值失真)
        vv = (o > 0) & np.isfinite(o)
        vv &= ~(v <= 0)
        if self.enforce_susp:
            vv &= v.notna()
        if self.enforce_lim and self.mask_source == "external":
            vv &= self.mask
        self.vo = o.where(vv).ffill()
        self.cols = {c: j for j, c in enumerate(o.columns)}
        self._o, self._vo, self._ok = o.to_numpy(), self.vo.to_numpy(), valid.to_numpy()
        c_ = self.cp
        self.buy_fee_rate = c_.commission_rate + c_.transfer_rate + c_.slippage_bps / 1e4
        self.sell_fee_rate = self.buy_fee_rate + c_.stamp_rate
        self.min_commission = c_.min_commission

    def fee(self, side: str, amount: float) -> float:
        """一笔成交的总费用:佣金(含最低收费)+ 过户费 + 滑点(双边)+ 印花税(仅卖出)。"""
        if amount <= 0:
            return 0.0
        cp = self.cp
        f = max(amount * cp.commission_rate, cp.min_commission) + amount * (cp.transfer_rate + cp.slippage_bps / 1e4)
        return f + (amount * cp.stamp_rate if side == "sell" else 0.0)

    def val_price(self, i: int, code: str) -> float:
        j = self.cols.get(code)
        x = float(self._vo[i, j]) if j is not None else 0.0
        return x if np.isfinite(x) else 0.0

    def value(self, pf: Portfolio, i: int) -> float:
        """第 i 个交易日开盘时的总资产 = Σ股数×估值价 + 现金。"""
        return pf.cash + sum(int(n) * self.val_price(i, c) for c, n in pf.shares.items())

    def rebalance(self, pf: Portfolio, i: int, target: Dict[str, float]):
        """在第 i 个交易日的开盘价把持仓调到目标权重(权重只是本次调仓的输入,不进状态)。
        返回 (新 Portfolio, 信息 dict);不修改传入的 pf。"""
        lot = self.lot
        sh = {c: int(n) for c, n in pf.shares.items() if int(n) > 0}
        cash = float(pf.cash)
        nav = self.value(pf, i)
        tv = {c: w * nav for c, w in target.items()}
        info = {"fees": 0.0, "blocked_buys": 0, "blocked_sells": 0, "trades": []}

        def ok(c):
            j = self.cols.get(c)
            return j is not None and bool(self._ok[i, j])

        # 第 1 步:卖出。股数向下取整到整手(整仓卖出就是全部持有,本来就是整手倍数)
        for c in sorted(sh):
            vp = self.val_price(i, c)
            cur, want = sh[c] * vp, tv.get(c, 0.0)
            if want >= cur - 1e-9:
                continue
            if not ok(c):
                info["blocked_sells"] += 1
                continue
            px = float(self._o[i, self.cols[c]])
            n = sh[c] if want <= 1e-9 else min(lot_round((cur - want) / px, lot), sh[c])
            if n <= 0:
                continue
            fee = self.fee("sell", n * px)
            if cash + n * px - fee < 0:                    # 极小额卖出的最低佣金会让现金变负,不做
                continue
            cash += n * px - fee
            sh[c] -= n
            info["fees"] += fee
            info["trades"].append((c, "sell", n, px, fee))

        # 第 2 步:用卖完后的现金买入
        todo = [c for c in sorted(tv) if tv[c] - sh.get(c, 0) * self.val_price(i, c) > 1e-9]
        sum_w = sum(target[c] for c in todo)
        cash0 = cash
        for c in todo:
            if not ok(c):
                info["blocked_buys"] += 1
                continue
            px = float(self._o[i, self.cols[c]])
            need = tv[c] - sh.get(c, 0) * self.val_price(i, c)
            budget = min(cash0 * target[c] / sum_w, cash, need)
            if budget <= 0:
                continue
            rate = max(self.buy_fee_rate, self.min_commission / budget)     # 费用预留:含最低佣金折算的费率
            n = lot_round(budget / (1.0 + rate) / px, lot)
            while n > 0 and n * px + self.fee("buy", n * px) > budget:
                n -= lot
            if n <= 0:
                continue
            fee = self.fee("buy", n * px)
            cash -= n * px + fee
            sh[c] = sh.get(c, 0) + n
            info["fees"] += fee
            info["trades"].append((c, "buy", n, px, fee))

        if cash < 0:                                       # 不应发生;防御:宁可报错也不带着负现金往下算
            raise ReplayError(f"现金为负: {cash}")
        return Portfolio({c: int(n) for c, n in sorted(sh.items()) if n > 0}, cash), info



def _weights(picks: list, vol_row, weighting: str) -> dict:
    if not picks:
        return {}
    if weighting == "equal":
        return {c: 1.0 / len(picks) for c in picks}
    v = {c: float(vol_row.get(c, np.nan)) for c in picks}
    good = [x for x in v.values() if np.isfinite(x) and x > 0]
    fill = float(np.median(good)) if good else 1.0           # 波动率缺失的用同组中位数,全缺失则退化为等权
    inv = {c: 1.0 / (v[c] if np.isfinite(v[c]) and v[c] > 0 else fill) for c in picks}
    tot = sum(inv.values())
    return {c: inv[c] / tot for c in picks}


def _history_chain(W, extra=()) -> list:
    """逐日累计哈希:chain[j] 覆盖面板第 0..j 行的全部字段。第 j 日之后追加数据不影响 chain[j],
    但改动 ≤j 的任何一格都会让它变化,用来在续跑时发现历史被改。"""
    arrs = [np.ascontiguousarray(x.to_numpy(float)) for x in (W.o, W.h, W.l, W.c, W.v, W.a, W.t, *extra)]
    out, prev = [], b""
    for j in range(len(W.idx)):
        h = hashlib.sha256(prev)
        h.update(str(W.idx[j].date()).encode("utf-8"))
        for a in arrs:
            h.update(a[j].tobytes())
        prev = h.digest()
        out.append(prev.hex())
    return out


def _config(strats, start, top_n, weighting, universe, cp, seed, catalog_hash, capital, industry, eng):
    ind_hash = None if not industry else hashlib.sha256(
        json.dumps(sorted((str(k), str(v)) for k, v in industry.items())).encode("utf-8")).hexdigest()
    return {"start": str(pd.Timestamp(start).date()), "strategies": [s["key"] for s in strats],
            "top_n": top_n, "weighting": weighting, "universe": universe, "seed": seed,
            "capital": capital, "industry_hash": ind_hash, "ledger": "shares_cash_v3",
            "cost": asdict(cp), "cost_source": COST_SOURCE, "catalog_hash": catalog_hash,
            "mask_source": eng.mask_source,
            "execution": "T日收盘出信号,T+1开盘成交,T+1开盘到T+2开盘为持有收益"}


def run_replay(panel: dict, start, end, out_path, strategies, top_n: int = 20,
               weighting: str = "equal", universe_n: int | None = None, industry: dict | None = None,
               cost_params=None, seed: int = 0, strict: bool = False, capital: float = 1_000_000.0,
               tradable_mask: pd.DataFrame | None = None) -> dict:
    """回放 [start, end] 内的信号日。返回 {"processed": 新处理的天数, "last_date": ..., "n_strategies": ...}。

    strategies: [{"id":..., "params": {...}}, ...](随机对照 params 为空)。
    out_path:  .sqlite/.db 或 .jsonl;已存在则续跑(配置必须与上次完全一致,否则报错)。
    seed:      随机对照的额外种子,和清单里每个对照的固定种子、日期一起决定当天抽样。
    capital:   初始现金(元);之后每个策略各自记账 Portfolio(整股股数, 现金),净值 = Σ股数×价 + 现金。
    tradable_mask: 日期×代码 bool(True=可买可卖)。不传则用 T+1 开盘较 T 收盘 ±9.9% 的近似(见 Engine/CostParams)。
    """
    if not isinstance(capital, (int, float)) or isinstance(capital, bool) or not capital > 0:
        raise ReplayError(f"capital 必须是正数,收到 {capital!r}")
    if weighting not in WEIGHTINGS:
        raise ReplayError(f"weighting 必须是 {WEIGHTINGS}")
    if not isinstance(top_n, int) or isinstance(top_n, bool) or top_n <= 0:
        raise ReplayError(f"top_n 必须是正整数,收到 {top_n!r}")
    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    if t1 < t0:
        raise ReplayError("end 早于 start")
    cp = cost_params if cost_params is not None else CostParams()
    strats = _norm_strategies(strategies)
    _, chash = registry.load_validated()

    codes = sorted(panel)
    full_panel = panel                         # 超额收益的基准用全市场等权,不随 universe_n 缩小
    if universe_n is not None:
        codes = select_universe(panel, t0, universe_n)
    panel = {c: panel[c] for c in codes}
    W = factors.Wide(panel, industry)
    dates = W.idx
    eng = Engine(W.o, W.v, tradable_mask=tradable_mask, cost_params=cp, close=W.c)   # 引擎只拿开盘/成交量/(T 日)收盘
    if universe_n is None:
        engF = eng
    else:
        WF = factors.Wide(full_panel, None)
        engF = Engine(WF.o, WF.v, tradable_mask=tradable_mask, cost_params=cp, close=WF.c)   # 基准与持仓同一套掩码/估值口径
    bench = engF.vo.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).mean(axis=1).reindex(dates)
    cfg = _config(strats, t0, top_n, weighting, codes, cp, seed, chash, float(capital), industry, eng)

    store = _open_store(out_path)
    try:
        old = store.get_meta()
        if old is None:
            store.set_meta(cfg)
        elif old != cfg:
            diff = sorted(k for k in set(old) | set(cfg) if old.get(k) != cfg.get(k))
            raise ReplayError(f"与已有输出的配置不一致,不能续跑: {diff}")
        last_done = store.last_date()
        chain = _history_chain(W, (eng.mask, bench.to_frame("b")))   # 外部掩码也进指纹:已处理区间的掩码被改动同样不能续跑
        if last_done is not None:                 # 续跑前核对:已处理区间的输入数据不能变,否则新旧日子会混算
            try:
                j = dates.get_loc(pd.Timestamp(last_done))
            except KeyError:
                raise ReplayError(f"已完成的日期 {last_done} 不在当前面板里,不能续跑") from None
            prev_row = store.last_row(strats[0]["key"], last_done)
            if j + 2 >= len(dates) or prev_row is None or prev_row.get("data_fp") != chain[j + 2]:
                raise ReplayError("已处理区间的行情数据与上次不一致(或缺少数据指纹),不能续跑")

        # 向量化因子表(真实策略);对照没有因子
        frames = {}
        for s in strats:
            if not s["control"]:
                frames[s["key"]] = (factors.factor_frame(s["id"], panel, s["params"], industry)
                                    .reindex(index=dates, columns=codes))
        vol = W.ret.rolling(VOL_WINDOW, min_periods=VOL_WINDOW // 2).std()
        c = W.c
        processed, last_date = 0, last_done
        for i in range(len(dates) - 2):
            T = dates[i]
            if T < t0 or T > t1:
                continue
            ds = str(T.date())
            if last_done is not None and ds <= last_done:
                continue
            # 市场基准与持仓估值同一口径:都用前向填充价。停牌股停牌期间收益记 0,复牌价一到把涨跌补回;
            # 若直接用原始开盘价,停牌日开盘价为 0/缺失会让基准变成 -100% 或被排除,造成虚假超额收益
            r_valid = (eng.vo.iloc[i + 2] / eng.vo.iloc[i + 1] - 1).replace([np.inf, -np.inf], np.nan)
            mkt = float(bench.iloc[i + 2]) if np.isfinite(bench.iloc[i + 2]) else 0.0     # 全市场等权,不随股票池缩小
            cand = [k for k in codes if np.isfinite(c.iloc[i][k])]        # T 日有收盘价的股票
            vrow = vol.iloc[i]

            # 阶段一:真实策略选股(只用 ≤T 数据)
            picks_by_key = {}
            for s in strats:
                if s["control"]:
                    continue
                if strict:
                    sc = factors.score(s["id"], panel, T, s["params"], industry)
                    sc = {k: x for k, x in sc.items() if k in set(codes)}
                else:
                    row = frames[s["key"]].iloc[i]
                    sc = {k: float(x) for k, x in row.items() if np.isfinite(x)}
                picks_by_key[s["key"]] = [k for k, _ in sorted(sc.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]]
            # 阶段二:随机对照匹配抽样——指定了 match 的对照,条数 = 该真实策略当天的选股条数(逐策略匹配);
            # 没指定则取当天所有真实策略选股条数的最大值(没有真实策略则取 top_n)。
            # 从 T 日有收盘价的股票里抽;买不进的由引擎的成交约束统一处理,和真实策略同一口径
            n_all = max((len(p) for p in picks_by_key.values()), default=top_n)
            for s in strats:
                if s["control"]:
                    n = min(len(picks_by_key[s["match"]]) if s["match"] else n_all, len(cand))
                    rng = random.Random(f"{seed}|{s['seed']}|{s['key']}|{ds}")
                    picks_by_key[s["key"]] = sorted(rng.sample(cand, n)) if n else []

            day_rows = []
            for s in strats:
                key = s["key"]
                picks = picks_by_key[key]
                prev = store.last_row(key, last_date) if last_date is not None else None
                state = prev or {"shares": {}, "cash": float(capital), "cum_net": 0.0, "cum_excess": 0.0,
                                 "days": 0, "hit_days": 0, "picks_total": 0, "picks_hit": 0}
                target = _weights(picks, vrow, weighting)

                # 账本:状态只有 Portfolio(整股股数, 现金)。T+1 开盘(第 i+1 日)调仓,持有到 T+2 开盘(第 i+2 日)
                pf = Portfolio({k: int(x) for k, x in state["shares"].items() if int(x) > 0}, float(state["cash"]))
                nav = eng.value(pf, i + 1)
                new_pf, info = eng.rebalance(pf, i + 1, target)
                nav_end = eng.value(new_pf, i + 2)
                shares, cash = new_pf.shares, new_pf.cash
                gross = sum(n * (eng.val_price(i + 2, k) - eng.val_price(i + 1, k)) for k, n in shares.items()) / nav
                cost = info["fees"] / nav
                net = gross - cost
                excess = net - mkt
                actual = {k: n * eng.val_price(i + 1, k) / nav for k, n in shares.items()}
                hits = sum(1 for k in picks if np.isfinite(r_valid[k]) and float(r_valid[k]) > mkt)
                n_scored = sum(1 for k in picks if np.isfinite(r_valid[k]))

                day_rows.append({
                    "key": key, "date": ds, "picks": picks, "weights": {k: target[k] for k in sorted(target)},
                    "weights_after": {k: actual[k] for k in sorted(actual)},      # 仅供阅读的派生记录,不是状态
                    "shares": shares, "cash": cash, "nav_start": nav, "nav_end": nav_end,
                    "gross": gross, "cost": cost, "net": net, "mkt": mkt, "excess": excess,
                    "blocked_buys": info["blocked_buys"], "blocked_sells": info["blocked_sells"],
                    "exec_date": str(dates[i + 1].date()), "next_date": str(dates[i + 2].date()),
                    "data_fp": chain[i + 2],
                    "days": state["days"] + 1, "hit_days": state["hit_days"] + (1 if excess > 0 else 0),
                    "picks_total": state["picks_total"] + n_scored, "picks_hit": state["picks_hit"] + hits,
                    "cum_net": (1 + state["cum_net"]) * (1 + net) - 1,
                    "cum_excess": state["cum_excess"] + excess,
                })
            store.write_day(day_rows)
            last_date = ds
            processed += 1
        return {"processed": processed, "last_date": last_date, "n_strategies": len(strats),
                "universe": len(codes), "cost_source": COST_SOURCE}
    finally:
        store.close()
