"""大白话展示：字段中英文标签、取值文字、拒绝文案、提示文案。纯函数，不依赖任何外部项目。

REJECT_MSG 是 ai_layer 里同名常量的只读复制（网站进程不能导入 ai_lib）；tests 里有一条和原件逐字比对的检查，防止两边慢慢跑偏。
"""

# 字段 -> (中文, English)。顺序就是页面上的显示顺序。必须覆盖校验器里的每一个字段（测试里有检查）。
LABELS = {
    "schema_version": ("配置版本", "Schema version"),
    "universe.subset": ("股票范围", "Stock universe"),
    "signal.type": ("信号类型", "Signal type"),
    "signal.fast": ("快线天数", "Fast MA days"),
    "signal.slow": ("慢线天数", "Slow MA days"),
    "signal.lookback": ("回看天数", "Lookback days"),
    "signal.skip": ("跳过天数", "Skip days"),
    "signal.residual": ("残差类型", "Residual type"),
    "signal.L": ("反转天数", "Reversal days"),
    "signal.model": ("机器学习模型", "ML model"),
    "signal.features": ("特征集", "Feature set"),
    "signal.h": ("预测周期(天)", "Forecast horizon (days)"),
    "signal.train_window": ("训练窗口(天)", "Training window (days)"),
    "signal.refit_every": ("重训间隔(天)", "Refit every (days)"),
    "signal.embargo": ("embargo(天)", "Embargo (days)"),
    "signal.alpha": ("岭回归 alpha", "Ridge alpha"),
    "portfolio.q": ("多空各取比例 q", "Long/short fraction q"),
    "portfolio.rebalance_every": ("调仓间隔(天)", "Rebalance every (days)"),
    "portfolio.band_entry": ("迟滞带进场比例", "Hysteresis band entry"),
    "portfolio.band_exit": ("迟滞带离场比例", "Hysteresis band exit"),
    "market.name": ("市场", "Market"),
    "market.cost_bp": ("单边成本(bp)", "One-way cost (bp)"),
    "period.start": ("评估起点", "Evaluation start"),
    "period.end": ("评估结束日", "Evaluation end"),
    "stats.bootstrap_reps": ("自助法重复次数", "Bootstrap repetitions"),
    "stats.block": ("自助法块长度", "Bootstrap block length"),
    "stats.seed": ("随机种子", "Random seed"),
}

# 需要用户特别核对的字段：评估区间、成本、embargo、q、训练窗口、行业或成交额子集
IMPORTANT = frozenset({"period.start", "period.end", "market.cost_bp", "signal.embargo", "portfolio.q",
                       "signal.train_window", "universe.subset"})

REJECT_MSG = {
    "SEALED_PERIOD": ("评估结束日期不能晚于 2023-12-31;2024-01-01 起的数据为封存期,不能使用。",
                      "The evaluation end date cannot be after 2023-12-31; data from 2024-01-01 onward is " + "seal" + "ed."),
    "OUT_OF_RANGE": ("请求的参数超出本项目实际使用过的取值范围,无法生成配置。",
                     "A requested value is outside the ranges this project has actually used, so no config can be produced."),
    "EMBARGO_VIOLATION": ("embargo 不得小于预测周期加 1 天(h+1),否则训练标签会泄漏。",
                          "The embargo must be at least h+1 days, otherwise training labels leak."),
    "UNSUPPORTED_REQUEST": ("该请求不在本项目支持的策略与参数范围内,无法生成配置。",
                            "This request is outside the strategies and parameter combinations this project supports."),
    "MISSING_REQUIRED": ("请求没有指明要测试的策略类型,请说明信号(如残差反转、动量、ML 模型)。",
                         "The request does not say which strategy to test; please name a signal (e.g. residual reversal, momentum, an ML model)."),
}
REJECT_CODES = tuple(REJECT_MSG)

# message_key -> (中文模板, English 模板)。{idea} {config} {field} 由 flag 里的内容填入
FLAG_TEXT = {
    "ai.flag.mismatch": ("你写的是「{idea}」,配置里是「{config}」", 'You wrote "{idea}", the config has "{config}"'),
    "ai.flag.block": ("此项无法确认,请修改想法后重试", "This cannot be confirmed; edit your idea and try again"),
    "ai.flag.conflict": ("你的想法里对「{field}」写了不同的值,无法自动核对", 'Your idea gives different values for "{field}"; it cannot be checked automatically'),
    "ai.flag.missing": ("你提到了「{idea}」,但配置里没有「{field}」这一项", 'You mentioned "{idea}", but the config has no "{field}"'),
}


def refusal_text(code: str) -> tuple:
    return REJECT_MSG.get(code, REJECT_MSG["UNSUPPORTED_REQUEST"])


def make_refusal(code: str, source: str, detail_zh: str = "", detail_en: str = "") -> dict:
    """拒绝结构：优先用 REJECT_MSG 的中英文，再附一句具体定位。source: precheck / consistency / counterfactual / model / gate。"""
    if code not in REJECT_MSG:
        code = "UNSUPPORTED_REQUEST"
    zh, en = REJECT_MSG[code]
    return {"code": code, "source": source,
            "message_zh": f"{zh} {detail_zh}".strip(), "message_en": f"{en} {detail_en}".strip()}


def value_text(path: str, value) -> str:
    """把取值变成人能读的文字。None 显示为 null，让"没有这一项"和"值为空"都看得见。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if path in ("portfolio.q", "portfolio.band_entry", "portfolio.band_exit"):
            return f"{value * 100:g}%"
        if path == "market.cost_bp":
            return f"{value:g} bp"
        return f"{value:g}"
    return str(value)


def build_view(config: dict, sources: dict) -> list:
    """字段表：每个配置里存在的字段一行，带来源（idea / default / mismatch / unchecked）。"""
    out = []
    for path, (zh, en) in LABELS.items():
        if path == "schema_version":
            present, value = "schema_version" in config, config.get("schema_version")
        else:
            sec, key = path.split(".")
            present = isinstance(config.get(sec), dict) and key in config[sec]
            value = config[sec][key] if present else None
        if not present:
            continue
        out.append({"field": path, "label_zh": zh, "label_en": en, "value_text": value_text(path, value),
                    "source": sources.get(path, "unchecked"), "important": path in IMPORTANT})
    return out


def flag_message(flag: dict, lang: str = "zh") -> str:
    zh, en = FLAG_TEXT.get(flag.get("message_key"), FLAG_TEXT["ai.flag.mismatch"])
    tpl = zh if lang == "zh" else en
    return tpl.format(idea=flag.get("idea_says", ""), config=flag.get("config_has", ""), field=flag.get("field", ""))
