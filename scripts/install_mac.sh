#!/bin/bash
# 一键安装：虚拟环境 + 首次下载数据 + 两个开机自启的后台任务
#   1) 每日任务：每 15 分钟检查一次（开机/唤醒后也会马上检查），有已收盘但没更新的市场、
#      或者上次没下载完的数据，就联网更新；没联网就等下次；都是最新的就直接退出
#   2) 网页服务：开机自动启动，只允许本机访问
# 用法：bash scripts/install_mac.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LA="$HOME/Library/LaunchAgents"
PY="$ROOT/.venv/bin/python"
UID_="$(id -u)"

echo "==> 项目位置：$ROOT"

# ---- 1. 虚拟环境
if [ ! -x "$PY" ]; then
  echo "==> 创建虚拟环境"
  python3 -m venv .venv
fi
echo "==> 安装依赖（第一次需要一两分钟）"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements.txt

# ---- 2. 权限：数据、日志、令牌只有你自己能读
mkdir -p data/daily logs .secret
chmod 700 data data/daily logs .secret
"$PY" -c "import app" >/dev/null      # 生成访问令牌（.secret/token，权限 600）
chmod 600 .secret/token

# ---- 3. 首次下载数据（加 --skip-download 跳过）
if [ "${1:-}" != "--skip-download" ]; then
  echo "==> 首次下载两个市场的数据（几分钟）"
  "$PY" daily.py || echo "   ⚠️ 有部分数据下载失败，稍后在网页里点「立即更新」重试"
fi

# ---- 4. 后台任务
mkdir -p "$LA"
cat > "$LA/com.quantdaily.update.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.quantdaily.update</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$ROOT/daily.py</string><string>--auto</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>$ROOT/logs/launchd_update.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/launchd_update.log</string>
  <key>Umask</key><integer>63</integer>
</dict></plist>
EOF

cat > "$LA/com.quantdaily.server.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.quantdaily.server</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$ROOT/app.py</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/logs/launchd_server.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/launchd_server.log</string>
  <key>Umask</key><integer>63</integer>
</dict></plist>
EOF

for job in update server; do
  launchctl bootout "gui/$UID_/com.quantdaily.$job" 2>/dev/null || true
  # bootout 是异步的，马上 bootstrap 会报 "5: Input/output error"，所以等一下并重试
  loaded=0
  for i in 1 2 3 4 5; do
    sleep 1
    if launchctl bootstrap "gui/$UID_" "$LA/com.quantdaily.$job.plist" 2>/dev/null; then loaded=1; break; fi
    if launchctl print "gui/$UID_/com.quantdaily.$job" >/dev/null 2>&1; then loaded=1; break; fi
  done
  [ "$loaded" = 1 ] || { echo "⚠️  后台任务 $job 加载失败"; exit 1; }
done
echo "==> 后台任务已安装"

# ---- 5. 等网页服务启动（第一次导入库比较慢，最多等 40 秒）
echo "==> 等待网页服务启动"
ok=0
for i in $(seq 1 40); do
  if curl -s -o /dev/null "http://127.0.0.1:8765/"; then ok=1; break; fi
  sleep 1
done
if [ "$ok" != 1 ]; then
  if grep -qi "Operation not permitted\|PermissionError" logs/launchd_server.log 2>/dev/null; then
    REAL="$("$PY" -c 'import sys,os;print(os.path.realpath(sys.executable))')"
    echo
    echo "⚠️  macOS 拦截了后台程序访问「文稿」文件夹。需要授权一次："
    echo "   系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 点 + → 按 Cmd+Shift+G 输入下面这个路径 → 打开并打勾："
    echo "   $REAL"
    echo "   授权后重新运行：bash scripts/install_mac.sh"
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" || true
  else
    echo "⚠️  网页服务还没启动成功，最后几行日志："
    tail -5 logs/launchd_server.log 2>/dev/null
  fi
  exit 1
fi
echo
echo "✅ 完成！打开仪表盘：bash scripts/open_dashboard.sh"
echo "   每 15 分钟自动检查：收盘后自动更新，下载中断（断网/睡眠）会在联网后自动续传。"
