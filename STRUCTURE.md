# Структура проекта MeetMind

## Архитектура расширения

```
meetmind/
├── meetmind-firefox/              # Firefox расширение
│   ├── manifest.json              # WebExtension Manifest V2
│   ├── content_script.js          # Главный скрипт инжектируемый на meet.google.com
│   ├── background.js              # Background сервис расширения
│   └── popup/                     # Popup интерфейс
│       ├── popup.html
│       ├── popup.js
│       └── popup.css
├── example_server.py              # Пример WebSocket сервера на Python
├── requirements.txt               # Python зависимости
├── README.md                      # Полная документация
├── QUICKSTART.md                  # Быстрый старт
└── STRUCTURE.md                   # Этот файл
```

## Компоненты расширения

### 1. manifest.json (36 строк)

**WebExtension Manifest V2** - конфигурация расширения:
- Версия: 1.0.0
- Permissions: activeTab, tabs, storage, notifications
- Content script: инжектируется на https://meet.google.com/*
- Background script: background.js
- Browser action: popup/popup.html
- Icons: SVG emoji 🎙️

### 2. content_script.js (598 строк)

**Главный скрипт расширения**, инжектируется на meet.google.com:

#### WSClient класс (135 строк)
- Управляет WebSocket соединением к ws://localhost:8765
- Автоматическое переподключение каждые 3 секунды
- Очередирование сообщений если соединение потеряно
- Методы:
  - `connect()` - подключение к серверу
  - `send(event)` - отправка события
  - `getMeetingId()` - извлечение ID встречи из URL
  - `notifyStatus()` - уведомление background script о статусе

#### SpeakerObserver класс (157 строк)
- Отслеживает активного спикера в реальном времени
- MutationObserver на body для отслеживания изменений
- Несколько стратегий поиска спикера:
  1. `[data-requested-participant-id][aria-label]`
  2. `video[data-participant-id]`
  3. `[aria-label*="is speaking"]`
- Дедупликация - максимум раз в 2 секунды
- Отправляет: `{type: "speaker", name, ts, meeting_id}`

#### ChatObserver класс (119 строк)
- Отслеживает новые сообщения в чате
- Ищет контейнер `[jsname="xySENc"]`
- MutationObserver на чат-панель
- Дедупликация через messageId
- Отправляет: `{type: "chat", name, text, ts, meeting_id}`

#### ParticipantObserver класс (102 строк)
- Отслеживает список участников
- Фиксирует начальный список при загрузке
- Отслеживает присоединение и уход
- Отправляет:
  - `{type: "participant_join", name, ts, meeting_id}`
  - `{type: "participant_leave", name, ts, meeting_id}`

#### MeetingLifecycle класс (43 строк)
- Отслеживает начало и конец встречи
- beforeunload событие для конца встречи
- Отправляет:
  - `{type: "meeting_start", ts, meeting_id}`
  - `{type: "meeting_end", ts, meeting_id}`

#### Инициализация (42 строки)
- Проверка что мы на meet.google.com
- Задержка в 3 секунды для полной загрузки интерфейса
- Инициализация всех наблюдателей
- Обработка ошибок на каждом уровне
- Сохранение объектов в глобальные переменные для отладки

### 3. background.js (131 строка)

**Background сервис расширения**:

#### Управление состоянием
- `tabStatus` объект - отслеживает статус каждой вкладки
- Слушает `chrome.runtime.onMessage` от content scripts

#### Обработчики событий
- `onMessage` - получает обновления статуса WS от content scripts
- `onUpdated` - отслеживает загрузку вкладок meet.google.com
- `onRemoved` - удаляет статус закрытых вкладок

#### Функции
- `updateBadge(tabId, connected)` - обновляет значок расширения
- `extractMeetingId(url)` - парсит meeting ID из URL

#### API для popup
- `getStatus` - получить текущий статус подключения
- `reconnect` - принудительное переподключение

### 4. popup/popup.html (47 строк)

**HTML интерфейс popup**:
- Header с логотипом и названием
- Status section - индикатор подключения
- Meeting info - информация о текущей встречу
- Action buttons - переподключение и помощь
- Footer - версия и инструкции

### 5. popup/popup.css (243 строк)

