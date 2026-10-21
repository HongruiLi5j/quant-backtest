"""当前本机会话的相关股票；只在私有接口里拼接，不写入公共清单或存档。"""
from __future__ import annotations

from . import datasource, members, trade_list


def mine(strategy="blend2", day=None, user=None, *, cache=None, watchlist=None, positions=None):
    public = trade_list.load(day=day, strategy=strategy, cache=cache)
    watchlist = datasource.load_watchlist("cn") if watchlist is None else dict(watchlist)
    watchlist.setdefault("600531", "豫光金铅")
    positions = set(members.position_codes(user["id"], "cn") if user and positions is None else (positions or []))
    current = {row["code"]: row for row in public.get("rows", []) + public.get("exceptions", [])}
    rows = []
    for code in set(watchlist) | positions:
        row = dict(current.get(code, {"code": code, "name": watchlist.get(code, code), "action": None,
                                      "now": None, "next": None, "close": None, "performance": {}, "flags": [],
                                      "reason": "所选日期和策略的已发布清单没有该股信号"}))
        row.update(in_watchlist=code in watchlist, in_member_positions=code in positions)
        rows.append(row)
    # 固定股票第一；其余股票的卖出信号置顶。
    rows.sort(key=lambda row: (row["code"] != "600531", row["action"] != "卖出", row["code"]))
    return {"strategy": strategy, "data_date": public.get("data_date"), "member_logged_in": user is not None, "rows": rows}
