# MeetMind v3 — Spec «Sentinel»

> Невидимый локальный ИИ-секретарь встреч для macOS. Встреча в Zoom/Meet закончилась →
> через несколько минут в Telegram приходит структурированный фоллоу-ап. Ноль ручных
> действий, ноль утечек аудио в сеть.

**Статус:** утверждённая спека v3. Заменяет SPEC_v2.md.
**План реализации:** [PLAN_v3.md](./PLAN_v3.md).
**Как выбиралась архитектура:** 3 независимых предложения (Continuum / Native Tap Core / Sentinel),
судейская оценка по 5 критериям. Победил Sentinel (39/50) с прививками идей из двух других.

---

## 1. Vision

MeetMind v3 — персональный секретарь для одного пользователя (русскоязычный разработчик,
macOS Apple Silicon 24GB, Claude Max, живёт в Telegram и Obsidian). Он молча живёт в фоне,
сам замечает, что идёт встреча, пишет аудио на диск, расшифровывает и суммирует полностью
локально (бесплатно, через Ollama), а через несколько минут после конца звонка присылает
в Telegram готовый фоллоу-ап: контекст, решения, action items с владельцами и дедлайнами.
Claude Max используется точечно как **опциональный бустер качества**, а не как обязательная
зависимость.

Три жёстких правила, которым подчинены все технические решения:

1. **Ноль ручных действий в happy path.** Если фиче нужен GUI-шаг (Aggregate Device,
   интерактивный OAuth) — шаг автоматизируется нативным API или фича не входит в happy path.
2. **Единственный источник истины — SQLite.** Race condition в состоянии сессии — это не
   техдолг, а потерянный или задвоенный фоллоу-ап, т.е. провал продукта.
3. **Деградация вместо падения.** Диаризация, Claude, календарь, Obsidian — опциональные
   слои поверх гарантированного минимального пути (запись → транскрипт → Ollama → Telegram).
   Падение любого опционального слоя никогда не останавливает доставку.

## 2. Требования (MoSCoW)

### Must (без этого продукт бессмысленен)

- M1. Автодетект начала/конца встречи в Zoom и Google Meet без ручного запуска.
- M2. Локальная запись system audio + микрофона; аудио никогда не уходит в сеть.
- M3. Всегда видимый REC-индикатор в menu bar во время записи.
- M4. Локальный офлайн STT (mlx-whisper) — работает без интернета.
- M5. Локальная суммаризация через Ollama как дефолтный бесплатный путь.
- M6. Фиксированный формат фоллоу-апа: контекст, что обсуждалось, договорённости,
  action items (владелец + дедлайн), открытые вопросы.
- M7. Автодоставка в Telegram через несколько минут после конца встречи.
- M8. Полностью автоматический пайплайн detect→record→transcribe→summarize→deliver.
- M9. Устойчивость к сбою на любом шаге: сырое аудио и транскрипт не удаляются до
  подтверждённой доставки; ретраи с backoff; рестарт демона = продолжение очереди.
- M10. Отмена активной записи одним хоткеем: аудио удаляется, ничего не отправляется.
- M11. Ручной старт/стоп для встреч вне Zoom/Meet (телефон на столе, офлайн-разговор).
- M12. Предохранитель «забыл выключить»: жёсткий лимит длительности + авто-стоп.
- M13. Setup с нуля за один проход ≤15 минут (разрешения, модели, Telegram token).

### Should

- S1. Календарный контекст (тема, участники) через EventKit — не гейтит пайплайн.
- S2. Заметка в Obsidian vault параллельно с Telegram.
- S3. Диаризация без имён (Speaker 1/2) как неблокирующий batch-постпроцесс.
- S4. Retention: авточистка аудио старше N дней (только для доставленных встреч).
- S5. Локальная история с полнотекстовым поиском (SQLite FTS5).
- S6. Claude-бустер: включается explicit opt-in (глобально или per-meeting).
- S7. Прозрачность: в заметке видно, какой движок использован и что уходило в сеть.

### Could (v3.1+)

- C1. Диаризация с именами (voice enrollment + список участников из EventKit).
- C2. Telegram-бот с RAG: вопросы по прошедшим встречам.
- C3. Автосоздание задач в Linear/Todoist из action items.
- C4. Live-заметки во время встречи.

### Won't (v3)

