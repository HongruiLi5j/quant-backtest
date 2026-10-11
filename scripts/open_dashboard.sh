#!/bin/bash
# 用带访问令牌的地址打开仪表盘（令牌只在本机，第一次打开后浏览器会记住）
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOKEN="$(cat "$ROOT/.secret/token" 2>/dev/null)"
if [ -z "$TOKEN" ]; then
  echo "还没有令牌：先运行 bash scripts/install_mac.sh"; exit 1
fi
if ! curl -s -o /dev/null "http://127.0.0.1:8765/"; then
  if [ -f "$HOME/Library/LaunchAgents/com.quantdaily.server.plist" ]; then
    echo "网页服务没响应，正在重启后台服务…"
    launchctl kickstart -k "gui/$(id -u)/com.quantdaily.server" 2>/dev/null
  else
    echo "后台服务没安装，先临时启动（关掉这个终端窗口就会停止；建议运行 bash scripts/install_mac.sh）"
    "$ROOT/.venv/bin/python" "$ROOT/app.py" &
  fi
  for i in $(seq 1 30); do curl -s -o /dev/null "http://127.0.0.1:8765/" && break; sleep 1; done
fi
open "http://127.0.0.1:8765/?t=$TOKEN"
