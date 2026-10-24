"""只读已发布的 v4 今日买卖 JSON；不导入生成器，不重算、不锁定。"""
from datetime import date, datetime, time
import json
import os
from pathlib import Path
import stat

from .core import BEIJING, valid_symbol

SIGNAL_FILE = Path(__file__).resolve().parents[2] / 'data' / 'daily' / 'trade_list_cn.json'
ACTIONS = ('买入', '卖出', '加仓', '减仓')


def read_signals(codes=(), *, now=None):
    """只返回明确关注的代码。格式不符整份返回无信号，不猜字段或默认策略。"""
    empty = {'status': '无信号', 'signals': []}
    if not codes:
        return empty
    try:
        # 全路径逐层拒绝符号链接；仅 O_RDONLY，无 mkdir/写入/锁定。
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in SIGNAL_FILE.absolute().parts[1:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            source = os.open(SIGNAL_FILE.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        finally:
            os.close(fd)
        with os.fdopen(source, 'r', encoding='utf-8') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return empty
            payload = json.load(stream)
        if payload['version'] != 4 or payload['market'] != 'cn':
            return empty
        day = date.fromisoformat(payload['data_date'])
        generated = datetime.fromisoformat(payload['generated_at'])
        current = now or datetime.now(BEIJING)
        if (generated.tzinfo is None or current.tzinfo is None
                or generated.astimezone(BEIJING).date() < day
                or (generated.astimezone(BEIJING).date() == day and generated.astimezone(BEIJING).time() < time(15, 30))
                or generated > current or day > current.astimezone(BEIJING).date()):
            return empty
        rows = []
        for strategy, pack in payload['strategies'].items():
            if not isinstance(strategy, str) or not isinstance(pack['rows'], list):
                return empty
            for row in pack['rows']:
                code = row['code']
                symbol = ('sh' if isinstance(code, str) and code.startswith('6') else 'sz') + str(code)
                if not valid_symbol(symbol) or row['action'] not in ACTIONS or row['last_date'] != day.isoformat():
                    return empty
                if code in codes:
                    rows.append({'code': code, 'direction': row['action'], 'signal_date': day.isoformat(),
                                 'strategy': strategy})
        return {'status': '有信号' if rows else '无信号', 'signals': rows}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return empty
