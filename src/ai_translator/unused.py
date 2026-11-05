"""剔除模型给"当前策略用不到的参数"填的值（校验阶段，在交给闸门之前）。纯函数，不依赖任何外部项目。

依据是 api_prompt.SYSTEM_PROMPT 里已经写明的配置形状：
- signal 里只能有当前 type 对应的键，别的 type 的键必须不存在（连 null 也不该有）；
- equal_weight / ma_cross 不做多空分组，portfolio.q 和 rebalance_every 必须是 null；
- ml 的 alpha 只有 ridge 用，hgb / mlp 必须是 null。

以前模型乱填这些值时只能靠闸门碰运气：闸门放行就悄悄保留在配置里。现在统一清掉，并给出 warn 标记，
让用户在确认时看到"哪些项被清除了"。只清除，不改写别的值，也不把任何非法取值"修成"合法值。
"""
import copy

from .plainview import value_text

# 每种信号类型允许的 signal 键（顺序同提示词）。不在表里的类型不处理，交给闸门拒绝。
SIGNAL_KEYS = {
    "equal_weight": ("type",),
    "ma_cross": ("type", "fast", "slow"),
    "momentum": ("type", "lookback", "skip"),
    "reversal_raw": ("type", "lookback"),
    "residual_reversal": ("type", "residual", "L"),
    "ml": ("type", "model", "features", "h", "train_window", "refit_every", "embargo", "alpha"),
}
ALL_SIGNAL_KEYS = frozenset(k for keys in SIGNAL_KEYS.values() for k in keys)
# 不做多空分组的类型：q 和调仓间隔没有意义
NO_PORTFOLIO = frozenset({"equal_weight", "ma_cross"})
RULE_ID = "V-UNUSED"


def _warn(path: str, value) -> dict:
    # idea_says 留空：这是模型填的值，不是用户写的；config_has 只放被清除的那个数值/枚举，不放任何用户原文
    return {"rule_id": RULE_ID, "level": "warn", "field": path, "idea_says": "",
            "config_has": value_text(path, value)[:40], "message_key": "ai.flag.unused"}


def prune_unused(doc):
    """返回 (清理后的新配置, warn 标记列表)。doc 不是"配置形状"（拒绝/追问/类型未知）时原样返回、无标记。不修改入参。"""
    if not isinstance(doc, dict) or "reject" in doc or "clarify" in doc:
        return doc, []
    signal = doc.get("signal")
    sig_type = signal.get("type") if isinstance(signal, dict) else None
    if not isinstance(sig_type, str) or sig_type not in SIGNAL_KEYS:
        return doc, []
    out, flags = copy.deepcopy(doc), []

    # 别的类型的 signal 键：整个删掉。只处理已知键名，模型自造的键名交给输出协议检查和闸门。
    allowed = SIGNAL_KEYS[sig_type]
    for key in list(out["signal"]):
        if key in ALL_SIGNAL_KEYS and key not in allowed:
            flags.append(_warn(f"signal.{key}", out["signal"].pop(key)))

    portfolio = out.get("portfolio")
    if sig_type in NO_PORTFOLIO and isinstance(portfolio, dict):
        for key in ("q", "rebalance_every"):
            if portfolio.get(key) is not None:
                flags.append(_warn(f"portfolio.{key}", portfolio[key]))
                portfolio[key] = None

    # alpha 只有 ridge 用；model 缺失或未知时不动，由闸门判断
    sig = out["signal"]
    if sig_type == "ml" and sig.get("model") in ("hgb", "mlp") and sig.get("alpha") is not None:
        flags.append(_warn("signal.alpha", sig["alpha"]))
        sig["alpha"] = None
    return out, flags
