"""B12：AI 想法翻译的三个已知缺陷。运行：.venv/bin/python tests/test_ai_translator_b12.py

1. 闸门/反事实拒绝时，把出错字段名和规则名带给前端，但不回显校验器自由文本或用户原文；
2. 模型给策略用不到的参数填值：在交给闸门前剔除并给 warn，不再悄悄保留；
3. 日志脱敏只遮住疑似密钥的那一小段，正常文字保留。

全部用临时目录、假后端、假闸门；不联网，不读真实 data/，不碰 ai_layer。
"""
import os

os.environ["QB_TESTING"] = "1"      # 必须在导入被测模块之前：打开防护栏，FakeBackend 才能创建

import copy  # noqa: E402
import datetime  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from src.ai_translator import api, config, consistency, logstore, plainview, unused  # noqa: E402

ACCESS = {"is_local": True, "authenticated": True, "requested_with": True, "origin_ok": True}


def cfg_of(signal, every=None, q=None, cost=5):
    return {"schema_version": "1.0", "universe": {"subset": "all"}, "signal": signal,
            "portfolio": {"q": q, "rebalance_every": every, "band_entry": None, "band_exit": None},
            "market": {"name": "us", "cost_bp": cost}, "period": {"start": "auto", "end": "2023-12-31"},
            "stats": {"bootstrap_reps": 2000, "block": 21, "seed": 0}}


def eq_cfg(**kw):
    return cfg_of({"type": "equal_weight"}, **kw)


def ml_cfg(model="ridge", alpha=100.0):
    return cfg_of({"type": "ml", "model": model, "features": "v1", "h": 5, "train_window": 756,
                   "refit_every": 63, "embargo": 6, "alpha": alpha}, every=5, q=0.1)


class StubOracle:
    """假闸门：按传入函数返回校验结果，并记录它收到的配置。"""

    def __init__(self, fn=None):
        self.fn = fn
        self.calls = []

    def validate(self, doc):
        self.calls.append(copy.deepcopy(doc))
        if self.fn:
            return self.fn(doc)
        return {"legal": True, "code": None, "errors": []}


class Fixture:
    def __init__(self, outcomes, oracle):
        self.root = Path(tempfile.mkdtemp(prefix="qb_ai_b12_"))
        (self.root / "data").mkdir()
        (self.root / "data" / "ai_translator.json").write_text(json.dumps({"enabled": True, "backend": "fake"}))
        self.fake = api.FakeBackend(outcomes)
        self.oracle = oracle
        loader = config.ConfigLoader(self.root, lambda *a, **k: None)
        self.service = api.API(self.root, loader=loader, backend_factory=lambda cfg: self.fake, oracle=oracle)

    def call(self, action, body):
        return api.handle("POST", "/api/ai/" + action, json.dumps(body).encode(), service=self.service, **ACCESS)

    def translate(self, idea="equal-weight portfolio"):
        code, result = self.call("translate", {"idea": idea})
        assert code == 200, (code, result)
        return result

    def rows(self):
        return [json.loads(line) for p in sorted((self.root / "data" / "ai_translator").glob("log_*.jsonl"))
                for line in p.read_text().splitlines()]

    def close(self):
        self.service.close()


