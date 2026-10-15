"""
Системные уведомления на Mac.

Пока MeetMind.app на связи (опрашивает демон через notifications_pop не реже
APP_POLL_TTL_SEC), уведомления копятся в очереди и показываются приложением:
от имени MeetMind, с переходом в карточку встречи по клику (продукт P16).
Без приложения (демон из терминала) — как раньше, через osascript.

Экранирование: AppleScript string-литералы ломаются на встроенных кавычках
и бэкслешах — v2 подставлял title/message в f-строку без экранирования
(баг). Порядок важен: сперва бэкслеши, потом кавычки — иначе кавычка,
добавленная экранированием бэкслеша, сама попала бы под второй replace.
"""
import logging
import subprocess
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)


def _escape_applescript(s: str) -> str:
    """Экранировать строку для подстановки в AppleScript string-литерал."""
    return s.replace('\\', '\\\\').replace('"', '\\"')


APP_POLL_TTL_SEC = 15  # приложение опрашивает каждые 3 с; 15 с тишины — его нет

_clock = time.monotonic
_hub_lock = threading.Lock()
_queue: deque = deque(maxlen=100)
_next_id = 1
_app_seen_at: Optional[float] = None


def _reset_hub() -> None:
    """Для тестов: забыть приложение и очередь."""
    global _next_id, _app_seen_at
    with _hub_lock:
        _queue.clear()
        _next_id = 1
        _app_seen_at = None


def pop_for_app() -> list[dict]:
    """Забрать накопленные уведомления (вызывает MeetMind.app на каждом опросе)
    и отметить, что приложение на связи."""
    global _app_seen_at
    with _hub_lock:
        _app_seen_at = _clock()
        items = list(_queue)
        _queue.clear()
    return items


def _app_listening() -> bool:
    return _app_seen_at is not None and _clock() - _app_seen_at <= APP_POLL_TTL_SEC


def notify(title: str, message: str, subtitle: Optional[str] = None, *,
           session_id: Optional[str] = None) -> bool:
    """Показать уведомление: через приложение, если оно на связи (session_id —
    какую встречу открыть по клику), иначе через osascript.

    Общая точка входа для всех предохранителей (duration-limit/disk-guard/
    silence) — единственное место, где строится osascript-скрипт, поэтому
    единственное место, где нужно экранировать.

    Возвращает True при успешной постановке в очередь / вызове osascript,
    False при ошибке/таймауте.
    """
    global _next_id
    with _hub_lock:
        if _app_listening():
            _queue.append({"id": _next_id, "title": title,
                           "message": f"{subtitle}\n{message}" if subtitle else message,
                           "session_id": session_id})
            _next_id += 1
            return True
    t = _escape_applescript(title)
    m = _escape_applescript(message)
    script = f'display notification "{m}" with title "{t}"'
    if subtitle is not None:
        script += f' subtitle "{_escape_applescript(subtitle)}"'

    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return True
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления: {e}")
        return False

