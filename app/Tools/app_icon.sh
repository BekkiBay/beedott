#!/usr/bin/env bash
# Иконка приложения: app/Tools/app_icon.sh [вариант]  (по умолчанию islandLetter)
#   → app/Resources/AppIcon.icns (подхватывает app/build.sh)
# Лист сравнения вариантов: app/Tools/app_icon.sh --sheet <папка>
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${TMPDIR:-/tmp}/meetmind-app-icon"
swiftc -target arm64-apple-macosx14.4 -o "$BIN" "$DIR/AppIcon/main.swift"

if [ "${1:-}" = "--sheet" ]; then
    # app/Tools/app_icon.sh --sheet <папка> [вариант1,вариант2,…]
    "$BIN" sheet "${2:-$DIR/../build/icon-sheet}" ${3:+"$3"}
    exit 0
fi

STYLE="${1:-islandLetter}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
"$BIN" iconset "$STYLE" "$WORK/AppIcon.iconset"
mkdir -p "$DIR/../Resources"
iconutil -c icns "$WORK/AppIcon.iconset" -o "$DIR/../Resources/AppIcon.icns"
echo "готово: $DIR/../Resources/AppIcon.icns ($STYLE)"
