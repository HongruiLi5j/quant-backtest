"""美股信号只读适配器（占位）：目前美股没有任何策略，也没有已发布的信号文件。

故意不读任何文件：不碰 data/daily/trade_list_cn.json（那是 A 股的，读了会把 A 股信号混进美股页面）。
接口与 A 股版 read_signals 保持一致，将来美股信号发布后只需改这一个函数。
"""


def read_signals(codes=(), *, now=None):
    """始终返回"无信号"；不做任何 I/O，不导入生成器，不锁定。"""
    return {'status': '无信号', 'signals': []}
