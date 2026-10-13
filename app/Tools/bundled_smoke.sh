#!/usr/bin/env bash
# Проверка встроенного режима без GUI: тот же uv sync, что делает мастер, в
# отдельный app-dir; status, link-ffmpeg, install-model (модель из общего кэша
# HF), запуск демона и health. В бандл ничего не должно записаться.
#   app/Tools/bundled_smoke.sh [путь к BeeDott.app]   (по умолчанию app/build/BeeDott.app)
set -euo pipefail
APP="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/build/BeeDott.app}"
RES="$APP/Contents/Resources"
WORK="$(mktemp -d)"
export MEETMIND_APPDIR="$WORK/app"
R="$MEETMIND_APPDIR/runtime"
# Как у RuntimeManager: байткод — в runtime/, исходники — из бандла.
export PYTHONPATH="$RES/backend" PYTHONPYCACHEPREFIX="$R/pycache"
mkdir -p "$R/bin"
touch "$WORK/marker"
trap '[ -n "${DPID:-}" ] && kill "$DPID" 2>/dev/null; echo "work dir: $WORK"' EXIT

echo "== uv sync (чистый Python и пакеты)"
cp "$RES/bin/uv" "$R/bin/uv"
UV_PROJECT_ENVIRONMENT="$R/venv" UV_PYTHON_INSTALL_DIR="$R/python" UV_CACHE_DIR="$R/uv-cache" \
UV_PYTHON_PREFERENCE=only-managed "$R/bin/uv" sync --frozen --no-dev --python 3.11 --project "$RES/backend"

setup() {
    (cd "$R" && PATH="$R/bin:/usr/bin:/bin" "$R/venv/bin/python" -m daemon.setup "$@")
}
echo "== link-ffmpeg";   setup link-ffmpeg
"$R/bin/ffmpeg" -hide_banner -version | head -1
echo "== status";        setup status
echo "== install-model"; setup install-model | tail -2

echo "== демон"
(cd "$R" && PATH="$R/bin:/usr/bin:/bin" "$R/venv/bin/python" "$RES/backend/main.py" >"$WORK/daemon.log" 2>&1) &
DPID=$!
for _ in $(seq 1 30); do
    if (cd "$R" && "$R/venv/bin/python" -m daemon.cli health 2>/dev/null); then
        break
    fi
    sleep 1
done
(cd "$R" && "$R/venv/bin/python" -m daemon.cli health)

echo "== в бандл ничего не записано"
CHANGED=$(find "$APP" -newer "$WORK/marker" | head -5)
if [ -n "$CHANGED" ]; then echo "ЗАПИСАНО В БАНДЛ:"; echo "$CHANGED"; exit 1; fi
echo "OK — bundled-режим работает"
