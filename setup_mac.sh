#!/usr/bin/env bash
# =============================================================================
# MeetMind v2 — setup для macOS Apple Silicon
#
# Что делает:
#   1) Проверяет Homebrew
#   2) Устанавливает BlackHole 2ch (если ещё нет)
#   3) Создаёт Python venv и ставит зависимости
#   4) Выводит инструкцию по настройке Aggregate Device
# =============================================================================
set -e

cd "$(dirname "$0")"

# ---- helpers ----
say()  { echo -e "\n\033[1;36m▸\033[0m \033[1m$1\033[0m"; }
ok()   { echo -e "  \033[32m✓\033[0m $1"; }
warn() { echo -e "  \033[33m⚠\033[0m  $1"; }
err()  { echo -e "  \033[31m✗\033[0m $1"; }

# ---- 1) Homebrew ----
say "Проверяю Homebrew"
if command -v brew >/dev/null 2>&1; then
    ok "brew установлен: $(brew --version | head -1)"
else
    err "Homebrew не найден. Установи: https://brew.sh"
    exit 1
fi

# ---- 2) BlackHole ----
say "BlackHole (для записи системного звука)"
if [ -d "/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver" ]; then
    ok "BlackHole 2ch уже установлен"
else
    warn "BlackHole не найден. Устанавливаю через brew (потребуется sudo)…"
    brew install --cask blackhole-2ch
    ok "BlackHole установлен. Может потребоваться перезагрузка."
fi

# ---- 4) Python ----
say "Python окружение"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    err "python3 не найден. brew install python@3.11"
    exit 1
fi

if [ ! -d ".venv" ]; then
    "$PY" -m venv .venv
    ok ".venv создан"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt
ok "Python-зависимости установлены"

# ---- 5) .env ----
say ".env"
if [ ! -f ".env" ]; then
    cp .env.example .env
    ok ".env создан из .env.example — открой и заполни"
else
    ok ".env уже существует"
fi

# ---- 6) Aggregate Device ----
say "Создание Aggregate Device (нужно сделать ВРУЧНУЮ)"
cat <<'EOF'
  1. Открой Audio MIDI Setup:
       open -a "Audio MIDI Setup"
  2. В левом нижнем углу нажми «+» → «Create Aggregate Device»
  3. В правой панели поставь галочки:
       ✓ Built-in Microphone (или твой основной микрофон)
       ✓ BlackHole 2ch
  4. Переименуй устройство в «MeetMind In»
     (или поменяй AGGREGATE_DEVICE_NAME в .env на своё имя)
  5. Готово. MeetMind будет использовать это устройство.

  ─── Чтобы СЛЫШАТЬ собеседников И писать их в файл, также создай Multi-Output:
  6. «+» → «Create Multi-Output Device»
  7. Поставь галочки: ✓ Built-in Output (или твои наушники) + ✓ BlackHole 2ch
  8. В Системных настройках → Звук → Output: выбери этот Multi-Output
     перед началом встречи. Zoom/Meet будут играть звук и в наушники, и в BlackHole.
EOF

say "Готово!"
cat <<'EOF'

Запуск:
  source .venv/bin/activate
  python main.py                 # с menubar
  python main.py --no-menubar    # без UI (для serve-режима)

Саммари делает Claude Opus. По умолчанию — через claude CLI (Max-подписка):
  claude login
Или через Anthropic API — в .env:
  SUMMARY_BACKEND=api
  ANTHROPIC_API_KEY=sk-ant-…

Что заполнить в .env обязательно:
  TELEGRAM_BOT_TOKEN   — токен бота от @BotFather
  TELEGRAM_CHAT_ID     — твой chat_id от @userinfobot

Опционально:
  OBSIDIAN_VAULT_PATH  — для копий в Obsidian
  NOTION_TOKEN/DB_ID   — для копий в Notion

EOF
