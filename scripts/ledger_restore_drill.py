#!/usr/bin/env python3
"""信号账本快照的恢复演练(命令行):证明 signal_ledger.backup_snapshot() 做的快照真的能恢复。

做什么:把最新一份快照(data/backups/signal_ledger/YYYYMMDD/)整个拷到一个全新的临时目录里"恢复",
  1. 按快照里的 MANIFEST.json 核对每个文件的大小和 sha256(拷贝过程没出错、快照没被篡改/损坏);
  2. 对恢复出来的 signal_ledger.db 跑 sqlite 的 PRAGMA integrity_check;
  3. 逐表统计行数,并和当前库(只读打开)对比;账本只增不删,所以当前库的行数应该 >= 快照;
  4. 输出一行结论(通过 / 警告 / 失败),演练完删掉临时目录。
只读真实快照和当前库,所有写入都在新建的临时目录里,不覆盖任何已有文件;不联网。
退出码:0 = 通过;1 = 失败(快照不能用);2 = 警告(能恢复,但有需要人看的差异)。
示例:.venv/bin/python scripts/ledger_restore_drill.py
      .venv/bin/python scripts/ledger_restore_drill.py --all
"""
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASS, WARN, FAIL = "通过", "警告", "失败"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}
EXIT_CODE = {PASS: 0, FAIL: 1, WARN: 2}


def list_snapshots(folder) -> list[Path]:
    """文件夹里所有 YYYYMMDD 快照目录，按日期从老到新；.part(没做完的半成品)不算。"""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(d for d in folder.iterdir() if d.is_dir() and re.fullmatch(r"\d{8}", d.name))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):       # 分块读，快照库有上百 MB
            h.update(chunk)
    return h.hexdigest()


def _open_ro(path: Path) -> sqlite3.Connection:
    """只读方式打开(mode=ro)：就算代码写错了也写不进去。"""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)


def table_counts(db: Path) -> dict[str, int]:
    """库里每张用户表的行数。表名来自 sqlite_master（不是用户输入），仍用引号包起来。"""
    con = _open_ro(db)
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        con.close()


