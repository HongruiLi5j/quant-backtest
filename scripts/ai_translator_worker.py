"""本地推理进程：严格 JSON 行协议；标准输出仅用于协议。"""
import os

os.setpriority(os.PRIO_PROCESS, 0, 10)

import argparse
import contextlib
import hashlib
import importlib.util
import json
import queue
import sys
import threading
import time
from pathlib import Path

MAX_LINE = 64 * 1024
ALLOWED = {"statarb", "statarb.schema", "statarb.ml", "statarb.trials"}
sys.dont_write_bytecode = True


def strict_object(data):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate key")
            obj[key] = value
        return obj

    def constant(_):
        raise ValueError("nonfinite")

    obj = json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(obj, dict):
        raise ValueError("object required")
    json.dumps(obj, allow_nan=False)
    return obj


def emit(obj):
    line = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    if len(line) > MAX_LINE:
        raise ValueError("line too long")
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ai-layer-dir", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--fake-model-module")
    args = parser.parse_args()
    if args.fake_model_module and os.environ.get("QB_TESTING") != "1":
        parser.error("fake model requires QB_TESTING=1")
    parent_pid = os.getppid()
    incoming = queue.Queue(maxsize=1)

    def watch_parent():
        while True:
            time.sleep(0.2)
            if parent_pid <= 1 or os.getppid() != parent_pid:
                os._exit(0)

    def read_input():
        while True:
            line = sys.stdin.buffer.readline(MAX_LINE + 1)
            if not line:
                os._exit(0)
            try:
                if len(line) > MAX_LINE or not line.endswith(b"\n"):
                    raise ValueError("line too long")
                obj = strict_object(line.decode("utf-8"))
                if obj == {"op": "shutdown"}:
                    os._exit(0)
                if set(obj) != {"op", "id", "idea"} or obj["op"] != "translate" \
                        or not isinstance(obj["id"], str) or not isinstance(obj["idea"], str):
                    raise ValueError("invalid request")
                incoming.put_nowait(obj)
            except Exception:
                os._exit(3)

    threading.Thread(target=watch_parent, daemon=True).start()
    threading.Thread(target=read_input, daemon=True).start()
    started = time.monotonic()
    base = Path(args.ai_layer_dir).expanduser().resolve()
    model_dir, adapter_dir = Path(args.model).resolve(), Path(args.adapter).resolve()
    if any(base not in p.parents for p in (model_dir, adapter_dir)):
        raise ValueError("invalid model path")

    # 只准读源码与模型资产；目录边界和扩展名拦截数据材料与任何外部项目写入。
    allowed_dirs = [base / "models", base / "adapters", base / ".venv"]
    denied = []
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def inside(p, directory):
        return p == directory or directory in p.parents

    def hook(event, values):
        if event == "import" and (values[0].split(".")[0] in {"train", "evaluate", "gen_data"}
                                  or values[0].startswith("gen_")):
            denied.append(1)
            raise PermissionError("import blocked")
        if event != "open" or not isinstance(values[0], (str, bytes, os.PathLike)):
            return
        p = Path(os.fsdecode(values[0])).resolve()
        flags = values[2]
        writing = isinstance(flags, int) and bool(flags & write_flags)
        code = p.suffix in {".py", ".pyc", ".so", ".dylib"}
        blocked = p.suffix == ".jsonl" or writing
        if inside(p, base) and not (code and p.parent in (base, base / "__pycache__")) \
                and not any(inside(p, d) for d in allowed_dirs):
            blocked = True
        if blocked:
            denied.append(1)
            raise PermissionError("file access blocked")

    sys.addaudithook(hook)

    def check_imports():
        modules = {m for m in sys.modules if m == "statarb" or m.startswith("statarb.")}
        if denied or not modules <= ALLOWED or {"train", "evaluate", "gen_data"} & sys.modules.keys():
            raise ValueError("IMPORT_ALLOWLIST")

    try:
        with contextlib.redirect_stdout(sys.stderr):
            sys.path.insert(0, str(base))
            from ai_lib import SYSTEM_PROMPT, make_messages
            check_imports()
            if not isinstance(SYSTEM_PROMPT, str):
                raise ValueError("invalid prompt")
            if args.fake_model_module:
                spec = importlib.util.spec_from_file_location("qb_fake_model", args.fake_model_module)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                load, generate, make_sampler = module.load, module.generate, module.make_sampler
            else:
                from mlx_lm import load, generate
                from mlx_lm.sample_utils import make_sampler
            model, tok = load(str(model_dir), adapter_path=str(adapter_dir))
            check_imports()
            schema_file = Path(sys.modules["statarb.schema"].__file__)
            schema_hash = hashlib.sha256(schema_file.read_bytes()).hexdigest()
        emit({"op": "ready", "model_dir": str(model_dir), "adapter_dir": str(adapter_dir),
              "load_s": time.monotonic() - started, "schema_sha256": schema_hash})
        while True:
            request = incoming.get()
            started = time.monotonic()
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    prompt = tok.apply_chat_template(make_messages(request["idea"]),
                                                     add_generation_prompt=True, tokenize=False)
                    text = generate(model, tok, prompt=prompt, max_tokens=420,
                                    sampler=make_sampler(temp=0.0), verbose=False)
                    check_imports()
                try:
                    obj = strict_object(text)
                    mode = "strict"
                except (ValueError, TypeError):
                    obj, mode = None, "none"
                emit({"op": "result", "id": request["id"], "ok": True, "raw_output": text,
                      "parse_mode": mode, "obj": obj, "gen_ms": int((time.monotonic() - started) * 1000)})
            except Exception:
                emit({"op": "result", "id": request["id"], "ok": False, "error": "GEN_FAILED"})
    except Exception:
        emit({"op": "fatal", "error": "IMPORT_OR_LOAD_FAILED"})
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
