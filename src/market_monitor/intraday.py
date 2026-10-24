"""低频实时信号跟踪（T006 S3b）：A 执行时机提醒、B 当日日线信号追踪、C 当日新信号探测。

全部是纯函数（B 的写盘除外，且路径由调用者指定）；不请求任何外部数据，不改信号账本/策略。
盘中价格不是收盘价：这里的结果只是"提醒与记录"，不是经过验证的交易建议。
报价样本（sample）是调度器在内存里攒的 dict：
    {'t': 带时区 datetime, 'price', 'prev_close', 'open', 'volume'(当日累计，原始单位), 'name'}
quotes_history = {代码: [sample, ...]}（按时间升序）；quotes_snapshot = {代码: 最新 sample}。
"""
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
import math
import os
from pathlib import Path
import re
from statistics import mean, median

from .core import BEIJING
from .store import STORE_ROOT, StorageError, directory

LOG = logging.getLogger('market_monitor')

INTRADAY_DIR = STORE_ROOT.parent / 'market_monitor_intraday'     # data/market_monitor_intraday
MAX_UNIVERSE = 60                  # 与 watchlist.MAX_WATCH 一致，不扫全市场
BUY_ACTIONS = ('买入', '加仓')
SELL_ACTIONS = ('卖出', '减仓')
OPEN_TIME = time(9, 30)
REFERENCE_NOTE = '仅参考,未经分钟级回测验证'

# 保守默认阈值（可配）
DEFAULT_THRESHOLDS = {
    'momentum': 0.03,          # 开盘后 X 分钟动量：当前价/开盘价-1
    'momentum_minutes': 5,
    'volume_ratio': 2.0,       # 当日累计成交量 / N 日均量
    'breakout': 0.01,          # 相对当日此前最高/最低价 1%
    'drop_pct': 5.0,           # A.d 持仓当日跌幅（%）
    'window_minutes': 5,       # A.a 开盘窗口
}
VOLUME_SCALE = 100             # 腾讯成交量按"手"(100 股)估计；单位未经官方证实，见 howto


# ---------------------------------------------------------------- 涨跌停规则
def limit_pct(code, name=''):
    """涨跌停幅度：ST 5%、创业板/科创板 20%、主板 10%（与 datasource.limit_pct 同口径，这里不导入 pandas）。"""
    if 'ST' in str(name).upper():
        return 0.05
    if str(code).startswith(('688', '689', '300', '301')):
        return 0.20
    return 0.10


def _cents(x):
    return int(Decimal(repr(float(x))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) * 100)


def limit_prices(prev_close, pct):
    """涨停价/跌停价（分），按交易所惯例对昨收×(1±幅度) 四舍五入到分。"""
    up = Decimal(repr(float(prev_close))) * (Decimal(1) + Decimal(repr(pct)))
    dn = Decimal(repr(float(prev_close))) * (Decimal(1) - Decimal(repr(pct)))
    q = Decimal('0.01')
    return (int(up.quantize(q, rounding=ROUND_HALF_UP) * 100), int(dn.quantize(q, rounding=ROUND_HALF_UP) * 100))


def _gt(x, threshold):
    """严格大于，带 1e-9 容差，避免 10.3/10-1=0.0300000000000002 这类浮点误差把"恰好 3%"判成触发。"""
    return x - threshold > 1e-9


def _pos(x):
    return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def limit_state(code, name, price, prev_close):
    """返回 'up'（涨停）/'down'（跌停）/None；价格恰好等于涨停价也算涨停（边界含等号）。"""
    if not (_pos(price) and _pos(prev_close)):
        return None
    up, dn = limit_prices(prev_close, limit_pct(code, name))
    cents = _cents(price)
    if cents >= up:
        return 'up'
    if cents <= dn:
        return 'down'
    return None


def distance_to_limits(code, name, price, prev_close):
    """(距涨停%, 距跌停%)：涨停价/现价-1、跌停价/现价-1（后者为负）；数据无效返回 (None, None)。"""
    if not (_pos(price) and _pos(prev_close)):
        return None, None
    up, dn = limit_prices(prev_close, limit_pct(code, name))
    return (up / 100 / price - 1) * 100, (dn / 100 / price - 1) * 100


