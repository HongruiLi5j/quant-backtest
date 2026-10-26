"""测试引导层（T023）：导入实验室脚本时，拦住对真实 data/ 的任何读取，并记下尝试读取的路径。

背景：scripts/strategy_lab*.py 会导入 config，config 在模块初始化时会尝试读 data/auto_params.json（读不到就用默认值）。
测试规则是不得读真实 data/，所以导入期间把 Path.read_text 和 open 换成"碰到 data/ 下的文件就报 FileNotFoundError"的版本。
config 自己捕获这个异常并用默认值，行为与文件不存在时一致。
"""
import builtins
import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BLOCKED = []          # 被拦下的读取尝试（相对项目根的路径）


def _under_data(p):
    try:
        q = pathlib.Path(p).resolve()
        return DATA == q or DATA in q.parents
    except Exception:
        return False


def import_guarded(*names):
    """在拦截真实 data/ 读取的状态下导入这些模块，返回模块列表。"""
    real_rt, real_open = pathlib.Path.read_text, builtins.open

    def rt(self, *a, **k):
        if _under_data(self):
            BLOCKED.append(str(pathlib.Path(self).resolve().relative_to(ROOT)))
            raise FileNotFoundError(str(self))
        return real_rt(self, *a, **k)

    def op(file, *a, **k):
        if isinstance(file, (str, pathlib.Path)) and _under_data(file):
            BLOCKED.append(str(pathlib.Path(file).resolve().relative_to(ROOT)))
            raise FileNotFoundError(str(file))
        return real_open(file, *a, **k)

    pathlib.Path.read_text, builtins.open = rt, op
    try:
        return [importlib.import_module(n) for n in names]
    finally:
        pathlib.Path.read_text, builtins.open = real_rt, real_open
