"""实时盯盘的监控名单：我的持仓 + 'cn' 自选 + 今日买卖清单前 N 只；只读，不改任何来源。

优先级固定为 持仓 > 自选 > 信号：超过硬上限被截断时，先丢掉优先级最低的信号股。
所有来源函数都可注入（测试用合成数据）；默认实现全部只读，任何一个来源出错都降级为空列表。
"""
import logging
import math

from .core import valid_symbol

LOG = logging.getLogger('market_monitor')

DEFAULT_MAX_WATCH = 500  # 默认名单上限（T016 S2：原固定 60，现为可配置参数，批量行情源才放得开）
HARD_MAX_WATCH = 5500    # 绝对硬上限（约等于 A 股全市场）：构造参数也不能超过它
MAX_WATCH = DEFAULT_MAX_WATCH   # 兼容旧导入名：等于默认值
DEFAULT_TOP_N = 20      # 今日买卖清单默认取前 20 只（T006_plan_rev1 / 所有者 14:29 原话）
SIGNAL_ACTIONS = ('买入', '卖出', '加仓', '减仓')
SOURCE_HOLDING, SOURCE_WATCH, SOURCE_SIGNAL = 'holding', 'watchlist', 'signal'


def validate_max_watch(value):
    """max_watch 必须是 1..HARD_MAX_WATCH 的真 int（拒绝 bool/浮点）。"""
    if type(value) is not int or not 1 <= value <= HARD_MAX_WATCH:
        raise ValueError('invalid_max_watch')
    return value


def _valid_code(code):
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        return False
    return valid_symbol(('sh' if code.startswith('6') else 'sz') + code)


def _price(value):
    """目标价必须是有限正数（排除 bool），否则当作没有。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def row_targets(row):
    """从清单行里取目标买入/卖出价。清单目前没有这两个字段时返回空，绝不自己猜价格。"""
    buy = _price(row.get('buy_price', row.get('target_buy')))
    sell = _price(row.get('sell_price', row.get('target_sell')))
    return {k: v for k, v in (('buy', buy), ('sell', sell)) if v is not None}


def default_holdings():
    """读 T013 持仓台账里当前仍持有的代码；台账缺失/出错返回空列表（降级）。"""
    try:
        from src.holdings.calc import replay
        from src.holdings.store import HoldingsStore
        states, _ = replay(HoldingsStore().load_trades())
        return [code for code, st in states.items() if st['shares'] > 0]
    except Exception:
        LOG.info('watchlist: holdings source unavailable, degraded to empty')
        return []


def default_watch():
    try:
        from src import datasource
        return list(datasource.load_watchlist('cn'))
    except Exception:
        LOG.info('watchlist: watchlist source unavailable, degraded to empty')
        return []


def default_signals():
    """今日买卖最新清单（默认策略）中有明确动作的行，按清单原顺序；只读。"""
    try:
        from src import trade_list
        data = trade_list.load(strategy='blend2')
        if data.get('empty'):
            return []
        return [row for row in data.get('rows', []) if isinstance(row, dict) and row.get('action') in SIGNAL_ACTIONS]
    except Exception:
        LOG.info('watchlist: signal source unavailable, degraded to empty')
        return []


def default_stops():
    """D108：持仓的自动止损线（见 stops.py）；出错降级为空。"""
    try:
        from .stops import stop_lines
        return stop_lines()
    except Exception:
        LOG.info('watchlist: stop lines unavailable, degraded to empty')
        return {}


def build_watchlist(top_n=DEFAULT_TOP_N, *, max_watch=DEFAULT_MAX_WATCH, holdings_fn=default_holdings, watch_fn=default_watch,
                    signals_fn=default_signals, stops_fn=None):
    """返回 {'codes': [...], 'sources': {code: [来源]}, 'targets': {code: {'buy','sell'}},
    'signals': {code: {'action','name','close'}}（S3b：供盘中实时分析用，只读拷贝）,
    'stops': {code: 止损线}（D108：只给名单里的持仓股，见 stops.stop_lines）, 'truncated': bool}。"""
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
        if not _valid_code(code):
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
    if stops_fn is None:                     # 只有用真实持仓台账时才默认算止损线；注入假持仓（测试）时不去读真实台账和行情
        stops_fn = default_stops if holdings_fn is default_holdings else dict
    try:
        stops = dict(stops_fn() or {})
    except Exception:
        LOG.info('watchlist: stop lines failed, degraded to empty')
        stops = {}
    return {'codes': order, 'sources': {c: sources[c] for c in order},
            'targets': {c: t for c, t in targets.items() if c in keep},
            'signals': {c: v for c, v in signals.items() if c in keep},
            'stops': {c: v for c, v in stops.items() if c in keep and SOURCE_HOLDING in sources.get(c, [])},
            'truncated': truncated}
