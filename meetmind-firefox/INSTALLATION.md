# Установка MeetMind Firefox Расширения

## Быстрая установка (разработка)

### 1. Откройте Firefox
```
about:debugging#/runtime/this-firefox
```

### 2. Нажмите "Load Temporary Add-on"

### 3. Выберите файл `manifest.json` из папки:
```
/Users/b.botirov/ИИшки/Супер ИИшные приколы/meetmind/meetmind-firefox/
```

**Расширение установлено\!** Оно будет удалено при перезагрузке Firefox.

## Постоянная установка

1. Откройте `about:addons`
2. Нажмите значок ⚙️ (шестерёнка)
3. Выберите "Install Add-on from File"
4. Выберите папку `meetmind-firefox/`

## Проверка установки

После установки вы должны увидеть:
- Иконка 🎙️ в toolbar'е Firefox
- При нажатии на неё - popup с статусом

## Требования

- Firefox 50+
- WebSocket сервер на localhost:8765 (запустите `example_server.py`)

## Готово\!

Теперь перейдите на https://meet.google.com и создайте встречу.
События должны начать приходить на сервер.
