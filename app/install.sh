#!/usr/bin/env bash
# app/install.sh — собрать BeeDott.app (бывший MeetMind) и поставить в /Applications.
#
# Что делает:
#   1) не трогает идущую запись/обработку (FORCE=1 — всё равно);
#   2) собирает приложение (app/build.sh, иконка — app/Tools/app_icon.sh);
#   3) закрывает запущенное приложение (демон оно гасит само);
#   4) запоминает путь к репозиторию (repoPath): из /Applications приложение
#      не может вывести его из своего места, а демон живёт в репо;
#   5) ставит /Applications/BeeDott.app, убирает старый MeetMind.app и сборочную
#      копию (чтобы Spotlight и автозапуск не находили две), перерегистрирует
#      автозапуск;
#   6) запускает установленное приложение.
#
# Данные и идентификатор не трогаются: ~/Library/Application Support/MeetMind
# (БД, записи), ~/MeetMind (заметки), ~/Library/Logs/MeetMind, .env в репо,
# bundle id com.jvmshid.meetmind (настройки и разрешения macOS).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$APP_DIR/.." && pwd)"
DEST="/Applications/BeeDott.app"
OLD_DEST="/Applications/MeetMind.app"
BUNDLE_ID="com.jvmshid.meetmind"
DB="$HOME/Library/Application Support/MeetMind/meetmind.db"

say() { echo "== $1"; }

if [ -f "$DB" ] && [ "${FORCE:-0}" != "1" ]; then
    busy=$(sqlite3 -readonly "$DB" "SELECT count(*) FROM sessions WHERE state IN
        ('RECORDING','RECORDED','TRANSCRIBING','SUMMARIZING','DELIVERING')")
    if [ "$busy" != "0" ]; then
        echo "Идёт запись или обработка встречи ($busy) — установка прервёт её."
        echo "Повтори позже или запусти с FORCE=1."
        exit 1
    fi
fi

say "сборка"
[ -f "$APP_DIR/Resources/AppIcon.icns" ] || "$APP_DIR/Tools/app_icon.sh"
"$APP_DIR/build.sh"

running() { pgrep -x BeeDott >/dev/null || pgrep -x MeetMind >/dev/null; }
say "закрываю запущенное приложение"
if running; then
    osascript -e "quit app id \"$BUNDLE_ID\"" >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do running || break; sleep 0.5; done
    if running; then
        echo "не закрылось само — завершаю"
        pkill -x BeeDott || true
        pkill -x MeetMind || true
        sleep 1
    fi
fi

say "путь к репозиторию: $REPO"
defaults write "$BUNDLE_ID" repoPath "$REPO"

say "ставлю $DEST"
rm -rf "$DEST" "$OLD_DEST"
ditto "$APP_DIR/build/BeeDott.app" "$DEST"
rm -rf "$APP_DIR/build/BeeDott.app" "$APP_DIR/build/MeetMind.app"
# Автозапуск включится заново уже для копии в /Applications (первый запуск).
defaults delete "$BUNDLE_ID" loginItemConfigured >/dev/null 2>&1 || true

say "запуск"
open "$DEST"
echo "готово: $DEST"
