#!/bin/zsh
# TrendBar.app をビルドする。Xcode 不要（Command Line Tools の swiftc だけ使う）。
# 出力: <repo>/build/TrendBar.app（ad-hoc 署名）
set -euo pipefail

APP_DIR="${0:A:h}"
ROOT="${APP_DIR:h}"
BUILD="$ROOT/build"
APP="$BUILD/TrendBar.app"
TMP_BIN="$BUILD/TrendBar.bin"

mkdir -p "$BUILD"

echo "swiftc: $(swiftc --version 2>&1 | head -1)"
swiftc -O -swift-version 5 -parse-as-library \
  -target arm64-apple-macos13.0 \
  -framework AppKit -framework SwiftUI -framework Carbon \
  -o "$TMP_BIN" \
  "$APP_DIR"/*.swift

# 既存の .app を置き換える（コンパイルが通ってから消す）
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
mv "$TMP_BIN" "$APP/Contents/MacOS/TrendBar"
cp "$APP_DIR/Info.plist" "$APP/Contents/Info.plist"
plutil -lint "$APP/Contents/Info.plist" >/dev/null

codesign --force --sign - "$APP"
codesign --verify "$APP"

echo "built: $APP"