# ---------------------------------------------------------------- 公共小工具
def _local(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('需要带时区时间')
    return now.astimezone(BEIJING)


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
        if key not in merged or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('invalid_threshold')
        merged[key] = value
    return merged


def _event(now, code, kind, severity, message, **params):
    return {'timestamp': _iso(now), 'code': code, 'kind': kind, 'severity': severity, 'message': message, 'params': params}


# ---------------------------------------------------------------- A 执行时机提醒
def execution_hints(now, signals, positions, quotes_snapshot, *, thresholds=None):
    """纯函数：今日买卖清单 + 持仓 + 最新快照 -> 事件列表 [{timestamp, code, kind, message, ...}]。

    a) 开盘后 window_minutes 分钟内，对尚未持有的买入信号发 buy_window（持有的视为已执行）；
    b) 当前涨停且有买入信号：limit_up_no_buy；
    c) 当前跌停且（有卖出信号或持仓里有）：limit_down_no_sell；
    d) 持仓股当日跌幅 > drop_pct%：holding_drop。
    """
    th = _thresholds(thresholds)
    local = _local(now)
    actions, held = _action_map(signals), _codes(positions)
    snap = quotes_snapshot or {}
    events = []
    start = datetime.combine(local.date(), OPEN_TIME, tzinfo=BEIJING)
    in_window = local.weekday() < 5 and start <= local < start + timedelta(minutes=th['window_minutes'])
    for code in sorted(set(actions) | held):
        acts = actions.get(code, set())
        buy, sell = bool(acts & set(BUY_ACTIONS)), bool(acts & set(SELL_ACTIONS))
        if in_window and '买入' in acts and code not in held:
            events.append(_event(now, code, 'buy_window', 'ok', f'{code} 买入窗口打开（开盘后 {int(th["window_minutes"])} 分钟内）',
                                 minutes=int(th['window_minutes'])))
        sample = snap.get(code)
        if not isinstance(sample, dict):
            continue
        price, prev, name = sample.get('price'), sample.get('prev_close'), sample.get('name') or ''
        state = limit_state(code, name, price, prev)
        if state == 'up' and buy:
            events.append(_event(now, code, 'limit_up_no_buy', 'danger', f'{code} 涨停不可买，追不进', price=price))
        if state == 'down' and (sell or code in held):
            events.append(_event(now, code, 'limit_down_no_sell', 'danger', f'{code} 跌停不可卖', price=price))
        if code in held and _pos(price) and _pos(prev):
            pct = (price / prev - 1) * 100
            if _gt(-pct, th['drop_pct']):
                events.append(_event(now, code, 'holding_drop', 'danger',
                                     f'{code} 持仓异常波动，当日跌 {-pct:.2f}%，考虑止损', pct=round(pct, 4)))
    return events


# ---------------------------------------------------------------- B 当日信号追踪
def _today_samples(day, samples):
    return [s for s in samples or () if isinstance(s, dict) and isinstance(s.get('t'), datetime)
            and s['t'].tzinfo is not None and s['t'].astimezone(BEIJING).date().isoformat() == day and _pos(s.get('price'))]


def _valid_day(day):
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('invalid_day')
    return day


def _ret(price, base):
    return (price / base - 1) * 100 if _pos(price) and _pos(base) else None


def track_signals(day, signals, quotes_history, *, now=None, directory_path=None, write=True):
    """对今日买卖清单每条信号生成追踪记录，并（write=True）追加写入 <目录>/<day>.jsonl。

    开盘价取交易所开盘价字段（open），缺失时退回当日第一笔有效报价；建议价取信号行 suggest_open 或信号日收盘价 close。
    return_since_signal / return_since_open 只对买入类信号计算；卖出类为 None（不计入正确率）。
    """
    _valid_day(day)
    now = now or datetime.now(BEIJING)
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
        up, dn = distance_to_limits(code, last.get('name') or row.get('name') or '', price, last.get('prev_close'))
        records.append({
            'type': 'track', 'day': day, 'ts': _iso(now), 'code': code, 'name': last.get('name') or row.get('name') or '',
            'action': action, 'open': open_price, 'open_source': open_source, 'price': price,
            'change_vs_open_pct': _ret(price, open_price), 'to_limit_up_pct': up, 'to_limit_down_pct': dn,
            'ref_price': ref if _pos(ref) else None,
            'return_since_signal': _ret(price, ref) if buy else None,
            'return_since_open': _ret(price, open_price) if buy else None})
    if write and records:
        append_jsonl(day, records, directory_path)
    return records


def summarize(records):
    """从追踪记录（每只取最新一条）汇总实时正确率：winner = return_since_open > 0。"""
    latest = {}
    for r in records or ():
        if isinstance(r, dict) and r.get('type') == 'track' and _num(r.get('return_since_open')) is not None:
            latest[r.get('code')] = r      # 文件按追加顺序，后者覆盖前者
    values = [r['return_since_open'] for r in latest.values()]
    return {'total': len(values), 'winners': sum(1 for v in values if v > 0), 'losers': sum(1 for v in values if v < 0),
            'mean_return': mean(values) if values else None, 'median_return': median(values) if values else None}


def _num(x):
    return float(x) if not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x) else None


