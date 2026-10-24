"""L1 模型前预检：只看想法文本，命中"必拒"规则就不调用模型。纯函数。

规则（设计文档 7.4）：
- P-DATE：想法含日期语境里的年份 >= 2024，或含"至今 / 最新数据"这类相对当前时间的说法 -> SEALED_PERIOD。
  模糊时间词直接拒绝（偏保守，会有误伤；用户修改想法后重试即可）。
- P-EMBARGO：想法同时写了 embargo=N 和预测周期 h=H，且 N < H+1 -> EMBARGO_VIOLATION。
  H+1 这条规则本身来自设计文档；L3 的反事实校验会让校验器自己再判一次。

预检的局限见设计文档 7.6：措辞不在模式里的表达、中文数字、否定语境都抓不到，抓不到时不会拦截，也不报警。
"""
from .extract import Extraction, extract
from .plainview import make_refusal


def _clip(s: str, n: int = 40) -> str:
    return s if len(s) <= n else s[:n] + "…"


def precheck(idea: str, ex: Extraction | None = None) -> dict:
    """返回 {"blocked", "rules": [{rule_id, code, detail_zh, detail_en}], "refusal": 第一条命中规则对应的拒绝 | None}。"""
    ex = ex or extract(idea)
    rules = []
    hit = ex.late_dates[0] if ex.late_dates else None
    if hit is not None:
        t = _clip(hit.text)
        rules.append({"rule_id": "P-DATE", "code": "SEALED_PERIOD",
                      "detail_zh": f"你的想法里写了「{t}」,评估区间不能晚于 2023-12-31。",
                      "detail_en": f'Your idea mentions "{t}"; the evaluation period cannot extend past 2023-12-31.'})
    elif ex.relative:
        t = _clip(ex.relative[0][2])
        rules.append({"rule_id": "P-DATE", "code": "SEALED_PERIOD",
                      "detail_zh": f"你的想法里写了「{t}」,这会让评估区间进入封存期;请写明不晚于 2023-12-31 的结束日。",
                      "detail_en": f'Your idea says "{t}", which would push the evaluation past 2023-12-31; give an end date no later than 2023-12-31.'})
    emb, h = ex.facts.get("signal.embargo"), ex.facts.get("signal.h")
    if emb is not None and h is not None and emb.value < h.value + 1:
        rules.append({"rule_id": "P-EMBARGO", "code": "EMBARGO_VIOLATION",
                      "detail_zh": f"你的想法里 embargo 是 {emb.value} 天,预测周期是 {h.value} 天,embargo 至少要 {h.value + 1} 天。",
                      "detail_en": f"Your idea has an embargo of {emb.value} days with a {h.value}-day horizon; the embargo must be at least {h.value + 1} days."})
    refusal = None
    if rules:
        r = rules[0]
        refusal = make_refusal(r["code"], "precheck", r["detail_zh"], r["detail_en"])
    return {"blocked": bool(rules), "rules": rules, "refusal": refusal}
