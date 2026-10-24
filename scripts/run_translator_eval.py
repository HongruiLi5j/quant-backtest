"""所有者手动运行的准确率评测；复用网站入口，不自动运行真实评测。"""
import argparse
import datetime
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from src.ai_translator import api, api_llm, config, paths
from src.ai_translator.backend import TranslatorError

MAX_CALLS = 50
CALL_LIMIT = "EVAL_CALL_LIMIT"
DEFAULT_CASES = REPO / "tests" / "ai_translator_eval_cases.json"


class EvalError(Exception):
    """只使用固定错误码，不携带输入、配置、凭据或异常详情。"""


def call_limit(value):
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("max-calls 必须为 1–50 的整数") from None
    if not 1 <= number <= MAX_CALLS:
        raise argparse.ArgumentTypeError("max-calls 必须为 1–50 的整数")
    return number


def load_cases(path, max_calls):
    try:
        cases = json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=config._no_constant,
                           object_pairs_hook=config._no_duplicates)
        if not isinstance(cases, list):
            raise ValueError()
        ids = set()
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("id"), str) or case["id"] in ids:
                raise ValueError()
            ids.add(case["id"])
            if (not isinstance(case.get("idea"), str) or not case["idea"].strip()
                    or case.get("expected_outcome") not in ("accept", "reject", "ambiguous")
                    or not isinstance(case.get("language"), str)
                    or type(case.get("category")) not in (str, int)):
                raise ValueError()
            if case["expected_outcome"] == "accept" and not isinstance(case.get("expected_fields"), dict):
                raise ValueError()
            if case["expected_outcome"] == "reject":
                if not isinstance(case.get("expected_code"), str):
                    raise ValueError()
                codes = case.get("accepted_codes", [])
                if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
                    raise ValueError()
    except Exception:
        raise EvalError("EVAL_CASES_INVALID") from None
    excluded = [c["id"] for c in cases if c["expected_outcome"] == "ambiguous"]
    active = [c for c in cases if c["expected_outcome"] != "ambiguous"]
    # 按剔除含糊项后的总用例数保守检查；不靠预检结果突破总上限。
    if len(active) > max_calls:
        raise EvalError("EVAL_TOO_MANY_CASES")
    return active, excluded


def fields_match(expected, doc):
    comparisons = []
    for field, value in expected.items():
        path, wanted = field, value
        if field == "signal.rebalance":
            path = "portfolio.rebalance_every"
            wanted = 1 if value == "daily" else value
        elif field == "signal.top_pct":
            path = "portfolio.q"
        actual, present = doc, True
        for part in path.split("."):
            if not isinstance(actual, dict) or part not in actual:
                actual, present = None, False
                break
            actual = actual[part]
        # bool 不得伪装成 0/1；数值的 JSON 整数和浮点表示可相等。
        equal = present and actual == wanted and isinstance(actual, bool) == isinstance(wanted, bool)
        comparisons.append({"field": field, "config_path": path, "expected": value,
                            "mapped_expected": wanted, "actual": actual, "present": present, "match": equal})
    return all(c["match"] for c in comparisons), comparisons


class EvalLog:
    """网站日志接口的独立接收器；原始响应、全文和配置均不保留、不落盘。"""

    def __init__(self):
        self.last = {}

    def translate(self, **record):
        validation = record["gate"]
        self.last = {"usage": record["usage"], "gate_caught": bool(
            validation is not None and not validation["legal"] and "reject" not in (record["config"] or {})),
            "flags": [{k: f[k] for k in ("rule_id", "level", "field")} for f in record["flags"]]}


class CountingBackend(api_llm.ApiLlmBackend):
    """仅在真实发送边界计数；预算拒绝、预检拒绝均不会消耗调用数。"""

    def __init__(self, cfg, *, root, max_calls):
        super().__init__(cfg, root=root)
        self.calls = 0
        self.limit_reached = False
        self.max_calls = min(max_calls, MAX_CALLS)

    def _send(self, payload, key):
        if self.calls >= self.max_calls:
            self.limit_reached = True
            return None, "TRANSLATOR_BACKEND_FAILED"
        self.calls += 1
        return super()._send(payload, key)