# ---------------------------------------------------------------- 缺陷 1：拒绝原因带字段名
class RefusalFieldsTests(unittest.TestCase):
    def test_gate_fields_whitelist_dedupe_limit_and_no_free_text(self):
        errors = [
            {"path": "market.cost_bp", "code": "OUT_OF_RANGE", "message": "cost 7 token=SECRETSECRETSECRET1"},
            {"path": "/private/internal/file.py", "code": "OUT_OF_RANGE", "message": "internal"},
            {"path": "market.cost_bp", "code": "UNSUPPORTED_REQUEST", "message": "duplicate field"},
            {"path": "signal", "code": "bad code with spaces", "message": "section level"},
            {"path": "signal.model_made_up_key", "code": "OUT_OF_RANGE", "message": "unknown key"},
            {"path": "period.end", "code": "SEALED_PERIOD", "message": "late"},
            {"path": "stats.seed", "code": "OUT_OF_RANGE", "message": "over the limit of three"},
            "not a dict",
        ]
        fields, rules = plainview.gate_fields(errors)
        # 重复字段只留第一次；非白名单路径丢弃；非规则码格式的 code 退回 UNSUPPORTED_REQUEST；最多 3 个
        self.assertEqual(fields, ["market.cost_bp", "signal", "period.end"])
        self.assertEqual(rules, ["OUT_OF_RANGE", "UNSUPPORTED_REQUEST", "SEALED_PERIOD"])
        self.assertNotIn("message", json.dumps([fields, rules]))
        self.assertEqual(plainview.gate_fields(None), ([], []))
        self.assertEqual(plainview.gate_fields([{"path": "/x", "code": "OUT_OF_RANGE"}]), ([], []))

    def test_make_refusal_without_fields_keeps_old_shape(self):
        old = plainview.make_refusal("OUT_OF_RANGE", "gate")
        self.assertEqual(set(old), {"code", "source", "message_zh", "message_en"})
        new = plainview.make_refusal("OUT_OF_RANGE", "gate", fields=["market.cost_bp"], rules=["OUT_OF_RANGE"])
        self.assertEqual((new["fields"], new["rules"]), (["market.cost_bp"], ["OUT_OF_RANGE"]))
        # 规则列表缺失或长度对不上时，用拒绝码补齐，保证 fields 与 rules 等长
        filled = plainview.make_refusal("OUT_OF_RANGE", "gate", fields=["a", "b"], rules=["X"])
        self.assertEqual(filled["rules"], ["OUT_OF_RANGE", "OUT_OF_RANGE"])

    def test_api_gate_refusal_carries_fields_and_rules_without_echoing_idea(self):
        secret = "ABCDEFGHIJKLMNOPQR123456"
        oracle = StubOracle(lambda doc: {"legal": False, "code": "OUT_OF_RANGE", "errors": [
            {"path": "market.cost_bp", "code": "OUT_OF_RANGE", "message": f"echo {secret}"},
            {"path": "/Users/someone/private.py", "code": "OUT_OF_RANGE", "message": "internal path"}]})
        fx = Fixture([eq_cfg(cost=7)], oracle)
        try:
            result = fx.translate(f"equal-weight cost 7 bp token={secret}")
        finally:
            fx.close()
        refusal = result["refusal"]
        self.assertEqual(refusal["source"], "gate")
        self.assertEqual(refusal["fields"], ["market.cost_bp"])
        self.assertEqual(refusal["rules"], ["OUT_OF_RANGE"])
        self.assertFalse(result["can_confirm"] or result["config"])
        dumped = json.dumps(result)
        self.assertNotIn(secret, dumped)
        self.assertNotIn("/Users/someone", dumped)

    def test_counterfactual_refusal_carries_field(self):
        def validate(doc):
            if doc["market"]["cost_bp"] == 7:
                return {"legal": False, "code": "OUT_OF_RANGE", "errors": [
                    {"path": "market.cost_bp", "code": "OUT_OF_RANGE", "message": "x"}]}
            return {"legal": True, "code": None, "errors": []}
        fx = Fixture([eq_cfg()], StubOracle(validate))
        try:
            result = fx.translate("equal-weight with 7 bp")
        finally:
            fx.close()
        refusal = result["refusal"]
        self.assertEqual(refusal["source"], "counterfactual")
        self.assertEqual((refusal["fields"], refusal["rules"]), (["market.cost_bp"], ["OUT_OF_RANGE"]))

    def test_embargo_consistency_refusal_carries_field(self):
        # 想法里 embargo=4 小于 h+1=6：一致性比对直接产出 C-EMBARGO 拦截（不经过预检）
        res = consistency.compare("ridge h=5 embargo of 4 days", ml_cfg(), StubOracle())
        self.assertEqual([(f["rule_id"], f["level"]) for f in res["flags"]], [("C-EMBARGO", "block")])
        self.assertEqual(res["refusal"]["fields"], ["signal.embargo"])
        self.assertEqual(res["refusal"]["rules"], ["EMBARGO_VIOLATION"])


