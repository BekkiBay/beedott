# MeetMind Web UI

Полнофункциональный веб-интерфейс для MeetMind - системы управления встречами и фоллоу-апами.

## Быстрый старт

### Установка зависимостей

```bash
cd web/
pip install -r requirements.txt
```

### Запуск сервера

```bash
python web_server.py
```

Сервер запустится на `http://localhost:5000`

## Структура проекта

```
web/
├── web_server.py           # Flask приложение
├── templates/
│   ├── base.html          # Базовый шаблон (навбар, модальное окно)
│   ├── index.html         # Главная страница (список сессий)
│   └── session.html       # Страница деталей сессии (фоллоу-ап)
├── static/
│   ├── style.css          # Все стили (тёмная тема, адаптивный дизайн)
│   └── app.js             # JavaScript (модальное окно, уведомления, API)
└── requirements.txt       # Зависимости
```

## Функциональность

### Главная страница (/)

- **Grid карточек сессий** - все встречи с датой, названием, участниками
- **Поиск** - фильтрация по названию встречи в реальном времени
- **Кнопка "Добавить встречу"** - открывает модальное окно для добавления вручную
- **Статус демона** - индикатор активности демона в навбаре

### Страница сессии (/session/<session_id>)

- **Метаданные** - левая панель с информацией о встрече
  - Дата и время
  - Длительность
  - Участники с аватарами
- **Фоллоу-ап** - основной контент
  - Красиво отрендеренный Markdown через marked.js
  - Поддержка заголовков, списков, ссылок, кода
- **Кнопки действий**
  - Копировать - копирует raw markdown в буфер обмена
  - Отправить в TG - отправляет фоллоу-ап в Telegram (если подключено)
  - Транскрипт - показывает полный транскрипт встречи (если доступен)

### Модальное окно "Добавить встречу"

- Поле для ввода URL встречи (обязательно)
- Поле для названия встречи (опционально)
- Валидация URL
- Кнопка для создания сессии вручную

## API Endpoints

### GET /api/sessions
Список всех сессий с метаданными
```json
[
  {
    "id": "session_id_1",
    "title": "Название встречи",
    "date": "2024-04-10T14:30:00",
    "duration": 3600,
    "participants": ["Иван Петров", "Мария Сидорова"],
    "status": "completed",
    "has_followup": true,
    "has_enriched": false
  }
]
```

### GET /api/session/<session_id>
Полные данные сессии
```json
{
  "id": "session_id_1",
  "metadata": {...},
  "followup": "# Заголовок\n\nТекст...",
  "enriched": null
}
```

### POST /api/start-manual
Создание новой сессии вручную
**Request:**
```json
{
  "url": "https://meet.google.com/...",
  "title": "Название встречи"
}
```
**Response:**
```json
{
  "success": true,
  "session_id": "new_session_id",
  "title": "Название встречи"
}
```

### GET /api/status
Статус текущей активной сессии
```json
{
  "active_session": {
    "id": "session_id",
    "title": "Запись...",
    "status": "recording",
    "duration": 300
  }
}
```

### GET /api/daemon-status
Проверка активности демона
```json
{
  "daemon_active": true
}
```

### POST /api/send-telegram/<session_id>
Отправка фоллоу-апа в Telegram
```json
{
  "success": true,
  "message": "Фоллоу-ап отправлен в Telegram"
}
```

## Дизайн

### Цветовая схема

- **Фон**: #0f0f0f (чёрный)
- **Вторичный фон**: #1a1a1a (серый)
- **Акцент**: #6366f1 (индиго)
- **Текст**: #f5f5f5 (белый)
- **Вторичный текст**: #a0a0a0 (серый)

### Типография

- **Font**: Inter (Google Fonts)
- **Font size**: 16px (базовый)
- **Line height**: 1.6

### Брейкпоинты

- Desktop: 1200px+
- Tablet: 768px-1199px
- Mobile: < 768px

## Требования

- Python 3.8+
- Flask 2.3+
- Flask-CORS 4.0+

## Интеграция с daemon/storage.py

Сервер импортирует Storage из `daemon/storage.py` для работы с сессиями:

```python
from storage import Storage
storage = Storage()

# Методы Storage, используемые в web_server.py:
storage.get_all_sessions()           # Список всех ID сессий
storage.get_session_metadata(id)     # Метаданные сессии
storage.get_followup(id)             # Markdown фоллоу-апа
storage.get_enriched(id)             # Обогащённые данные
storage.get_active_session()         # ID текущей активной сессии
storage.session_has_followup(id)     # Проверка наличия фоллоу-апа
storage.session_has_enriched(id)     # Проверка наличия enriched
storage.create_manual_session(url, title)  # Создание вручную
storage.is_available()               # Проверка доступности демона
```

## Интеграция с telegram_bot.py

Для отправки в Telegram нужен модуль `telegram_bot.py` с функцией:
```python
def send_followup_to_telegram(session_id, followup):
    # Отправляет фоллоу-ап в Telegram
```

## Развертывание

Для production используйте WSGI сервер вместо встроенного Flask:

```bash
# Установка gunicorn
pip install gunicorn

# Запуск
gunicorn -w 4 -b 0.0.0.0:5000 web_server:app
```

## Лицензия

MeetMind © 2024
