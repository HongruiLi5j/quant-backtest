"""A 股「北京时间 15:30 前不用当天 K 线」这条规则的唯一实现（E06）。

背景：交易所 15:00 收盘，但新浪/腾讯/东方财富的日线要到 15:30 前后才稳定；15:30 以前接口返回的「今天」
那一根是盘中半天数据，用它算信号等于用了未来信息（违反 CLAUDE.md 的「严禁未来函数」）。
这条规则原来在十几个文件里各写一遍（`time(15, 30)`、`15 * 60 + 30`、`(15, 30)` 元组），改规则容易漏。
现在所有 A 股调用点都从这里取，改 CLOSE_READY 一处就全跟着变。

约定：
- 时区固定 Asia/Shanghai（中国没有夏令时，所以「夏令时」不影响结果；用 ZoneInfo 是为了把别的时区的时间正确换算过来）。
- 不带时区的 datetime 一律当成北京时间的墙上时间（和原来 allmarket.last_trading_day 以及基本面脚本的做法一致）。
- 本模块只管「一天里几点算收盘数据就绪」；周末/节假日由调用方传入的交易日历决定，不带日历时只做日期减一。
- 只适用 A 股。美股（config.MARKETS["us"]["close_ready"] = 16:30）等其它市场的收盘线不在这里。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
CLOSE_READY = time(15, 30)      # 北京时间 15:30 起，当天的日线才算收完、可以用


def to_beijing(now: datetime | None = None) -> datetime:
    """换成带北京时区的时间。None 取当前时刻；不带时区的当作北京时间墙上时间；带时区的换算过来。"""
    if now is None:
        return datetime.now(BEIJING)
    if now.tzinfo is None or now.utcoffset() is None:
        return now.replace(tzinfo=BEIJING)
    return now.astimezone(BEIJING)


def is_close_ready(now: datetime | None = None) -> bool:
    """北京时间是否已到 15:30（含 15:30:00 整）。False 表示「今天」这根日线还是盘中半天数据。"""
    return to_beijing(now).time() >= CLOSE_READY


def last_usable_bar_date(now: datetime | None = None, calendar=None) -> date:
    """最后一根可用日线的日期：北京时间 15:30 起算今天，否则算昨天。

    calendar 给了就再向前取不晚于该日期的最近一个交易日（周末/节假日自动跳过；日历里没有这样的日期会抛 ValueError，
    和原来对空序列取 max 的行为一致）。不给就只做「今天/昨天」的判断，不看周末。
    """
    local = to_beijing(now)
    cutoff = local.date() if is_close_ready(local) else local.date() - timedelta(days=1)
    if calendar is None:
        return cutoff
    return max(d for d in calendar if d <= cutoff)


def bar_usable(day, now: datetime | None = None) -> bool:
    """日期为 day 的那根日线现在能不能用（day 不晚于最后一根可用日线）。day 可以是 date、datetime，或 YYYY-MM-DD 字符串。"""
    last = last_usable_bar_date(now)
    if isinstance(day, str):
        return day <= last.isoformat()      # 和原来对 ISO 字符串直接比大小的写法一致
    if isinstance(day, datetime):
        day = day.date()
    return day <= last


def close_ready_at(day: date) -> datetime:
    """day 这一天的「收盘数据就绪」时刻（北京时间 15:30，带时区）。"""
    return datetime.combine(day, CLOSE_READY, BEIJING)


def clock_state(now: datetime | None = None) -> tuple[str, bool]:
    """(北京日期字符串, 是否已过 15:30)。缓存键用：这两个量变了，行情读取结果就可能变，必须重算。"""
    local = to_beijing(now)
    return (str(local.date()), is_close_ready(local))