def score(case, result, record, calls):
    expectation = case["expected_outcome"]
    refusal = result.get("refusal") or {}
    produced = result.get("status") == "ok" and isinstance(result.get("config"), dict)
    rejected = result.get("status") == "rejected"
    code = refusal.get("code") or result.get("error")
    field_match, comparisons = fields_match(case.get("expected_fields") or {}, result["config"]) if produced else (False, [])
    code_match = rejected and code in [case.get("expected_code"), *case.get("accepted_codes", [])]
    correct = (produced and field_match) if expectation == "accept" else (rejected and code_match)
    return {"id": case["id"], "category": case["category"], "language": case["language"],
            "expected": expectation, "expected_code": case.get("expected_code"),
            "accepted_codes": case.get("accepted_codes", []), "expected_fields": case.get("expected_fields"),
            "actual": "accept" if produced else ("reject" if rejected else "error"), "code": code,
            "evaluated": True, "correct": bool(correct), "produced": produced, "rejected": rejected,
            "field_match": produced and field_match, "code_match": bool(code_match),
            "false_accept": expectation == "reject" and produced,
            "silent_alteration": expectation == "accept" and produced and not field_match,
            "stopped_by": refusal.get("source") or ("backend" if code else "complete"),
            "gate_caught": record.get("gate_caught", False), "fields": comparisons,
            "flags": record.get("flags", []), "calls": calls, "usage": record.get("usage")}


def summarize(rows):
    tested = [r for r in rows if r["evaluated"]]
    accepts = [r for r in tested if r["expected"] == "accept"]
    rejects = [r for r in tested if r["expected"] == "reject"]

    def rate(group, field):
        n = sum(bool(r[field]) for r in group)
        return {"numerator": n, "denominator": len(group), "rate": n / len(group) if group else None}

    return {"total": len(rows), "evaluated": len(tested), "not_evaluated": len(rows) - len(tested),
            "accept_produced": rate(accepts, "produced"), "accept_fields_all_correct": rate(accepts, "field_match"),
            "reject_correctly_rejected": rate(rejects, "rejected"), "rejection_code_match": rate(rejects, "code_match"),
            "false_accept": sum(r["false_accept"] for r in tested),
            "silent_alteration": sum(r["silent_alteration"] for r in tested),
            "gate_caught": sum(r["gate_caught"] for r in tested),
            "stopped_by": dict(Counter(r["stopped_by"] for r in tested if r["rejected"])),
            "calls": sum(r["calls"] for r in tested),
            "usage": {k: sum((r["usage"] or {}).get(k, 0) for r in tested)
                      for k in ("input_tokens", "output_tokens", "estimated_usd")},
            "estimated_usage_rows": sum(bool((r["usage"] or {}).get("estimated")) for r in tested)}


