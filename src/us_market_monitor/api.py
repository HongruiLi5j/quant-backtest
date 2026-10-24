"""美股盯盘 ctx 处理函数（纯函数，不依赖 HTTP/app）。

鉴权上下文 Ctx 由 app.py 在每个请求里用服务端事实构造；这里复用 A 股的 Ctx 和 _gate：
可信本机 + 已认证 + dashboard 头，写操作再加 Origin 校验，任何一项不满足统一返回同一个 403，
不区分原因、不泄露路由是否存在。错误只返回固定码，不回显用户输入。
"""
import json
import math

from ..market_monitor.wiring import Ctx, FORBIDDEN, INVALID, _gate, _pairs
from .core import Condition, positive
from .service import symbol_for
from .signals_readonly import read_signals
from .store import StorageError

MAX_BODY_BYTES = 8192
WRITE_ACTIONS = frozenset({'start', 'stop', 'watch_add', 'watch_remove', 'rule_set', 'rule_delete'})
SCHEMA = {'status': (set(), set()), 'start': (set(), set()), 'stop': (set(), set()),
          'watch_add': ({'code'}, {'code'}), 'watch_remove': ({'code'}, {'code'}),
          'rule_set': ({'code', 'direction', 'threshold'},
                       {'code', 'direction', 'threshold', 'cooldown_seconds', 'daily_limit'}),
          'rule_delete': ({'code', 'direction'}, {'code', 'direction'}),
          'alerts': (set(), set()), 'trend': ({'code'}, {'code'}), 'export_quality': (set(), set())}
UNKNOWN = (404, {'error': 'UNKNOWN_ACTION'})
TOO_LARGE = (413, {'error': 'BODY_TOO_LARGE'})
STORAGE = (500, {'error': 'STORAGE_ERROR'})


def handle(ctx, service, action, body=b'{}'):
    """返回 (HTTP 状态码, 字典)。先鉴权再看动作，未授权者无法探测动作是否存在。"""
    if not isinstance(ctx, Ctx):
        return FORBIDDEN
    # 未知动作按只读门禁处理；已知写动作要求 Origin 校验
    denied = _gate(ctx, isinstance(action, str) and action in WRITE_ACTIONS)
    if denied:
        return denied
    if not isinstance(action, str) or action not in SCHEMA:
        return UNKNOWN
    try:
        if isinstance(body, dict):
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        elif isinstance(body, str):
            encoded = body.encode('utf-8')
        elif isinstance(body, bytes):
            encoded = body
        else:
            return INVALID
        if len(encoded) > MAX_BODY_BYTES:
            return TOO_LARGE
        data = json.loads(encoded, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        required, allowed = SCHEMA[action]
        if not isinstance(data, dict) or not required <= data.keys() <= allowed:
            return INVALID
        if 'code' in data:
            symbol_for(data['code'])
        if 'direction' in data and data['direction'] not in ('up', 'down'):
            return INVALID
        condition = None
        if action == 'rule_set':
            cooldown = data.get('cooldown_seconds', 300)
            limit = data.get('daily_limit', 3)
            if (not positive(data['threshold']) or isinstance(cooldown, bool)
                    or not isinstance(cooldown, (int, float)) or not math.isfinite(cooldown)
                    or cooldown < 0 or type(limit) is not int or not 1 <= limit <= 1000):
                return INVALID
            condition = Condition(data['code'], data['direction'], data['threshold'], cooldown, limit)
        if action == 'status':
            result = service.status()
            result['closing_signals'] = read_signals(result['watch'], now=service.now())
        elif action == 'start':
            started = service.start()
            result = service.status()
            result['started'] = started
        elif action == 'stop':
            service.stop()
            result = service.status()
        elif action in ('watch_add', 'watch_remove', 'rule_set', 'rule_delete'):
            service.configure(action, data['code'], condition if action == 'rule_set' else data.get('direction'))
            result = service.status()
        elif action == 'alerts':
            result = service.alerts()
        elif action == 'trend':
            result = service.trend(data['code'])
        else:
            result = service.export_quality()
        return 200, {'ok': True, 'data': result}
    except StorageError:
        return STORAGE
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        return INVALID
