"""L2 一致性比对 + L3 反事实校验：把想法里抽取的"事实"和模型给出的配置逐字段比较。纯函数（oracle 由调用方传入）。

设计文档 7.2 – 7.4。要点：
- 想法里明确写了、配置里不一样 -> 标 mismatch，先交 L3。
- L3：把想法里的值写回配置的副本，交给校验器（oracle）判。校验器说这个字段本身非法，就升级为 block，
  拒绝码取自校验器；否则只是 warn（模型把它改成了另一个合法值）。合法取值范围只存在于校验器里，这里不抄一份。
- 与设计文档字面的一处细化：L3 判非法时，只有"校验器报错的位置正好是被替换的那个字段"才 block。
  如果报错在别的字段（比如把 h 换成 21 之后，调仓间隔和 embargo 跟着不匹配），说明用户要的值本身可能合法，只是和配置里其他项
  搭不上，这时只 warn，避免把合法的想法误拦。
- 想法里没写的字段：值等于系统提示词里写明的默认值 -> default；其余 -> unchecked。
- 同一字段想法里写了两个值（conflicts）、或处在否定语境（negated）：不比较，来源标 unchecked。

局限见设计文档 7.6：抽不到的表述不会报警，没有警告不等于配置正确。
"""
import copy

from .extract import Extraction, extract
from .plainview import LABELS, REJECT_CODES, make_refusal, value_text

_MISSING = object()

# 校验器报错的 path 与被替换字段不同名的情况：迟滞带的进出场是成对检查的，报在 band_entry 上
_BAND = ("portfolio.band_entry", "portfolio.band_exit")
_MATCH_PATHS = {p: set(_BAND) for p in _BAND}