def evaluate(cases_path=DEFAULT_CASES, *, root=None, max_calls=MAX_CALLS):
    if type(max_calls) is not int or not 1 <= max_calls <= MAX_CALLS:
        raise EvalError("EVAL_MAX_CALLS_INVALID")
    cases, excluded = load_cases(cases_path, max_calls)
    loader = config.ConfigLoader(root)
    state = loader.load()
    if not state.enabled or state.backend != "api_llm" or not state.cfg["statarb_src_dir"]:
        raise EvalError("EVAL_REQUIRES_ENABLED_API_LLM")
    cfg = state.cfg
    worker = CountingBackend(cfg, root=root, max_calls=max_calls)
    # 与网站和后端相同的环境变量入口；仅检查存在性，不显示或保存值。
    if not os.environ.get(cfg["api_llm"]["api_key_env"]):
        worker.close()
        raise EvalError("EVAL_API_KEY_MISSING")
    logs = EvalLog()
    service = api.API(root, loader=loader, backend_factory=lambda _: worker, logs=logs)
    rows, stop_reason = [], None
    try:
        for case in cases:
            if worker.calls >= max_calls and stop_reason is None:
                stop_reason = CALL_LIMIT
            if stop_reason:
                rows.append({"id": case["id"], "expected": case["expected_outcome"],
                             "category": case["category"], "language": case["language"], "evaluated": False,
                             "expected_code": case.get("expected_code"), "expected_fields": case.get("expected_fields"),
                             "actual": "not_evaluated", "correct": None, "stopped_by": None, "usage": None,
                             "stop_reason": stop_reason})
                continue
            # 保留网站的 10 次/60 秒限速，不注入放宽的 rate_hit。
            if rows:
                time.sleep(6.1)
            before = worker.calls
            logs.last = {}
            _, result = service.handle("POST", "/api/ai/translate",
                                       json.dumps({"idea": case["idea"]}).encode("utf-8"),
                                       is_local=True, authenticated=True, requested_with=True, origin_ok=True)
            rows.append(score(case, result, logs.last, worker.calls - before))
            if worker.limit_reached:
                stop_reason = CALL_LIMIT
            elif result.get("error") == api_llm.BUDGET_ERROR:
                stop_reason = api_llm.BUDGET_ERROR
    finally:
        service.close()
        worker.close()
    report = {"schema": 1, "created_at": datetime.datetime.now().astimezone().isoformat(),
              "model": cfg["api_llm"]["model"], "max_calls": max_calls, "excluded_ambiguous_ids": excluded,
              "stop_reason": stop_reason, "results": rows, "summary": summarize(rows),
              "metric_notes": "比率分母为已评测的相应 accept/reject 用例；字段全对率也以全部已评测 accept 为分母。错误不算拒绝。费用来自后端 usage，estimated=true 为预算预留估算。",
              "by_category": {}, "by_language": {}}
    for field in ("category", "language"):
        report["by_" + field] = {str(key): summarize([r for r in rows if r[field] == key])
                                for key in sorted({r[field] for r in rows}, key=str)}
    return report


def write_report(report, root=None):
    directory = paths.state_dir(root) / "eval"
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory.parent, 0o700)
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    target = directory / f"eval_{stamp}.json"
    # 同秒重复运行拒绝覆盖已有证据。
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return target


def print_summary(report, target):
    summary = report["summary"]
    print("指标                         结果")
    for name in ("accept_produced", "accept_fields_all_correct", "reject_correctly_rejected", "rejection_code_match"):
        metric = summary[name]
        pct = f"{metric['rate']:.1%}" if metric["rate"] is not None else "N/A"
        print(f"{name}: {metric['numerator']}/{metric['denominator']} ({pct})")
    for name in ("evaluated", "not_evaluated", "calls", "false_accept", "silent_alteration", "gate_caught"):
        print(f"{name}: {summary[name]}")
    print("usage:", json.dumps(summary["usage"]))
    print("stopped_by:", json.dumps(summary["stopped_by"]))
    print("excluded_ambiguous:", ", ".join(report["excluded_ambiguous_ids"]) or "none")
    print("stop_reason:", report["stop_reason"] or "none")
    print("失败/未评测用例:")
    for row in report["results"]:
        if not row["correct"]:
            print(f"{row['id']}: expected={row['expected']} actual={row['actual']} "
                  f"layer={row.get('stopped_by', '-')} code={row.get('code') or row.get('stop_reason') or '-'}")
            for field in row.get("fields", []):
                if not field["match"]:
                    print(json.dumps(field, ensure_ascii=False))
    print("结果:", target)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--max-calls", type=call_limit, default=MAX_CALLS)
    if os.environ.get("QB_TESTING") == "1":
        parser.add_argument("--root", type=Path, required=True, help="仅测试：临时根目录")
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.cases, root=getattr(args, "root", None), max_calls=args.max_calls)
        target = write_report(report, getattr(args, "root", None))
        print_summary(report, target)
        return 2 if report["stop_reason"] else 0
    except EvalError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        print("EVAL_FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
