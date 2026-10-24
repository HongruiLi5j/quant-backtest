"""数据质量工具包(T010):离线、纯函数,不联网,不读写 data/ 下的真实文件。

目前只有 S1 的时点状态表;一致性检查与日报在后续阶段加入。
"""
from .status_table import (  # noqa: F401
    StatusRow, StockInfo, build_range, build_status, infos_from_frame,
    parse_date, summarize, write_table)

__all__ = ["StatusRow", "StockInfo", "build_range", "build_status",
           "infos_from_frame", "parse_date", "summarize", "write_table"]
