<p align="center"><img src="docs/logo.png" width="160" alt="BeeDott"></p>

# BeeDott — локальный auto-followupper для встреч

> Бывший MeetMind. Новое имя видно в `/Applications/BeeDott.app`, Finder и уведомлениях;
> внутренние пути (`~/MeetMind`, `~/Library/Application Support/MeetMind`), bundle id
> и имена модулей пока прежние, чтобы не потерять данные и настройки.

Приложение в строке меню macOS. Записывает встречи (твой микрофон + звук
собеседников), локально расшифровывает через MLX-Whisper, собирает фоллоу-ап
через Claude и отправляет его в Telegram + сохраняет заметкой Markdown / в
Obsidian. Аудио никуда не уходит — в Claude идёт только текст транскрипта.

---

## Установка

**Коллегам (чистый Mac, без репо и терминала):** `app/package.sh` собирает
`app/build/BeeDott.zip`, инструкция — [docs/INSTALL.md](docs/INSTALL.md). При
первом запуске мастер сам ставит Python-пакеты и модель Whisper в
`~/Library/Application Support/MeetMind/runtime`, спрашивает ключ Anthropic и
по желанию подключает Telegram. Повторно — меню → «Настройка…».

**Из репозитория (машина разработчика):** нужны macOS на Apple Silicon,
[uv](https://docs.astral.sh/uv/) и Xcode Command Line Tools (`swiftc`).

```bash
uv sync                        # Python-зависимости демона в .venv
app/install.sh                 # собрать BeeDott.app и поставить в /Applications
app/Tools/app_icon.sh          # перерисовать иконку, потом снова install.sh
```

`install.sh` не ставится, пока идёт запись или обработка (`FORCE=1` — всё
равно). Путь к репо сохраняется в настройках приложения, и демон стартует из
репо (`uv run python main.py`) даже при запуске из `/Applications`.
Автозапуск — через «Объекты входа».

Настройки — мастер «Настройка…» в меню (пишет в
`~/Library/Application Support/MeetMind/config.env`) или `.env` в корне репо,
пока `config.env` нет. Ключи — в [.env.example](.env.example).

## Два способа вызвать Claude

Способ — `SUMMARY_BACKEND`:

| `SUMMARY_BACKEND` | Когда | Что нужно |
|---|---|---|
| `claude_cli` | Есть Max-подписка | `claude` в PATH (или `MEETMIND_CLAUDE_BIN`), `claude login` |
| `api` | Нет Max, есть API-ключ | `ANTHROPIC_API_KEY=sk-ant-...`, модель — `ANTHROPIC_MODEL` |

Мастер настройки работает через `api`; `claude_cli` — для запуска из репо по
Max-подписке (`npm install -g @anthropic-ai/claude-code` → `claude login`).

## Встречи в Claude (MCP)

BeeDott — MCP-сервер для Claude Desktop и Claude Code: `python -m
daemon.mcp_server` (stdio), ходит в демон через `control.sock`, данные не
покидают Mac, пока Claude сам их не запросит. Подключение — «Настройка…» → шаг
«Claude» (пишет `mcpServers.beedott` в конфиг Claude Desktop и выполняет
`claude mcp add-json --scope user beedott …`). Вручную для Claude Code — та же
команда из шага мастера («Команда для Claude Code»). Инструменты:
`list_meetings`, `search_meetings`, `get_meeting`, `action_items`,
`recording_status`. Спек: `docs/superpowers/specs/2026-10-14-claude-mcp-design.md`.

---

## Как устроено

```
BeeDott.app (Swift, строка меню)
  ├─ CaptureEngine — Core Audio Process Taps: микрофон + звук системы → WAV
  ├─ автодетект звонка в браузере, Календарь, островок у чёлки, хоткеи
  ├─ Библиотека встреч, мастер настройки
  └─ запускает Python-демон и говорит с ним по unix-сокету control.sock
          │
          ▼
Python-демон (main.py → daemon/)
  SQLite-очередь: transcribe ──▶ summarize ──▶ deliver
                  MLX-Whisper    Claude          Telegram, ~/MeetMind/*.md, Obsidian
```

| Где | Что |
|---|---|
| `app/Sources/MeetMind/` | Swift-приложение: захват аудио, UI, супервизор демона, мастер настройки |
| `daemon/app.py`, `daemon/control.py` | Демон и JSON-протокол сокета для приложения |
| `daemon/orchestrator.py`, `daemon/stages.py` | Очередь стадий: по одной задаче, ретраи, проверка памяти |
| `daemon/transcriber.py` | MLX-Whisper large-v3-turbo на Metal, нарезка длинных записей, дедуп повторов |
| `daemon/summarizer.py` | Промпт и JSON-схема фоллоу-апа; вызов — `claude_cli.py` или `anthropic_api.py` |
| `daemon/render.py`, `daemon/sinks/` | Markdown фоллоу-апа и доставка: Telegram, заметка, Obsidian |
| `daemon/recorder.py` | Запасная запись из Python (sounddevice), если пишет не приложение |
| `daemon/setup.py` | Команды мастера первичной настройки (`python -m daemon.setup`) |
| `daemon/mcp_server.py` | MCP-сервер для Claude: список, поиск, карточка, задачи, статус; через control.sock |
| `daemon/reconcile.py`, `daemon/retention.py` | Восстановление после краша, удаление старого аудио |

Демоном можно управлять из терминала: `uv run python -m daemon.cli
{daemon,record start|stop|cancel,status,reprocess <id>,health,import <файл>}`.

## Запись

- **Вручную** — меню, островок у чёлки или ⌘⇧R (старт/стоп). ⌃⌥⌘X — отменить
  запись, ⌃⌥⌘M или Fn+F5 — выключить свой микрофон в идущей записи.
- **Календарь** — за минуту до встречи островок предлагает «Записать»;
  название встречи берётся из события.
- **Google Meet в браузере** — если браузер пишет микрофон и в Календаре сейчас
  встреча со ссылкой на Meet, запись стартует сама; без встречи — уведомление
  «Записать?».

## Где лежат данные

```
~/Library/Application Support/MeetMind/
  config.env                       # настройки и ключи (chmod 600)
  meetmind.db                      # сессии и очередь стадий (SQLite)
  sessions/<id>/                   # audio.wav, transcript.*, followup.*
~/MeetMind/YYYY-MM-DD/HHMM-<встреча>.md      # MARKDOWN_BASE_DIR
<OBSIDIAN_VAULT_PATH>/Meetings/YYYY-MM-DD/…  # если задан vault
```

Аудио доставленных встреч удаляется через 14 дней после записи
(`MEETMIND_RETENTION_DAYS`, 0 — не удалять; «Хранить аудио» в карточке
встречи — тоже не удалять); транскрипты и заметки не удаляются.

## Тесты

```bash
uv run pytest                  # демон (без медленных и железных: -m slow / -m hardware)
app/Tests/run.sh               # чистая Swift-логика
```

## Юридическое примечание

Запись разговора без явного согласия собеседников может нарушать законы:
**РФ** (ст. 137 УК), **GDPR** в ЕС, в США двух-сторонний consent в
CA/FL/IL/MD/MA/MT/NV/NH/PA/WA. BeeDott хранит аудио **только локально**, в
Claude уходит только текст транскрипта. Уведомлять собеседников — твоя
ответственность.

## Известные ограничения

- Только macOS на Apple Silicon.
- Нет diarization (разметки, кто из собеседников говорит).
- Транскрипция не в реальном времени — Whisper работает после записи.
- Приложение не нотарифицировано: при первом запуске macOS просит
  подтверждение (см. [docs/INSTALL.md](docs/INSTALL.md)).

## Лицензия

MIT
