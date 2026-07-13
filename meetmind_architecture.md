# MeetMind — Полная архитектура

> Локальный ИИ-секретарь встреч: записывает, транскрибирует, суммаризирует, уведомляет.

---

## Компоненты (3 плана реализации)

```
┌─────────────────────────────────────────────────────────────────────┐
│                        PLAN C — DESKTOP UI                         │
│                    (встроенный в демон, порт 5000)                 │
│                                                                     │
│  Web UI (Flask + Jinja2 / или React SPA)                           │
│  ├── 📋 История фоллоу-апов  → рендер .md красиво (marked.js)      │
│  ├── 🔗 Ручной запуск встречи → вставь ссылку → демон заходит      │
│  └── ⚙️  Настройки           → токены, шаблоны, устройства          │
│                                                                     │
│  macOS Notification (встроенная)                                   │
│  └── За 1 мин до встречи → уведомление поверх всего               │
│       [ГО 🚀]  [ОТМЕН ❌]                                           │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ internal API
┌──────────────────────────▼──────────────────────────────────────────┐
│                       PLAN A — PYTHON DAEMON                       │
│                                                                     │
│  scheduler.py        → APScheduler, проверяет Google Cal каждые    │
│                         2 мин, создаёт Session за 5 мин до старта  │
│  calendar_client.py  → Google Calendar API (OAuth2), читает        │
│                         события на ближайшие 24 часа               │
│  recorder.py         → sounddevice + BlackHole, пишет аудио        │
│                         в .wav файл пока сессия активна            │
│  websocket_server.py → asyncio WebSocket сервер на порту 8765,     │
│                         принимает события от Firefox Add-on        │
│  session.py          → Session dataclass, держит контекст встречи: │
│                         название, участники, события, аудио-файл   │
│  transcriber.py      → openai-whisper (локально, модель medium),   │
│                         транскрибирует .wav → raw текст            │
│  enricher.py         → мёрджит аудио транскрипт + speaker events  │
│                         + chat messages по таймкодам               │
│  summarizer.py       → Claude API (claude-sonnet-4-6),             │
│                         структурированный фоллоу-ап по шаблону     │
│  notifier.py         → macOS уведомления (osascript / rumps)       │
│  telegram_bot.py     → python-telegram-bot, отправка фоллоу-апа   │
│  storage.py          → чтение/запись JSON + .md файлов сессий      │
│  config.py           → все токены и настройки (из .env)            │
│  web_server.py       → Flask, UI для просмотра истории             │
│  main.py             → точка входа, запускает всё                  │
└──────────┬────────────────────────┬────────────────────────────────┘
           │                        │
┌──────────▼──────────┐  ┌──────────▼──────────────┐
│  Google Calendar    │  │  Anthropic Claude API    │
│  API (OAuth2)       │  │  (суммаризация)          │
└─────────────────────┘  └──────────────────────────┘
           │
┌──────────▼──────────┐
│   Telegram Bot      │
│   → твой чат        │
└─────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                     PLAN B — FIREFOX ADD-ON                        │
│                    (meetmind-firefox/)                             │
│                                                                     │
│  manifest.json       → WebExtension Manifest V2, permissions       │
│  content_script.js   → инжектируется на meet.google.com            │
│  ├── SpeakerObserver → MutationObserver на DOM индикатор спикера   │
│  ├── ChatObserver    → MutationObserver на чат-панель              │
│  ├── ParticipantList → читает список участников                    │
│  └── WSClient        → WebSocket клиент → ws://localhost:8765      │
│  background.js       → Service worker, управляет lifecycle         │
│  popup/              → мини-UI расширения (статус, вкл/выкл)       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Флоу работы (полный)

```
T-5 мин
  scheduler.py → нашёл встречу в Google Cal
  → Session создана {title, attendees, start_time, meet_url}
  → recorder.py стартует буферную запись
  → notifier.py ставит уведомление на T-1

