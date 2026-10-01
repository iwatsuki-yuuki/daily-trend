#!/bin/zsh
# launchd 登録と trend コマンドのリンクを外す。~/daily-trend/data（過去の記録）は消さない。
UID_=$(id -u)
for name in run trendbar; do
  label="local.dailytrend.$name"
  launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
rm -f "$HOME/.local/bin/trend"
echo "登録を解除しました。フォルダごと消す場合は ~/daily-trend を手動で削除してください。"