- Облачное хранение/синхронизация; мультипользовательность; Teams/Slack Huddles;
  анализ видео/слайдов; бот-участник звонка; Firefox-расширение; веб-UI; Notion как
  обязательный sink (остаётся опциональным legacy-sink); прямой Google Calendar OAuth.

## 3. Архитектура

```
┌─────────────────────────────────────────────────────────────────────┐
│  MeetMind.app — единый подписанный Swift-бандл (TCC-идентичность)   │
│                                                                     │
│  MenuBarExtra UI          DetectionEngine           CaptureEngine   │
│  REC ● / Start/Stop/      NSWorkspace (zoom.us,     Core Audio      │
│  Cancel / hotkey ⌘⇧R      браузер) + audio-device-  Process Tap     │
│  Onboarding wizard        busy (confirmed signal)   (PID) + mic     │
│  Supervisor (heartbeat,   + Accessibility title     AVAudioEngine   │
│  авторестарт демона)      meeting_key=(app,id)      → WAV стерео    │
│         │                        │                       │          │
│         │ ControlSocket (unix socket, JSON-RPC)          │ WAV      │
│         ▼                        ▼                       ▼          │
│  ┌───────────────────────── SQLite (WAL) ──────────────────────┐   │
│  │ sessions · pipeline_jobs · deliveries · notes_fts (FTS5)     │   │
│  │ writer: ТОЛЬКО Python-демон (Swift — через ControlSocket)    │   │
│  └──────────────────────────────────────────────────────────────┘   │
│         ▲                                                           │
│         │ poll                                                      │
│  ┌──────┴──────────────────────────────────────────────────────┐   │
│  │ PipelineDaemon (Python, заморожен внутри Contents/Resources)│   │
│  │ Orchestrator: очередь джобов, backoff, memory-guard          │   │
│  │  transcribe (mlx-whisper large-v3-turbo, анти-галлюцинация)  │   │
│  │  → diarize (опц., MLX/senko, после выгрузки Whisper)         │   │
│  │  → summarize (Ollama Vikhr-8B; + Claude CLI opt-in бустер)   │   │
│  │  → deliver (TelegramSink · NoteSink md/Obsidian · Notion)    │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
   Fallback-захват (навсегда поддерживаемый): BlackHole + программный
   Aggregate Device (PyObjC CoreAudio HAL) — для macOS < 14.4 / сбоя Tap.
```

### Поток данных

DetectionEngine ловит confirmed-сигнал (процесс zoom.us/браузер **И** занятость
аудио-устройства; Accessibility-заголовок для meeting_id; ручной хоткей — всегда) →
Swift вызывает `start_recording` по ControlSocket (строку `sessions(state=RECORDING)`
пишет демон) и стартует CaptureEngine: Process Tap на
PID + микрофон, стерео WAV чанками на диск (ch0=система, ch1=мик), REC горит.
По debounced-концу (смена/исчезновение meeting_id, стоп, лимит) — `state=RECORDED`,
создаётся `pipeline_job(queued)`. PipelineDaemon забирает job: transcribe → (diarize) →
summarize → deliver. `delivered_at` — только после подтверждения Telegram API.
Файлы удаляются **только** в `DELIVERED` (retention-sweep по возрасту) или `CANCELLED`
(немедленно). Любой сбой = `FAILED_RETRY` + backoff, сырые файлы нетронуты.
Рестарт демона — это просто продолжение чтения очереди, отдельного recovery-кода нет.

### State machine сессии

```
sessions.state:
DETECTED → RECORDING → RECORDED → TRANSCRIBING → TRANSCRIBED
   → SUMMARIZING → SUMMARIZED → DELIVERING → DELIVERED   (терминальный)
любое нетерминальное состояние → CANCELLED               (терминальный)
после исчерпания ретраев стадии → FAILED
FAILED → (reprocess) → RECORDED | TRANSCRIBED | SUMMARIZED
         (откат к состоянию перед самой ранней непройденной стадией)

pipeline_jobs.status (отдельный автомат УРОВНЯ ДЖОБА, не сессии):
queued → running → done
running → failed_retry → (backoff) → queued → …   (сессия остаётся в *_ING)
failed_retry после 8 попыток → failed (и сессия → FAILED)
```