T-1 мин
  macOS уведомление поверх всего:
  ┌─────────────────────────────────┐
  │ 🔔 MeetMind                     │
  │ Через минуту: "Sync с командой" │
  │ [ГО 🚀]         [ОТМЕН ❌]      │
  └─────────────────────────────────┘

  [ГО] → открывает ссылку встречи в Firefox
        → Firefox Add-on автоматически активируется
  [ОТМЕН] → session.cancel(), запись остановлена

T=0 встреча идёт
  Firefox Add-on стримит события в websocket_server.py:
  {"type": "speaker",  "name": "Bekki",  "ts": "14:31:05"}
  {"type": "chat",     "name": "Артём",  "ts": "14:31:45", "text": "вот линк: ..."}
  {"type": "speaker",  "name": "Артём",  "ts": "14:32:00"}

T=end встреча закончилась (вкладка закрыта / Add-on послал meeting_end)
  recorder.py → останавливает запись → session.wav

  transcriber.py
  → Whisper medium → raw_transcript.txt (без имён, с таймкодами)

  enricher.py
  → мёрджит raw_transcript + speaker_events + chat_messages
  → enriched_transcript.json:
    [
      {"ts": "14:31:05", "speaker": "Bekki",  "text": "окей стартуем",     "source": "audio"},
      {"ts": "14:31:45", "speaker": "Артём",  "text": "вот линк: ...",     "source": "chat"},
      {"ts": "14:32:00", "speaker": "Артём",  "text": "я за дедлайн пятн", "source": "audio"}
    ]

  summarizer.py → Claude API с шаблоном →
  ┌─────────────────────────────────────────────────────────┐
  │ **Sync с командой** · 13 апр 2026, 14:30–15:00 (30 мин)│
  │ 👥 Bekki, Артём, Марина                                 │
  │                                                         │
  │ **Контекст**                                            │
  │ Обсуждали Q2 роадмап, разошлись по приоритетам         │
  │                                                         │
  │ **Проблема**                                            │
  │ Артём считает дедлайн пятницы нереальным для auth фичи  │
  │                                                         │
  │ **Итоговые договорённости**                             │
  │ • Bekki: дедлайн двигаем на понедельник                 │
  │ • Артём: делает auth MVP без OAuth до пн               │
  │ • Марина: ревьюит PR до пн 12:00                        │
  │                                                         │
  │ **Итого**                                               │
  │ 📅 17 апр (пн) — auth MVP от Артёма                    │
  │ 📅 17 апр 12:00 — PR ревью от Марины                   │
  └─────────────────────────────────────────────────────────┘

  storage.py → сохраняет .md файл в sessions/2026-04-13_sync-s-komandoy/
  telegram_bot.py → шлёт фоллоу-ап в твой TG чат
```

---

## UI (Plan C)

### История фоллоу-апов (web, порт 5000)
```
http://localhost:5000

┌────────────────────────────────────────────────────────────┐
│ MeetMind                                    ⚙️  Настройки  │
├────────────────────────────────────────────────────────────┤
│  📅 Сегодня, 14:30  │  Sync с командой       │  ✅ готово  │
│  📅 Вчера, 10:00    │  1-1 с Артёмом         │  ✅ готово  │
│  📅 11 апр, 15:00   │  Planning Q2           │  ✅ готово  │
├────────────────────────────────────────────────────────────┤
│                                                            │
│  [Контент фоллоу-апа в красивом Markdown]                 │
│  Заголовки, жирный текст, чеклисты                        │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### Ручной запуск встречи
```
┌────────────────────────────────────────────────────────────┐
│  🔗 Добавить встречу вручную                               │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  https://meet.google.com/abc-defg-hij               │  │
│  └──────────────────────────────────────────────────────┘  │
│  Название: [______________]                                │
│  [🚀 Начать запись]                                        │
└────────────────────────────────────────────────────────────┘
```

### macOS уведомление (за 1 мин до встречи)
- Нативное через `osascript` с кнопками ГО / ОТМЕН
- Кнопки обрабатывает демон через URL scheme `meetmind://go` и `meetmind://cancel`

