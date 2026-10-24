"""美股行情记录、质量检查和价格穿越纯函数；镜像 src/market_monitor/core.py。

与 A 股版的差异只有：时区（美东，自动处理夏令时）、代码格式、
时间戳是带时区的 datetime（不是 14 位字符串）、成交量单位为股。
不内置交易日历，不推断节假日/停牌；休市日靠"行情时间过旧"拦截。
"""
from dataclasses import dataclass, replace
from datetime import datetime, time
import math
import re
from zoneinfo import ZoneInfo

US_EASTERN = ZoneInfo('America/New_York')
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)


def valid_symbol(symbol):
    """美股普通代码：1-5 个大写 ASCII 字母，可带 -X 后缀（如 BRK-B）。
    Yahoo 用连字符，点号写法（BF.B）查不到数据，直接拒绝。

    指数（^GSPC）、期货（ES=F）、小写、带空白、全角字符一律拒绝。
    """
    return isinstance(symbol, str) and re.fullmatch(r'[A-Z]{1,5}(?:-[A-Z]{1,2})?', symbol) is not None


def us_continuous_session(at):
    """美东周一至周五 09:30–16:00 的钟表提示，含两端点；不含盘前盘后。

    用 zoneinfo 转换，夏令时由系统时区库处理，不手写 UTC 偏移。
    不是交易日历：节假日仍会返回 True，由行情过旧检查拦截。
    """
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('需要带时区时间')
    local = at.astimezone(US_EASTERN)
    return local.weekday() < 5 and SESSION_OPEN <= local.time() <= SESSION_CLOSE


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float | None
    previous_close: float | None
    opening: float | None
    high: float | None
    low: float | None
    volume: int | None  # 单位：股（不乘 100）
    timestamp: datetime | None  # 行情自带时间，必须带时区
    received_at: datetime
    elapsed_seconds: float = 0.0
    quality: str = 'unchecked'
    reasons: tuple[str, ...] = ()
    session: str = 'unknown'
    provider: str = 'yfinance_unofficial'


def positive(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


def valid_volume(value):
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def validate_quote(quote, expected_symbol, previous=None, stale_seconds=1200):
    """阈值为保守占位值，非供应商承诺；previous 应保留上次观测用于查重。

    stale_seconds 默认 1200（20 分钟）：Yahoo 免费行情可能延迟，具体多久未核实。
    """
    if not positive(stale_seconds):
        raise ValueError('过旧阈值必须为有限正数')
    reasons = []
    if not valid_symbol(expected_symbol) or quote.symbol != expected_symbol:
        reasons.append('code_mismatch')
    if not all(positive(v) for v in (quote.price, quote.previous_close, quote.opening)):
        reasons.append('invalid_price')
    # 最高/最低可缺失；有值则必须为正且 high >= low
    extremes = (quote.high, quote.low)
    if any(v is not None for v in extremes):
        if not all(positive(v) for v in extremes) or quote.high < quote.low:
            reasons.append('invalid_range')
    if not math.isfinite(quote.elapsed_seconds) or quote.elapsed_seconds < 0:
        reasons.append('invalid_elapsed')
    received = quote.received_at
    received_ok = aware(received)
    session = ('regular_session_hint' if us_continuous_session(received) else 'off_session') if received_ok else 'unknown'
    if not received_ok:
        reasons.append('invalid_received_time')
    source = quote.timestamp if aware(quote.timestamp) else None
    if source is None:
        reasons.append('invalid_source_time')
    if source is not None and received_ok:
        age = (received - source).total_seconds()
        if age < -5:
            reasons.append('future_time')
        if us_continuous_session(received) and age > stale_seconds:
            reasons.append('stale')
    if source is not None:
        # 行情自带时间落在常规时段之外（盘前/盘后）：不触发提醒
        clock = source.astimezone(US_EASTERN).time()
        if clock > SESSION_CLOSE:
            reasons.append('after_hours')
        elif clock < SESSION_OPEN:
            reasons.append('pre_market')
    if not valid_volume(quote.volume):
        reasons.append('invalid_volume')
    if (previous is not None and previous.symbol == quote.symbol and source is not None
            and aware(previous.timestamp)):
        if source < previous.timestamp:
            reasons.append('timestamp_regression')
        elif source == previous.timestamp:
            reasons.append('duplicate_timestamp')
        if (source.astimezone(US_EASTERN).date() == previous.timestamp.astimezone(US_EASTERN).date()
                and valid_volume(quote.volume) and valid_volume(previous.volume)
                and quote.volume < previous.volume):
            reasons.append('volume_regression')
    if not reasons:
        quality = 'valid' if session == 'regular_session_hint' else 'off_session'
    elif reasons == ['stale']:
        quality = 'stale'
    elif set(reasons) <= {'after_hours', 'pre_market'}:
        quality = 'after_hours'
    else:
        quality = 'invalid'
    return replace(quote, quality=quality, reasons=tuple(reasons), session=session)


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


def alert_step(state, condition, quote, *, connected=True, max_gap_seconds=1200):
    """纯函数，返回新状态和可选提醒。None/断线/异常/换配置均重建基线。

    每个条件单独持有状态；冷却及次数不因断线或配置变化清零。
    去重来自严格递增时间和重新穿越；冷却内的穿越不会延迟补发。
    max_gap_seconds 同时用于观测缺口和记录过旧检查；调大它也会放宽过旧阈值。
    默认 1200 与 validate_quote 一致（A 股版为 120；美股免费行情有延迟，且每只更新间隔更长）。
    每日计数按美东日期归零。
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
    day = checked.timestamp.astimezone(US_EASTERN).date().isoformat()
    if day != state.day:
        state = replace(state, day=day, count=0)
        baseline = None
    state = replace(state, baseline=checked)
    if baseline is None:
        return state, None
    gap = (checked.timestamp - baseline.timestamp).total_seconds()
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
            Alert(condition, baseline.price, checked.price, checked.timestamp, checked.received_at))
