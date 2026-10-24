"""月度价值排名 Markdown 报告(T014 S4)。

build_monthly_report(ranking, universe_info, prev_ranking=None) 把 ranker.RankResult 渲染成 Markdown:
 1. 前 N / 后 N 名表;
 2. 与上月对比(进入/退出前 N、名次变化最大者);
 3. 各因子贡献拆解:综合分 = Σ(w_f × z_f)/Σw_f,所以因子 f 对某只股票的贡献 = w_f × z_f / Σw_f。
    这里给"前 N 组 / 后 N 组 的平均贡献"和"前 10 名各自的前 3 大贡献因子";
 4. 前 N 名所属行业分布(与全部入选股票的行业占比对比);
 5. 免责声明与局限。

只做渲染:不联网、不读文件、不修改输入。write_report 默认拒绝覆盖已有文件。
注意:table 里的 z_* 列在 impute 模式下是"补值之后"的 z,所以贡献拆解里补值的部分是行业中位数,不是真实数据。
"""
import os
import tempfile

import numpy as np
import pandas as pd

from . import factors as F

DISCLAIMER = (
    "> **免责声明**:本报告是基本面因子行业内 Z-score 打分的机械输出,仅供研究和学习,不构成任何投资建议。"
    "打分没有经过样本外检验、没有扣除交易成本,排名靠前不代表未来收益更高。"
)

LIMITS = [
    "财报滞后 1~3 个月:公告日距报告期很远,价格往往已经反映了更新的信息。",
    "业绩变脸不可预测:排名只反映已公告数据,无法预见盈利突然恶化、造假或会计差错更正。",
    "价值陷阱:低估值常常是市场已预期基本面恶化的结果。",
    "重述与幸存者偏差:免费数据多为最新值而非当时值,退市公司通常缺失。",
    "多重检验与样本量:12 个因子未经 Holm 等校正的验证,月度截面样本有限。",
    "本排名与短中期技术面系统相互独立,不混打分;两者并列看只是人工参考。",
]


def _cell(s):
    return str(s).replace("|", "/").replace("\n", " ")


def _f(v, nd=3):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ""
    return "" if not np.isfinite(x) else ("%." + str(nd) + "f") % x


def _table(df):
    lines = ["| 排名 | 代码 | 名称 | 行业 | 综合分 |", "|---|---|---|---|---|"]
    for r in df.itertuples(index=False):
        lines.append("| %d | %s | %s | %s | %s |" % (r.rank, r.code, _cell(r.name), _cell(r.industry), _f(r.score)))
    if len(df) == 0:
        lines.append("| (无) | | | | |")
    return lines


def _prev_table(prev):
    """prev 可为 RankResult 或带 code/rank 列的 DataFrame;无法识别返回 None。"""
    t = getattr(prev, "table", prev)
    if not isinstance(t, pd.DataFrame) or len(t) == 0 or not {"code", "rank"} <= set(t.columns):
        return None
    return t[["code", "rank"]].copy()


def _changes(table, prev, n):
    out = ["## 与上月对比", ""]
    p = _prev_table(prev) if prev is not None else None
    if p is None:
        return out + ["- 无上月排名(首次运行或上月为空),不做对比。", ""]
    prank = dict(zip(p["code"].astype(str), p["rank"].astype(int)))
    cur = dict(zip(table["code"].astype(str), table["rank"].astype(int)))
    now_top = {c for c, r in cur.items() if r <= n}
    old_top = {c for c, r in prank.items() if r <= n}
    names = dict(zip(table["code"].astype(str), table["name"].astype(str)))
    entered = sorted(now_top - old_top, key=cur.get)
    exited = sorted(old_top - now_top, key=prank.get)
    out.append("- 前 %d 名:保留 %d 只,新进入 %d 只,退出 %d 只。" % (n, len(now_top & old_top), len(entered), len(exited)))
    if entered:
        out.append("- 新进入:" + ";".join(
            "%s %s(上月%s→本月%d)" % (c, _cell(names.get(c, "")), prank.get(c, "未入选"), cur[c]) for c in entered))
    if exited:
        out.append("- 退出(上月前 %d):" % n + ";".join(
            "%s %s(上月%d→本月%s)" % (c, _cell(names.get(c, "")), prank[c], cur.get(c, "未入选/被剔除")) for c in exited))
    both = [c for c in cur if c in prank and c in now_top]
    moves = sorted(((prank[c] - cur[c], c) for c in both), key=lambda x: (-abs(x[0]), x[1]))[:5]
    moves = [m for m in moves if m[0] != 0]
    if moves:
        out.append("- 前 %d 名内名次变化最大(正=上升):" % n + ";".join(
            "%s %+d(%d→%d)" % (c, d, prank[c], cur[c]) for d, c in moves))
    return out + [""]