# ---------------------------------------------------------------- 缺陷 2：剔除用不到的参数
class PruneUnusedTests(unittest.TestCase):
    def test_equal_weight_filled_portfolio_and_foreign_signal_keys_removed(self):
        doc = eq_cfg(every=1, q=0.1)
        doc["signal"].update({"lookback": 252, "alpha": None})
        before = copy.deepcopy(doc)
        out, flags = unused.prune_unused(doc)
        self.assertEqual(doc, before, "入参不能被修改")
        self.assertEqual(out["signal"], {"type": "equal_weight"})
        self.assertIsNone(out["portfolio"]["q"])
        self.assertIsNone(out["portfolio"]["rebalance_every"])
        self.assertEqual({f["field"] for f in flags},
                         {"portfolio.q", "portfolio.rebalance_every", "signal.lookback", "signal.alpha"})
        for f in flags:
            self.assertEqual((f["rule_id"], f["level"], f["message_key"]), ("V-UNUSED", "warn", "ai.flag.unused"))
        # 其余字段原封不动
        self.assertEqual(out["market"], doc["market"])
        self.assertEqual(out["stats"], doc["stats"])

    def test_ma_cross_unused_values(self):
        doc = cfg_of({"type": "ma_cross", "fast": 20, "slow": 50, "skip": 21}, every=5, q=0.2)
        out, flags = unused.prune_unused(doc)
        self.assertEqual(out["signal"], {"type": "ma_cross", "fast": 20, "slow": 50})
        self.assertEqual(sorted(f["field"] for f in flags), ["portfolio.q", "portfolio.rebalance_every", "signal.skip"])

    def test_ml_alpha_only_ridge_uses_it(self):
        out, flags = unused.prune_unused(ml_cfg("hgb", alpha=100.0))
        self.assertIsNone(out["signal"]["alpha"])
        self.assertEqual([f["field"] for f in flags], ["signal.alpha"])
        for model in ("ridge",):
            out, flags = unused.prune_unused(ml_cfg(model, alpha=100.0))
            self.assertEqual((out["signal"]["alpha"], flags), (100.0, []))
        out, flags = unused.prune_unused(ml_cfg("mlp", alpha=None))
        self.assertEqual(flags, [])

    def test_legit_configs_untouched(self):
        for doc in (eq_cfg(),
                    cfg_of({"type": "momentum", "lookback": 252, "skip": 21}, every=1, q=0.1),
                    cfg_of({"type": "reversal_raw", "lookback": 5}, every=1, q=0.1),
                    cfg_of({"type": "residual_reversal", "residual": "market", "L": 5}, every=5, q=0.1),
                    ml_cfg("ridge")):
            out, flags = unused.prune_unused(doc)
            self.assertEqual((out, flags), (doc, []))

    def test_non_config_documents_are_passed_through(self):
        reject = {"schema_version": "1.0", "reject": {"code": "OUT_OF_RANGE", "message": "m"}}
        clarify = {"schema_version": "1.0", "clarify": {"message": "m"}}
        unknown_type = eq_cfg(q=0.1)
        unknown_type["signal"]["type"] = "made_up"
        for doc in (reject, clarify, unknown_type, {"signal": "x"}, [], None):
            self.assertEqual(unused.prune_unused(doc), (doc, []))

    def test_warning_text_exists_in_both_languages(self):
        flag = {"message_key": "ai.flag.unused", "field": "portfolio.q"}
        self.assertIn("portfolio.q", plainview.flag_message(flag, "zh"))
        self.assertIn("portfolio.q", plainview.flag_message(flag, "en"))
        self.assertNotEqual(plainview.flag_message(flag, "zh"), plainview.flag_message(flag, "en"))

    def test_api_prunes_before_gate_warns_and_requires_ack(self):
        model_out = eq_cfg(every=1, q=0.1)
        oracle = StubOracle()
        fx = Fixture([model_out], oracle)
        try:
            result = fx.translate("equal-weight portfolio")
            # 返回给前端、交给闸门、写进日志的都是清理后的配置
            self.assertIsNone(result["config"]["portfolio"]["q"])
            self.assertIsNone(result["config"]["portfolio"]["rebalance_every"])
            self.assertIsNone(oracle.calls[0]["portfolio"]["q"])
            unused_flags = [f for f in result["flags"] if f["rule_id"] == "V-UNUSED"]
            self.assertEqual({f["field"] for f in unused_flags}, {"portfolio.q", "portfolio.rebalance_every"})
            # warn 要求用户确认时勾选"已看到提示"
            self.assertTrue(result["needs_ack"] and result["can_confirm"])
            self.assertEqual(fx.call("confirm", {"request_id": result["request_id"]})[0], 409)
            code, confirmed = fx.call("confirm", {"request_id": result["request_id"], "ack_flags": True})
            self.assertEqual(code, 200)
            self.assertIsNone(confirmed["config"]["portfolio"]["q"])
            row = fx.rows()[0]
            self.assertIsNone(row["config"]["portfolio"]["q"])
            self.assertEqual(sorted(f["field"] for f in row["flags"] if f["rule_id"] == "V-UNUSED"),
                             ["portfolio.q", "portfolio.rebalance_every"])
        finally:
            fx.close()

    def test_api_clean_config_has_no_unused_flag(self):
        fx = Fixture([eq_cfg()], StubOracle())
        try:
            result = fx.translate("equal-weight portfolio")
        finally:
            fx.close()
        self.assertEqual(result["flags"], [])
        self.assertFalse(result["needs_ack"])

    def test_pruning_does_not_launder_an_explicit_user_value(self):
        # 用户明说 q=0.2、模型在等权里填了 0.2：清除后配置里是 null，一致性比对必须还能发现"想法与配置不符"
        fx = Fixture([eq_cfg(q=0.2)], StubOracle())
        try:
            result = fx.translate("equal-weight with q 20%")
        finally:
            fx.close()
        fields = {f["field"]: f["rule_id"] for f in result["flags"]}
        self.assertEqual(fields.get("portfolio.q"), "V-UNUSED")
        self.assertTrue(result["needs_ack"] or result["refusal"])


