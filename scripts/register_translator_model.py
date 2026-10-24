"""登记实验模型；不激活版本，不修改评估记录。路径全部由参数指定。"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ai_translator import paths, registry


def register(args):
    base = paths.guard(args.ai_layer_dir)
    target = paths.guard(args.registry) if args.registry else base / "MODEL_REGISTRY.json"
    with registry.FileLock(target.with_suffix(target.suffix + ".lock")):
        obj = registry.read_registry(target) if target.exists() else {
            "schema": 1, "active_version": None, "history": [], "versions": []}
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", args.version) \
                or any(v["version"] == args.version for v in obj["versions"]):
            raise registry.RegistryError("VERSION_INVALID_OR_EXISTS")
        adapter = registry.relative_path(base, args.adapter_dir)
        model = registry.relative_path(base, args.base_model)
        ref = {"file": args.eval_file, "run_id": args.train_run_id, "status": args.eval_status}
        ref["line_sha256"] = registry.evaluation_hash(base, ref)
        record = {"version": args.version, "adapter_path": adapter.relative_to(base).as_posix(),
                  "adapter_sha256": registry.adapter_sha256(adapter),
                  "base_model": model.relative_to(base).as_posix(),
                  "base_model_weights_sha256": registry.WEIGHTS.digest(model / "model.safetensors"),
                  "train_run_id": args.train_run_id, "eval_ref": ref, "criteria_met": {},
                  "meets_launch_gate": False, "status": "experimental",
                  "registered_at": datetime.datetime.now().astimezone().isoformat(),
                  "note": "实验版本；上线指标须由所有者人工判断"}
        obj["versions"].append(record)
        registry.atomic_write(target, obj)
        return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("adapter_dir", help="相对 ai-layer-dir")
    parser.add_argument("train_run_id")
    parser.add_argument("eval_status")
    parser.add_argument("--ai-layer-dir", required=True)
    parser.add_argument("--base-model", required=True, help="相对 ai-layer-dir")
    parser.add_argument("--eval-file", required=True, help="相对 ai-layer-dir")
    parser.add_argument("--registry")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(register(args), ensure_ascii=False))
        print("登记完成：experimental；active_version 未改变")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print("登记失败：" + (str(exc) if isinstance(exc, registry.RegistryError) else "INVALID_INPUT"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
