#!/usr/bin/env bash
# =============================================================================
# Одноразовый тест: транскрипция + followup через Claude Code CLI
# =============================================================================
#
# Usage:
#   ./test_one_meeting.sh                              # last_vstrechka.m4a
#   ./test_one_meeting.sh path/to/audio.m4a            # любой файл
#   MODEL=sonnet ./test_one_meeting.sh                 # через Sonnet вместо Opus
#   WHISPER_MODEL=large-v3 ./test_one_meeting.sh       # выбор Whisper модели
#
# Требует:
#   - python3 + mlx-whisper установленный (pip install mlx-whisper)
#   - claude CLI установленный и авторизованный (через Max)
#   - ffmpeg
# =============================================================================
set -euo pipefail

cd "$(dirname "$0")"

# ─── параметры ───────────────────────────────────────────────────────────────
AUDIO="${1:-last_vstrechka.m4a}"
MODEL="${MODEL:-opus}"
WHISPER_MODEL="${WHISPER_MODEL:-large-v3}"
TS=$(date +%Y%m%d-%H%M%S)
WORK_DIR="./test_output/$TS"
mkdir -p "$WORK_DIR"

TRANSCRIPT="$WORK_DIR/transcript.txt"
FOLLOWUP="$WORK_DIR/followup.md"

say()  { echo -e "\n\033[1;36m▸\033[0m \033[1m$1\033[0m"; }
ok()   { echo -e "  \033[32m✓\033[0m $1"; }
err()  { echo -e "  \033[31m✗\033[0m $1"; }

# ─── проверки ────────────────────────────────────────────────────────────────
say "Проверки"
[ -f "$AUDIO" ] || { err "Файл не найден: $AUDIO"; exit 1; }
ok "Аудио: $AUDIO ($(du -h "$AUDIO" | cut -f1))"

command -v claude >/dev/null || { err "claude CLI не найден. Установи: npm install -g @anthropic-ai/claude-code"; exit 1; }
ok "claude CLI: $(which claude)"

command -v ffmpeg >/dev/null || { err "ffmpeg не найден. brew install ffmpeg"; exit 1; }
# mlx-whisper доступен либо как CLI команда (через pipx), либо как Python модуль
if command -v mlx_whisper >/dev/null 2>&1; then
    MLX_WHISPER_CMD="mlx_whisper"
    ok "mlx-whisper CLI: $(which mlx_whisper)"
elif python3 -c "import mlx_whisper" 2>/dev/null; then
    MLX_WHISPER_CMD="python3 -m mlx_whisper"
    ok "mlx-whisper: Python модуль"
else
    err "mlx-whisper не найден."
    err "Поставь любым способом:"
    err "  pipx install mlx-whisper           (рекомендуется)"
    err "  pip3 install --user mlx-whisper"
    exit 1
fi

# ─── длительность ────────────────────────────────────────────────────────────
DURATION=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$AUDIO")
DURATION_MIN=$(python3 -c "print(round($DURATION/60,1))")
ok "Длительность: $DURATION_MIN мин"

# ─── транскрипция ────────────────────────────────────────────────────────────
say "Транскрипция через mlx-whisper $WHISPER_MODEL (~$(python3 -c "print(round($DURATION_MIN/8,1))") мин на M5 Pro)"
T0=$(date +%s)

# Подбираем имя репозитория. Турбо имеет другую конвенцию имени.
case "$WHISPER_MODEL" in
    turbo|large-v3-turbo) HF_REPO="mlx-community/whisper-large-v3-turbo" ;;
    large|large-v3)       HF_REPO="mlx-community/whisper-large-v3-mlx" ;;
    *)                    HF_REPO="mlx-community/whisper-${WHISPER_MODEL}-mlx" ;;
esac
ok "Модель: $HF_REPO"

# Вызываем mlx_whisper CLI — он напишет файлы $WORK_DIR/transcript.{txt,json}
#
# Анти-галлюцинационные флаги:
#   --condition-on-previous-text False  — не даёт модели «зациклиться» на своём
#                                          предыдущем выводе в тихих местах
#   --temperature 0                     — старт детерминированно (greedy)
#   --temperature-increment-on-fallback 0.2 — если compression-ratio fails,
#                                          модель ретраит с +0.2, +0.4… до 1.0,
#                                          чтобы выпрыгнуть из лупа
#   --compression-ratio-threshold 2.4  — детектор loops: если повторяется,
#                                          триггерит fallback temperature
#   --no-speech-threshold 0.6          — пропускать сегменты без речи
$MLX_WHISPER_CMD "$AUDIO" \
    --model "$HF_REPO" \
    --language ru \
    --output-dir "$WORK_DIR" \
    --output-name transcript \
    --output-format txt \
    --task transcribe \
    --temperature 0 \
    --temperature-increment-on-fallback 0.2 \
    --compression-ratio-threshold 2.4 \
    --logprob-threshold -1.0 \
    --no-speech-threshold 0.6 \
    --condition-on-previous-text False 2>&1 | tail -5

