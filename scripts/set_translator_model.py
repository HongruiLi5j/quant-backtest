"""持有 worker 锁后校验、冒烟、原子切换；dry-run 和失败均保留登记文件原始字节。"""
import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ai_translator import config, gate, local_lora, paths, registry


def smoke(cfg, record, root, lock_fd, smoke_file, *, test_command=None, test_gate_script=None):
    if (test_command is not None or test_gate_script is not None) and os.environ.get("QB_TESTING") != "1":
        raise registry.RegistryError("TEST_INJECTION_FORBIDDEN")
    cases = json.loads(paths.guard(smoke_file).read_text(encoding="utf-8"))
    if not isinstance(cases, list) or len(cases) != 10 or sum(c.get("should_reject") is True for c in cases) != 3 \
            or not all(isinstance(c.get("idea"), str) and c["idea"].strip() for c in cases):
        raise registry.RegistryError("SMOKE_INVALID")
    oracle = gate.Gate.from_config(cfg, **({"script": test_gate_script} if test_gate_script else {}))
    owner = local_lora.Supervisor(cfg, root=root, model_provider=lambda: record,
                                  worker_lock_fd=lock_fd, test_command=test_command)
    deadline = time.monotonic() + cfg["timeouts"]["load_s"] + len(cases) * cfg["timeouts"]["request_s"]
    try:
        for case in cases:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise registry.RegistryError("SMOKE_TIMEOUT")
            owner.cfg["timeouts"]["load_s"] = min(cfg["timeouts"]["load_s"], remaining)
            owner.cfg["timeouts"]["request_s"] = min(cfg["timeouts"]["request_s"], remaining)
            result = owner.translate(case["idea"])
            if result["refusal"] or result["parse_mode"] != "strict" or not isinstance(result["config"], dict):
                raise registry.RegistryError("SMOKE_PIPELINE_FAILED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise registry.RegistryError("SMOKE_TIMEOUT")
            oracle.timeout_s = remaining
            oracle.validate(result["config"])
            if time.monotonic() >= deadline:
                raise registry.RegistryError("SMOKE_TIMEOUT")
        if oracle.calls != len(cases):
            raise registry.RegistryError("SMOKE_GATE_NOT_CALLED")
    finally:
        owner.close()
    return {"responses": len(cases), "parsed": len(cases), "gate_calls": oracle.calls}


def switch(args):
    root = paths.guard(args.root)
    base = paths.guard(args.ai_layer_dir)
    target = paths.guard(args.registry) if args.registry else base / "MODEL_REGISTRY.json"
    with registry.FileLock(paths.lock_path(root)) as worker_lock:
        with registry.FileLock(target.with_suffix(target.suffix + ".lock")):
            before = target.read_bytes()
            obj = registry.read_registry(target)
            version = args.version
            if args.rollback:
                if not obj["history"] or not obj["history"][-1].get("from"):
                    raise registry.RegistryError("NO_ROLLBACK_TARGET")
                version = obj["history"][-1]["from"]
            record = registry.verified_version(base, obj, version)
            ref = record.get("eval_ref")
            if registry.evaluation_hash(base, ref) != ref.get("line_sha256"):
                raise registry.RegistryError("EVAL_HASH_MISMATCH")
            cfg = config.ConfigLoader(root).load().cfg
            cfg.update(enabled=True, backend="local_lora", ai_layer_dir=str(base), expect_model_version=version)
            if args.statarb_src_dir:
                cfg["statarb_src_dir"] = str(paths.guard(args.statarb_src_dir))
            cfg = config.validate(cfg)
            result = smoke(cfg, record, root, worker_lock.fd, args.smoke_file,
                           test_command=json.loads(args.test_command) if args.test_command else None,
                           test_gate_script=args.test_gate_script)
            # 冒烟期间资产或登记被外部改动也不能激活。
            registry.verified_version(base, obj, version)
            if target.read_bytes() != before or registry.evaluation_hash(base, ref) != ref["line_sha256"]:
                raise registry.RegistryError("REGISTRY_OR_EVAL_CHANGED")
            if not args.dry_run:
                obj["history"].append({"from": obj["active_version"], "to": version,
                                       "at": datetime.datetime.now().astimezone().isoformat(),
                                       "via": "rollback" if args.rollback else "set"})
                obj["active_version"] = version
                registry.atomic_write(target, obj)
                from src import admin
                admin.set_root(root)
                admin.audit("ai_translator_model_switched", {"from": obj["history"][-1]["from"],
                                                           "to": version, "via": obj["history"][-1]["via"]})
            print(json.dumps(result))
            print("dry-run 通过；registry 未修改" if args.dry_run else "切换完成：" + version)
            if record["status"] == "experimental":
                print("该版本为实验版本，页面会显示‘实验版本’")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--root", default=str(paths.ROOT))
    parser.add_argument("--ai-layer-dir", required=True)
    parser.add_argument("--statarb-src-dir")
    parser.add_argument("--registry")
    parser.add_argument("--smoke-file", default=str(paths.ROOT / "tests" / "ai_translator_smoke.json"))
    parser.add_argument("--test-command")
    parser.add_argument("--test-gate-script")
    args = parser.parse_args(argv)
    if bool(args.version) == args.rollback:
        parser.error("必须指定版本或 --rollback，二选一")
    try:
        switch(args)
        return 0
    except Exception as exc:
        print("切换失败：" + (str(exc) if isinstance(exc, registry.RegistryError) else "PIPELINE_FAILED"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
