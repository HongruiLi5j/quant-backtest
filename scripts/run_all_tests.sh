#!/bin/bash
# 一条命令跑全部测试：bash scripts/run_all_tests.sh
# 依次跑：pytest（tests/ 下全部）、tests/test_strategies.py、tests/test_basic.py、
# tests/test_engines.py（带 PYTHONPATH=.engines_lib）。某一步失败不会中断后面的步骤，
# 最后汇总一行；有任何一步失败，整个脚本退出码为 1。
# 脚本式测试（test_engines.py）缺本地日线时会"跳过"并以 0 退出，这里把跳过数单独列出，不当成真正通过。
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "找不到 $PY：先建好项目的 .venv 再运行"; exit 2
fi
cd "$ROOT" || exit 2

LOGDIR="$(mktemp -d "${TMPDIR:-/tmp}/run_all_tests.XXXXXX")"
NAMES=()
RESULTS=()
FAILED=0

# run_step <名字> <命令...>：实时显示输出，同时存一份日志，用来数 SKIP 行
run_step() {
  local name="$1"; shift
  local log="$LOGDIR/${#NAMES[@]}.log"
  echo "===== $name ====="
  "$@" 2>&1 | tee "$log"
  local code=${PIPESTATUS[0]}
  local skips
  skips=$(grep -c '^SKIP' "$log")
  NAMES+=("$name")
  if [ "$code" -ne 0 ]; then
    RESULTS+=("失败(退出码 $code)"); FAILED=$((FAILED + 1))
  elif [ "$skips" -gt 0 ]; then
    RESULTS+=("通过(跳过 $skips 项)")
  else
    RESULTS+=("通过")
  fi
  echo
}

run_step "pytest" "$PY" -m pytest -q
run_step "tests/test_strategies.py" "$PY" tests/test_strategies.py
run_step "tests/test_basic.py" "$PY" tests/test_basic.py
run_step "tests/test_engines.py" env PYTHONPATH=.engines_lib "$PY" tests/test_engines.py

echo "===== 汇总 ====="
SUMMARY=""
for i in "${!NAMES[@]}"; do
  SUMMARY+="${NAMES[$i]}：${RESULTS[$i]}；"
done
if [ "$FAILED" -eq 0 ]; then
  echo "全部 ${#NAMES[@]} 步通过 —— ${SUMMARY%；}"
  exit 0
fi
echo "有 $FAILED/${#NAMES[@]} 步失败 —— ${SUMMARY%；}（各步日志在 $LOGDIR）"
exit 1