---

## Структура файлов проекта

```
meetmind/
├── main.py                    # точка входа
├── config.py                  # настройки, токены из .env
├── .env.example               # шаблон переменных окружения
├── requirements.txt
│
├── daemon/
│   ├── scheduler.py           # APScheduler, следит за календарём
│   ├── calendar_client.py     # Google Calendar API
│   ├── recorder.py            # sounddevice + BlackHole
│   ├── websocket_server.py    # asyncio WebSocket сервер :8765
│   ├── session.py             # Session dataclass
│   ├── transcriber.py         # Whisper
│   ├── enricher.py            # мёрдж транскрипт + события
│   ├── summarizer.py          # Claude API
│   ├── notifier.py            # macOS уведомления
│   ├── telegram_bot.py        # Telegram
│   └── storage.py             # чтение/запись файлов
│
├── web/
│   ├── web_server.py          # Flask app
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html         # список сессий
│   │   └── session.html       # просмотр одного фоллоу-апа
│   └── static/
│       ├── style.css
│       └── marked.min.js      # рендер Markdown → HTML
│
├── sessions/                  # хранилище сессий
│   └── 2026-04-13_sync-s-komandoy/
│       ├── meta.json          # метаданные встречи
│       ├── enriched.json      # обогащённый транскрипт
│       ├── followup.md        # финальный фоллоу-ап
│       └── audio.wav          # аудио запись (можно удалить после)
│
└── meetmind-firefox/          # Firefox Add-on
    ├── manifest.json
    ├── background.js
    ├── content_script.js      # инжектируется на meet.google.com
    └── popup/
        ├── popup.html
        └── popup.js
```

---

## Технический стек

| Компонент | Технология | Зачем |
|---|---|---|
| Python демон | Python 3.11+, asyncio | основа всего |
| Планировщик | APScheduler | проверка календаря каждые 2 мин |
| Google Calendar | google-api-python-client + OAuth2 | читать встречи |
| Аудио захват | sounddevice + BlackHole | захват системного звука |
| Транскрипция | openai-whisper (medium, локально) | STT без облака |
| WebSocket | websockets | коммуникация с Firefox Add-on |
| Claude API | anthropic SDK (claude-sonnet-4-6) | суммаризация |
| Telegram | python-telegram-bot | доставка фоллоу-апа |
| macOS уведомления | osascript / rumps | нативные уведомления с кнопками |
| Web UI | Flask + Jinja2 + marked.js | просмотр истории |
| Firefox Add-on | WebExtension API (MV2) | speaker/chat detection в Meet |

---

## Этапы реализации

| # | План | Что входит | Результат |
|---|---|---|---|
| A | Python Daemon | scheduler, calendar, recorder, whisper, claude, telegram | Работает без UI и без Add-on |
| B | Firefox Add-on | content_script, speaker/chat observer, websocket client | Обогащённый транскрипт с именами |
| C | Desktop UI | Flask web, macOS уведомления, ручной запуск | Полный продукт |

---

## Кейсы которые решает система

### Базовые
- Автоматически знает когда встреча — ничего запускать не надо
- Записывает и транскрибирует аудио локально (конфиденциально)
- Фоллоу-ап прилетает в TG сразу после встречи

### Продвинутые (уже в архитектуре)
- Знает **кто именно** что говорил (через Firefox Add-on)
- Фиксирует **чат** — ссылки, уточнения параллельно речи
- Локальная история всех встреч с красивым просмотром
- Ручной запуск через UI — вставил ссылку, нажал кнопку

### На будущее (следующие версии)
- `/summary [дата]` — переспросить фоллоу-ап через TG
- Action items → автоматически в Notion / Linear
- Повторяющиеся встречи — сравнение с прошлым фоллоу-апом
- Контекст перед встречей ("с кем говорили в прошлый раз")
- Zoom App (Plan D)
