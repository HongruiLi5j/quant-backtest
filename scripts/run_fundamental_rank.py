"""月度价值排名命令行(T014 S3)。

用法:
    python3 -B scripts/run_fundamental_rank.py --date 2026-10-22 --top 50 --out REPORT.md \
        --fundamental fund.csv --prices prices.csv

- 不联网、不下载:数据只从调用者用 --fundamental / --prices 指定的本地 CSV 读取(不给就拒绝运行,不会去猜 data/ 下的文件)。
- 默认拒绝覆盖已有输出文件;确需覆盖加 --force。
- A 股规则:排名日是"今天"(北京时间)且现在早于 15:30,当天 K 线是盘中半天数据,拒绝运行。
- 报告带免责声明与风险提示。排名不是买卖建议。

fundamental CSV:列 code,可选 name / industry / available_date,以及 factors.py 文档里的标准字段。
prices CSV:列 code,date,close(只用来剔除排名日无行情的股票,不参与打分)。
"""
import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import market_clock  # noqa: E402
from src.fundamental import factors as F  # noqa: E402
from src.fundamental.ranker import RankConfig, RankResult, build_ranking  # noqa: E402

BJT = timezone(timedelta(hours=8))
PROTECTED_DIRS = ("data/daily",)

DISCLAIMER = (
    "> **免责声明**:本报告是对 12 个基本面因子做行业内 Z-score 后的等权打分,仅供研究和学习,不构成任何投资建议。"
    "打分函数没有经过样本外检验、没有扣除交易成本,排名靠前不代表未来收益更高。"
)

RISKS = [
    "财报滞后:公告日距报告期 1~4 个月,价格早已反映更新的信息;价值信号是慢变量,不能用于短期买卖点。",
    "价值陷阱:低 PE / 高股息常常是市场已预期盈利下滑或股价暴跌的结果。",
    "财务造假与盈余操纵:免费结构化数据来自公司自披露,排名靠前不等于公司可靠,需人工核对审计意见与监管问询。",
    "行业口径:行业为当前分类快照;银行/保险/券商/地产的负债率、毛利率等不可比,行业样本不足时已回退全市场(见下方备注)。",
    "重述与幸存者偏差:免费数据多为最新值而非当时值,退市公司通常缺失,历史上看起来更乐观。",
    "多重检验与样本量:12 个因子等权合成未经 Holm 等校正的验证;月度截面样本有限且持有期重叠。",
    "交易可行性:靠前股票可能是低流动性小盘股、涨跌停或停牌无法成交,且 A 股 T+1。",
]


class CliError(Exception):
    pass


def _fmt(v, nd=3):
    try:
        return ("%." + str(nd) + "f") % float(v)
    except (TypeError, ValueError):
        return ""


def _md_cell(s):
    return str(s).replace("|", "/").replace("\n", " ")


def _table_md(df):
    lines = ["| 排名 | 代码 | 名称 | 行业 | 综合分 | 有效因子 | 补值 | 回退全市场 |", "|---|---|---|---|---|---|---|---|"]
    for r in df.itertuples(index=False):
        lines.append("| %d | %s | %s | %s | %s | %d | %d | %d |" % (
            r.rank, r.code, _md_cell(r.name), _md_cell(r.industry), _fmt(r.score), r.n_factors, r.n_imputed, r.n_fallback))
    if len(df) == 0:
        lines.append("| (无) | | | | | | | |")
    return "\n".join(lines)


def render_report(res: RankResult, top_n: int, generated_at: str) -> str:
    asof = res.as_of_date.strftime("%Y-%m-%d") if res.as_of_date is not None else "?"
    out = ["# 月度价值排名(基本面,独立于技术面)", "", DISCLAIMER, "",
           "- 排名日:%s    生成时间:%s" % (asof, generated_at),
           "- 输入股票数:%d    入选排名:%d    剔除:%d    配置指纹:`%s`"
           % (res.n_input, len(res.table), len(res.excluded), res.cfg_hash),
           "- 方法:12 因子(%s)各自在行业内 Z-score(±截尾),方向统一后等权平均;不含任何技术面输入。"
           % ", ".join(F.FACTOR_IDS), ""]
    out += ["## 做多候选(前 %d 名,综合分最高)" % top_n, "", _table_md(res.longs), "",
            "## 避开候选(后 %d 名,综合分最低)" % top_n, "", _table_md(res.avoids), ""]
    out += ["## 数据备注", ""]
    out += ["- " + n for n in res.notes] or ["- (无)"]
    if len(res.excluded):
        counts = res.excluded["reason"].value_counts().sort_index()
        out += ["", "剔除原因统计:" + ";".join("%s=%d" % (k, v) for k, v in counts.items())]
    out += ["", "## 风险提示", ""] + ["%d. %s" % (i, r) for i, r in enumerate(RISKS, 1)] + [""]
    return "\n".join(out)