# Доп-защита: дедупим последовательные одинаковые строки на случай если
# loop всё-таки проскочил. Сохраняем .raw.txt и перезаписываем .txt чистым.
if [ -f "$TRANSCRIPT" ]; then
    cp "$TRANSCRIPT" "${TRANSCRIPT%.txt}.raw.txt"
    python3 - <<PYEOF
from pathlib import Path
p = Path("$TRANSCRIPT")
lines = p.read_text().splitlines()
clean, prev, dup_count = [], None, 0
for line in lines:
    s = line.strip()
    if not s:
        continue
    if s == prev:
        dup_count += 1
        if dup_count < 2:           # одно повторение оставляем
            clean.append(s)
        continue
    dup_count = 0
    clean.append(s)
    prev = s
p.write_text("\n".join(clean) + "\n")
print(f"  cleaned: {len(lines)} → {len(clean)} строк")
PYEOF
fi

T1=$(date +%s)
[ -f "$TRANSCRIPT" ] || { err "Транскрипт не создан: $TRANSCRIPT"; exit 1; }
ok "Транскрипция готова за $((T1-T0))с → $TRANSCRIPT"
WORDS=$(wc -w <"$TRANSCRIPT")
ok "Слов в транскрипте: $WORDS"

# ─── промпт ──────────────────────────────────────────────────────────────────
say "Готовлю промпт для Claude $MODEL"
PROMPT_FILE="$WORK_DIR/prompt.txt"
cat >"$PROMPT_FILE" <<EOF
Проанализируй запись встречи и создай структурированный фоллоу-ап.

Дата записи: $(date +"%d.%m.%Y")
Длительность: $DURATION_MIN минут
Источник: запись с микрофона + системного звука

Полная транскрипция:
---
$(cat "$TRANSCRIPT")
---

Создай фоллоу-ап СТРОГО в этом формате (без отступлений):

**Встреча** · $(date +"%d.%m.%Y") ($DURATION_MIN мин)

**Контекст**
[2-3 предложения: о чём встреча, какая проблема обсуждалась, к чему пришли]

**Что обсуждалось**
• [пункт 1 — кто что сказал/предложил]
• [пункт 2]
• [пункт 3]

**Договорённости**
• [конкретное решение 1]
• [конкретное решение 2]

**Action items**
📅 [дата дедлайна или "asap"] — [что сделать] — [кто отвечает]
📅 [...] — [...] — [...]

**Открытые вопросы**
• [что осталось нерешённым]

Если каких-то пунктов в записи нет — пиши "нет" в соответствующей секции, не выдумывай.
Если в транскрипции имена не различимы — пиши "Спикер 1", "Спикер 2" итд.
Отвечай ТОЛЬКО фоллоу-апом, без преамбулы вроде "Вот ваш фоллоу-ап:" и без postscript.
EOF
ok "Промпт: $PROMPT_FILE ($(wc -c <"$PROMPT_FILE") байт)"

# ─── Claude Code CLI ────────────────────────────────────────────────────────
say "Запускаю Claude $MODEL через CLI (использует твою Max-подписку)"
T0=$(date +%s)
SYSTEM_PROMPT="Ты — продакт-менеджер, который слушает встречи и пишет краткие, конкретные фоллоу-апы на русском языке. Никакой воды, никаких общих фраз. Только факты, договорённости, дедлайны, ответственные."

cat "$PROMPT_FILE" | claude \
    --print \
    --model "$MODEL" \
    --append-system-prompt "$SYSTEM_PROMPT" \
    --output-format text \
    >"$FOLLOWUP"
T1=$(date +%s)
ok "Followup готов за $((T1-T0))с → $FOLLOWUP"

# ─── вывод ──────────────────────────────────────────────────────────────────
say "Результат"
echo "────────────────────────────────────────────────────────"
cat "$FOLLOWUP"
echo ""
echo "────────────────────────────────────────────────────────"
echo ""
echo "Файлы:"
echo "  Транскрипт:  $TRANSCRIPT"
echo "  Промпт:      $PROMPT_FILE"
echo "  Followup:    $FOLLOWUP"
echo ""
echo "Открыть Markdown:  open $FOLLOWUP"
