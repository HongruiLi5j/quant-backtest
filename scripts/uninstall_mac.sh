#!/bin/bash
# 卸载后台任务（不会删除数据和代码）
UID_="$(id -u)"
for job in update server; do
  launchctl bootout "gui/$UID_/com.quantdaily.$job" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/com.quantdaily.$job.plist"
done
echo "已停止并移除自动更新和网页服务。"
