"""美股低频实时信号跟踪：A 执行时机提醒、B 当日信号追踪、C 当日新信号探测。镜像 src/market_monitor/intraday.py。

全部是纯函数（写盘除外，且路径由调用者指定）；不请求任何外部数据，不改信号账本/策略。
盘中价格不是收盘价：这里的结果只是"提醒与记录"，不是经过验证的交易建议。
报价样本（sample）是调度器在内存里攒的 dict：
    {'t': 带时区 datetime, 'price', 'prev_close', 'open', 'high', 'low', 'volume'(当日累计，单位：股), 'name'}
quotes_history = {代码: [sample, ...]}（按时间升序）；quotes_snapshot = {代码: 最新 sample}。

与 A 股版的差异（因子公式相同，阈值可配）：
- 没有涨跌停：删除"涨停不可买/跌停不可卖"。LULD 熔断无法从 yfinance 可靠识别，不做识别。
- 时间一律按美东（America/New_York，夏令时自动）：开盘 09:30，"当日"=美东日期。
- 成交量单位是股，不乘 100（A 股的"手"才乘）。
- 持仓异常波动阈值默认 7%（A 股 5%）；追踪表的"距涨跌停"换成"距当日高/低点"。
- 流动性过滤：价格 < min_price，或（有均量时）均成交额 < min_dollar_volume 的股票，
  新机会事件带 display_only=True——只展示，不登记追踪、不算入提示。
  均成交额 = 20 日均量 × 当前价，是近似值（日线均价缺失时的替代）。
- 均量缺失：放量项不判定，事件 params 里标 volume_data='insufficient'（页面可显示"数据不足"）。
"""
from datetime import datetime, time, timedelta
import logging
import math
from pathlib import Path
import re

from ..market_monitor.intraday import (register_triggers, summarize,  # noqa: F401  复用已验收的纯函数
                                       append_jsonl as _append_jsonl, read_jsonl as _read_jsonl)
from .core import US_EASTERN
from .store import STORE_ROOT

LOG = logging.getLogger('us_market_monitor')

INTRADAY_DIR = STORE_ROOT.parent / 'us_market_monitor_intraday'     # data/us_market_monitor_intraday
MAX_UNIVERSE = 30
BUY_ACTIONS = ('买入', '加仓')
SELL_ACTIONS = ('卖出', '减仓')
OPEN_TIME = time(9, 30)
REFERENCE_NOTE = '仅参考,未经分钟级回测验证'
VOLUME_SCALE = 1               # 美股成交量单位就是股

# 保守默认阈值（可配；全部为正有限数，判断一律严格大于）
DEFAULT_THRESHOLDS = {
    'momentum': 0.03,              # 开盘后 X 分钟动量：当前价/开盘价-1
    'momentum_minutes': 5,
    'volume_ratio': 2.0,           # 当日累计成交量 / N 日均量
    'breakout': 0.01,              # 相对当日此前最高/最低价 1%
    'drop_pct': 7.0,               # 持仓当日跌幅（%）
    'window_minutes': 5,           # 买入窗口：开盘后 N 分钟（含端点）
    'min_price': 5.0,              # 流动性过滤：最低价（美元）
    'min_dollar_volume': 10_000_000.0,   # 流动性过滤：20 日均成交额（美元，近似）
}


def _gt(x, threshold):
    """严格大于，带 1e-9 容差，避免浮点误差把"恰好 3%"判成触发。"""
    return x - threshold > 1e-9


def _pos(x):
    return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def _num(x):
    return float(x) if not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x) else None


def _ret(price, base):
    return (price / base - 1) * 100 if _pos(price) and _pos(base) else None


def _valid_day(day):
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('invalid_day')
    return day


