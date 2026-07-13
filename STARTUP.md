# 🚀 Запуск MeetMind локально на Mac

## Быстрый старт (3 шага)

### 1. Установка зависимостей

```bash
cd "/Users/b.botirov/ИИшки/Супер ИИшные приколы/meetmind"
pip install -r requirements.txt
```

### 2. Запуск проекта

```bash
python run.py
```

Это запустит:
- ✅ Flask Web Server на http://localhost:5000
- ✅ MeetMind Daemon (основная логика)
- ✅ Откроет браузер автоматически

### 3. Проверка работы

- Откройте http://localhost:5000 в браузере
- Вы должны увидеть UI для управления сессиями

---

## Конфигурация

Все параметры находятся в файле `.env`:

```ini
TELEGRAM_BOT_TOKEN=<токен от @BotFather>
TELEGRAM_CHAT_ID=<твой chat id>
ANTHROPIC_API_KEY=YOUR_KEY_HERE        # ← Замените на свой ключ
AUDIO_DEVICE_NAME=BlackHole 2ch
WHISPER_MODEL=medium
GOOGLE_CREDENTIALS_PATH=credentials.json
SESSIONS_DIR=sessions
WS_PORT=8765
WEB_PORT=5000
LOG_LEVEL=INFO
```

### Важные переменные:
- **ANTHROPIC_API_KEY** - ключ для Claude API (нужно добавить)
- **SESSIONS_DIR** - где сохраняются записи встреч (по умолчанию `sessions/`)
- **WEB_PORT** - порт для веб-интерфейса (по умолчанию 5000)
- **TELEGRAM_BOT_TOKEN** и **TELEGRAM_CHAT_ID** - уже заполнены

---

## Структура проекта

```
meetmind/
├── run.py                 # Главный скрипт (запускает всё)
├── main.py               # Основная логика демона
├── config.py             # Конфигурация из .env
├── requirements.txt      # Зависимости Python
├── .env                  # Переменные окружения
├── daemon/
│   ├── storage.py        # Сохранение сессий на диск
│   └── ...               # Остальные модули демона
└── web/
    ├── web_server.py     # Flask приложение
    ├── templates/        # HTML шаблоны
    └── static/           # CSS, JS, изображения
```

---

## Что было исправлено

### ✅ Проблема 1: requirements.txt
- **Было:** только websockets и asyncio
- **Стало:** полный список всех зависимостей (14 пакетов)

### ✅ Проблема 2: web_server.py
- **Было:** вызывает методы которых нет в storage.py
- **Стало:** использует только реальные методы (list_sessions, get_session)
- **Исправлены импорты:** `from daemon.storage import Storage` и `from config import SESSIONS_DIR`

### ✅ Проблема 3: Единый запуск
- **Создан файл:** run.py
- **Запускает:** Flask server в отдельном потоке + основной демон
- **Открывает браузер** автоматически на localhost:5000

### ✅ Проблема 4: .env файл
- **Создан файл** .env с готовыми значениями
- **Telegram токены** уже заполнены
- **ANTHROPIC_API_KEY** нужно добавить вручную

### ✅ Проблема 5: config.py
- **Проверено:** load_dotenv(), SESSIONS_DIR как Path, LOG_LEVEL - всё есть
- **Всё работает:** конфиг корректно читает .env файл

---

## Решение проблем

### Ошибка: "ModuleNotFoundError: No module named 'daemon'"
```bash
# Убедитесь что находитесь в корневой папке meetmind
cd "/Users/b.botirov/ИИшки/Супер ИИшные приколы/meetmind"
python run.py
```

### Ошибка: "Address already in use: port 5000"
```bash
# Измените WEB_PORT в .env файле на другой, например 5001
WEB_PORT=5001
```

### Браузер не открывается
```bash
# Откройте вручную:
open http://localhost:5000
```

---

## API endpoints

После запуска доступны следующие эндпоинты:

- `GET /` - главная страница
- `GET /api/sessions` - список всех сессий
- `GET /api/session/<session_id>` - данные конкретной сессии
- `GET /api/status` - статус текущей сессии
- `GET /api/daemon-status` - статус демона

---

## Продолжение разработки

После запуска через `run.py`:
1. Основной код находится в `main.py`
2. Веб-интерфейс находится в `web/`
3. Сохраненные сессии находятся в `sessions/`

Enjoy\! 🎉
