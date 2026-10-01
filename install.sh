#!/bin/zsh
# daily-trend のインストール。何度実行してもよい（上書き）。
set -euo pipefail
ROOT="$HOME/daily-trend"
LA="$HOME/Library/LaunchAgents"
UID_=$(id -u)

echo "1) trend コマンドを ~/.local/bin に置く"
mkdir -p "$HOME/.local/bin"
ln -sf "$ROOT/bin/trend" "$HOME/.local/bin/trend"

echo "2) TrendBar.app をビルド"
"$ROOT/app/build.sh"

echo "3) launchd に登録（毎日の生成 / TrendBar の常駐）"
mkdir -p "$LA" "$ROOT/data/logs"
for name in run trendbar; do
  label="local.dailytrend.$name"
  sed "s#__ROOT__#$ROOT#g" "$ROOT/launchd/$label.plist" > "$LA/$label.plist"
  launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_" "$LA/$label.plist"
done
echo "完了。ホットキー: config.toml の [app] hotkey（既定 ⌃⌥T）"