def integrity_check(db: Path) -> str:
    """返回 'ok' 或 sqlite 报告的第一条问题；打不开的文件返回错误说明。"""
    try:
        con = _open_ro(db)
        try:
            return str(con.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error as e:
        return f"{type(e).__name__}: {e}"


def verify_manifest(snapshot: Path, restored: Path) -> tuple[list[str], list[str]]:
    """用 MANIFEST.json 核对恢复出来的文件。返回 (问题, 提示)：问题 = 缺文件/大小或哈希不一致；提示 = 清单本身的说明。"""
    problems, notes = [], []
    mf = snapshot / "MANIFEST.json"
    if not mf.is_file():
        return problems, ["没有 MANIFEST.json，无法核对文件哈希"]
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
        files = manifest["files"]
    except (ValueError, KeyError, TypeError) as e:
        return ["MANIFEST.json 读不出来：" + str(e)], notes
    for rel, meta in files.items():
        p = restored / rel
        if not p.is_file():
            problems.append(f"{rel} 缺失")
        elif p.stat().st_size != meta.get("bytes"):
            problems.append(f"{rel} 大小 {p.stat().st_size} ≠ 清单 {meta.get('bytes')}")
        elif _sha256(p) != meta.get("sha256"):
            problems.append(f"{rel} sha256 不一致")
    if manifest.get("failed"):
        notes.append("快照制作时就有文件没备份成功：" + "、".join(manifest["failed"]))
    return problems, notes


def compare_counts(snap: dict[str, int], cur: dict[str, int]) -> list[dict]:
    """逐表对比。账本是只增不删（INSERT OR IGNORE / REPLACE），所以当前库行数 < 快照 或 当前库有而快照没有的表，都要提醒。"""
    rows = []
    for t in sorted(set(snap) | set(cur)):
        s, c = snap.get(t), cur.get(t)
        if s is None:
            flag = "快照里没有这张表"
        elif c is None:
            flag = "当前库里没有这张表"
        elif c < s:
            flag = "当前库行数比快照少"
        else:
            flag = ""
        rows.append({"table": t, "snapshot": s, "current": c, "diff": None if s is None or c is None else c - s, "flag": flag})
    return rows


def drill_one(snapshot, current_db, work_base=None, keep=False) -> dict:
    """对一份快照做一次演练。所有写入都在 work_base 下新建的临时目录里；任何异常都转成"失败"结果而不是抛出。"""
    snapshot = Path(snapshot)
    res = {"snapshot": snapshot.name, "status": PASS, "problems": [], "notes": [], "counts": [], "integrity": None, "workdir": None}

    def bump(level):
        if _RANK[level] > _RANK[res["status"]]:
            res["status"] = level

    src_db = snapshot / "signal_ledger.db"
    if not src_db.is_file():
        res["problems"].append("快照里没有 signal_ledger.db")
        res["status"] = FAIL
        return res
    tmp = Path(tempfile.mkdtemp(prefix="ledger_drill_", dir=work_base))        # 全新目录：不可能覆盖已有文件
    res["workdir"] = str(tmp)
    try:
        restored = tmp / "restored"
        shutil.copytree(snapshot, restored)                                    # "恢复" = 把整份快照原样拷出来
        problems, notes = verify_manifest(snapshot, restored)
        res["notes"] += notes
        if problems:
            res["problems"] += problems
            bump(FAIL)
        res["integrity"] = integrity_check(restored / "signal_ledger.db")
        if res["integrity"] != "ok":
            res["problems"].append("integrity_check 不是 ok：" + res["integrity"])
            bump(FAIL)
        try:
            snap_counts = table_counts(restored / "signal_ledger.db")
        except sqlite3.Error as e:
            res["problems"].append(f"统计行数失败：{type(e).__name__}: {e}")
            bump(FAIL)
            return res
        if not snap_counts:
            res["problems"].append("恢复出来的库里一张表都没有")
            bump(FAIL)
        cur = Path(current_db) if current_db else None
        if cur is None or not cur.is_file():
            res["notes"].append("当前库不存在，跳过行数对比")
            res["counts"] = compare_counts(snap_counts, {})
            bump(WARN)
        else:
            try:
                res["counts"] = compare_counts(snap_counts, table_counts(cur))
            except sqlite3.Error as e:
                res["notes"].append(f"当前库读取失败，跳过行数对比：{type(e).__name__}: {e}")
                res["counts"] = compare_counts(snap_counts, {})
                bump(WARN)
            if any(r["flag"] for r in res["counts"]):
                bump(WARN)
        return res
    except Exception as e:                                                      # 演练本身出错也要给结论，不能崩
        res["problems"].append(f"演练过程出错：{type(e).__name__}: {e}")
        res["status"] = FAIL
        return res
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
            res["workdir"] = None


def summary_line(res: dict) -> str:
    """一行结论。"""
    total = sum(r["snapshot"] or 0 for r in res["counts"])
    head = f"账本快照恢复演练 {res['snapshot']}：{res['status']}"
    detail = f" | integrity_check={res['integrity']} | {len(res['counts'])} 张表 / {total} 行"
    flags = [f"{r['table']}:{r['flag']}" for r in res["counts"] if r["flag"]]
    extra = ""
    if res["problems"]:
        extra += " | 问题：" + "；".join(res["problems"])
    if flags:
        extra += " | 差异：" + "；".join(flags)
    return head + detail + extra


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="信号账本快照恢复演练(只读真实快照，写入只在临时目录)")
    ap.add_argument("--data-dir", default=str(ROOT / "data"), help="数据目录，默认 <项目>/data；当前库 = <data-dir>/signal_ledger.db，快照 = <data-dir>/backups/signal_ledger")
    ap.add_argument("--snapshot", default=None, help="指定快照目录(如 20261029)；默认最新一份")
    ap.add_argument("--all", action="store_true", help="把保留的所有快照都演练一遍")
    ap.add_argument("--work-dir", default=None, help="临时目录建在哪里(默认系统临时目录)")
    ap.add_argument("--keep", action="store_true", help="演练完保留恢复出来的临时目录(默认删除)")
    ap.add_argument("--verbose", action="store_true", help="另外打印逐表行数对比")
    a = ap.parse_args(argv)

    data = Path(a.data_dir)
    folder = data / "backups" / "signal_ledger"
    snaps = list_snapshots(folder)
    if a.snapshot:
        snaps = [d for d in snaps if d.name == a.snapshot]
    elif not a.all:
        snaps = snaps[-1:]
    if not snaps:
        print(f"账本快照恢复演练：{FAIL} | 在 {folder} 里没有找到可演练的快照")
        return EXIT_CODE[FAIL]
    worst = PASS
    for snap in snaps:
        res = drill_one(snap, data / "signal_ledger.db", work_base=a.work_dir, keep=a.keep)
        print(summary_line(res))
        for n in res["notes"]:
            print("  提示：" + n)
        if a.keep and res["workdir"]:
            print("  临时目录保留在：" + res["workdir"])
        if a.verbose:
            for r in res["counts"]:
                print(f"  {r['table']:<18} 快照={r['snapshot']} 当前={r['current']} 差={r['diff']} {r['flag']}")
        if _RANK[res["status"]] > _RANK[worst]:
            worst = res["status"]
    return EXIT_CODE[worst]


if __name__ == "__main__":
    sys.exit(main())
