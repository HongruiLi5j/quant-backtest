"""pytest 配置与「一条命令跑全部测试」脚本的静态检查（E08）。

用法：.venv/bin/python -m pytest tests/test_pytest_config_e08.py

只读仓库根目录的 pytest.ini 和 scripts/run_all_tests.sh，不读 data/，不联网。
最后一个用例会起一个子进程跑 pytest --collect-only（只收集、不执行测试），
并跳过 tests/，所以只验证「根目录的演示脚本不再被 import」。
"""
import configparser
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INI = ROOT / "pytest.ini"
SCRIPT = ROOT / "scripts" / "run_all_tests.sh"


def _ini():
    cp = configparser.ConfigParser()
    cp.read(INI, encoding="utf-8")
    return cp["pytest"]


class PytestIniTests(unittest.TestCase):
    def test_testpaths_is_tests_only(self):
        self.assertEqual(_ini()["testpaths"].strip(), "tests")

    def test_norecursedirs_keeps_defaults_and_skips_data_dirs(self):
        # 设置 norecursedirs 会覆盖 pytest 默认值，所以默认的 .* 和 venv 必须还在
        dirs = _ini()["norecursedirs"].split()
        for need in (".*", "venv", "build", "dist", "node_modules", "data", "handoff", "logs", "_to_delete"):
            self.assertIn(need, dirs)

    def test_demo_script_is_ignored(self):
        self.assertIn("--ignore=test_talib_demo.py", _ini()["addopts"].split())

    def test_demo_script_still_exists_at_root(self):
        # 如果演示脚本被移走/改名，这条提醒你把 pytest.ini 里对应的 --ignore 也清掉
        self.assertTrue((ROOT / "test_talib_demo.py").exists())

    def test_collecting_root_does_not_import_demo_script(self):
        # 演示脚本在 import 时就会运行并打印 "MACD 列名"；被正确忽略时不应出现
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-s", "-p", "no:cacheprovider",
             ".", "--ignore=tests"],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        self.assertNotIn("MACD", out)
        self.assertNotIn("pandas_ta", out)
        self.assertIn(r.returncode, (0, 5))   # 5 = 没收集到任何测试，正是我们想要的


class RunAllTestsScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_four_steps_present_in_order(self):
        # 只看 run_step 调用行（文件头注释里也提到了这些名字）
        calls = [x for x in self.text.splitlines() if x.startswith("run_step ")]
        marks = ['-m pytest -q', 'tests/test_strategies.py', 'tests/test_basic.py', 'tests/test_engines.py']
        self.assertEqual(len(calls), len(marks))
        for call, mark in zip(calls, marks):
            self.assertIn(mark, call)

    def test_engines_step_sets_pythonpath(self):
        line = next(x for x in self.text.splitlines() if "tests/test_engines.py" in x and x.startswith("run_step"))
        self.assertIn("PYTHONPATH=.engines_lib", line)

    def test_uses_project_venv_python_not_bare_python(self):
        self.assertIn('.venv/bin/python', self.text)
        for x in self.text.splitlines():
            code = x.split("#")[0]
            self.assertNotIn("python3 ", code)

    def test_failure_does_not_stop_later_steps_and_sets_exit_code(self):
        self.assertNotIn("set -e", self.text)
        self.assertIn("exit 1", self.text)


if __name__ == "__main__":
    unittest.main()
