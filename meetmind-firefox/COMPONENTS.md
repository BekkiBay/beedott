# MeetMind Firefox Расширение - Описание компонентов

## Архитектурная диаграмма

```
┌─────────────────────────────────────────────────────────────────┐
│                        Google Meet                               │
│  (https://meet.google.com/xxx-xxxx-xxx)                         │
└───────────────────┬─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│              content_script.js (инжектированный)                │
│                                                                   │
│  ┌─────────────────────────────────────────────────────┐        │
│  │ WSClient                                            │        │
│  │ - Управляет WebSocket соединением                   │        │
│  │ - Переподключается автоматически                    │        │
│  │ - Очередирует сообщения                             │        │
│  └────────────────────┬────────────────────────────────┘        │
│                       │                                          │
│  ┌────────────────────┴────────────────────────────────┐        │
│  │                                                      │        │
│  ▼                    ▼                    ▼             ▼       │
│  SpeakerObserver     ChatObserver     ParticipantObserver...   │
│  - Находит спикера   - Отслеживает  - Отслеживает        │
│  - MutationObserver    чат             участников        │
│  - Дедупликация      - DOM parsing   - Join/Leave        │
│                                                          │
│  + MeetingLifecycle (начало/конец встречи)             │
└──────────────────────┬──────────────────────────────────┘
                       │ WebSocket JSON события
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│        ws://localhost:8765 (ваш WebSocket сервер)              │
│                                                                   │
│  Получает события и обрабатывает их дальше                     │
└─────────────────────────────────────────────────────────────────┘
```

## Детальное описание компонентов

### 1. WSClient - WebSocket управление

**Класс:** `class WSClient`
**Файл:** content_script.js (131 строка)
**Ответственность:** Управление соединением и отправка событий

**Методы:**
```javascript
constructor(url)      - инициализация с URL сервера
connect()            - подключение к WebSocket
send(event)          - отправка события (с очередированием)
scheduleReconnect()  - планирование переподключения
flushQueue()         - отправка очереди сообщений
getMeetingId()       - извлечение ID из URL
notifyStatus()       - уведомление background script
reconnect()          - принудительное переподключение
```

**Состояние:**
- `url` - адрес WebSocket сервера
- `ws` - объект WebSocket соединения
- `isConnected` - статус подключения
- `messageQueue` - очередь сообщений если соединение потеряно
- `reconnectInterval` - интервал переподключения (3000ms)

**События WebSocket:**
- `onopen` - соединение установлено, отправляем очередь
- `onerror` - ошибка соединения (логируем)
- `onclose` - соединение закрыто, планируем переподключение

### 2. SpeakerObserver - Отслеживание спикера

**Класс:** `class SpeakerObserver`
**Файл:** content_script.js (157 строк)
**Ответственность:** Определение активного спикера в реальном времени

**Методы:**
```javascript
start()                          - инициализация
waitForMeetInterface()           - ожидание загрузки интерфейса
setupObserver()                  - установка MutationObserver
checkActiveSpeaker()             - проверка текущего спикера
extractNameFromElement(element)  - извлечение имени участника
stop()                           - отключение наблюдателя
```

**Стратегии поиска спикера:**
1. `[data-requested-participant-id][aria-label]` - через aria-label
2. `video[data-participant-id]` - через видео элемент
3. `[aria-label*="is speaking"]` - через aria-label "is speaking"

**Дедупликация:**
- Не отправляем один и тот же спикер чаще чем раз в 2 секунды
- Переменная: `lastSpeakerTime` (2000ms интервал)

**Извлечение имени:**
- Из aria-label атрибута: "Иван Сидоров, is speaking"
- Из data-participant-id и текста рядом
- Fallback: первая строка текста элемента

**События:**
```json
{
  "type": "speaker",
  "name": "Иван Сидоров",
  "ts": "2026-04-13T10:30:45.123Z",
  "meeting_id": "xxx-xxxx-xxx"
}
```

### 3. ChatObserver - Отслеживание чата

**Класс:** `class ChatObserver`
**Файл:** content_script.js (119 строк)
**Ответственность:** Отслеживание новых сообщений в чате

**Методы:**
```javascript
start()                  - инициализация
waitForChat()            - ожидание загрузки чат-панели
setupObserver()          - MutationObserver на чат
processNewMessage()      - обработка нового сообщения
stop()                   - отключение наблюдателя
```

**Селекторы для чат-панели:**
- `[jsname="xySENc"]` - основной контейнер Google Meet чата
- `[role="region"][aria-label*="chat"]` - регион с чатом
- `[data-chat-message]` - отдельное сообщение

**Дедупликация:**
- Используется `messageId` из data атрибута или текста
- Set для отслеживания уже обработанных сообщений

**Извлечение данных:**
- Имя автора: из `[data-sender-name]` или элементов с "author", "name"
- Текст: из `[data-message-text]` или `<p>`, `<span>` элементов

