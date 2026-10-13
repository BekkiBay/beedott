#!/usr/bin/env bash
# app/package.sh — собрать BeeDott.app и упаковать в zip для коллег (docs/INSTALL.md).
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$APP_DIR/build.sh"
OUT="$APP_DIR/build/BeeDott.zip"
rm -f "$OUT"
ditto -c -k --keepParent "$APP_DIR/build/BeeDott.app" "$OUT"
echo "готово: $OUT ($(du -h "$OUT" | cut -f1))"
