#!/usr/bin/env bash
# Превью островка у чёлки в PNG: app/Tools/island_preview.sh <папка-для-png>
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${1:-$DIR/../build/island-preview}"
mkdir -p "$OUT_DIR"
BIN="${TMPDIR:-/tmp}/meetmind-island-preview"
swiftc -target arm64-apple-macosx14.4 -o "$BIN" \
    "$DIR/../Sources/MeetMind/IslandView.swift" "$DIR/../Sources/MeetMind/IslandModel.swift" "$DIR/IslandPreview/main.swift"
"$BIN" "$OUT_DIR"