def _local(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('需要带时区时间')
    return now.astimezone(US_EASTERN)


def _iso(now):
    return _local(now).isoformat()


def _codes(items):
    return {str(x) for x in (items.keys() if isinstance(items, dict) else items or ())}


def _action_map(signals):
    """{代码: 动作集合}；信号行格式不对的直接忽略。"""
    out = {}
    for row in signals or ():
        if isinstance(row, dict) and isinstance(row.get('code'), str) and isinstance(row.get('action'), str):
            out.setdefault(row['code'], set()).add(row['action'])
    return out


def _thresholds(custom):
    merged = dict(DEFAULT_THRESHOLDS)
    for key, value in (custom or {}).items():
        if key not in merged or not _pos(value):
            raise ValueError('invalid_threshold')
        merged[key] = value
    return merged


def _event(now, code, kind, severity, message, **params):
    return {'timestamp': _iso(now), 'code': code, 'kind': kind, 'severity': severity, 'message': message, 'params': params}


# ---------------------------------------------------------------- A 执行时机提醒
def execution_hints(now, signals, positions, quotes_snapshot, *, thresholds=None):
    """纯函数：美股买卖清单 + 持仓 + 最新快照 -> 事件列表。

    a) 美东开盘后 window_minutes 分钟内（09:30:00–09:35:00 含端点，周一至周五），
       对尚未持有的买入信号发 buy_window；
    b) 持仓股当日跌幅 > drop_pct%：holding_drop。
    没有涨跌停类事件，也没有 T+1 类提示（美股无此限制）。
    """
    th = _thresholds(thresholds)
    local = _local(now)
    actions, held = _action_map(signals), _codes(positions)
    snap = quotes_snapshot or {}
    events = []
    start = datetime.combine(local.date(), OPEN_TIME, tzinfo=US_EASTERN)
    in_window = local.weekday() < 5 and start <= local <= start + timedelta(minutes=th['window_minutes'])
    for code in sorted(set(actions) | held):
        acts = actions.get(code, set())
        if in_window and '买入' in acts and code not in held:
            events.append(_event(now, code, 'buy_window', 'ok',
                                 f'{code} 买入窗口打开（美东开盘后 {int(th["window_minutes"])} 分钟内）',
                                 minutes=int(th['window_minutes'])))
        sample = snap.get(code)
        if not isinstance(sample, dict) or code not in held:
            continue
        price, prev = sample.get('price'), sample.get('prev_close')
        if _pos(price) and _pos(prev):
            pct = (price / prev - 1) * 100
            if _gt(-pct, th['drop_pct']):
                events.append(_event(now, code, 'holding_drop', 'danger',
                                     f'{code} 持仓异常波动，当日跌 {-pct:.2f}%，考虑止损', pct=round(pct, 4)))
    return events


# ---------------------------------------------------------------- B 当日信号追踪
def _today_samples(day, samples):
    return [s for s in samples or () if isinstance(s, dict) and isinstance(s.get('t'), datetime)
            and s['t'].tzinfo is not None and s['t'].astimezone(US_EASTERN).date().isoformat() == day
            and _pos(s.get('price'))]


def _distance_to_extremes(samples, open_price):
    """(距当日高点%, 距当日低点%)：高点/现价-1（≥0）、低点/现价-1（≤0）。

    高低点取行情自带的当日最高/最低（有则用），再与已采样价格和开盘价取并集，保证现价一定落在区间内。
    """
    last = samples[-1]
    prices = [open_price] + [s['price'] for s in samples]
    highs = prices + ([last['high']] if _pos(last.get('high')) else [])
    lows = prices + ([last['low']] if _pos(last.get('low')) else [])
    price = last['price']
    return (max(highs) / price - 1) * 100, (min(lows) / price - 1) * 100


def track_signals(day, signals, quotes_history, *, now=None, directory_path=None, write=True):
    """对每条美股买卖信号生成追踪记录，并（write=True）追加写入 <目录>/<day>.jsonl。

    开盘价取行情开盘字段（open），缺失时退回当日第一笔有效报价；建议价取信号行 suggest_open 或信号日收盘价 close。
    return_since_signal / return_since_open 只对买入类信号计算；卖出类为 None（不计入正确率）。
    """
    _valid_day(day)
    now = now or datetime.now(US_EASTERN)
    records = []
    for row in signals or ():
        if not (isinstance(row, dict) and isinstance(row.get('code'), str) and isinstance(row.get('action'), str)):
            continue
        code, action = row['code'], row['action']
        samples = _today_samples(day, (quotes_history or {}).get(code))
        if not samples:
            continue
        last = samples[-1]
        price = last['price']
        open_price, open_source = (last.get('open'), 'exchange') if _pos(last.get('open')) else (samples[0]['price'], 'first_quote')
        ref = row.get('suggest_open') if _pos(row.get('suggest_open')) else row.get('close')
        buy = action in BUY_ACTIONS
        to_high, to_low = _distance_to_extremes(samples, open_price)
        records.append({
            'type': 'track', 'day': day, 'ts': _iso(now), 'code': code, 'name': last.get('name') or row.get('name') or '',
            'action': action, 'open': open_price, 'open_source': open_source, 'price': price,
            'change_vs_open_pct': _ret(price, open_price), 'to_day_high_pct': to_high, 'to_day_low_pct': to_low,
            'ref_price': ref if _pos(ref) else None,
            'return_since_signal': _ret(price, ref) if buy else None,
            'return_since_open': _ret(price, open_price) if buy else None})
    if write and records:
        append_jsonl(day, records, directory_path)
    return records


def track_signals_summary(day, directory_path=None):
    """读 <day>.jsonl 汇总；文件不存在或坏行都不报错（坏行跳过）。"""
    return summarize(read_jsonl(day, directory_path))


# ---------------------------------------------------------------- C 当日新信号探测
def default_avg_volumes(codes, n=20):
    """从本地美股日线缓存读 N 日均量（股）；读不到的代码不返回。只读本机缓存文件，不联网。"""
    out = {}
    try:
        from src import datasource
        for code in codes:
            try:
                df = datasource.load_cached('us', code)
                vol = df['Volume'].dropna().tail(n)
                if len(vol) >= max(5, n // 2) and float(vol.mean()) > 0:
                    out[code] = float(vol.mean())
            except Exception:
                continue
    except Exception:
        LOG.info('intraday: avg volume source unavailable')
    return out


def detect_new_signals(day, quotes_history, holdings, universe_limit=MAX_UNIVERSE, *, now=None,
                       thresholds=None, avg_volumes=None, volume_scale=VOLUME_SCALE):
    """用日线因子在当日分钟数据上重算三个：动量、放量、短期突破。只处理名单内最多 universe_limit 只。

    - 动量：当前价/开盘价-1，美东开盘后 momentum_minutes 分钟之后才判断；
    - 放量：当日累计成交量×volume_scale / N 日均量（缺均量则跳过此项）；放量且价格低于开盘价算负向；
    - 突破：当前价相对"此前当日最高/最低价（含开盘价）"超过 breakout 的幅度（取此前价格，避免当前价自己和自己比）。
    持仓股触发任一负向 -> consider_sell（红）；未持仓股触发任一正向 -> watch（关注，仅参考）。
    流动性不足（价格或均成交额低于阈值）的事件带 display_only=True。
    """
    _valid_day(day)
    if type(universe_limit) is not int or not 1 <= universe_limit <= MAX_UNIVERSE:
        raise ValueError('invalid_universe_limit')
    th = _thresholds(thresholds)
    held = _codes(holdings)
    history = quotes_history or {}
    avg = avg_volumes or {}
    ordered = [c for c in history if c in held] + [c for c in history if c not in held]
    events = []
    for code in ordered[:universe_limit]:
        samples = _today_samples(day, history.get(code))
        if not samples:
            continue
        last = samples[-1]
        price = last['price']
        when = now or last['t']
        open_price = last['open'] if _pos(last.get('open')) else samples[0]['price']
        up, down, values = [], [], {}
        start = datetime.combine(last['t'].astimezone(US_EASTERN).date(), OPEN_TIME, tzinfo=US_EASTERN)
        mom = price / open_price - 1
        if last['t'].astimezone(US_EASTERN) >= start + timedelta(minutes=th['momentum_minutes']):
            values['momentum'] = round(mom * 100, 4)
            if _gt(mom, th['momentum']):
                up.append('momentum')
            elif _gt(-mom, th['momentum']):
                down.append('momentum')
        volume, base = _num(last.get('volume')), _num(avg.get(code))
        if volume is not None and base and base > 0:
            ratio = volume * volume_scale / base
            values['volume_ratio'] = round(ratio, 4)
            if _gt(ratio, th['volume_ratio']):
                (down if price < open_price else up).append('volume')
        else:
            values['volume_data'] = 'insufficient'
        prior = [s['price'] for s in samples[:-1]]
        if prior:
            high, low = max([open_price] + prior), min([open_price] + prior)
            if _gt(price / high - 1, th['breakout']):
                up.append('breakout')
                values['breakout_pct'] = round((price / high - 1) * 100, 4)
            elif _gt(-(price / low - 1), th['breakout']):
                down.append('breakout')
                values['breakout_pct'] = round((price / low - 1) * 100, 4)
        # 流动性：价格过低一律只展示；均成交额只在有均量时才能判断
        illiquid = price < th['min_price'] or (bool(base) and base * price < th['min_dollar_volume'])
        names = {'momentum': '动量', 'volume': '放量', 'breakout': '突破'}
        if code in held and down:
            ev = _event(when, code, 'consider_sell', 'danger',
                        f'{code} 持仓触发负向信号（{"、".join(names[t] for t in down)}），考虑卖出；{REFERENCE_NOTE}',
                        price=price, triggers=down, **values)
        elif code not in held and up:
            ev = _event(when, code, 'watch', 'watch',
                        f'{code} 触发正向信号（{"、".join(names[t] for t in up)}），关注；{REFERENCE_NOTE}',
                        price=price, triggers=up, **values)
        else:
            continue
        ev['reference_only'] = True
        if illiquid:
            ev['display_only'] = True
        events.append(ev)
    return events


def track_triggers(day, triggers, quotes_history, *, now=None, directory_path=None, write=True):
    """对每条当日新信号计算 return_since_trigger = (当前价/触发价-1)*100，并追加写入同一 jsonl。"""
    _valid_day(day)
    now = now or datetime.now(US_EASTERN)
    records = []
    for info in (triggers or {}).values():
        samples = _today_samples(day, (quotes_history or {}).get(info['code']))
        if not samples:
            continue
        records.append({'type': 'trigger', 'day': day, 'ts': _iso(now), 'code': info['code'], 'kind': info['kind'],
                        'triggers': info.get('triggers', []), 'trigger_price': info['trigger_price'],
                        'trigger_ts': info['trigger_ts'], 'price': samples[-1]['price'],
                        'return_since_trigger': _ret(samples[-1]['price'], info['trigger_price'])})
    if write and records:
        append_jsonl(day, records, directory_path)
    return records


# ---------------------------------------------------------------- JSONL 读写（目录 700、文件 600、拒绝符号链接）
# 底层复用 A 股版已验收的实现，只把默认目录换成美股专用目录。
def append_jsonl(day, records, directory_path=None):
    _append_jsonl(day, records, Path(directory_path or INTRADAY_DIR))


def read_jsonl(day, directory_path=None):
    return _read_jsonl(day, Path(directory_path or INTRADAY_DIR))
