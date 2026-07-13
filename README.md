# MeetMind v2 — локальный auto-followupper для встреч

Локально (на твоём Mac) записывает встречи Zoom/Meet/Teams, транскрибирует
аудио через MLX-Whisper, гонит транскрипт **одновременно через локальную
LLM (Ollama Qwen2.5) и Claude API**, потом отправляет оба фоллоу-апа в
Telegram + сохраняет в Markdown / Obsidian / Notion.

Можно сравнивать качество движков и переключаться по ситуации.

---

## Быстрый старт

Требования: macOS Apple Silicon (M-серия), Python 3.11+, Homebrew.

```bash
git clone <repo> && cd meetmind
./setup_mac.sh                          # установит BlackHole, Ollama, модель, зависимости
# заполнить .env (см. ниже)
source .venv/bin/activate
python main.py                          # стартует с menubar-иконкой
```

В `.env` минимально надо:

```env
# Если есть Max-подписка (рекомендуется — Opus 4.7 без отдельной оплаты)
USE_CLAUDE_CODE=true
CLAUDE_CODE_MODEL=opus

# ИЛИ через Anthropic API (отдельный биллинг)
# USE_ANTHROPIC=true
# ANTHROPIC_API_KEY=sk-ant-...

TELEGRAM_BOT_TOKEN=...                  # @BotFather
TELEGRAM_CHAT_ID=...                    # @userinfobot
```

Остальное по желанию (Obsidian vault, Notion DB и т.д.) — см. `.env.example`.

## Три варианта cloud-движка

MeetMind поддерживает три способа использовать Claude:

| Способ | Когда | Что нужно |
|---|---|---|
| **Claude Code CLI** | Есть Max-подписка | `claude` в PATH, авторизован в CLI |
| **Anthropic API** | Нет Max, есть API-ключ | `ANTHROPIC_API_KEY=sk-ant-...` |
| **Без cloud** | Только локально | `USE_CLAUDE_CODE=false`, `USE_ANTHROPIC=false` |

Claude Code CLI — рекомендуемый путь для Max-подписчиков: ты уже платишь
$100-200/мес за Max, через CLI получаешь Opus 4.7 без отдельной оплаты
токенов. Установка CLI: `npm install -g @anthropic-ai/claude-code` →
`claude login`.

---

## Архитектура

```
                            ┌──── Calendar ──┐
                            ├── ProcessWatch ├──▶ Recorder ──▶ WAV
   Триггеры (любой)         └── Menubar/⌘⇧R ─┘  (BlackHole+Mic
                                                 Aggregate Device)
                                                       │
                                                       ▼
                                              MLX-Whisper large-v3
                                                       │
                                              сегменты транскрипта
                                                       │
                          ┌────────────────────────────┼────────────────┐
                          ▼                            ▼                ▼
                  Ollama qwen2.5:14b             Claude Sonnet 4.5    (параллельно)
                          │                            │
                          └────────────┬───────────────┘
                                       ▼
                              FollowupResult{local, cloud}
                                       │
              ┌──────────────┬─────────┴──────┬───────────────┐
              ▼              ▼                ▼               ▼
          Telegram      ~/MeetMind/      Obsidian vault    Notion DB
                        YYYY-MM-DD/
```

## Что делает каждый компонент

| Файл | Что |
|---|---|
| `daemon/recorder.py` | sounddevice + Aggregate Device («MeetMind In» = BlackHole+Mic). Fallback на BlackHole, затем на дефолтный мик |
| `daemon/transcriber.py` | MLX-Whisper large-v3 на Metal. Fallback на openai-whisper |
| `daemon/summarizer.py` | Hybrid: Ollama (`/api/chat`) + Anthropic SDK параллельно через `ThreadPoolExecutor` |
| `daemon/sinks/markdown.py` | `~/MeetMind/2026-05-26/HHMM-title.md` |
| `daemon/sinks/telegram.py` | python-telegram-bot v22 async. Шлёт два сообщения: 🖥 локальный + ☁ облачный |
| `daemon/sinks/obsidian.py` | Markdown с frontmatter в твой vault |
| `daemon/sinks/notion.py` | REST API, создаёт новую страницу в указанной БД |
| `daemon/scheduler.py` | Поллит Google Calendar каждые 2 мин, стартует запись за ~5 мин до встречи |
| `daemon/process_watcher.py` | psutil — детектит запуск `zoom.us` процесса |
| `tray.py` | rumps menubar + global hotkey ⌘⇧R |

