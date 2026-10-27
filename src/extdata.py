"""外部公开数据（T037）：Ken French 美股因子（日度）+ FRED 3 个月国库券利率。**只在所有者手动运行时联网**，结果缓存到 data/external/（data/ 不上传）。

边界：
- 只允许固定的 https 地址（见 SOURCES）；下载有大小上限、超时；不读 .secret、不带任何凭据；不被每日任务/定时任务调用。
- Ken French 页面只写了版权署名（Eugene F. Fama and Kenneth R. French），没有写使用条款：只在本机用于研究，**不转发原文件**，对外只展示派生结果并署名。
- FRED DTB3 来自美联储 H.15，标注 Public Domain: Citation Requested；使用时需要署名「This product uses the FRED® API but is not endorsed
  or certified by the Federal Reserve Bank of St. Louis.」（这里用的是网页 CSV 下载，同样按此署名）。带 "Copyright" 备注的 FRED 序列不在白名单内。
- 缺失值（-99.99、-999、"."）记为缺失，不插值、不补零。
用法（项目根目录）：.venv/bin/python -m src.extdata
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path
from datetime import datetime
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "data" / "external"
MAX_BYTES = 2 * 1024 * 1024
MAX_UNZIPPED = 5 * 1024 * 1024
TIMEOUT = 30
UA = "quant-backtest-research (local, manual fetch)"

SOURCES = {
    "ff3": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_daily_CSV.zip",
    "mom": "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Momentum_Factor_daily_CSV.zip",
    "dtb3": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTB3",
}
ATTRIBUTION = {
    "ken_french": "Copyright Eugene F. Fama and Kenneth R. French. Source: Kenneth R. French Data Library.",
    "fred": "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis. Series DTB3: Board of Governors of the Federal Reserve System (US), H.15.",
}
MISSING = {"-99.99", "-999", "-999.0", "-99.99.", ".", ""}


class ExtDataError(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    """白名单地址只允许原地返回，任何重定向都拒绝(重定向可能跳到白名单之外，甚至 http)。"""

    def redirect_request(self, *a, **k):
        raise ExtDataError("不允许重定向")


_default_open = build_opener(_NoRedirect).open


def _http(url: str, opener=None) -> bytes:
    opener = opener or _default_open
    if url not in SOURCES.values():
        raise ExtDataError("地址不在白名单内")
    if not url.startswith("https://"):
        raise ExtDataError("只允许 https")
    with opener(Request(url, headers={"User-Agent": UA}), timeout=TIMEOUT) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ExtDataError("下载内容超过大小上限")
    if not data:
        raise ExtDataError("下载内容为空")
    return data


def _unzip_csv(blob: bytes) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise ExtDataError("不是合法的 zip")
    names = [i for i in z.infolist() if i.filename.lower().endswith(".csv") and not i.filename.startswith(("/", "..")) and "/" not in i.filename]
    if len(names) != 1:
        raise ExtDataError("zip 里需要恰好一个根目录下的 csv")
    if names[0].file_size > MAX_UNZIPPED:
        raise ExtDataError("解压后过大")
    raw = z.read(names[0])
    if len(raw) > MAX_UNZIPPED:
        raise ExtDataError("解压后过大")
    return raw.decode("latin-1")


def _num(s: str):
    s = s.strip()
    if s in MISSING:
        return None
    try:
        v = float(s)
    except ValueError:
        raise ExtDataError(f"无法解析数值：{s[:20]!r}")
    if v in (-99.99, -999.0):
        return None
    if v != v or abs(v) == float("inf"):
        raise ExtDataError("出现非有限数值")
    return v


def _valid_day(d: str):
    try:
        datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        raise ExtDataError("日期不是真实存在的日历日")


def parse_french(text: str, columns: list[str]) -> list[dict]:
    """Ken French 日度 CSV：前面是说明文字，表头行以逗号开头(第一格为空)，数据行第一格是 YYYYMMDD；数据后面是版权说明。百分数 → 小数。"""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith(",") and len(l.split(",")) == len(columns) + 1), None)
    if start is None:
        raise ExtDataError("找不到表头行")
    header = [c.strip() for c in lines[start].split(",")[1:]]
    if header != columns:
        raise ExtDataError(f"列名不符：{header}")
    rows, seen = [], set()
    body = lines[start + 1:]
    for i, l in enumerate(body):
        p = [x.strip() for x in l.split(",")]
        if not p or not re.fullmatch(r"\d{8}", p[0]):
            if rows:
                if any(re.match(r"\s*\d{8}\s*,", x) for x in body[i + 1:]):
                    raise ExtDataError("数据中途出现坏行")        # 坏行后面还有数据行：不能把它当成数据结束
                break                                           # 数据结束(后面是版权说明)
            continue
        if len(p) != len(columns) + 1:
            raise ExtDataError("数据行列数不对")
        d = f"{p[0][:4]}-{p[0][4:6]}-{p[0][6:]}"
        _valid_day(d)
        if d in seen or (rows and d <= rows[-1]["date"]):
            raise ExtDataError("日期重复或倒序")
        seen.add(d)
        vals = [_num(x) for x in p[1:]]
        rows.append({"date": d, **{c: (None if v is None else round(v / 100.0, 10)) for c, v in zip(columns, vals)}})
    if len(rows) < 1000:
        raise ExtDataError("数据行数异常偏少")
    return rows


def parse_fred(text: str, sid: str) -> list[dict]:
    """FRED 网页 CSV：observation_date,<SID>；"." 或空为缺失。保持原始单位(百分数年化利率)。"""
    rd = csv.reader(io.StringIO(text))
    head = next(rd, None)
    if head != ["observation_date", sid]:
        raise ExtDataError(f"表头不符：{head}")
    rows, last = [], ""
    for r in rd:
        if len(r) != 2 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r[0]) or r[0] <= last:
            raise ExtDataError("日期格式错误、重复或倒序")
        _valid_day(r[0])
        last = r[0]
        rows.append({"date": r[0], sid.lower(): _num(r[1])})
    if len(rows) < 1000:
        raise ExtDataError("数据行数异常偏少")
    return rows


def _fmt(v):
    return "" if v is None else repr(v)


def _stage_csv(path: Path, rows: list[dict], cols: list[str]):
    """把 CSV 写成同目录的临时文件(还没替换正式文件)，返回 (临时路径, sha256)。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", *cols])
    for r in rows:
        w.writerow([r["date"], *[_fmt(r.get(c)) for c in cols]])
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    return tmp, hashlib.sha256(buf.getvalue().encode("utf-8")).hexdigest()