**Инвариант №1:** удаление `audio.wav`/`transcript.*` разрешено только в
`DELIVERED` (по retention) и `CANCELLED` (сразу).
**Инвариант №2:** все записи в SQLite делает только Python-демон (внутри — один
writer: orchestrator). Swift и Telegram-бот мутируют состояние исключительно через
ControlSocket; читать БД напрямую им можно, писать — нет.
**Инвариант №3:** ни одна should-фича (диаризация, Claude, календарь, Obsidian)
не может заблокировать переход к DELIVERING.

## 4. Ключевые решения

| # | Решение | Обоснование |
|---|---|---|
| D1 | Захват: **Core Audio Process Taps** (macOS 14.4+) в Swift; **BlackHole + программный Aggregate Device — постоянный fallback**, не временный. Fallback автоматизируется ЦЕЛИКОМ: включая программное создание Multi-Output Device (BlackHole + колонки) и переключение default output на время записи с восстановлением после | Устраняет главную UX-боль v2 (ручной Aggregate Device в Audio MIDI Setup). Per-process tap не зависит от смены дефолтного устройства (AirPods mid-call). Без автоматизации output-стороны fallback пишет пустой канал системного звука — недопустимо для «постоянного режима» |
| D2 | **Спайк Process Tap — самый первый шаг проекта** (до пайплайна), параллельно спайк программного Aggregate Device | Самая рискованная ставка де-рискуется до вложений во всё остальное. Если Tap не взлетел — проект продолжается на fallback-пути без потери темпа |
| D3 | Top-level бандл — **единый подписанный Swift .app** (MenuBarExtra); Python-демон заморожен PyInstaller внутри Contents/Resources. **Без py2app, без rumps в проде** | Одна TCC-идентичность = одна строка разрешений. Team ID / Bundle ID фиксируются с первого коммита — иначе TCC-сброс при каждой пересборке (боль v2) |
| D4 | IPC — **ControlSocket**: unix domain socket + JSON-RPC (`start/stop/cancel/reprocess/status/health/heartbeat`) | Явный контракт Swift↔Python↔Telegram-бот. Заменяет три несогласованные WS-реализации v2 |
| D5 | Swift-родитель **супервайзит** Python-демон: heartbeat каждые 5с, 3 пропуска → рестарт с backoff | Тихое падение демона не останавливает продукт; очередь в SQLite делает рестарт бесплатным |
| D6 | Состояние — **SQLite WAL** + конечный автомат; файлы аудио/текста — data-plane на диске | Прямое устранение гонок v2 (session_manager dict без блокировок). FTS5-поиск истории — бесплатный побочный эффект |
| D7 | STT — **mlx-whisper large-v3-turbo**, реюз `daemon/transcriber.py`; анти-галлюцинационные флаги из `test_one_meeting.sh` становятся дефолтом; чанкинг для 2-3ч встреч | Уже работает и проверено на русском в этом проекте; turbo ~1.6GB — комфортно в 24GB |
| D8 | Суммаризация — **Ollama (Vikhr-YandexGPT-5-Lite-8B-it)** дефолт; LLM возвращает **JSON по схеме**, markdown рендерим сами; map-reduce для длинных транскриптов | Русскоязычный тюн, ~5-6GB Q4. JSON-схема даёт валидацию, поиск и стабильный формат вместо парсинга свободного текста |
| D9 | **Claude = opt-in бустер поверх готового Ollama-результата**, через headless `claude -p` (Max-подписка); строгая валидация ответа, тихий fallback | Сбой Claude = «не улучшили», а не «не доставили». По состоянию на июль 2026 headless CLI списывается из общего лимита Max (Anthropic анонсировала, но приостановила отдельный платный пул для `claude -p`/SDK — перепроверить перед M7); лимит делится с обычной разработкой — поэтому не default-on |
| D10 | Диаризация — **вне критического пути**: batch-постпроцесс (senko или pyannote-MLX) после выгрузки Whisper; Speaker 1/2, обновляет заметку постфактум | Should-фича не имеет права рисковать NFR «≤10 минут до Telegram». Лёгкие MLX-модели — десятки МБ |
| D11 | Память: **последовательная загрузка/выгрузка моделей** + memory-guard перед тяжёлой стадией (мало RAM → FAILED_RETRY, не своп) | 24GB unified memory: Whisper+LLM+диаризация одновременно не помещаются |
| D12 | Детект: **confirmed-signal** — процесс И занятость аудио-устройства; meeting_key=(app, meeting_id); debounce per meeting_id; EventKit только контекст; хоткей — гарантированный fallback | Один сигнал даёт ложные срабатывания (Zoom в фоне) и пропуски. Ключ по meeting_id решает «две встречи подряд» (воспроизведённый баг v2) |
| D13 | Календарь — **EventKit** (локальный macOS Calendar, уже синхронизирован с Google), не Google OAuth | OAuth-флоу v2 интерактивен и несовместим с headless-демоном; EventKit не требует токенов и соответствует «локальному» позиционированию |
| D14 | Секреты — в `~/Library/Application Support/MeetMind/config.env` (chmod 600), вне репозитория; `credentials.json` из v2 удаляется | Аудит нашёл живой Google OAuth secret в корне репо |
| D15 | **Claude Auth** — авторизация бустера через Claude-аккаунт (Max), два режима: (a) дефолт — keychain-credentials интерактивного `claude` login; (b) выделенный годовой OAuth-токен `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` в config.env (для launchd-надёжности). Демон вызывает `claude` по полному пути, БЕЗ `--bare`, с вычищенными `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` из env; self-check при старте — `claude auth status` (exit code 0/1) | Прецеденс аутентификации Claude Code: `ANTHROPIC_API_KEY` в env **тихо переключает биллинг на API-кредиты** в `-p` режиме; `--bare` игнорирует `CLAUDE_CODE_OAUTH_TOKEN`; launchd-процессы имеют минимальный env/PATH и могут упираться в Keychain (известные issues #69173, #37512) — поэтому env-инъекция токена как надёжный режим |

## 5. Схема данных (SQLite)

Файл: `~/Library/Application Support/MeetMind/meetmind.db` (WAL, busy_timeout=5000).

```sql
CREATE TABLE sessions (              -- writer: Python-демон (мутации извне — только через ControlSocket)
  id          TEXT PRIMARY KEY,      -- uuid4
  meeting_key TEXT,                  -- "zoom:12345" | "meet:abc-defg-hij" | NULL (manual)
  title       TEXT,
  source      TEXT NOT NULL,         -- auto_zoom | auto_meet | manual
  state       TEXT NOT NULL,         -- см. state machine
  started_at  TEXT NOT NULL,         -- ISO8601 UTC
  ended_at    TEXT,
  audio_path  TEXT,
  gap_note    TEXT,                  -- "[запись прервана: сон 14:02–14:31]"
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE TABLE pipeline_jobs (         -- writer: Python
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    TEXT NOT NULL REFERENCES sessions(id),
  stage         TEXT NOT NULL,       -- transcribe | summarize | deliver | diarize
  chain         INTEGER NOT NULL DEFAULT 1,  -- 1=основная цепочка (двигает sessions.state),
                                             -- 0=standalone постпроцесс (diarize): state не трогает
  status        TEXT NOT NULL DEFAULT 'queued', -- queued|running|done|failed_retry|failed|skipped
  attempts      INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  last_error    TEXT,
  updated_at    TEXT NOT NULL,
  UNIQUE(session_id, stage)
);
CREATE TABLE deliveries (            -- writer: Python
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id   TEXT NOT NULL REFERENCES sessions(id),
  sink         TEXT NOT NULL,        -- telegram | note | obsidian | notion
                                     -- note и obsidian — два ЭКЗЕМПЛЯРА NoteSink,
                                     -- отслеживаются и ретраятся раздельно
  status       TEXT NOT NULL DEFAULT 'pending', -- pending | delivered | failed_retry | failed
  attempts     INTEGER NOT NULL DEFAULT 0,
  delivered_at TEXT,
  detail       TEXT,                 -- message_id / путь к файлу / ошибка
  UNIQUE(session_id, sink)
);
CREATE VIRTUAL TABLE notes_fts USING fts5(session_id UNINDEXED, title, transcript, summary);
```

## 6. ControlSocket (JSON-RPC поверх unix socket)

Сокет: `~/Library/Application Support/MeetMind/control.sock`. Кадр — одна строка JSON (`\n`-delimited).

```jsonc
// запрос:  {"id": 1, "method": "start_recording", "params": {"source": "manual", "title": null}}
// ответ:   {"id": 1, "result": {"session_id": "..."}}
// ошибка:  {"id": 1, "error": {"code": "no_active_session", "message": "..."}}
```

| method | params | кто вызывает | что делает |
|---|---|---|---|
| `start_recording` | `source, title?, meeting_key?, audio_path_owner?` (`app`=пишет Swift CaptureEngine, `daemon`=Python Recorder) | Swift UI / детект / hotkey | создать сессию, начать запись |
| `stop_recording` | `session_id?` | Swift / hotkey / debounce | закрыть WAV, поставить job в очередь |
| `cancel_recording` | `session_id?` | Swift / hotkey | стоп + удалить файлы + CANCELLED |
| `reprocess` | `session_id` | Telegram-бот / UI | сбросить failed-джобы в queued |
| `status` | — | Swift UI / бот | активная сессия, очередь, последняя ошибка |
| `health` | — | Swift Supervisor | `{"ok": true, "version": ...}` |
| `heartbeat` | — | демон → Swift (notification) | «я жив», каждые 5с |

Пока Swift-слоя нет (M1-M2), те же методы доступны через `python -m daemon.cli`.

## 7. Формат фоллоу-апа

LLM возвращает строгий JSON (Ollama `format=json`):

```json
{
  "context": "2-3 предложения: о чём встреча, к чему пришли",
  "discussed": ["пункт — кто что сказал/предложил"],
  "decisions": ["конкретное решение"],
  "action_items": [{"task": "что сделать", "owner": "кто", "deadline": "2026-07-20 | asap | null"}],
  "open_questions": ["что осталось нерешённым"]
}
```

Правила промпта (перенос из v2): «не выдумывай — пиши пустой список», «имена неразличимы →
Спикер 1/2», системный промпт продакт-менеджера. Рендер в markdown — на нашей стороне
(`daemon/render.py`), с подписью движка (`🖥 vikhr-8b` / `☁️ claude sonnet`) и
дисклеймером о локальном хранении. Claude-бустер получает транскрипт + Ollama-JSON и
возвращает улучшенный JSON той же схемы; невалидный ответ → остаётся Ollama-версия.

## 8. Судьба кода v2

| Компонент | Вердикт | Комментарий |
|---|---|---|
| `daemon/transcriber.py` | **reuse** | + анти-галлюцинационные флаги, чанкинг |
| `daemon/summarizer.py` | **adapt** | JSON-схема, map-reduce, Claude → opt-in бустер |
| `daemon/sinks/base.py`, `telegram.py`, `markdown.py` | **reuse** | markdown+obsidian сливаются в NoteSink |
| `daemon/sinks/obsidian.py` | merge → NoteSink | дублирует markdown.py |
| `daemon/sinks/notion.py` | adapt (опц.) | + warning при обрезке children[:100] |
| `daemon/recorder.py` | **adapt** → fallback-путь | потоковая запись на диск, стерео, guards |
| `daemon/process_watcher.py` | **reuse** | как один из сигналов детекта (интерим M1-M4) |
| `daemon/session.py` | reuse как схема | поля → колонки `sessions` |
| `daemon/notifier.py` | adapt | фикс экранирования AppleScript |
| `config.py` | adapt | пути → App Support, без WEB_PORT |
| `setup_mac.sh` | adapt | основа онбординга |
| `test_one_meeting.sh` | источник флагов/промпта | остаётся ручным smoke-test |
| `daemon/scheduler.py`, `session_manager.py`, `websocket_server.py` | **rewrite/drop** | заменяются SQLite + ControlSocket |
| `run.py`, `example_server.py`, `daemon/telegram_bot.py`, `web/`, `meetmind-firefox/`, `daemon/calendar_client.py`, `daemon/enricher.py`, `tray.py` | **drop** | мёртвый/сломанный/заменённый код (tray.py — после M3) |

## 9. NFR

- **Латентность:** встреча ≤60 мин → фоллоу-ап в Telegram ≤10 мин после конца.
  Встреча 2-3 ч → обработка ≤30% длительности, уведомление если >15 мин.
- **Приватность:** аудио и сырой транскрипт не покидают Mac. Наружу — только текст
  саммари в Telegram и (при явном opt-in) транскрипт в Claude. Это видно в заметке.
- **Надёжность:** 0% пайплайнов с потерей сырых данных; все шаги идемпотентны.
- **Setup:** ≤15 мин с нуля, один проход, без терминала после установки.
- **Idle:** <1% CPU, <150MB RAM (Swift-слой; Python-демон спит на poll).
- **Обработка:** модели загружаются последовательно; пониженный QoS/nice.

## 10. Edge cases (контракт поведения)

| Кейс | Ожидаемое поведение |
|---|---|
| Спонтанный звонок без календаря | Детект по процессу/устройству, не по календарю. Заголовок из окна или «Встреча HH:MM» |
| Две встречи подряд | Смена meeting_key завершает старую сессию и открывает новую; записи не склеиваются |
| Mac заснул посреди встречи | willSleep → отметка; wake: звонок жив → продолжить с gap_note; мёртв → закрыть по последней метке и обработать |
| Диск заполнен | Preflight <2GB → предупреждение; кончилось во время записи → аккуратный стоп, сохранённое обрабатывается |
| Встреча 3 часа | Потоковая запись, чанкованный STT, map-reduce суммаризация, уведомление о долгой обработке |
| Ollama не запущена | Демон пытается поднять сам; иначе job в pending + уведомление. Автопереключения на Claude НЕТ |
| Нет интернета | Локальные шаги штатно; Telegram/Claude — в очередь до сети; Claude-встреча получает Ollama-версию с пометкой |
| Забыл выключить запись | Хард-лимит 6ч → авто-стоп + уведомление; >30 мин тишины → предложение остановиться |
| Демон тихо умер | Supervisor: 3 пропущенных heartbeat → рестарт; очередь продолжается с места |
| Mac перезагрузился / приложение крашнулось посреди RECORDING | Startup-reconciliation при старте демона: сессии в нетерминальных состояниях без живого рекордера/джобы закрываются по данным на диске (WAV есть → RECORDED + gap_note + в очередь; WAV пуст → CANCELLED). Ни одна сессия не «зависает» в RECORDING навсегда |
| Обработка длится >15 мин (длинная встреча) | Промежуточное уведомление «обработка продолжается» по таймеру от момента RECORDED |

## 11. Риски

| Риск | Митигация |
|---|---|
| Process Tap: подпись, TCC, малодокументированный API | Спайк первым шагом (M0), timebox; fallback BlackHole-путь — постоянный режим |
| Developer ID ($99/год) нет/не хочется | Сначала пробуем бесплатный personal team (обычный Apple ID даёт стабильный Team ID — для локальной сборки под себя этого может хватить; Developer ID нужен прежде всего для нотаризации/распространения). Не хватило → проект полностью работоспособен на fallback-пути (Continuum-режим, см. §12) |
| Accessibility-детект Meet хрупок к обновлениям браузера | Только как источник meeting_id, не единственный триггер; счётчик недетектов; хоткей всегда |
| 24GB: swap/OOM на длинной встрече | Последовательные стадии + memory-guard + QoS; диаризация после выгрузки Whisper |
| Vikhr-8B слабее Claude на action items | Метрика качества → триггер на Claude opt-in / T-pro 2.0 (последовательно); движок помечен в заметке |
| Дрейф флагов `claude` CLI между версиями | Валидация JSON-схемы; любой сбой → тихий откат на Ollama-результат |
| Claude Auth из демона: launchd-хэнги (issue #69173), `CLAUDE_CODE_OAUTH_TOKEN` может снести keychain-entry интерактивных сессий (issue #37512) | Self-check `claude auth status` при старте; smoke-test точной launchd-команды в M6 Verify; после включения token-режима проверить, что интерактивный `claude` login жив; сбой auth → бустер отключается с одним уведомлением, пайплайн не страдает |
| SQLite конкурентная запись | WAL + один writer на таблицу; UI/бот ходят через ControlSocket, не в БД |
| Scope creep (FTS, бот, диаризация) | Всё это Phase 2 (M7); must-фичи закрываются в M1-M6 |

## 12. Запасной вариант «Continuum»

Судейский разрыв мал (39 vs 37). Если бюджет времени — пара часов в неделю, легитимно
остановиться на **Continuum**: тот же пайплайн и SQLite-ядро, но без Swift вообще —
rumps-menubar, BlackHole + программный Aggregate Device навсегда, py2app.
План построен так, что **M0-M2 общие для обоих путей**, а решение Swift/не-Swift
принимается на гейте после M2 по результату спайка и фактическому темпу.

## 13. Метрики успеха

- >95% встреч — фоллоу-ап доставлен полностью автоматически.
- Медиана «конец встречи → Telegram» ≤10 мин (встречи <60 мин).
- <2% встреч с некорректным детектом старта/конца.
- 0% пайплайнов с невосстановимой потерей данных.
- Продукт всё ещё включён через 30/60/90 дней.
