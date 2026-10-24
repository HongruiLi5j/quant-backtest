"""离线记录、质量检查和价格穿越纯函数；不推断节假日或停牌。"""
from dataclasses import dataclass, replace
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import math
import re
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo('Asia/Shanghai')


def valid_symbol(symbol):
    return isinstance(symbol, str) and re.fullmatch(
        r'(?:sh(?:600|601|603|605|688)|sz(?:000|001|002|003|300|301))\d{3}', symbol) is not None


def continuous_session(at):
    """仅周一至周五的钟表提示，含端点；日历不可用，不代表确认交易日。"""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('需要带时区时间')
    local = at.astimezone(BEIJING)
    clock = local.time()
    return local.weekday() < 5 and (time(9, 30) <= clock <= time(11, 30)
                                  or time(13) <= clock <= time(15))


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    price: float | None
    previous_close: float | None
    opening: float | None
    volume_raw: str
    source_time_raw: str
    source_time: datetime | None
    received_at: datetime
    elapsed_seconds: float
    quality: str = 'unchecked'
    reasons: tuple[str, ...] = ()
    session: str = 'unknown'
    provider: str = 'tencent_public_unverified'
    fields_raw: tuple[str, ...] = ()


def positive(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def volume(value):
    try:
        result = Decimal(value)
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def validate_quote(quote, expected_symbol, previous=None, stale_seconds=120):
    """阈值为所有者验收目标，非供应商承诺；previous 应保留上次观测用于查重。"""
    if not positive(stale_seconds):
        raise ValueError('过旧阈值必须为有限正数')
    reasons = []
    if not valid_symbol(expected_symbol) or quote.symbol != expected_symbol:
        reasons.append('code_mismatch')
    if not all(positive(v) for v in (quote.price, quote.previous_close, quote.opening)):
        reasons.append('invalid_price')
    if not math.isfinite(quote.elapsed_seconds) or quote.elapsed_seconds < 0:
        reasons.append('invalid_elapsed')
    received = quote.received_at
    aware = isinstance(received, datetime) and received.tzinfo is not None and received.utcoffset() is not None
    session = ('continuous_auction_hint' if continuous_session(received) else '非交易时段') if aware else 'unknown'
    if not aware:
        reasons.append('invalid_received_time')
    try:
        if not re.fullmatch(r'\d{14}', quote.source_time_raw):
            raise ValueError()
        source = datetime.strptime(quote.source_time_raw, '%Y%m%d%H%M%S').replace(tzinfo=BEIJING)
    except (ValueError, TypeError):
        source = None
        reasons.append('invalid_source_time')
    if source is not None and aware:
        age = (received - source).total_seconds()
        if age < -5:
            reasons.append('future_time')
        if continuous_session(received) and age > stale_seconds:
            reasons.append('stale')
    current_volume = volume(quote.volume_raw)
    if current_volume is None:
        reasons.append('invalid_volume')
    if previous is not None and previous.symbol == quote.symbol and source is not None and previous.source_time is not None:
        if source < previous.source_time:
            reasons.append('timestamp_regression')
        elif source == previous.source_time:
            reasons.append('duplicate_timestamp')
        if source.date() == previous.source_time.astimezone(BEIJING).date():
            prior_volume = volume(previous.volume_raw)
            if current_volume is not None and prior_volume is not None and current_volume < prior_volume:
                reasons.append('volume_regression')
    quality = ('stale' if reasons == ['stale'] else 'invalid') if reasons else ('valid' if session == 'continuous_auction_hint' else 'off_session')
    return replace(quote, source_time=source, quality=quality, reasons=tuple(reasons), session=session)


@dataclass(frozen=True)
class Condition:
    symbol: str
    direction: str
    threshold: float
    cooldown_seconds: float = 300
    daily_limit: int = 3

    def __post_init__(self):
        if (not valid_symbol(self.symbol) or self.direction not in ('up', 'down')
                or not positive(self.threshold) or not math.isfinite(self.cooldown_seconds)
                or self.cooldown_seconds < 0 or type(self.daily_limit) is not int or self.daily_limit < 1):
            raise ValueError('非法价格条件')


@dataclass(frozen=True)
class AlertState:
    condition: Condition | None = None
    baseline: Quote | None = None
    last_alert: datetime | None = None
    day: str = ''
    count: int = 0


@dataclass(frozen=True)
class Alert:
    condition: Condition
    previous_price: float
    price: float
    source_time: datetime
    received_at: datetime


def alert_step(state, condition, quote, *, connected=True, max_gap_seconds=120):
    """纯函数，返回新状态和可选提醒。None/断线/异常/换配置均重建基线。

    每个条件单独持有状态；冷却及次数不因断线或配置变化清零。
    去重来自严格递增时间和重新穿越；冷却内的穿越不会延迟补发。
    max_gap_seconds 同时用于观测缺口和记录过旧检查；调大它也会放宽过旧阈值。
    """
    if not positive(max_gap_seconds):
        raise ValueError('缺口阈值必须为有限正数')
    baseline = state.baseline if state.condition == condition else None
    state = replace(state, condition=condition, baseline=None)
    if not connected or quote is None:
        return state, None
    # 即使调用者忘记先校验，也不接受自称有效的损坏记录。
    checked = validate_quote(quote, condition.symbol, baseline, max_gap_seconds)
    if quote.quality != 'valid' or checked.quality != 'valid':
        return state, None
    day = checked.source_time.astimezone(BEIJING).date().isoformat()
    if day != state.day:
        state = replace(state, day=day, count=0)
        baseline = None
    state = replace(state, baseline=checked)
    if baseline is None:
        return state, None
    gap = (checked.source_time - baseline.source_time).total_seconds()
    receive_gap = (checked.received_at - baseline.received_at).total_seconds()
    if gap <= 0 or receive_gap <= 0:
        return replace(state, baseline=None), None
    if gap > max_gap_seconds or receive_gap > max_gap_seconds:
        return state, None
    crossed = (baseline.price < condition.threshold <= checked.price if condition.direction == 'up'
               else baseline.price > condition.threshold >= checked.price)
    if (not crossed or state.count >= condition.daily_limit
            or (state.last_alert is not None and (checked.received_at - state.last_alert).total_seconds() < condition.cooldown_seconds)):
        return state, None
    return (replace(state, last_alert=checked.received_at, count=state.count + 1),
            Alert(condition, baseline.price, checked.price, checked.source_time, checked.received_at))
