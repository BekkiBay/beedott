# Полное руководство по установке MeetMind

## Этап 1: Базовая установка

### 1.1. Установить Homebrew (если не установлен)

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

### 1.2. Установить Python 3.9+

```bash
brew install python@3.11
alias python3=/usr/local/bin/python3.11
```

### 1.3. Установить зависимости

```bash
cd /путь/к/meetmind
pip install -r requirements.txt
```

## Этап 2: Google Calendar API

### 2.1. Создать проект в Google Cloud Console

1. Открыть https://console.cloud.google.com/
2. Создать новый проект
3. Перейти в "APIs & Services" > "Library"
4. Поиск "Google Calendar API"
5. Нажать "Enable"

### 2.2. Создать OAuth2 credentials

1. В "APIs & Services" перейти в "Credentials"
2. Нажать "Create Credentials" > "OAuth 2.0 Client ID"
3. Выбрать "Desktop application"
4. Скачать JSON файл
5. Переименовать в `credentials.json` и положить в папку meetmind/

```bash
# Проверить что файл существует
ls -la credentials.json
```

## Этап 3: BlackHole Audio

### 3.1. Установить BlackHole

```bash
# Через Homebrew
brew install blackhole-2ch

# ИЛИ вручную с https://existential.audio/blackhole/
```

### 3.2. Добавить BlackHole к системе

Перезагрузить Mac или запустить:
```bash
sudo launchctl load /Library/LaunchDaemons/audio.existential.BlackHole.driver.plist
```

### 3.3. Проверить что устройство видно

```bash
python3 -c "import sounddevice as sd; print([d for d in sd.query_devices() if 'Black' in d['name']])"
```

Должен вывести что-то вроде:
```
[{'name': 'BlackHole 2ch', 'hostapi': 0, ...}]
```

## Этап 4: Telegram (опционально)

### 4.1. Создать Telegram бота

1. Открыть Telegram и найти @BotFather
2. Отправить `/start`
3. Отправить `/newbot`
4. Придумать имя и username бота
5. BotFather выдаст TOKEN
6. Скопировать токен

### 4.2. Получить Chat ID

1. Добавить созданного бота в контакты
2. Отправить любое сообщение боту
3. Открыть в браузере:
```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```
4. В ответе найти `"chat":{"id":123456789}`
5. Это ваш CHAT_ID

## Этап 5: Claude API

### 5.1. Получить API ключ

1. Открыть https://console.anthropic.com/
2. Перейти в API Keys
3. Создать новый ключ
4. Скопировать его (начинается с `sk-ant-`)

## Этап 6: Конфигурация

### 6.1. Создать .env файл

```bash
cp .env.example .env
```

### 6.2. Отредактировать .env

```bash
nano .env
```

Заполнить:
```
TELEGRAM_BOT_TOKEN=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg
TELEGRAM_CHAT_ID=987654321
ANTHROPIC_API_KEY=sk-ant-v0-xxxxxxxxxxxxx
AUDIO_DEVICE_NAME=BlackHole 2ch
WHISPER_MODEL=medium
GOOGLE_CREDENTIALS_PATH=credentials.json
SESSIONS_DIR=sessions
WS_PORT=8765
LOG_LEVEL=INFO
```

## Этап 7: Первый запуск

### 7.1. Запустить демон

```bash
python3 main.py
```

Должно вывести:
```
2024-03-15 10:00:00,000 - __main__ - INFO - === Инициализация MeetMind Daemon ===
2024-03-15 10:00:00,123 - daemon.calendar_client - INFO - Успешно подключены к Google Calendar
2024-03-15 10:00:00,456 - daemon.websocket_server - INFO - WebSocket сервер запущен на ws://localhost:8765
2024-03-15 10:00:00,789 - daemon.scheduler - INFO - Планировщик встреч запущен
2024-03-15 10:00:01,012 - __main__ - INFO - === MeetMind Daemon готов ===
```

