"""个人模式：根目录 personal_mode.json 的 enabled 严格为 true 才开启。"""
from pathlib import Path
import json


CONFIG = Path(__file__).resolve().parent.parent / "personal_mode.json"


def is_enabled() -> bool:
    """每次请求读取开关；缺失、不可读或损坏时恢复完整模式。"""
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(config, dict) and config.get("enabled") is True


def hidden_routes() -> tuple[str, ...]:
    """以 / 结尾的项匹配整个路径家族，其余项只精确匹配。

    会员 GET me/portfolio 的可信本机只读例外由服务端认证后处理。
    """
    return (
        "/member", "/member/", "/member.js", "/api/member/",
        "/admin", "/admin/", "/admin.js", "/api/admin/",
        "/api/sim", "/api/share", "/api/share_visits", "/__share_login",
        "/share_gate.html", "/share_gate.js", "/api/eng/", "/api/code/",
        "/api/ai/", "/api/offline", "/ai_translator.js", "/engines.js",
    )