def _eq(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def _resolve(fact_field: str, cfg_type: str):
    """事实字段 -> 配置路径。"reversal.N" 对 residual_reversal 是 L，对 reversal_raw 是 lookback，其他类型不比较。"""
    if fact_field != "reversal.N":
        return fact_field
    return {"residual_reversal": "signal.L", "reversal_raw": "signal.lookback"}.get(cfg_type)


def _get(cfg: dict, path: str):
    if path == "schema_version":
        return cfg.get("schema_version", _MISSING)
    sec, key = path.split(".")
    s = cfg.get(sec)
    return s.get(key, _MISSING) if isinstance(s, dict) else _MISSING


def _leaf_paths(cfg: dict) -> list:
    return [p for p in LABELS if _get(cfg, p) is not _MISSING]


def is_default(path: str, cfg: dict) -> bool:
    """配置里的值是否等于系统提示词里写明的默认值（想法没提这个字段时用）。"""
    v, t = _get(cfg, path), cfg["signal"]["type"]
    if path == "schema_version":
        return v == "1.0"
    if path == "universe.subset":
        return v == "all"
    if path == "portfolio.q":
        return v is None if t in ("equal_weight", "ma_cross") else _eq(v, 0.1)
    if path in ("portfolio.band_entry", "portfolio.band_exit"):
        return v is None
    if path == "market.name":
        return v == "us"
    if path == "market.cost_bp":
        return _eq(v, 5)
    if path == "period.start":
        return v == "auto"
    if path == "period.end":
        return v == "2023-12-31"
    if path in ("stats.bootstrap_reps", "stats.block", "stats.seed"):
        return _eq(v, {"stats.bootstrap_reps": 2000, "stats.block": 21, "stats.seed": 0}[path])
    if t == "ml":
        h = cfg["signal"].get("h")
        if path == "signal.refit_every":
            return _eq(v, 63)
        if path == "signal.embargo":
            return isinstance(h, (int, float)) and _eq(v, h + 1)
        if path == "signal.alpha":
            return _eq(v, 100) if cfg["signal"].get("model") == "ridge" else v is None
        if path == "portfolio.rebalance_every":
            return isinstance(h, (int, float)) and _eq(v, h)
    if path == "portfolio.rebalance_every" and t in ("equal_weight", "ma_cross"):
        return v is None
    return False


def _flag(rule_id, level, path, idea_v, cfg_v, key, **extra):
    f = {"rule_id": rule_id, "level": level, "field": path,
         "idea_says": value_text(path, idea_v), "config_has": value_text(path, cfg_v), "message_key": key}
    f.update(extra)
    return f


def _block_refusal(flag: dict, source: str) -> dict:
    label_zh, label_en = LABELS.get(flag["field"], (flag["field"],) * 2)
    return make_refusal(
        flag["code"], source,
        f"你的想法里「{label_zh}」写的是 {flag['idea_says']},这个取值不被允许。",
        f"Your idea asks for {label_en} = {flag['idea_says']}, which is not allowed.")


def compare(idea: str, config: dict, oracle, ex: Extraction | None = None) -> dict:
    """返回 {"flags": [...], "sources": {路径: idea|default|mismatch|unchecked}, "refusal": 拒绝 | None}。

    config 必须是已经通过闸门的配置；oracle 需要有 validate(doc) -> {"legal", "errors", "code"}（就是 Gate）。
    oracle 抛异常就直接向上抛：宁可失败，也不在没有校验器的情况下放行。
    """
    if not isinstance(config, dict) or "reject" in config:
        return {"flags": [], "sources": {}, "refusal": None}
    ex = ex or extract(idea)
    cfg_type = config["signal"]["type"]
    flags, sources, forced = [], {}, set()

    # 想法里的事实，按页面字段顺序整理成 路径 -> Fact（reversal.N 可能与显式的 lookback 撞车，显式的优先）
    by_path = {}
    for name, fact in ex.facts.items():
        path = _resolve(name, cfg_type)
        if path and (name != "reversal.N" or path not in by_path):
            by_path[path] = fact
    for name in set(ex.conflicts) | ex.negated:
        path = _resolve(name, cfg_type)
        if path:
            forced.add(path)

    # C-CONFLICT
    for name in ex.conflicts:
        path = _resolve(name, cfg_type) or name
        flags.append({"rule_id": "C-CONFLICT", "level": "warn", "field": path,
                      "idea_says": "", "config_has": "", "message_key": "ai.flag.conflict"})

    # C-TYPE：只比较关键词明确的类型；单独的"反转"不产出事实
    type_mismatch = False
    tf = ex.facts.get("signal.type")
    if tf is not None:
        if _eq(tf.value, cfg_type):
            sources["signal.type"] = "idea"
        else:
            type_mismatch = True
            sources["signal.type"] = "mismatch"
            flags.append(_flag("C-TYPE", "warn", "signal.type", tf.value, cfg_type, "ai.flag.mismatch"))

    # C-EMBARGO：embargo 小于 h+1（h 优先取想法里写的，没写就用配置里的）
    emb_block = False
    ef = by_path.get("signal.embargo")
    if ef is not None:
        hf = by_path.get("signal.h")
        h = hf.value if hf is not None else _get(config, "signal.h")
        if isinstance(h, (int, float)) and not isinstance(h, bool) and ef.value < h + 1:
            emb_block = True
            f = _flag("C-EMBARGO", "block", "signal.embargo", ef.value, _get(config, "signal.embargo"),
                      "ai.flag.block", code="EMBARGO_VIOLATION")
            f["refusal"] = make_refusal("EMBARGO_VIOLATION", "consistency",
                                        f"你的想法里 embargo 是 {ef.value} 天,预测周期是 {h} 天,embargo 至少要 {h + 1} 天。",
                                        f"Your idea has an embargo of {ef.value} days with a {h}-day horizon; the embargo must be at least {h + 1} days.")
            flags.append(f)

    # C-NUM / C-END（含 L3 反事实校验）
    for path in LABELS:
        fact = by_path.get(path)
        if fact is None or path == "signal.type":
            continue
        cur = _get(config, path)
        if cur is _MISSING:
            if not type_mismatch:
                flags.append({"rule_id": "C-NUM", "level": "warn", "field": path, "idea_says": value_text(path, fact.value),
                              "config_has": "", "message_key": "ai.flag.missing"})
            continue
        if _eq(fact.value, cur):
            sources[path] = "idea"
            continue
        sources[path] = "mismatch"
        if path == "signal.embargo" and emb_block:
            continue                                            # 已经由 C-EMBARGO 拦下，不重复
        rule = "C-END" if path == "period.end" else "C-NUM"
        cf = copy.deepcopy(config)
        apply, can_test = [path], True
        if path in _BAND:                                       # 迟滞带的进场 / 离场必须成对设置：想法里两个都写了就一起写回副本
            other = _BAND[1] if path == _BAND[0] else _BAND[0]
            if other in by_path:
                apply.append(other)
            elif _get(config, other) is None:
                can_test = False                                # 只写了一半、另一半配置里是 null：单独替换本身就不成对，无法判断，只 warn
        hits = []
        if can_test:
            for p in apply:
                sec, key = p.split(".")
                cf[sec][key] = by_path[p].value
            res = oracle.validate(cf)
            hits = [e for e in res["errors"] if e["path"] in _MATCH_PATHS.get(path, {path})] if not res["legal"] else []
        if hits and path in _BAND and any(f["level"] == "block" and f["field"] in _BAND for f in flags):
            continue                                            # 同一对迟滞带只报一次 block
        if hits:
            code = hits[0]["code"]
            f = _flag(rule, "block", path, fact.value, cur, "ai.flag.block", code=code if code in REJECT_CODES else "UNSUPPORTED_REQUEST")
            f["refusal"] = _block_refusal(f, "counterfactual")
            flags.append(f)
        else:
            flags.append(_flag(rule, "warn", path, fact.value, cur, "ai.flag.mismatch"))

    # 其余字段的来源
    for path in _leaf_paths(config):
        if path in sources:
            continue
        sources[path] = "default" if path not in forced and is_default(path, config) else "unchecked"

    blocks = [f for f in flags if f["level"] == "block"]
    refusal = blocks[0]["refusal"] if blocks else None
    return {"flags": flags, "sources": sources, "refusal": refusal}
