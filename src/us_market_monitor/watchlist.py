"""美股实时盯盘的监控名单：持仓 > 自选 > 信号；只读，不改任何来源。镜像 src/market_monitor/watchlist.py。

与 A 股版的差异：
- 代码校验用美股 valid_symbol（1-5 个大写字母，可带 -X），指数/期货/小写/点号写法一律丢弃。
- 持仓：T013 台账只支持六位 A 股代码，美股第一版返回空（不改 src/holdings）。
- 自选：datasource.load_watchlist('us')（本机只读 JSON/默认列表，不联网）。
- 信号：目前没有美股买卖清单，第一版返回空；signals_fn 可注入，将来接上即可。
- 名单上限 30（A 股版 500）：yfinance 逐只请求，名单越大每只刷新越慢。
所有来源函数都可注入（测试用合成数据）；任何一个来源出错都降级为空列表。
"""
import logging
import math

from .core import valid_symbol

LOG = logging.getLogger('us_market_monitor')

MAX_US_WATCH = 30      # 名单硬上限：构造参数也不能超过它
DEFAULT_MAX_WATCH = MAX_US_WATCH
DEFAULT_TOP_N = 0      # 第一版没有美股信号来源，默认不取信号
SIGNAL_ACTIONS = ('买入', '卖出', '加仓', '减仓')
SOURCE_HOLDING, SOURCE_WATCH, SOURCE_SIGNAL = 'holding', 'watchlist', 'signal'


def validate_max_watch(value):
    """max_watch 必须是 1..MAX_US_WATCH 的真 int（拒绝 bool/浮点）。"""
    if type(value) is not int or not 1 <= value <= MAX_US_WATCH:
        raise ValueError('invalid_max_watch')
    return value


def _price(value):
    """目标价必须是有限正数（排除 bool），否则当作没有。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def row_targets(row):
    """从信号行里取目标买入/卖出价；没有这两个字段时返回空，绝不自己猜价格。"""
    buy = _price(row.get('buy_price', row.get('target_buy')))
    sell = _price(row.get('sell_price', row.get('target_sell')))
    return {k: v for k, v in (('buy', buy), ('sell', sell)) if v is not None}


def default_holdings():
    """美股持仓：T013 台账不支持美股代码，第一版为空。"""
    return []


def default_watch():
    """美股自选（只读本机文件）；出错降级为空。load_watchlist 返回 {代码: 名称}。"""
    try:
        from src import datasource
        return list(datasource.load_watchlist('us'))
    except Exception:
        LOG.info('watchlist: watchlist source unavailable, degraded to empty')
        return []


def default_signals():
    """美股今日买卖清单：尚不存在，第一版为空。"""
    return []


def build_us_watchlist(top_n=DEFAULT_TOP_N, *, max_watch=DEFAULT_MAX_WATCH, holdings_fn=default_holdings,
                       watch_fn=default_watch, signals_fn=default_signals):
    """返回 {'codes': [...], 'sources': {code: [来源]}, 'targets': {code: {'buy','sell'}},
    'signals': {code: {'action','name','close'}}, 'truncated': bool}。

    顺序 持仓 > 自选 > 信号，去重后同一代码保留全部来源；超出上限时先丢优先级最低的。
    """
    validate_max_watch(max_watch)
    if type(top_n) is not int or not 0 <= top_n <= max_watch:
        raise ValueError('invalid_top_n')

    def safe(fn):
        try:
            return list(fn())
        except Exception:
            LOG.info('watchlist: a source failed, degraded to empty')
            return []

    sources, targets, order, signals = {}, {}, [], {}

    def add(code, source):
        if not valid_symbol(code):
            return False
        if code not in sources:
            sources[code] = []
            order.append(code)
        if source not in sources[code]:
            sources[code].append(source)
        return True

    for code in safe(holdings_fn):
        add(code, SOURCE_HOLDING)
    for code in safe(watch_fn):
        add(code, SOURCE_WATCH)
    taken = 0
    for row in safe(signals_fn):
        if taken >= top_n:
            break
        code = row.get('code') if isinstance(row, dict) else None
        if add(code, SOURCE_SIGNAL):
            taken += 1
            signals[code] = {k: row.get(k) for k in ('action', 'name', 'close') if row.get(k) is not None}
            found = row_targets(row)
            if found:
                targets[code] = found
    truncated = len(order) > max_watch
    if truncated:
        LOG.warning('watchlist: over limit (%d > %d), truncated', len(order), max_watch)
        order = order[:max_watch]
    keep = set(order)
    return {'codes': order, 'sources': {c: sources[c] for c in order},
            'targets': {c: t for c, t in targets.items() if c in keep},
            'signals': {c: v for c, v in signals.items() if c in keep}, 'truncated': truncated}