## Триггеры записи

Все три работают одновременно. Любой может стартануть запись:

1. **Google Calendar** — события из календаря с пометкой времени.
2. **Process watcher** — psutil смотрит, не запустился ли `zoom.us`/etc.
3. **Menubar / Hotkey ⌘⇧R** — ручной старт.

Когда триггер сработал, идёт запись. Стоп: либо тот же триггер (процесс
закрылся, новый календарь не наступил), либо ⌘⇧R, либо «Остановить запись»
в menubar, либо WS-событие `meeting_end` от Firefox extension.

## Sinks

Все sinks работают параллельно. Если sink не настроен или упал — остальные
продолжают. Каждый sink возвращает `SinkResult`, логи показывают что куда
ушло.

## Setup Aggregate Device (важно)

Чтобы писать **и** свой голос, **и** собеседников — нужно одно устройство
с обоими источниками. macOS делает это через Audio MIDI Setup:

```
open -a "Audio MIDI Setup"
→ +  →  Create Aggregate Device
→ ✓ Built-in Microphone
→ ✓ BlackHole 2ch
→ переименуй в "MeetMind In"
```

Чтобы **слышать** собеседников при этом — отдельный Multi-Output Device:

```
+  →  Create Multi-Output Device
→ ✓ Built-in Output (или наушники)
→ ✓ BlackHole 2ch
→ выбери его в Системные настройки → Звук → Output перед звонком
```

Если Aggregate не настроен — MeetMind напишет только системный звук
(собеседников, без тебя). Если и BlackHole не установлен — только микрофон.

## Какую локальную LLM использовать (май 2026)

Дефолт: **Vikhr-Nemo 12B** — русский тюн поверх Mistral Nemo, 128K контекст,
помещается в 7.5GB Q4. Идеально под M5 Pro 24GB рядом с Whisper.

Альтернативы:
- **Qwen3.5 14B** — свежий мейнстрим (Feb 2026), 9GB Q4
- **T-pro 2.0 32B** (T-Bank) — peak русского качества, но требует
  `sudo sysctl iogpu.wired_limit_mb=20480` чтобы влезть на 24GB
- **Gemma 3 12B** — long-context champion от Google

Полный разбор: см. [MODEL_GUIDE.md](./MODEL_GUIDE.md) — там подробное
сравнение, цифры по памяти, как переключиться, как поднять GPU-лимит.

## Сравнение Ollama vs Claude

После каждой встречи в TG и .md-файле приходят **оба** варианта с пометками
о движке и времени генерации. Глазами можно увидеть, где какой лучше:

- Claude обычно сильнее на структурировании action items и нюансах
- Vikhr-Nemo локально — приватность + бесплатно + знает русский колорит,
  но иногда упускает детали на сложных встречах
- На длинных встречах (1+ час) разница больше; на коротких — почти одинаково

Если хочешь оставить только один движок — `USE_OLLAMA=false` или
`USE_ANTHROPIC=false` в `.env`.

## Юридическое примечание

Запись разговора без явного согласия собеседников может нарушать законы:
**РФ** (ст. 137 УК), **GDPR** в ЕС, в США двух-сторонний consent в
CA/FL/IL/MD/MA/MT/NV/NH/PA/WA. MeetMind хранит всё **только локально** и не
загружает аудио в облако (только текст транскрипта идёт в Claude API если
включён). Уведомлять собеседников — твоя ответственность.

## Файлы и куда что попадает

```
~/AIApp/meetmind/
  sessions/
    <session_id>/
      <session_id>.wav            # сырое аудио
  ~/MeetMind/                     # MARKDOWN_BASE_DIR
    2026-05-26/
      1430-call-with-team.md      # combined followup
  <Obsidian vault>/Meetings/2026-05-26/...
  Notion DB — новые страницы с заголовком встречи
```

## Запуск как сервис

`launchd` plist можно положить в `~/Library/LaunchAgents/com.meetmind.plist`,
но проще — открыть терминал и держать `python main.py` запущенным. С
menubar это удобно. Для перезапуска просто `⌘Q` в menubar.

## Известные ограничения

- Только macOS Apple Silicon
- Diarization (определение кто говорит из аудио) пока не реализовано — имена
  спикеров берутся только из Firefox extension для Google Meet
- Real-time транскрипция не делается — Whisper гоняется после записи
- Длинная встреча (> 1 ч) на Qwen 14B может занять до 3-5 минут

## Лицензия

MIT
