"""后台「导出并检查」（T020 步骤 2）：在本机运行 scripts/build_public_site.py（只导出 + 检查，不上传），把输出文字交给后台页面。

边界（所有者 2026-10-24 决定）：不做发布按钮；上传由所有者在终端手动执行，脚本输出里会给出针对该已审快照的命令。
- 默认关闭：后台设置 publish_check_on 为 True 才执行，读失败按关闭；
- 子进程：固定可执行文件(当前解释器)、固定脚本路径、固定参数、固定工作目录、最小环境变量、超时；串行（同一时刻只允许一个）；
- 不联网、不读 .secret、不接触任何凭据；输出只截取文字，不回传环境变量。
"""
from __future__ import annotations

import re
from urllib.parse import unquote
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_public_site.py"
TIMEOUT_SECONDS = 300
MAX_OUTPUT_CHARS = 12000
_BUSY = threading.Lock()


class PublishCheckError(Exception):
    """固定文案的错误，可以安全地显示给后台页面。"""


def enabled() -> bool:
    from . import admin_settings
    try:
        return admin_settings.get_all(strict=True).get("publish_check_on") is True      # 读库失败不能退回缓存里的 True
    except Exception:
        return False


def _env() -> dict:
    return {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}


# 任何以 / 开头的绝对路径(前面不是字母数字、点、冒号、斜杠、连字符、> )、file:// 地址、Windows 盘符路径、../ 开头的相对越界路径
_ABS = re.compile(r"(?<![\w.:/\->])/[^\s'\"<>()\[\],;]+")
_FILE_URL = re.compile(r"file:(?://)?[^\s'\"<>]*", re.I)
_WIN = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s'\"<>]*")
_UP = re.compile(r"(?<![\w])(?:\.\.[\\/])+[^\s'\"<>]*")
_COLON_ABS = re.compile(r"(?<=[:=])/(?!/)[^\s'\"<>()\[\],;]+")           # error:/etc/hosts、PATH=/usr/bin 这类紧跟冒号或等号的绝对路径


def redact(text: str) -> str:
    """把路径换掉再回传页面：项目根 → <项目>，用户主目录 → ~，用户名 → <用户>，其余绝对路径/file:// 地址/盘符路径/../ 路径 → <路径>。
    相对路径的命令（如 .venv/bin/python scripts/…）保持原样，方便复制。"""
    for _ in range(2):                                            # 百分号编码的路径(%2Fetc%2Fhosts)先解码再脱敏，解码后的文字才是回传的内容
        dec = unquote(text)
        if dec == text:
            break
        text = dec
    text = text.replace(str(ROOT), "<项目>")
    try:
        home = Path.home()
        text = text.replace(str(home), "~")
        if len(home.name) >= 3:
            text = re.sub(r"(?<![\w])" + re.escape(home.name) + r"(?![\w])", "<用户>", text)
    except Exception:
        pass
    text = _FILE_URL.sub("<路径>", text)
    text = _WIN.sub("<路径>", text)
    text = _UP.sub("<路径>", text)
    text = _COLON_ABS.sub("<路径>", text)
    return _ABS.sub("<路径>", text)


def run_check(*, runner=subprocess.run) -> dict:
    """返回 {"ok": 构建是否通过, "exit_code": int, "output": 文字}。关闭/忙/超时都抛 PublishCheckError(固定文案)。"""
    if not enabled():
        raise PublishCheckError("功能未开启（后台设置里打开「公开站导出检查」）")
    if not _BUSY.acquire(blocking=False):
        raise PublishCheckError("已经有一个导出检查在运行，请稍后再试")
    try:
        try:
            p = runner([sys.executable, "-B", str(SCRIPT)], cwd=str(ROOT), env=_env(), capture_output=True, text=True,
                       timeout=TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise PublishCheckError(f"导出检查超过 {TIMEOUT_SECONDS} 秒，已中止")
        except OSError:
            raise PublishCheckError("无法启动导出检查")
        text = ((p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")).strip()
        text = redact(text)
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + "\n…（输出过长，已截断）"
        return {"ok": p.returncode == 0, "exit_code": int(p.returncode), "output": text}
    finally:
        _BUSY.release()