def _contrib(longs, avoids, table, weights):
    zc = [c for c in table.columns if c.startswith("z_")]
    fids = [c[2:] for c in zc]
    if not zc:
        return ["## 各因子贡献拆解", "", "- 排名表无因子列。", ""]
    w = pd.Series({f: float((weights or {}).get(f, 1.0)) for f in fids})
    w = w / w.sum() if w.sum() > 0 else w
    out = ["## 各因子贡献拆解", "",
           "贡献 = 权重占比 × 因子 z;下表为各组平均贡献(正数拉高综合分)。z 已含补值,补值部分为行业中位数。", "",
           "| 因子 | 说明 | 前组平均贡献 | 后组平均贡献 | 差 |", "|---|---|---|---|---|"]
    for f, c in zip(fids, zc):
        a = (longs[c] * w[f]).mean() if len(longs) else np.nan
        b = (avoids[c] * w[f]).mean() if len(avoids) else np.nan
        meta = F.REGISTRY.get(f)
        out.append("| %s | %s | %s | %s | %s |" % (f, _cell(meta.name if meta else ""), _f(a, 4), _f(b, 4), _f(a - b, 4)))
    out += ["", "前 10 名各自贡献最大的 3 个因子:", ""]
    for r in longs.head(10).itertuples(index=False):
        d = r._asdict()
        parts = sorted(((d["z_" + f] * w[f], f) for f in fids if pd.notna(d["z_" + f])), reverse=True)[:3]
        out.append("- %d %s %s:" % (r.rank, r.code, _cell(r.name)) + ",".join("%s %+.3f" % (f, v) for v, f in parts))
    if len(longs) == 0:
        out.append("- (无)")
    return out + [""]


def _industry(longs, table):
    out = ["## 前组所属行业分布", "", "| 行业 | 前组只数 | 前组占比 | 全部入选占比 |", "|---|---|---|---|"]
    if len(longs) == 0 or len(table) == 0:
        return out + ["| (无) | | | |", ""]
    cnt = longs["industry"].value_counts()
    base = table["industry"].value_counts() / len(table)
    for ind in sorted(cnt.index, key=lambda k: (-cnt[k], str(k))):
        out.append("| %s | %d | %.1f%% | %.1f%% |" % (_cell(ind), cnt[ind], 100.0 * cnt[ind] / len(longs), 100.0 * base[ind]))
    return out + ["", "占比远高于全部入选占比说明排名集中在少数行业,需留意行业暴露。", ""]


def build_monthly_report(ranking, universe_info=None, prev_ranking=None) -> str:
    """ranking: ranker.RankResult。universe_info: 可选 dict,识别键
    top_n / generated_at / weights(因子 id->权重,缺省等权) / universe_name / data_source / extra_notes(list)。
    ranking 为空或 None 时返回只含免责声明与"无排名"说明的报告,不抛异常。"""
    info = dict(universe_info or {})
    table = getattr(ranking, "table", None)
    asof = getattr(ranking, "as_of_date", None)
    asof_s = asof.strftime("%Y-%m-%d") if asof is not None and not pd.isna(asof) else "?"
    out = ["# 月度价值排名报告(基本面,独立于技术面)", "", DISCLAIMER, "",
           "- 排名日:%s    生成时间:%s" % (asof_s, info.get("generated_at", "(未提供)")),
           "- 股票池:%s    数据来源:%s" % (info.get("universe_name", "(未提供)"), info.get("data_source", "(未提供)"))]
    if ranking is not None:
        ex = getattr(ranking, "excluded", None)
        out.append("- 输入 %s 只,入选 %d 只,剔除 %s 只;配置指纹 `%s`" % (
            getattr(ranking, "n_input", "?"), len(table) if table is not None else 0,
            len(ex) if ex is not None else "?", getattr(ranking, "cfg_hash", "")))
    out.append("")
    notes = list(getattr(ranking, "notes", []) or []) + list(info.get("extra_notes", []))
    if table is None or len(table) == 0:
        out += ["## 无排名结果", "",
                "本月没有任何股票入选排名(输入为空、数据未到或全部被剔除)。不生成名单。", ""]
        out += ["## 数据备注", ""] + (["- " + _cell(x) for x in notes] or ["- (无)"]) + [""]
        out += ["## 已知局限", ""] + ["%d. %s" % (i, s) for i, s in enumerate(LIMITS, 1)] + [""]
        return "\n".join(out)

    n = max(int(info.get("top_n") or len(ranking.longs)), 1)
    longs, avoids = ranking.longs, ranking.avoids
    out += ["## 前 %d 名(综合分最高)" % n, ""] + _table(longs) + [""]
    out += ["## 后 %d 名(综合分最低,避开候选)" % n, ""] + _table(avoids) + [""]
    out += _changes(table, prev_ranking, n)
    out += _contrib(longs, avoids, table, info.get("weights"))
    out += _industry(longs, table)
    out += ["## 数据备注", ""] + (["- " + _cell(x) for x in notes] or ["- (无)"])
    if getattr(ranking, "excluded", None) is not None and len(ranking.excluded):
        cnt = ranking.excluded["reason"].value_counts().sort_index()
        out += ["", "剔除原因统计:" + ";".join("%s=%d" % (k, v) for k, v in cnt.items())]
    out += ["", "## 已知局限", ""] + ["%d. %s" % (i, s) for i, s in enumerate(LIMITS, 1)] + [""]
    return "\n".join(out)


def write_report(path, text, overwrite=False):
    """写报告;默认不覆盖:已存在抛 FileExistsError。先写临时文件,再 os.link(不覆盖)/os.replace(覆盖)。"""
    path = os.fspath(path)
    if os.path.isdir(path):
        raise IsADirectoryError(path)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".report_", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if overwrite:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path
