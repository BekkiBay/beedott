#!/usr/bin/env bash
# app/build.sh — собирает BeeDott.app (бывший MeetMind) голым swiftc (без Xcode-проекта, см.
# spikes/SPIKE_RESULTS.md: swiftc + ad-hoc подпись работают на этой машине).
# Идемпотентен — можно гонять сколько угодно раз подряд.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$APP_DIR/Sources/MeetMind"
BUILD_DIR="$APP_DIR/build"
APP_BUNDLE="$BUILD_DIR/BeeDott.app"
CONTENTS="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS/MacOS"
BIN_NAME="BeeDott"
BUNDLE_ID="com.jvmshid.meetmind"

echo "== BeeDott build =="

mkdir -p "$MACOS_DIR"

shopt -s nullglob
SOURCES=("$SRC_DIR"/*.swift)
shopt -u nullglob
if [ ${#SOURCES[@]} -eq 0 ]; then
    echo "нет .swift файлов в $SRC_DIR" >&2
    exit 1
fi

echo "-- swiftc: ${#SOURCES[@]} файлов -> $MACOS_DIR/$BIN_NAME"
# -target фиксирует LC_BUILD_VERSION minos=14.4 (LSMinimumSystemVersion в Info.plist).
# Без этого swiftc по умолчанию ставит minos = версия текущего Xcode/toolchain
# (напр. 28.0), и `open`/LaunchServices отказывается запускать бандл на системе
# со СТАРШИМ номером сборки, но МЛАДШИМ major (kLSIncompatibleSystemVersionErr,
# -10825) — обнаружено при верификации M3 на macOS 27.0 + Xcode 26.5 toolchain.
#
# БЕЗ -parse-as-library (начиная с Task 4.1-4.2): entry point — Sources/MeetMind/main.swift
# (top-level код для headless --capture-test до запуска GUI). Файл main.swift
# компилируется как top-level entry автоматически по имени; -parse-as-library
# запрещает top-level код везде, включая main.swift, — проверено эмпирически.
swiftc -O \
    -target arm64-apple-macosx14.4 \
    -o "$MACOS_DIR/$BIN_NAME" \
    "${SOURCES[@]}"

echo "-- Info.plist -> $CONTENTS/Info.plist"
cp "$APP_DIR/Info.plist" "$CONTENTS/Info.plist"

# Иконка (app/Tools/app_icon.sh рисует её кодом). Без неё сборка всё равно идёт.
if [ -f "$APP_DIR/Resources/AppIcon.icns" ]; then
    echo "-- AppIcon.icns -> $CONTENTS/Resources"
    mkdir -p "$CONTENTS/Resources"
    cp "$APP_DIR/Resources/AppIcon.icns" "$CONTENTS/Resources/AppIcon.icns"
fi

echo "-- codesign (ad-hoc, identifier=$BUNDLE_ID)"
codesign --force -s - --identifier "$BUNDLE_ID" "$APP_BUNDLE"

echo "-- готово: $APP_BUNDLE"
codesign -dv "$APP_BUNDLE" 2>&1 | sed -n '1,6p'