def check_close_ready(as_of: pd.Timestamp, now: datetime):
    """A 股:北京时间 15:30 前,当天 K 线不可用。"""
    now_bj = now.astimezone(BJT) if now.tzinfo else now.replace(tzinfo=BJT)
    if as_of.date() > now_bj.date():
        raise CliError("排名日 %s 晚于今天(北京时间 %s),拒绝运行" % (as_of.date(), now_bj.date()))
    if as_of.date() == now_bj.date() and not market_clock.is_close_ready(now_bj):
        raise CliError("排名日是今天且北京时间未到 15:30,当天 K 线是盘中数据,拒绝运行")


def check_out_path(out: Path, force: bool):
    resolved = out.resolve()
    for p in PROTECTED_DIRS:
        prot = (ROOT / p).resolve()
        if resolved == prot or prot in resolved.parents:
            raise CliError("拒绝写入受保护目录 %s" % p)
    if out.is_dir():
        raise CliError("输出路径是目录:%s" % out)
    if out.exists() and not force:
        raise CliError("输出文件已存在,默认拒绝覆盖(确需覆盖请加 --force):%s" % out)


def write_atomic(out: Path, text: str, force: bool):
    """先写临时文件再 os.replace;不覆盖模式用 os.link 保证"不存在才创建"。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".rank_", suffix=".tmp", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if force:
            os.replace(tmp, out)
        else:
            try:
                os.link(tmp, out)
            except FileExistsError:
                raise CliError("输出文件已存在,默认拒绝覆盖:%s" % out)
            os.unlink(tmp)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def default_loader(args):
    """只读本地 CSV;没给路径就拒绝(不猜 data/ 路径,更不联网)。"""
    if not args.fundamental or not args.prices:
        raise CliError("未提供数据:请用 --fundamental 与 --prices 指定本地 CSV(本脚本不联网、不下载)")
    for p in (args.fundamental, args.prices):
        if not Path(p).is_file():
            raise CliError("找不到文件:%s" % p)
    fdf = pd.read_csv(args.fundamental, dtype={"code": str})
    pdf = pd.read_csv(args.prices, dtype={"code": str})
    return fdf, pdf


def build_parser():
    ap = argparse.ArgumentParser(description="月度价值排名(基本面 12 因子,行业内 Z-score)。不联网。")
    ap.add_argument("--date", required=True, help="排名日 YYYY-MM-DD")
    ap.add_argument("--top", type=int, default=50, help="前 N 名做多候选 / 后 N 名避开候选")
    ap.add_argument("--out", required=True, help="输出 Markdown 路径")
    ap.add_argument("--fundamental", help="基本面 CSV")
    ap.add_argument("--prices", help="日线行情 CSV(code,date,close)")
    ap.add_argument("--missing-mode", choices=["impute", "drop"], default="impute", help="缺数据处理")
    ap.add_argument("--min-industry-size", type=int, default=8)
    ap.add_argument("--min-factors", type=int, default=6)
    ap.add_argument("--force", action="store_true", help="允许覆盖已有输出")
    return ap


def main(argv=None, loader=None, now=None):
    """loader(args) -> (fundamental_df, price_df) 供测试/调用者注入;now 供测试注入当前时间。"""
    args = build_parser().parse_args(argv)
    try:
        try:
            as_of = pd.Timestamp(args.date)
        except (TypeError, ValueError):
            raise CliError("日期格式错误:%s" % args.date)
        if pd.isna(as_of):
            raise CliError("日期格式错误:%s" % args.date)
        if args.top < 1:
            raise CliError("--top 必须 >= 1")
        now = now or datetime.now(BJT)
        check_close_ready(as_of, now)
        out = Path(args.out)
        check_out_path(out, args.force)
        fdf, pdf = (loader or default_loader)(args)
        cfg = RankConfig(missing_mode=args.missing_mode, min_industry_size=args.min_industry_size,
                         min_factors=args.min_factors)
        res = build_ranking(fdf, pdf, as_of, top_n=args.top, config=cfg)
        text = render_report(res, args.top, now.astimezone(BJT).strftime("%Y-%m-%d %H:%M"))
        write_atomic(out, text, args.force)
    except (CliError, ValueError) as e:
        print("错误:%s" % e, file=sys.stderr)
        return 2
    print("已写入 %s(入选 %d,剔除 %d)" % (out, len(res.table), len(res.excluded)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
