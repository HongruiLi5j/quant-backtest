"""盯盘 HTTP 处理函数（纯函数）：鉴权上下文由 app.py 在每个请求里用服务端事实构造。

任何一项不满足（非可信本机 / 无 dashboard 头 / 未认证 / 写操作缺 Origin 校验）一律同一个 403，
响应体固定，不区分原因，不泄露这些路径是否存在。错误只返回固定码，不回显用户输入。
"""
from dataclasses import dataclass
import json

from .scheduler import validate_settings
from .watchlist import DEFAULT_MAX_WATCH, validate_max_watch

MAX_BODY_BYTES = 2048
FORBIDDEN = (403, {'error': 'FORBIDDEN'})
INVALID = (400, {'error': 'INVALID_INPUT'})
CONFIG_KEYS = {'interval', 'top_n', 'provider', 'max_watch', 'batch_mode'}
RUNTIME_KEYS = {'provider', 'max_watch', 'batch_mode'}
PROVIDERS = {'tencent': False, 'easy': True}   # provider 名称 -> 对应的 batch_mode（腾讯单只 / easyquotation 批量）


@dataclass
class Ctx:
    trusted_local: bool = False      # Host 合法、无代理/隧道头、非分享链接
    authed: bool = False             # 网站令牌 cookie 有效
    dashboard_header: bool = False   # X-Requested-With: dashboard
    origin_ok: bool = False          # 写操作的 Origin 检查结果（默认拒绝）


def _gate(ctx, write):
    """只认严格 True，避免真值（如 1、'yes'）被当成通过。"""
    ok = ctx.trusted_local is True and ctx.authed is True and ctx.dashboard_header is True
    if write:
        ok = ok and ctx.origin_ok is True
    return None if ok else FORBIDDEN


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


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
    # S3b：实时分析随报价一起返回（app.py 暂未登记独立的 intraday 路由，页面走这一条，少一次请求）
    if hasattr(scheduler, 'intraday_events') and hasattr(scheduler, 'intraday_tracking'):
        body['intraday'] = {'events': scheduler.intraday_events(), 'tracking': scheduler.intraday_tracking()}
    return 200, body


def handle_intraday_events(ctx, scheduler):
    """只读：最新的实时事件列表（执行提醒 + 新机会）。"""
    return _gate(ctx, False) or (200, {'ok': True, 'events': scheduler.intraday_events()})


def handle_intraday_tracking(ctx, scheduler):
    """只读：今日信号追踪快照（逐条追踪 + 实时正确率汇总）。"""
    return _gate(ctx, False) or (200, {'ok': True, 'tracking': scheduler.intraday_tracking()})


def handle_alerts(ctx, scheduler):
    return _gate(ctx, False) or (200, {'ok': True, 'alerts': scheduler.alerts()})


def _view(scheduler):
    """状态副本 + 当前 provider / max_watch / batch_mode，便于前端显示（不改调度器自己的 status）。"""
    status = dict(scheduler.status())
    status.setdefault('max_watch', DEFAULT_MAX_WATCH)
    status['batch_mode'] = status.get('batch_mode') is True
    status['provider'] = 'easy' if status['batch_mode'] else 'tencent'
    return status


def handle_config(ctx, scheduler, body=b''):
    """可改 interval（秒）、top_n，以及 provider（tencent/easy）、max_watch（1-5500）、batch_mode（bool）。

    速率、预算不能经接口修改；未知键、类型不对、越界一律同一个 INVALID，先全部校验再落地。
    provider 与 batch_mode 是同一件事的两种写法（tencent=单只，easy=批量），同时给出且矛盾则拒绝。
    调度器没有 set_runtime 时，带运行参数的请求一律拒绝，不假装生效。
    """
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
        if not isinstance(data, dict) or not data or not data.keys() <= CONFIG_KEYS:
            return INVALID
        current = scheduler.status()
        # 生效值只用于校验；只有请求里真的带了对应字段才传给 set_runtime
        # （降级时 status()['batch_mode'] 是实际值 False，原样回写会悄悄关掉用户的批量模式）
        max_watch = current.get('max_watch', DEFAULT_MAX_WATCH)
        batch_mode = current.get('batch_mode', False)
        runtime_args = {}
        if 'max_watch' in data:
            validate_max_watch(data['max_watch'])
            max_watch = runtime_args['max_watch'] = data['max_watch']
        if 'provider' in data:
            if type(data['provider']) is not str or data['provider'] not in PROVIDERS:
                raise ValueError()
            batch_mode = runtime_args['batch_mode'] = PROVIDERS[data['provider']]
        if 'batch_mode' in data:
            if type(data['batch_mode']) is not bool:
                raise ValueError()
            if 'provider' in data and PROVIDERS[data['provider']] != data['batch_mode']:
                raise ValueError()
            batch_mode = runtime_args['batch_mode'] = data['batch_mode']
        runtime = bool(data.keys() & RUNTIME_KEYS)
        if runtime and not hasattr(scheduler, 'set_runtime'):
            return INVALID
        # top_n 上界 = 生效后的 max_watch；间隔下限随生效后的 batch_mode
        interval, top_n = validate_settings(data.get('interval', current['interval']),
                                            data.get('top_n', current['top_n']),
                                            max_watch, batch_mode)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return INVALID
    if runtime:
        try:
            scheduler.set_runtime(**runtime_args)
        except (ValueError, TypeError):
            return INVALID
    scheduler.set_config(interval, top_n)
    view = _view(scheduler)
    return 200, {'ok': True, 'status': view, 'provider': view['provider'],
                 'max_watch': view['max_watch'], 'batch_mode': view['batch_mode']}
