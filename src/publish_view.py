"""发布页（N23）：只读地读出最近一次「导出并检查」的结果（build/<build_id>/report.json）和「上次已发布」记录，
给新界面 ui/w/publish.html 显示，并按所有者 2026-10-24 的决定生成**由所有者在终端手动执行**的命令（预览 / 复核+上传 / 记录已发布）。

边界：
- 只读 build/ 下名字符合 build_id 格式的目录里的 report.json，以及 build/published.json；不跟随符号链接；文件大小有上限；
- 不构建、不上传、不联网、不读 .secret、不执行任何命令；命令只是文字，由 build_id（格式校验）和 manifest 摘要（64 位十六进制）拼成；
- 构建本身仍走已有的后台动作 publish_check（src/publish_admin.py，默认关闭），本模块不改它。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
PROJECT = "accessiblequant"
PREVIEW_PORT = 8790
MAX_JSON = 2_000_000
MAX_LIST = 200
ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}(-\d+)?$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
PROJECT_DIR = "~/Documents/Projects/quant-backtest"      # 命令里用的项目位置（所有者的 Mac）
WRANGLER = "npx --yes wrangler@3"                          # 和旧 build_site.py 上传时用的一致；版本与登录由所有者负责


def _read_json(p: Path):
    try:
        st = os.lstat(p)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_JSON:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _strs(x, limit=MAX_LIST) -> list[str]:
    return [str(v)[:500] for v in x[:limit]] if isinstance(x, list) else []


def _latest(build_root: Path) -> str | None:
    try:
        names = [e.name for e in os.scandir(build_root) if ID_RE.match(e.name) and e.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    names = [n for n in names if (build_root / n / "report.json").is_file()]
    return max(names) if names else None                  # build_id 以 UTC 时间开头，字符串最大 = 最新


def commands(build_id: str, sha: str) -> dict:
    if not ID_RE.match(build_id or "") or not SHA_RE.match(sha or ""):
        return {}
    site = f"build/{build_id}/site"
    py = [".venv/bin/python", "scripts/build_public_site.py"]
    digest = ["--expect-manifest-sha256", sha]
    cd = f"cd {PROJECT_DIR}"
    return {
        "preview": f"{cd}/{site} && (python3 -m http.server {PREVIEW_PORT} --bind 127.0.0.1 >/dev/null 2>&1 &) && sleep 1 && "
                   f"open http://127.0.0.1:{PREVIEW_PORT}/quant-ai-dark/landing.html",
        "preview_stop": f"lsof -ti tcp:{PREVIEW_PORT} -sTCP:LISTEN | xargs kill",
        "deploy": f"{cd} && {shlex.join([*py, '--verify', build_id, '--release', *digest])} && "
                  f"{WRANGLER} pages deploy {site} --project-name {PROJECT} --branch main",
        "mark": f"{cd} && {shlex.join([*py, '--mark-published', build_id, *digest])}",
    }


def latest(build_root: Path | None = None) -> dict:
    build_root = build_root or BUILD
    pub = _read_json(build_root / "published.json")
    published = None
    if isinstance(pub, dict) and ID_RE.match(str(pub.get("build_id", ""))):
        published = {"build_id": pub["build_id"], "at": str(pub.get("published_at") or pub.get("at") or "")[:40] or None}
    bid = _latest(build_root)
    if not bid:
        return {"status": "none", "published": published}
    rep = _read_json(build_root / bid / "report.json")
    if not isinstance(rep, dict):
        return {"status": "unreadable", "build_id": bid, "published": published}
    d = rep.get("diff_vs_published") if isinstance(rep.get("diff_vs_published"), dict) else {}
    sha = str(rep.get("manifest_sha256", ""))
    ok, blockers = rep.get("ok") is True, _strs(rep.get("blockers"))
    ready = ok and not blockers and rep.get("release_ready") is True
    stats = rep.get("stats") if isinstance(rep.get("stats"), dict) else {}
    out = {
        "status": "ok", "build_id": bid, "manifest_sha256": sha if SHA_RE.match(sha) else None,
        "built_at": f"{bid[0:4]}-{bid[4:6]}-{bid[6:8]}T{bid[9:11]}:{bid[11:13]}:{bid[13:15]}Z",
        "ok": ok, "release_ready": ready, "files": int(stats.get("files", 0) or 0),
        "errors": _strs(rep.get("errors")), "blockers": blockers, "warnings": _strs(rep.get("warnings")),
        "diff": {"baseline": d.get("baseline") is not None and d.get("baseline") is not False,
                 **{k: _strs(d.get(k)) for k in ("added", "changed", "removed")},
                 **{f"n_{k}": len(d.get(k)) if isinstance(d.get(k), list) else 0 for k in ("added", "changed", "removed")}},
        "published": published, "is_published": bool(published and published["build_id"] == bid),
    }
    cmds = commands(bid, sha) if out["manifest_sha256"] else {}
    out["commands"] = {"preview": cmds.get("preview"), "preview_stop": cmds.get("preview_stop")} if cmds else {}
    if ready and cmds:
        out["commands"].update(deploy=cmds["deploy"], mark=cmds["mark"])
    return out