**Стили popup**:
- Тёмная тема (dark mode by default)
- Градиент фон
- Анимация пульсирующей точки статуса
- Responsive дизайн
- Кастомный scrollbar
- Кнопки с hover-эффектами
- Цветовая палитра:
  - Основной красный: #e94560
  - Успех: #4CAF50
  - Ошибка: #f44336
  - Background: linear gradient от #1a1a2e к #16213e

### 6. popup/popup.js (131 строка)

**Логика popup интерфейса**:
- Получение статуса WS от background script
- Обновление UI элементов:
  - Индикатор статуса (цвет, иконка)
  - ID текущей встречи
  - Статус подключения
- Обработчики кнопок:
  - Переподключение
  - Справка (alert с инструкциями)
- Auto-refresh каждые 2 секунды
- Прослушивание сообщений от background для обновлений

## Поток данных

```
Google Meet intерфейс
        ↓
content_script.js (observers)
        ↓
WebSocket (JSON события)
        ↓
ws://localhost:8765 (user server)
```

## События и их формат

### Все события содержат:
```json
{
  "type": "event_type",
  "ts": "2026-04-13T10:30:45.123Z",
  "meeting_id": "xxx-xxxx-xxx"
}
```

### Специфичные для события поля:

| Event | Поля | Описание |
|-------|------|---------|
| `meeting_start` | - | Встреча началась |
| `meeting_end` | - | Встреча закончилась |
| `speaker` | `name` | Активный спикер |
| `chat` | `name`, `text` | Сообщение в чате |
| `participant_join` | `name` | Участник присоединился |
| `participant_leave` | `name` | Участник покинул |

## Глобальные переменные для отладки

В контексте meet.google.com доступны:

```javascript
// WebSocket клиент
window.__meetmindClient
  .isConnected           // boolean - статус подключения
  .ws                    // WebSocket - объект соединения
  .messageQueue          // array - очередь сообщений
  .send(event)           // function - отправить событие
  .reconnect()           // function - переподключиться
  .getMeetingId()        // function - получить ID встречи

// Все наблюдатели
window.__meetmindObservers
  .speaker               // SpeakerObserver
  .chat                  // ChatObserver
  .participants          // ParticipantObserver
  .lifecycle             // MeetingLifecycle
```

## Обработка ошибок

### На каждом уровне:
1. **content_script.js**
   - Try-catch в каждом методе
   - Таймауты и fallbacks для DOM поиска
   - Дедупликация для предотвращения spam

2. **background.js**
   - Проверка chrome.runtime.lastError
   - Graceful handling закрытых вкладок
   - Безопасное удаление статуса

3. **popup.js**
   - Обработка асинхронных ошибок
   - Graceful degradation если background недоступен
   - Информативные сообщения об ошибках

## Производительность

### Оптимизации:
1. **MutationObserver** - вместо polling
2. **Дедупликация** - для избежания spam
3. **Очередирование** - async обработка событий
4. **Lazy initialization** - задержка в 3 сек для полной загрузки
5. **Set для дедупликации** - O(1) поиск участников

### Complexity:
- WSClient: O(1) send, O(n) reconnect
- SpeakerObserver: O(1) check per mutation
- ChatObserver: O(1) per new message
- ParticipantObserver: O(n) initial, O(1) per change
- MeetingLifecycle: O(1)

## Тестирование

### Можно протестировать через:
1. Python server - `python3 example_server.py`
2. WebSocket console - инструменты для отладки WS
3. Browser console - логирование и отладка
4. Developer tools - инспектирование DOM

## Совместимость

- **Firefox**: 50+ (WebSocket, MutationObserver поддержка)
- **Manifest Version**: 2 (для Firefox)
- **JavaScript**: ES6+ (async/await, class syntax)
- **API**: Chrome API (совместима через WebExtensions)

## Размеры

| Файл | Строк | Размер (approx) |
|------|-------|-----------------|
| manifest.json | 36 | 1KB |
| content_script.js | 598 | 22KB |
| background.js | 131 | 5KB |
| popup.html | 47 | 1.5KB |
| popup.css | 243 | 8KB |
| popup.js | 131 | 5KB |
| **Итого** | **1186** | **42.5KB** |

## Расширение в будущем

Возможные улучшения:
1. Manifest V3 миграция
2. Chrome совместимость
3. Пакетирование в .xpi файл
4. Chromium-based браузеры (Edge, Brave)
5. Детальная аналитика
6. Offline кэширование
7. Шифрование WS соединения (WSS)
8. Лучше DOM селекторы для новых версий Meet
