"""美股盯盘 HTTP 处理函数（纯函数）：鉴权上下文由 app.py 在每个请求里用服务端事实构造。

鉴权沿用 A 股盯盘已验收的实现（Ctx / _gate / FORBIDDEN / INVALID，单一门禁实现，不复制）：
任何一项不满足（非可信本机 / 无 dashboard 头 / 未认证 / 写操作缺 Origin 校验）一律同一个 403，
响应体固定，不区分原因，不泄露这些路径是否存在。错误只返回固定码，不回显用户输入。
只有 config 的校验是美股自己的（间隔 60–3600、名单上限 30）。
"""
import json

from ..market_monitor.wiring import Ctx, FORBIDDEN, INVALID, _gate, _pairs  # noqa: F401  Ctx 等供 app.py 使用
from .scheduler import validate_settings
from .watchlist import DEFAULT_MAX_WATCH

MAX_BODY_BYTES = 2048


def handle_status(ctx, scheduler):
    return _gate(ctx, False) or (200, {'ok': True, 'status': scheduler.status()})


def handle_start(ctx, scheduler):
    denied = _gate(ctx, True)
    if denied:
        return denied
    started = scheduler.start()
    return 200, {'ok': True, 'started': started, 'status': scheduler.status()}


def handle_stop(ctx, scheduler):
    denied = _gate(ctx, True)
    if denied:
        return denied
    scheduler.stop()
    return 200, {'ok': True, 'status': scheduler.status()}


def handle_quotes(ctx, scheduler):
    denied = _gate(ctx, False)
    if denied:
        return denied
    body = {'ok': True, 'quotes': scheduler.quotes(), 'status': scheduler.status()}
    # 实时分析随报价一起返回，页面少一次请求
    if hasattr(scheduler, 'intraday_events') and hasattr(scheduler, 'intraday_tracking'):
        body['intraday'] = {'events': scheduler.intraday_events(), 'tracking': scheduler.intraday_tracking()}
    return 200, body


def handle_intraday_events(ctx, scheduler):
    """只读：最新的实时事件列表（执行提醒 + 新机会）。"""
    return _gate(ctx, False) or (200, {'ok': True, 'events': scheduler.intraday_events()})


def handle_intraday_tracking(ctx, scheduler):
    """只读：今日信号追踪快照。"""
    return _gate(ctx, False) or (200, {'ok': True, 'tracking': scheduler.intraday_tracking()})


def handle_alerts(ctx, scheduler):
    return _gate(ctx, False) or (200, {'ok': True, 'alerts': scheduler.alerts()})


def handle_config(ctx, scheduler, body=b''):
    """只允许 interval（秒）和 top_n；速率、预算、名单上限都不能经接口修改。"""
    denied = _gate(ctx, True)
    if denied:
        return denied
    try:
        if isinstance(body, str):
            body = body.encode('utf-8')
        if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES:
            return INVALID
        data = json.loads(body, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(data, dict) or not data or not data.keys() <= {'interval', 'top_n'}:
            return INVALID
        current = scheduler.status()
        interval, top_n = validate_settings(data.get('interval', current['interval']),
                                            data.get('top_n', current['top_n']),
                                            current.get('max_watch', DEFAULT_MAX_WATCH))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return INVALID
    scheduler.set_config(interval, top_n)
    return 200, {'ok': True, 'status': scheduler.status()}
