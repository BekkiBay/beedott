#!/usr/bin/env bash
# Превью мастера первичной настройки в PNG: app/Tools/setup_preview.sh <папка-для-png>
# Собирает все исходники приложения, кроме main.swift (точка входа — превью).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-$DIR/../build/setup-preview}"
BIN="${TMPDIR:-/tmp}/meetmind-setup-preview"
SOURCES=()
for f in "$DIR"/../Sources/MeetMind/*.swift; do
    [ "$(basename "$f")" = "main.swift" ] || SOURCES+=("$f")
done
swiftc -target arm64-apple-macosx14.4 -o "$BIN" "${SOURCES[@]}" "$DIR/SetupPreview/main.swift"
"$BIN" "$OUT_DIR"
