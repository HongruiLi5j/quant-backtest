"""不依赖 HTTP/app 的本机处理函数；认证上下文必须由 S3 可信接线提供。"""
import json
import math

from .core import Condition, positive
from .service import symbol_for
from .signals_readonly import read_signals
from .store import StorageError

MAX_BODY_BYTES = 8192
SCHEMA = {'status': (set(), set()), 'start': (set(), set()), 'stop': (set(), set()),
          'watch_add': ({'code'}, {'code'}), 'watch_remove': ({'code'}, {'code'}),
          'rule_set': ({'code', 'direction', 'threshold'},
                       {'code', 'direction', 'threshold', 'cooldown_seconds', 'daily_limit'}),
          'rule_delete': ({'code', 'direction'}, {'code', 'direction'}),
          'alerts': (set(), set()), 'trend': ({'code'}, {'code'}), 'export_quality': (set(), set())}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def handle(service, action, body=b'{}', *, is_local_owner=False):
    """无调用方所有者布尔上下文即拒绝，错误从不回显用户输入。"""
    def error(code):
        return {'ok': False, 'error': code}
    if is_local_owner is not True:
        return error('OWNER_REQUIRED')
    if not isinstance(action, str) or action not in SCHEMA:
        return error('UNKNOWN_ACTION')
    try:
        if isinstance(body, dict):
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        elif isinstance(body, str):
            encoded = body.encode('utf-8')
        elif isinstance(body, bytes):
            encoded = body
        else:
            return error('INVALID_INPUT')
        if len(encoded) > MAX_BODY_BYTES:
            return error('BODY_TOO_LARGE')
        data = json.loads(encoded, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        required, allowed = SCHEMA[action]
        if not isinstance(data, dict) or not required <= data.keys() <= allowed:
            return error('INVALID_INPUT')
        if 'code' in data:
            symbol = symbol_for(data['code'])
        if 'direction' in data and data['direction'] not in ('up', 'down'):
            return error('INVALID_INPUT')
        condition = None
        if action == 'rule_set':
            cooldown = data.get('cooldown_seconds', 300)
            limit = data.get('daily_limit', 3)
            if (not positive(data['threshold']) or isinstance(cooldown, bool)
                    or not isinstance(cooldown, (int, float)) or not math.isfinite(cooldown)
                    or cooldown < 0 or type(limit) is not int or not 1 <= limit <= 1000):
                return error('INVALID_INPUT')
            condition = Condition(symbol, data['direction'], data['threshold'], cooldown, limit)
        if action == 'status':
            result = service.status()
            result['closing_signals'] = read_signals(result['watch'], now=service.now())
        elif action == 'start':
            service.start()
            result = service.status()
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
        return {'ok': True, 'data': result}
    except StorageError:
        return error('STORAGE_ERROR')
    except (ValueError, TypeError, OverflowError, UnicodeError, RecursionError):
        return error('INVALID_INPUT')
