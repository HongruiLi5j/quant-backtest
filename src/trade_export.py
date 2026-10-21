"""「今日买卖」完整 Excel 导出：包含买入、卖出、加仓、减仓全部信号，以及概览、数据异常和说明。

只读已发布的清单文件，不联网、不改任何数据；用 openpyxl 生成 .xlsx（内存里完成，不落盘）。
"""
from __future__ import annotations

import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ORDER = ("卖出", "减仓", "买入", "加仓")
NAVY = "1F3864"
FILL = {"买入": "FCE4E4", "加仓": "FCE4E4", "卖出": "E2F0D9", "减仓": "E2F0D9"}   # A 股习惯：红=买方向，绿=卖方向
THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _f(v):
    try:
        v = float(v)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _pct(v):
    v = _f(v)
    return None if v is None else v / 100


def _perf(r, key):
    return (r.get("performance") or {}).get(key)


# (表头, 取值函数, 数字格式, 列宽)
COLUMNS = [
    ("代码", lambda r: r["code"], "@", 9),
    ("名称", lambda r: r.get("name"), "@", 12),
    ("信号", lambda r: r.get("action"), "@", 7),
    ("今日模拟仓位", lambda r: _f(r.get("now")), "0.0%", 11),
    ("明日目标仓位", lambda r: _f(r.get("next")), "0.0%", 11),
    ("仓位变动（百分点）", lambda r: None if _f(r.get("now")) is None or _f(r.get("next")) is None else (r["next"] - r["now"]) * 100, "+0.0;-0.0;0.0", 12),
    ("前复权收盘价", lambda r: _f(r.get("close")), "0.00", 11),
    ("当天涨跌幅", lambda r: _pct(r.get("d1")), "+0.00%;-0.00%;0.00%", 10),
    ("20日平均成交额（亿元）", lambda r: None if _f(r.get("avg_turnover_20d")) is None else r["avg_turnover_20d"] / 1e8, "#,##0.00", 14),
    ("流动性", lambda r: r.get("liquidity"), "@", 10),
    ("一手参考金额（元）", lambda r: _f(r.get("lot_cost")), "#,##0", 13),
    ("小仓位买入", lambda r: "是" if r.get("small_target") else "", "@", 9),
    ("可交易性提示", lambda r: "；".join(r.get("flags") or []), "@", 28),
    ("样本外策略累计收益", lambda r: _pct(_perf(r, "total")), "0.0%", 13),
    ("样本外持有累计收益", lambda r: _pct(_perf(r, "b_total")), "0.0%", 13),
    ("样本外最大回撤", lambda r: _pct(_perf(r, "maxdd")), "0.0%", 11),
    ("样本外完整交易次数", lambda r: _perf(r, "trades"), "0", 12),
    ("样本外超额 Sharpe", lambda r: _f(_perf(r, "sharpe")), "0.00", 12),
    ("样本过少（<10 笔）", lambda r: "是" if _perf(r, "small_sample") else "", "@", 11),
    ("触发原因", lambda r: r.get("reason"), "@", 80),
]


def _sort_key(r):
    turnover = _f(r.get("avg_turnover_20d"))
    return (ORDER.index(r["action"]) if r.get("action") in ORDER else 9, -(turnover if turnover is not None else -1), r["code"])


def _header(ws, row, cols):
    for i, (title, _, _, width) in enumerate(cols, 1):
        c = ws.cell(row=row, column=i, value=title)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[row].height = 32


def _table(ws, rows, cols, title_rows):
    _header(ws, title_rows + 1, cols)
    for n, r in enumerate(rows, title_rows + 2):
        for i, (_, fn, fmt, _w) in enumerate(cols, 1):
            c = ws.cell(row=n, column=i, value=fn(r))
            c.number_format = fmt
            c.border = BORDER
            c.alignment = Alignment(vertical="top", wrap_text=(i == len(cols)))
            if cols[i - 1][0] == "信号" and r.get("action") in FILL:
                c.fill = PatternFill("solid", fgColor=FILL[r["action"]])
                c.font = Font(bold=True)
    ws.freeze_panes = ws.cell(row=title_rows + 2, column=3)
    ws.auto_filter.ref = f"A{title_rows + 1}:{get_column_letter(len(cols))}{title_rows + 1 + max(1, len(rows))}"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = f"{title_rows + 1}:{title_rows + 1}"


def _title(ws, text, sub):
    ws["A1"] = text
    ws["A1"].font = Font(bold=True, size=14, color=NAVY)
    ws["A2"] = sub
    ws["A2"].font = Font(color="595959", size=9)


