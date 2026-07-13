# MeetMind v2 — Tech Spec

**TL;DR:** Локальный auto-followupper для Zoom/Google Meet. Запись (мик+система) → транскрипция (MLX-Whisper) → суммаризация (Ollama + Claude параллельно) → доставка (Telegram + локальные .md + Obsidian/Notion). Триггеры: календарь, авто-детект процесса, menubar/hotkey. Mac-only, M5 Pro 24GB target.

---

## Что не так с v1 (existing code)

| Компонент | Статус v1 | Проблема |
|---|---|---|
| `recorder.py` | работает | пишет только с одного устройства; нет aggregate device логики; нет fallback если BlackHole не установлен |
| `transcriber.py` | работает | `openai-whisper` в 5-10× медленнее MLX-Whisper на Apple Silicon; модель `medium` хуже `large-v3` |
| `summarizer.py` | работает | использует deprecated `claude-3-5-sonnet-20241022`; не локально; нет fallback'а |
| `telegram_bot.py` | сломан (вероятно) | python-telegram-bot v20+ async-only, но код синхронный |
| `scheduler.py` | работает | только календарь, нет process-detect |
| `calendar_client.py` | работает | OK |
| `enricher.py` | работает | привязан к Firefox extension — хрупко |
| Sinks | только Telegram | нет MD, Obsidian, Notion |
| UI | нет | нет menubar, нет hotkey, нет визуального контроля |

---

## Архитектура v2

```
┌──────────────────────────────────────────────────────────────┐
│                         TRIGGERS                              │
│  ┌────────────┐  ┌──────────────┐  ┌──────────────────────┐ │
│  │ Calendar   │  │Process-watch │  │ Menubar / Hotkey     │ │
│  │ (existing) │  │ zoom.us +    │  │ rumps app + pynput   │ │
│  │            │  │ meet.google  │  │ ⌘⇧R = start/stop     │ │
│  └─────┬──────┘  └──────┬───────┘  └──────────┬───────────┘ │
└────────┼─────────────────┼─────────────────────┼─────────────┘
         └─────────────────┴─────────────────────┘
                           ▼ start_recording(session)
┌──────────────────────────────────────────────────────────────┐
│  Recorder (Aggregate Device "MeetMind In" = BlackHole + Mic) │
│  Fallback chain: aggregate → BlackHole-only → default mic    │
│  Output: WAV 16kHz mono per session                          │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│  Transcriber: mlx-whisper large-v3, ru, fp16 на Metal        │
│  Output: list[{start, end, text, speaker?}]                  │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│  Summarizer (HYBRID — обе ветки запускаются параллельно)     │
│  ┌──────────────────────┐    ┌──────────────────────────┐   │
│  │ Ollama qwen2.5:14b   │    │ Claude API (sonnet-4-5)  │   │
│  │ POST :11434/api/chat │    │ Anthropic SDK            │   │
│  └──────────┬───────────┘    └────────────┬─────────────┘   │
│             ▼                              ▼                  │
│   followup_local.md                followup_cloud.md          │
└──────────────────────────────┬───────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────┐
│  Sinks (все в параллель)                                      │
│  • TelegramSink — async, оба варианта в одном сообщении       │
│  • MarkdownSink — ~/MeetMind/2026-05-26/title-HHMM.md         │
│  • ObsidianSink — копия в указанный vault                     │
│  • NotionSink   — новая страница в указанной DB               │
└───────────────────────────────────────────────────────────────┘
```

---

## Решения по технологиям (с обоснованием)

| Слой | Выбор | Почему |
|---|---|---|
| Local LLM | **Qwen2.5 14B-Instruct** (Ollama) | Хороший RU, ~9GB в q4, помещается рядом с Whisper |
| Cloud LLM | **claude-sonnet-4-5** | Лучший RU-фоллоу-ап, актуальная модель |
| STT | **mlx-whisper large-v3** | 5-10× быстрее `openai-whisper` на Apple Silicon, лучшее качество |
| Diarization | отложено в v3 | pyannote требует HF token и тяжёл; для MVP — без неё |
| Audio capture | **Aggregate Device** через `sounddevice` | стандарт macOS, не требует sudo |
| Trigger | **3 канала** (calendar/process/menubar) | покрывает все сценарии |
| Menubar | **rumps** | минимальный Mac tray на Python |
| Hotkey | **pynput** | global hotkey без прав root |
| Process detect | **psutil** | кросс-платформенный, быстрый |
| Telegram | **python-telegram-bot v22 async** | чинит существующий баг |

---

## Что НЕ делаем (out of scope для v2)

- ❌ Speaker diarization (pyannote/WhisperX) — отложено в v3
- ❌ Поддержка Linux/Windows — только Mac
- ❌ Веб-интерфейс — `web/` оставляем как есть, не развиваем
- ❌ Firefox extension — оставляем как опциональный enrichment, но не делаем required
- ❌ Реал-тайм транскрипция во время встречи — только пост-обработка
- ❌ Анализ видео-потока — только аудио

---

## Юридическое примечание

Запись разговора без согласия собеседников может нарушать законы РФ (ст. 137 УК), ЕС (GDPR), США (двух-сторонний consent в CA/FL/IL/MD/MA/MT/NV/NH/PA/WA). MeetMind будет:
1. Хранить записи **только локально**.
2. При первом запуске показывать предупреждение о consent.
3. В Telegram-сообщении встречи добавлять disclaimer "запись сделана локально для личных заметок".

Юридическую ответственность за уведомление собеседников несёт пользователь.

---

## План реализации (порядок)

**Phase 1: core pipeline (приоритет)**
1. `daemon/summarizer.py` — гибридный (Ollama + Claude параллельно)
2. `daemon/transcriber.py` — переписать на mlx-whisper
3. `daemon/recorder.py` — aggregate device + fallback chain
4. `daemon/sinks/` — markdown, telegram (async fix), obsidian, notion
5. `config.py` + `.env.example` — новые настройки
6. `requirements.txt` — обновить зависимости

**Phase 2: triggers + UX**
7. `daemon/process_watcher.py` — psutil-based zoom/meet detect
8. `tray.py` — rumps menubar app + hotkey

**Phase 3: setup**
9. `setup_mac.sh` — установка BlackHole, Ollama, модель, aggregate device guide
10. `README.md` — переписать
11. Тест end-to-end на маленькой записи

---

## Метрики успеха

- Запись → готовый фоллоу-ап в TG: **< 2× от длины встречи** (30 мин встреча → ≤60 мин обработки)
- Качество фоллоу-апа: глазами — сравнить Ollama и Claude версии, выбрать предпочтения
- Stability: 5 встреч подряд без падений
