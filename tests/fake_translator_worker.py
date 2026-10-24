"""S3 的标准库假 worker。仅模拟协议和进程生命周期，绝不导入模型。"""
import os

os.setpriority(os.PRIO_PROCESS, 0, 10)

import argparse
import json
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--mode", default="normal")
parser.add_argument("--pid-log", required=True)
parser.add_argument("--schema-sha256", default="ef6322a00fc61933e34ba95e53c5c1afd3076b6e078a22e46c53e0f51f79860c")
parser.add_argument("--document")
parser.add_argument("--model-dir")
parser.add_argument("--adapter-dir")
args = parser.parse_args()
stubborn = args.mode in ("stubborn", "spawn_child_stubborn")
parent_pid = os.getppid()
children = []
incoming = queue.Queue()
stopping = threading.Lock()


def record_pid(pid):
    with open(args.pid_log, "a", encoding="utf-8") as f:
        f.write(str(pid) + "\n")


def finish(*_):
    """普通模式主动回收；顽固孙进程模式刻意只由监督者清理进程组。"""
    if not stopping.acquire(blocking=False):
        return
    if args.mode != "spawn_child_stubborn":
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()
    os._exit(0)


def watch_parent():
    while True:
        time.sleep(0.2)
        if os.getppid() != parent_pid:
            finish()


def read_input():
    for line in sys.stdin.buffer:
        try:
            obj = json.loads(line)
        except ValueError:
            finish()
            return
        if obj.get("op") == "shutdown":
            if not stubborn:
                finish()
            continue
        incoming.put(obj)
    if not stubborn:
        finish()


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


record_pid(os.getpid())
signal.signal(signal.SIGTERM, signal.SIG_IGN if stubborn else finish)
threading.Thread(target=watch_parent, daemon=True).start()
threading.Thread(target=read_input, daemon=True).start()

if args.mode == "load_hang":
    while True:
        time.sleep(1)
if args.mode == "bad_ready":
    emit({"op": "not_ready"})
else:
    emit({"op": "ready", "model_dir": str(Path(args.model_dir).resolve()) if args.model_dir else str(Path.cwd() / "models" / "fake"),
          "adapter_dir": str(Path(args.adapter_dir).resolve()) if args.adapter_dir else str(Path.cwd() / "adapters" / "fake"), "load_s": 0,
          "schema_sha256": args.schema_sha256})

while True:
    request = incoming.get()
    if request.get("op") != "translate":
        finish()
    if args.mode == "crash":
        os._exit(7)
    if args.mode in ("hang", "stubborn"):
        while True:
            time.sleep(1)
    if args.mode == "slow":
        time.sleep(0.6)
    if args.mode == "non_json":
        print("this is not JSON", flush=True)
        continue
    if args.mode == "long_line":
        sys.stdout.buffer.write(b"x" * (64 * 1024 + 1) + b"\n")
        sys.stdout.buffer.flush()
        continue
    if args.mode == "partial":
        sys.stdout.write('{"op":')
        sys.stdout.flush()
        continue
    if args.mode == "invalid_utf8":
        sys.stdout.buffer.write(b"\xff\n")
        sys.stdout.buffer.flush()
        continue
    child_pid = None
    if args.mode in ("spawn_child", "spawn_child_stubborn"):
        # 顽固模式的两个进程均忽略 TERM；孙进程不检查父进程，也不因 stdin EOF 退出。
        # ready 经专用管道确认，避免监督者开始回收时孙进程尚未安装信号处理。
        child_code = "import signal, time; "
        if args.mode == "spawn_child_stubborn":
            child_code += "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        child_code += "print('ready', flush=True); time.sleep(600)"
        child = subprocess.Popen([sys.executable, "-I", "-B", "-c", child_code],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, close_fds=True)
        children.append(child)
        child_pid = child.pid
        record_pid(child_pid)
        assert child.stdout.readline() == b"ready\n"
        child.stdout.close()
    doc = {"idea": request["idea"], "pid": os.getpid(), "child_pid": child_pid,
           "env": dict(os.environ), "nice": os.getpriority(os.PRIO_PROCESS, 0),
           "request_keys": sorted(request), "pgid": os.getpgrp(),
           "imports": sorted(sys.modules)}
    if args.document:
        doc = json.loads(Path(args.document).read_text(encoding="utf-8"))
    reply = {"op": "result", "id": request["id"], "ok": True,
             "obj": doc, "raw_output": json.dumps(doc, ensure_ascii=False),
             "parse_mode": "strict", "gen_ms": 0}
    if args.mode == "wrong_id":
        reply["id"] = "not-the-request"
    if args.mode == "bad_fields":
        reply["ok"] = "true"
    if args.mode == "bad_object":
        reply["obj"] = [doc]
    if args.mode == "lenient":
        reply["parse_mode"] = "lenient"
    if args.mode == "bad_output":
        reply.update(obj=None, raw_output="no object", parse_mode="none")
    if args.mode == "gen_failed":
        reply = {"op": "result", "id": request["id"], "ok": False, "error": "GEN_FAILED"}
    if args.mode == "duplicate_key":
        sys.stdout.write('{"op":"result","op":"result"}\n')
        sys.stdout.flush()
    else:
        emit(reply)