def track_signals_summary(day, directory_path=None):
    """读 <day>.jsonl 汇总；文件不存在或坏行都不报错（坏行跳过）。"""
    return summarize(read_jsonl(day, directory_path))


# ---------------------------------------------------------------- C 当日新信号探测
def default_avg_volumes(codes, n=20):
    """从本地日线缓存读 N 日均量（股）；读不到的代码不返回。只读本机缓存文件，不联网。"""
    out = {}
    try:
        from src import datasource
        for code in codes:
            try:
                df = datasource.load_cached('cn', code)
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

    - 动量：当前价/开盘价-1，开盘后 momentum_minutes 分钟之后才判断；
    - 放量：当日累计成交量×volume_scale / N 日均量（缺均量则跳过此项）；放量且价格低于开盘价算负向；
    - 突破：当前价相对"此前当日最高/最低价（含开盘价）"超过 breakout 的幅度（取此前价格，避免当前价自己和自己比）。
    持仓股触发任一负向 -> consider_sell（红）；未持仓股触发任一正向 -> watch（关注，仅参考）。
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
        start = datetime.combine(last['t'].astimezone(BEIJING).date(), OPEN_TIME, tzinfo=BEIJING)
        mom = price / open_price - 1
        if last['t'].astimezone(BEIJING) >= start + timedelta(minutes=th['momentum_minutes']):
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
        prior = [s['price'] for s in samples[:-1]]
        if prior:
            high, low = max([open_price] + prior), min([open_price] + prior)
            if _gt(price / high - 1, th['breakout']):
                up.append('breakout')
                values['breakout_pct'] = round((price / high - 1) * 100, 4)
            elif _gt(-(price / low - 1), th['breakout']):
                down.append('breakout')
                values['breakout_pct'] = round((price / low - 1) * 100, 4)
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
        events.append(ev)
    return events


def register_triggers(triggers, events):
    """记录每个 (代码, 种类) 的首次触发价；已有的不覆盖。返回同一个 dict。"""
    for ev in events or ():
        price = ev.get('params', {}).get('price')
        key = f"{ev['code']}:{ev['kind']}"
        if key not in triggers and _pos(price):
            triggers[key] = {'code': ev['code'], 'kind': ev['kind'], 'trigger_price': price, 'trigger_ts': ev['timestamp'],
                             'triggers': list(ev['params'].get('triggers', []))}
    return triggers


def track_triggers(day, triggers, quotes_history, *, now=None, directory_path=None, write=True):
    """对每条当日新信号计算 return_since_trigger = (当前价/触发价-1)*100，并追加写入同一 jsonl。"""
    _valid_day(day)
    now = now or datetime.now(BEIJING)
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


# ---------------------------------------------------------------- JSONL 读写（追加式，目录 700、文件 600、拒绝符号链接）
def append_jsonl(day, records, directory_path=None):
    _valid_day(day)
    lines = ''.join(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n' for r in records).encode('utf-8')
    with directory(Path(directory_path or INTRADAY_DIR), create=True) as fd:
        out = os.open(f'{day}.jsonl', os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            os.write(out, lines)
        finally:
            os.close(out)


def read_jsonl(day, directory_path=None):
    _valid_day(day)
    rows = []
    try:
        with directory(Path(directory_path or INTRADAY_DIR)) as fd:
            src = os.open(f'{day}.jsonl', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(src, 'r', encoding='utf-8') as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict):
                        rows.append(item)
    except (OSError, StorageError):      # 目录/文件不存在、权限不符都按"没有数据"处理
        pass
    return rows