**События:**
```json
{
  "type": "chat",
  "name": "Иван Сидоров",
  "text": "Привет, вы меня слышите?",
  "ts": "2026-04-13T10:30:45.123Z",
  "meeting_id": "xxx-xxxx-xxx"
}
```

### 4. ParticipantObserver - Отслеживание участников

**Класс:** `class ParticipantObserver`
**Файл:** content_script.js (102 строк)
**Ответственность:** Отслеживание присоединения и ухода участников

**Методы:**
```javascript
start()                           - инициализация
waitForParticipantList()          - ожидание загрузки списка
recordInitialParticipants()       - фиксирование начального списка
setupObserver()                   - MutationObserver
checkParticipantChanges()         - проверка изменений
extractParticipantName(element)   - извлечение имени
stop()                            - отключение наблюдателя
```

**Логика:**
1. При загрузке сохраняем текущих участников в Set
2. При изменении DOM сравниваем со старым списком
3. Новые участники - отправляем `participant_join`
4. Ушедшие участники - отправляем `participant_leave`

**События:**
```json
{
  "type": "participant_join",
  "name": "Иван Сидоров",
  "ts": "2026-04-13T10:30:45.123Z",
  "meeting_id": "xxx-xxxx-xxx"
}

{
  "type": "participant_leave",
  "name": "Мария Петрова",
  "ts": "2026-04-13T10:30:45.123Z",
  "meeting_id": "xxx-xxxx-xxx"
}
```

### 5. MeetingLifecycle - Жизненный цикл встречи

**Класс:** `class MeetingLifecycle`
**Файл:** content_script.js (43 строк)
**Ответственность:** Отслеживание начала и конца встречи

**Методы:**
```javascript
setupListeners()       - установка обработчиков событий
notifyMeetingStart()   - уведомление о начале встречи
notifyMeetingEnd()     - уведомление о конце встречи
```

**События:**
- `beforeunload` - пользователь закрывает вкладку
- `pagehide` - пользователь навигирует с страницы
- При инициализации - отправляем `meeting_start`

**События:**
```json
{
  "type": "meeting_start",
  "ts": "2026-04-13T10:30:45.123Z",
  "meeting_id": "xxx-xxxx-xxx"
}

{
  "type": "meeting_end",
  "ts": "2026-04-13T10:30:45.123Z",
  "meeting_id": "xxx-xxxx-xxx"
}
```

## Инициализация

**Функция:** `initMeetMind()` (42 строк)

1. Проверка что мы на meet.google.com
2. Создание WSClient для подключения к серверу
3. Задержка в 3 секунды (для полной загрузки интерфейса)
4. Инициализация всех наблюдателей:
   - SpeakerObserver
   - ChatObserver
   - ParticipantObserver
   - MeetingLifecycle
5. Сохранение в глобальные переменные для отладки

**Запуск:**
- При `DOMContentLoaded`
- При `window.load` (если не инициализировалась ранее)

## background.js - Управление состоянием

**Функции:**
```javascript
chrome.runtime.onMessage        - слушает сообщения от content scripts
chrome.tabs.onUpdated           - отслеживает загрузку вкладок
chrome.tabs.onRemoved           - удаляет статус закрытых вкладок
updateBadge(tabId, connected)   - обновляет значок расширения
extractMeetingId(url)           - парсит URL встречи
```

**Значок расширения:**
- Зелёный с "✓" - WebSocket подключено
- Красный с "✗" - WebSocket отключено

## popup/* - Интерфейс

### popup.html (47 строк)
- Header с логотипом
- Status indicator (пульсирующая точка)
- Meeting info (ID и статус)
- Action buttons (переподключение, помощь)

### popup.css (243 строк)
- Тёмная тема
- Gradient фоны
- Анимации (pulse, hover)
- Кастомный scrollbar
- Responsive дизайн

### popup.js (131 строка)
- `updateStatus()` - получить статус от background
- `updateUIStatus()` - обновить UI элементы
- Обработчики кнопок
- Auto-refresh каждые 2 сек

## Обработка ошибок

### На уровне WSClient:
```javascript
try {
  this.ws = new WebSocket(this.url);
} catch (error) {
  console.error('[MeetMind] Ошибка при подключении:', error);
  this.scheduleReconnect();
}
```

### На уровне Observers:
```javascript
checkActiveSpeaker() {
  try {
    // логика
  } catch (error) {
    console.error('[MeetMind] Ошибка при проверке спикера:', error);
  }
}
```

### На уровне background:
```javascript
chrome.tabs.sendMessage(tabId, message, () => {
  if (chrome.runtime.lastError) {
    // обработка ошибки
  }
});
```

## Логирование

Все компоненты логируют события в консоль браузера:
```
[MeetMind] WSClient инициализирован для ws://localhost:8765
[MeetMind] WebSocket подключено
[MeetMind] SpeakerObserver запущен
[MeetMind] Активный спикер: Иван Сидоров
[MeetMind] Новое сообщение в чате: ...
```

Для отладки - откройте F12 и посмотрите вкладку Console.
