"""纸面实盘规则与周报(T012)。导入本包不建任何文件、不联网、不读 data/。

S1 只提供 rules.py 里的三条硬规则纯函数(停止标准、仓位约束、单日亏损停手)。
"""
from .rules import (DAILY_LOSS, SINGLE_MAX, STOP_MONTHS, STOP_UNDERPERF, TOTAL_MAX,
                    daily_loss_halt, position_limits, stop_rule)

__all__ = ["STOP_MONTHS", "STOP_UNDERPERF", "SINGLE_MAX", "TOTAL_MAX", "DAILY_LOSS",
           "stop_rule", "position_limits", "daily_loss_halt"]
