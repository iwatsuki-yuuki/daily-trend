#!/bin/zsh
# daily-trend のインストール。何度実行してもよい（上書き）。
set -euo pipefail
ROOT="$HOME/daily-trend"
LA="$HOME/Library/LaunchAgents"
UID_=$(id -u)

if [[ ! -f "$ROOT/install.sh" ]]; then
  echo "エラー: $ROOT がありません。先に git clone https://github.com/iwatsuki-yuuki/daily-trend.git ~/daily-trend を実行してください。" >&2
  exit 1
fi
if ! command -v uv >/dev/null; then
  echo "エラー: uv が見つかりません。brew install uv で入れてください。" >&2
  exit 1
fi
# /usr/bin/swiftc は Command Line Tools が無くても存在するので、xcode-select で確かめる
if ! xcode-select -p >/dev/null 2>&1; then
  echo "エラー: Xcode Command Line Tools がありません。xcode-select --install で入れてください。" >&2
  exit 1
fi

echo "0) Python の環境（.venv）を用意"
# python が動かない .venv（作りかけ・Python 本体を消した等）は作り直す
"$ROOT/.venv/bin/python" -c "" 2>/dev/null || uv venv --clear --python 3.13 "$ROOT/.venv"
uv pip install --quiet --python "$ROOT/.venv/bin/python" -r "$ROOT/requirements.txt"

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
echo "完了。パネルを出すキーは config.toml の [app] hotkey（初期設定は option+t = ⌥T）"
echo "最初の1件は trend run か、メニューバーの「今すぐ生成／再生成」で作る（1分ほど）"
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
  echo "注意: ~/.local/bin に PATH が通っていないので、trend コマンドはまだ使えません。~/.zshrc に次の行を足してください:"
  echo '  export PATH="$HOME/.local/bin:$PATH"'
fi