### 7.2. Отладка проблем

Если есть ошибки, проверить логи:
```bash
# В реальном времени
tail -f meetmind.log

# Поиск по ошибкам
grep -i error meetmind.log
```

## Этап 8: Запуск как фоновый сервис (опционально)

### 8.1. Создать launch agent

```bash
mkdir -p ~/Library/LaunchAgents
cat > ~/Library/LaunchAgents/com.meetmind.daemon.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.meetmind.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3.11</string>
        <string>/path/to/meetmind/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/meetmind</string>
    <key>StandardErrorPath</key>
    <string>/path/to/meetmind/daemon.err</string>
    <key>StandardOutPath</key>
    <string>/path/to/meetmind/daemon.out</string>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
EOF
```

### 8.2. Загрузить сервис

```bash
launchctl load ~/Library/LaunchAgents/com.meetmind.daemon.plist
```

### 8.3. Управление сервисом

```bash
# Статус
launchctl list | grep meetmind

# Остановить
launchctl unload ~/Library/LaunchAgents/com.meetmind.daemon.plist

# Перезапустить
launchctl unload ~/Library/LaunchAgents/com.meetmind.daemon.plist
launchctl load ~/Library/LaunchAgents/com.meetmind.daemon.plist
```

## Этап 9: Тестирование

### 9.1. Проверить аудио запись

```python
python3 << 'EOF'
from daemon.recorder import AudioRecorder
from pathlib import Path

recorder = AudioRecorder("BlackHole 2ch")
print(f"Device ID: {recorder.device_id}")

# Записать 5 секунд
import time
recorder.start("test", Path("."))
time.sleep(5)
audio_file = recorder.stop()
print(f"Аудио файл: {audio_file}")
EOF
```

### 9.2. Проверить Whisper

```python
python3 << 'EOF'
from daemon.transcriber import Transcriber

transcriber = Transcriber("tiny")  # Для теста используем tiny
# Транскрибировать существующий аудио файл
result = transcriber.transcribe("test.wav")
print(result)
EOF
```

### 9.3. Проверить Claude

```python
python3 << 'EOF'
from daemon.summarizer import Summarizer
from daemon.session import Session
from datetime import datetime

summarizer = Summarizer("sk-ant-xxxxx")  # Ваш API ключ
session = Session("Тестовая встреча", datetime.now())

enriched = [
    {"ts": 0, "speaker": "Иван", "text": "Доброе утро все", "source": "audio"},
    {"ts": 10, "speaker": "Мария", "text": "Привет", "source": "audio"},
]

followup = summarizer.summarize(enriched, session)
print(followup)
EOF
```

## Часто встречающиеся проблемы

### "ModuleNotFoundError: No module named 'google'"

Решение:
```bash
pip install google-api-python-client google-auth-oauthlib
```

### "ConnectionError: Не удалось подключиться к BlackHole"

Решение:
1. Проверить что BlackHole установлен
2. Перезагрузить Mac
3. Проверить в System Preferences > Sound что BlackHole видно

### "OpenAI API error: Invalid API key"

Решение:
1. Проверить что в .env правильный API ключ
2. Убедиться что ключ начинается с `sk-ant-`
3. Проверить что нет пробелов в начале/конце

### WebSocket не подключается

Это нормально если расширение браузера не установлено. Демон будет работать в режиме "только аудио".

## Поддержка и отладка

Все логи сохраняются в `meetmind.log`. Для отладки:

```bash
# Посмотреть последние 50 строк
tail -50 meetmind.log

# Ищем все ошибки
grep ERROR meetmind.log

# Смотрим в реальном времени
tail -f meetmind.log
```

## Успешная установка!

Если вы видите это сообщение, значит всё работает:

```
=== MeetMind Daemon готов ===
```

Теперь добавьте встречу в Google Calendar и программа автоматически начнёт запись за 5 минут до её начала.
