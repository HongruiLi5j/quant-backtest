"""闸门校验子进程：只由 ai_layer 的 python 以 `-I -B` 运行，不加载 mlx，不加载模型。

协议：stdin 收一行 JSON {"doc": ...}，stdout 回一行 JSON。参数 --src 是校验器所在项目的 src 目录。
它只做三件事：导入校验器模块、检查导入了哪些模块、对文档调用 validate_document。
不调用 schema 里的试验登记相关函数，也不调用 trials 里的任何函数。

保护措施（失败即关闭）：
- 导入后 sys.modules 里的 statarb.* 必须属于允许名单，否则报错退出；
- 审计钩子拦截：任何 .jsonl 文件的打开；项目目录内除 src 之外的任何读；项目目录内的任何写。被拦的次数写进输出，网站侧看到就判闸门不可用；
- 不写字节码（-B 加 sys.dont_write_bytecode），否则会在别人的项目目录里生成 __pycache__。
"""
import json
import os
import sys

ALLOWED = {"statarb", "statarb.schema", "statarb.ml", "statarb.trials"}     # 与 src/ai_translator/gate.py 保持一致（测试里有检查）
MAX_IN = 64 * 1024
_WRITE = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def _fail(error):
    sys.stdout.write(json.dumps({"ok": False, "error": error}) + "\n")
    sys.stdout.flush()
    sys.exit(3)


def main():
    if "--src" not in sys.argv or sys.argv.index("--src") + 1 >= len(sys.argv):
        _fail("BAD_ARGS")
    src = os.path.realpath(sys.argv[sys.argv.index("--src") + 1])
    root = os.path.dirname(src)                                   # 校验器项目的根目录（src 的上一级）
    sys.dont_write_bytecode = True
    denied = []

    def inside(p, base):
        return p == base or p.startswith(base + os.sep)

    def hook(event, args):
        if event != "open":
            return
        path, _mode, flags = args
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        p = os.path.realpath(os.fsdecode(path))
        writing = isinstance(flags, int) and bool(flags & _WRITE)
        if p.endswith(".jsonl") or (inside(p, root) and (writing or not inside(p, src))):
            denied.append(1)
            raise PermissionError("blocked by gate")

    sys.addaudithook(hook)
    sys.path.insert(0, src)
    try:
        from statarb import schema
    except BaseException:
        _fail("IMPORT_FAILED")
    mods = sorted(m for m in sys.modules if m == "statarb" or m.startswith("statarb."))
    if not set(mods) <= ALLOWED:
        _fail("IMPORT_ALLOWLIST")
    raw = sys.stdin.read(MAX_IN + 1)
    if len(raw) > MAX_IN:
        _fail("INPUT_TOO_LARGE")
    try:
        doc = json.loads(raw)["doc"]
    except Exception:
        _fail("BAD_INPUT")
    issues = [{"code": str(i.get("code")), "path": str(i.get("path")), "message": str(i.get("message"))}
              for i in schema.validate_document(doc)]
    sys.stdout.write(json.dumps({"ok": True, "issues": issues, "reject_codes": list(schema.REJECT_CODES),
                                 "modules": mods, "denied": len(denied)}) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
