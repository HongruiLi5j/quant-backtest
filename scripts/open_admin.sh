#!/bin/bash
# 用管理令牌打开后台 /admin（令牌只在本机 .secret/admin_token，和主页面令牌不是同一个；会话 8 小时有效）
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOKEN="$(cat "$ROOT/.secret/admin_token" 2>/dev/null)"
if [ -z "$TOKEN" ]; then
  echo "还没有管理令牌：先启动一次网页服务（bash scripts/open_dashboard.sh），它会自动生成 .secret/admin_token"; exit 1
fi
if ! curl -s -o /dev/null "http://127.0.0.1:8765/admin"; then
  if [ -f "$HOME/Library/LaunchAgents/com.quantdaily.server.plist" ]; then
    echo "网页服务没响应，正在重启后台服务…"
    launchctl kickstart -k "gui/$(id -u)/com.quantdaily.server" 2>/dev/null
  else
    echo "后台服务没安装，先临时启动（关掉这个终端窗口就会停止；建议运行 bash scripts/install_mac.sh）"
    "$ROOT/.venv/bin/python" "$ROOT/app.py" &
  fi
  for i in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:8765/admin" && break; sleep 1; done
fi
open "http://127.0.0.1:8765/admin?t=$TOKEN"