# ---------------------------------------------------------------- 缺陷 3：脱敏只遮疑似密钥
class RedactionTests(unittest.TestCase):
    def test_ordinary_text_with_sensitive_words_is_kept(self):
        for text in ("我想测一下 token 化之后的词频，等权组合，成本 5bp",
                     "the secret sauce is equal weight; password policy is out of scope, cost 2 bp",
                     "Token ring and cookie jar are just words here, 动量 252 日"):
            self.assertEqual(logstore._safe_text(text), text)
            self.assertEqual(logstore._safe(text), text)

    def test_only_the_credential_assignment_is_masked(self):
        got = logstore._safe_text("equal weight, token=abc123, cost 5 bp; secret: hunter2 then 动量 252 日")
        self.assertEqual(got, "equal weight, [redacted], cost 5 bp; [redacted] then 动量 252 日")
        got = logstore._safe_text("先等权。Authorization: Bearer abc.def-ghi 之后用 5bp 成本")
        self.assertEqual(got, "先等权。[redacted] 之后用 5bp 成本")
        got = logstore._safe_text('{"signal": {"type": "equal_weight"}, "password": "pw1", "cost_bp": 5}')
        # 值前后的引号留着（只遮"关键词+值"本身），其余键值对原样保留
        self.assertEqual(got, '{"signal": {"type": "equal_weight"}, "[redacted]", "cost_bp": 5}')
        # 中文紧挨英文关键词也要能识别
        self.assertEqual(logstore._safe_text("我的password:abc123要用"), "我的[redacted]要用")

    def test_long_alnum_string_masked_but_sentence_kept(self):
        key = "sk-ant-api03-AbCdEf1234567890xyz"
        got = logstore._safe_text(f"use equal weight with key {key} and cost 5 bp")
        self.assertEqual(got, "use equal weight with key [redacted] and cost 5 bp")
        self.assertEqual(logstore._safe_text("A" * 16), "[redacted]")
        self.assertEqual(logstore._safe_text("A" * 15), "A" * 15)           # 15 位不够长，保留
        self.assertEqual(logstore._safe_text("1234567890123456"), "[redacted]")
        self.assertEqual(logstore._safe_text("动量回看 252 日，跳过 21 日，每日调仓"), "动量回看 252 日，跳过 21 日，每日调仓")

    def test_ip_redaction_still_works(self):
        self.assertEqual(logstore._safe_text("from 192.0.2.7 ok"), "from [redacted] ok")
        self.assertEqual(logstore._safe_text("2023-12-31 and 5bp"), "2023-12-31 and 5bp")

    def test_logstore_end_to_end_keeps_structured_fields_and_normal_idea(self):
        root = Path(tempfile.mkdtemp(prefix="qb_ai_b12_log_"))
        store = logstore.LogStore(root)
        store._now = lambda: datetime.datetime(2026, 3, 1, 12, 0, tzinfo=datetime.timezone.utc)
        rid, sha = "0123456789abcdef", "d" * 64
        idea = "帮我测一下 token 化的想法，等权组合；key sk-ant-api03-AbCdEf1234567890xyz 不要记"
        store.translate(request_id=rid, backend_id="fake", model={"version": "v1", "adapter_sha256": sha},
                        idea=idea, precheck={}, raw_output='{"note": "secret=abcd1234", "ok": 1}',
                        parse_mode="strict", config={"signal": {"type": "equal_weight"}}, gate=None,
                        flags=[], status="ok", latency_ms=1)
        store.feedback(request_id=rid, note="token=zzz 这条备注其余部分要保留 " + "Q" * 20)
        [row] = [json.loads(line) for p in store.directory.glob("log_*.jsonl") for line in p.read_text().splitlines()]
        # request_id 与哈希是结构化字段，不能被"疑似密钥"规则误遮（contains() 依赖 request_id 原样）
        self.assertEqual((row["request_id"], row["model"]["adapter_sha256"]), (rid, sha))
        self.assertTrue(store.contains(rid))
        self.assertEqual(row["idea"], "帮我测一下 token 化的想法，等权组合；key [redacted] 不要记")
        self.assertEqual(row["raw_output"], '{"note": "[redacted]", "ok": 1}')
        note = json.loads((store.directory / "feedback.jsonl").read_text())["note"]
        self.assertEqual(note, "[redacted] 这条备注其余部分要保留 [redacted]")


if __name__ == "__main__":
    unittest.main(verbosity=2)