def fetch_all(base: Path = BASE, opener=None) -> dict:
    """下载三份数据、解析、校验后一起写入；任何一份失败就一份都不写(不留下半新半旧的组合)。"""
    base = Path(base)
    ff3 = parse_french(_unzip_csv(_http(SOURCES["ff3"], opener)), ["Mkt-RF", "SMB", "HML", "RF"])
    mom = parse_french(_unzip_csv(_http(SOURCES["mom"], opener)), ["Mom"])
    dtb3 = parse_fred(_http(SOURCES["dtb3"], opener).decode("utf-8"), "DTB3")
    base.mkdir(parents=True, exist_ok=True)
    meta = {"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "sources": dict(SOURCES), "attribution": dict(ATTRIBUTION),
            "note": "仅本机研究使用；Ken French 原文件不转发；对外只展示派生结果并署名。收益为小数(已除以 100)，DTB3 为年化百分数。"}
    staged = []
    try:
        for key, rows, cols, name in (("ff3", ff3, ["Mkt-RF", "SMB", "HML", "RF"], "ff3_daily.csv"), ("mom", mom, ["Mom"], "mom_daily.csv"),
                                      ("dtb3", dtb3, ["dtb3"], "fred_dtb3.csv")):
            tmp, sha = _stage_csv(base / name, rows, cols)
            staged.append((tmp, base / name))
            meta[key] = {"file": name, "rows": len(rows), "first": rows[0]["date"], "last": rows[-1]["date"], "sha256": sha}
        mtmp = base / f"meta.json.{os.getpid()}.tmp"
        mtmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        staged.append((mtmp, base / "meta.json"))
    except Exception:
        for tmp, _ in staged:                                         # 任何一份写不下就一份都不换，清掉临时文件
            tmp.unlink(missing_ok=True)
        raise
    for tmp, dst in staged:                                           # 全部写好才开始替换；meta 最后换。读取时 load_factors 会按 meta 的 sha256 核对，半新半旧会被识别并拒用
        tmp.replace(dst)
    return meta


def load_factors(base: Path = BASE) -> dict | None:
    """读本地缓存：{日期: {"Mkt-RF","SMB","HML","RF","Mom"}}（小数；缺失为 None）。没有缓存返回 None。"""
    base = Path(base)
    try:
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        out: dict = {}
        for key, name in (("ff3", "ff3_daily.csv"), ("mom", "mom_daily.csv")):
            raw = (base / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != meta[key]["sha256"]:
                return None                                           # 文件与 meta 对不上(半新半旧或被改动)：不使用
            with io.StringIO(raw.decode("utf-8"), newline="") as f:
                for r in csv.DictReader(f):
                    d = r.pop("date")
                    out.setdefault(d, {}).update({k: (float(v) if v != "" else None) for k, v in r.items()})
        return out
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main() -> int:
    try:
        meta = fetch_all()
    except ExtDataError as e:
        print(f"失败：{e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"失败：{type(e).__name__}", file=sys.stderr)
        return 2
    for k in ("ff3", "mom", "dtb3"):
        m = meta[k]
        print(f"{k}: {m['rows']} 行，{m['first']} ~ {m['last']}，{m['file']}")
    print("缓存目录：data/external/；署名见 meta.json 的 attribution。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
