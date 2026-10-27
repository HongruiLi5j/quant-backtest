"""上传体检的接口层（T031 S2，纯函数）：把请求体（JSON 字节）解析成参数，调用 upload_check，返回 (HTTP 状态码, 可序列化字典)。

只做：大小限制、JSON 解析、键名白名单、类型检查、错误信息整理。**不读写文件、不联网、不记录请求内容、不导入 app**。
路由接线（app.py 里一行）和登录校验由调用方负责；本函数默认关闭开关：enabled 为 False 时一律返回 404（像这个接口不存在）。

请求体 JSON：{"dates": [...], "returns": [...], "n_trials": 整数, 可选: "benchmark", "n_trades", "split_date",
              "turnover", "cost_bp", "cost_included", "factor_model"}
"""
from __future__ import annotations

import json
import threading

from . import extdata, factor_attribution as fa, upload_check as uc

API_MAX_ROWS = 10000                      # 接口层行数上限（约 40 年日线）：2 万行一次要算约 8 秒，先砍一半
_BUSY = threading.BoundedSemaphore(1)     # 同一时刻只允许一个体检在算；忙时返回 429（接线时还应由调用方加请求频率限制）
MAX_BODY_BYTES = 2 * 1024 * 1024          # 2MB：约 2 万行日期+收益+基准+换手绰绰有余
ALLOWED_KEYS = {"dates", "returns", "n_trials", "benchmark", "n_trades", "split_date", "turnover", "cost_bp", "cost_included", "factor_model"}
REQUIRED_KEYS = ("dates", "returns", "n_trials")


def _err(code, msg):
    return code, {"error": msg}


def handle(body: bytes, *, enabled: bool = False, factor_loader=extdata.load_factors):
    """返回 (status, payload)。错误信息是固定文案或来自 upload_check 的校验消息（不回显用户内容原文）。"""
    if enabled is not True:
        return _err(404, "not_found")
    if not isinstance(body, (bytes, bytearray)):
        return _err(400, "请求体必须是 JSON")
    if len(body) > MAX_BODY_BYTES:
        return _err(413, f"请求体超过 {MAX_BODY_BYTES // 1024 // 1024}MB 上限")
    try:
        data = json.loads(bytes(body).decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_no_dup_keys)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _err(400, "请求体不是合法的 JSON（NaN/Infinity、重复字段也不允许）")
    if not isinstance(data, dict):
        return _err(400, "请求体必须是 JSON 对象")
    unknown = sorted(set(data) - ALLOWED_KEYS)
    if unknown:
        return _err(400, f"不认识的字段数量：{len(unknown)}")            # 不回显字段名，避免把用户内容原样放进响应
    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        return _err(400, "缺少必填字段：" + "、".join(missing))
    if isinstance(data["returns"], list) and len(data["returns"]) > API_MAX_ROWS:
        return _err(413, f"行数超过 {API_MAX_ROWS} 的上限")
    model = data.get("factor_model")
    if model is not None and not (isinstance(model, str) and model in fa.MODELS):
        return _err(400, "factor_model 只能是 ff3 或 ff4")
    if not _BUSY.acquire(blocking=False):
        return _err(429, "体检正在进行，请稍后再试")
    try:
        out = uc.upload_check(
            data["dates"], data["returns"], data["n_trials"], benchmark=data.get("benchmark"), n_trades=data.get("n_trades"),
            split_date=data.get("split_date"), turnover=data.get("turnover"), cost_bp=data.get("cost_bp"),
            cost_included=data.get("cost_included"))
        if model is not None:                                            # 因子归因(仅美股收益)：因子数据只读本机缓存，没有就明确告诉用户，不联网
            factors = factor_loader()
            if factors is None:
                return _err(400, "因子数据还没有下载（在终端运行 .venv/bin/python -m src.extdata）")
            out["factor_attribution"] = fa.attribute(data["dates"], data["returns"], factors, model)
    except (uc.UploadInputError, fa.AttributionError) as e:
        return _err(400, str(e)[:200])                                # 只有明确的输入校验错误才把消息返回给调用方
    except Exception:
        return _err(500, "体检计算失败")                                # 其他异常(含内部 ValueError)：固定文案，不泄漏路径/堆栈
    finally:
        _BUSY.release()
    try:
        json.dumps(out, ensure_ascii=False, allow_nan=False)
    except Exception:
        return _err(500, "体检计算失败")
    return 200, out


def _no_dup_keys(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("duplicate key")
        d[k] = v
    return d


def _reject_constant(name):
    raise ValueError(f"不允许 {name}")
