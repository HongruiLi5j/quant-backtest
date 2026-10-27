"""后台「导出并检查」（T020 步骤 2）：在本机运行 scripts/build_public_site.py（只导出 + 检查，不上传），把输出文字交给后台页面。

边界（所有者 2026-10-24 决定）：不做发布按钮；上传由所有者在终端手动执行，脚本输出里会给出针对该已审快照的命令。
- 默认关闭：后台设置 publish_check_on 为 True 才执行，读失败按关闭；
- 子进程：固定可执行文件(当前解释器)、固定脚本路径、固定参数、固定工作目录、最小环境变量、超时；串行（同一时刻只允许一个）；
- 不联网、不读 .secret、不接触任何凭据；输出只截取文字，不回传环境变量。
"""
from __future__ import annotations

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
        return admin_settings.get_all().get("publish_check_on") is True
    except Exception:
        return False


def _env() -> dict:
    return {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}


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
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + "\n…（输出过长，已截断）"
        return {"ok": p.returncode == 0, "exit_code": int(p.returncode), "output": text}
    finally:
        _BUSY.release()