def build_xlsx(pack: dict) -> bytes:
    """pack = trade_list.load() 的返回值（已发布清单中某个策略的内容 + 元数据）。"""
    day, label = pack.get("data_date", ""), pack.get("label", pack.get("strategy", ""))
    rows = sorted([r for r in pack.get("rows", []) if r.get("action") in ORDER], key=_sort_key)
    wb = Workbook()

    ws = wb.active
    ws.title = "概览"
    _title(ws, f"今日买卖 · {label}", f"数据日期 {day} · 清单生成于 {pack.get('generated_at', '')} · 导出时间 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    counts, main, small = pack.get("counts", {}), pack.get("counts_main", {}), pack.get("counts_small", {})
    for j, h in enumerate(("信号", "全部", "其中主要信号", "其中小仓位"), 1):
        c = ws.cell(row=4, column=j, value=h)
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor=NAVY); c.alignment = Alignment(horizontal="center"); c.border = BORDER
    for i, act in enumerate(ORDER, 5):
        for j, v in enumerate((act, counts.get(act, 0), main.get(act, 0), small.get(act, 0)), 1):
            c = ws.cell(row=i, column=j, value=v); c.border = BORDER
        ws.cell(row=i, column=1).fill = PatternFill("solid", fgColor=FILL[act])
    known = sum(1 for r in rows if r.get("liquidity") in ("正常", "流动性差"))
    info = [("成交额口径覆盖", f"{known} / {len(rows)} 条信号有可用的 20 日成交额；其余为“流动性未知”。"),
            ("数据异常", f"{len(pack.get('exceptions', []))} 只（见“数据异常”页），未参与上面的计数。"),
            ("价格缺失", str(pack.get("price_unavailable", 0))),
            ("重要声明", pack.get("disclaimer", "")),
            ("样本外口径", pack.get("performance_note", ""))]
    for k, (a, b) in enumerate(info, 10):
        ws.cell(row=k, column=1, value=a).font = Font(bold=True)
        ws.cell(row=k, column=2, value=b).alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=k, start_column=2, end_row=k, end_column=8)
        ws.row_dimensions[k].height = 95 if a == "样本外口径" else 30
    for col, w in zip("ABCDEFGH", (16, 14, 14, 12, 12, 12, 12, 12)):
        ws.column_dimensions[col].width = w

    ws = wb.create_sheet("全部信号")
    _title(ws, f"全部信号（{len(rows)} 条）", "排序：卖出、减仓、买入、加仓；同类按 20 日平均成交额从大到小。空白表示数据不足，不是零。")
    _table(ws, rows, COLUMNS, 2)
    for act in ORDER:
        sub = [r for r in rows if r["action"] == act]
        ws = wb.create_sheet(act)
        _title(ws, f"{act}（{len(sub)} 条）", "列与“全部信号”相同，只含该类信号。")
        _table(ws, sub, COLUMNS, 2)

    ws = wb.create_sheet("数据异常")
    ex = sorted(pack.get("exceptions", []), key=lambda r: r["code"])
    ecols = [COLUMNS[0], COLUMNS[1], ("数据日期", lambda r: r.get("last_date"), "@", 12), COLUMNS[3], COLUMNS[4], ("原因 / 提示", lambda r: "；".join(r.get("flags") or []) or r.get("reason"), "@", 70)]
    _title(ws, f"数据异常（{len(ex)} 只）", "这些股票因停牌、数据日期不符或价格缺失，没有给出买卖信号，需要自行核对。")
    _table(ws, ex, ecols, 2)

    ws = wb.create_sheet("说明与局限")
    _title(ws, "说明与局限", "请先读这一页再使用其他页的数据。")
    notes = ["这是按回测策略机械生成的信号清单，不是投资建议；本导出不含任何账户或持仓信息。"] + list(pack.get("limitations", [])) + [
        "“今日模拟仓位”是策略引擎模拟的仓位，不是你的实际持仓；“明日目标仓位”用于下一交易日开盘。",
        "仓位变动超过 10 个百分点才标为加仓/减仓（与每日扫描和回测引擎一致）。",
        "“小仓位买入”指明日目标低于 5% 的买入，网页默认折叠，这里完整保留。",
        "一手参考金额 = 前复权收盘价 × 100，不含手续费；次日实际开盘价可能不同。A 股 T+1，今天买入当天不能卖出。",
        "20 日平均成交额来自独立的成交额旁表；不足 20 个交易日或缺失时标“流动性未知”，不把缺失当作零。",
        "样本外 = 2020-01-01 起（250 个交易日预热后）的时间切分，并非完全干净的样本外；收益已计入个股单边 8bp 成本和涨跌停模型。",
        "红色底纹 = 买入方向（买入、加仓），绿色底纹 = 卖出方向（卖出、减仓），沿用 A 股红涨绿跌的习惯。",
    ]
    for i, n in enumerate(notes, 3):
        ws.cell(row=i, column=1, value=f"{i - 2}.").alignment = Alignment(vertical="top")
        c = ws.cell(row=i, column=2, value=n)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[i].height = 32 if len(n) > 60 else 18
    ws.column_dimensions["A"].width = 5
    ws.column_dimensions["B"].width = 120

    for sheet in wb.worksheets:
        sheet.sheet_view.showGridLines = False
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
