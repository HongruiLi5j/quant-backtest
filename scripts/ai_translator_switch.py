"""所有者的单一开关入口；复用 ConfigLoader 的保存与审计。"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ai_translator import config, paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("on", "off", "status"))
    parser.add_argument("--root", default=str(paths.ROOT))
    parser.add_argument("--ai-layer-dir")
    parser.add_argument("--statarb-src-dir")
    parser.add_argument("--backend", choices=("local_lora", "api_llm"), default="local_lora")
    args = parser.parse_args(argv)
    root = paths.guard(args.root)
    from src import admin
    admin.set_root(root)
    loader = config.ConfigLoader(root, admin.audit)
    state = loader.load()
    if args.action != "status":
        if state.problem and args.action == "on":
            print("CONFIG_INVALID：请先检查配置", file=sys.stderr)
            return 1
        cfg = state.cfg
        cfg.update(enabled=args.action == "on", backend=args.backend)
        for key in ("ai_layer_dir", "statarb_src_dir"):
            value = getattr(args, key)
            if value:
                cfg[key] = str(paths.guard(value))
        state = loader.save(cfg, via="admin")
    result = {"enabled": state.enabled, "backend": state.backend, "problem": state.problem}
    if state.backend == "api_llm":
        from src.ai_translator.api_llm import UsageStore
        from src.ai_translator.backend import TranslatorError
        result["api_key_env_set"] = "yes" if os.environ.get(state.cfg["api_llm"]["api_key_env"]) else "no"
        try:
            result["budget"] = UsageStore(state.cfg, root).snapshot()
        except TranslatorError as exc:
            result.update(budget=None, problem=exc.code)
    print(json.dumps(result, ensure_ascii=False))
    if args.action == "off":
        print("已关闭：运行中的翻译进程会立即回收（监督者检测间隔最多 0.2 秒）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
