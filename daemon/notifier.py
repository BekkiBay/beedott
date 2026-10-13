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

from daemon.session import Session

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
    silence) и для Notifier-методов ниже — единственное место, где строится
    osascript-скрипт, поэтому единственное место, где нужно экранировать.

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


class Notifier:
    """Уведомления, завязанные на Session (диалоги встреч, статусы)."""

    @staticmethod
    def notify_upcoming(session: Session) -> bool:
        """
        Показать уведомление о предстоящей встрече

        Args:
            session: Объект сессии

        Returns:
            True если нажато "ГО", False если "ОТМЕН"
        """
        start_time = session.start_time.strftime('%H:%M')
        title = session.title[:50]  # Обрезаем длинные названия
        body = _escape_applescript(f"Встреча '{title}' начинается в {start_time}")

        # AppleScript для диалога с двумя кнопками
        script = f"""
        display dialog "{body}" \\
            buttons {{"ГО", "ОТМЕН"}} \\
            default button 1 \\
            with icon caution
        """

        try:
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=60  # Максимум 60 секунд ожидания ответа
            )

            if result.returncode == 0:
                # Проверяем нажатую кнопку
                if 'ГО' in result.stdout:
                    logger.info(f'Пользователь нажал ГО для встречи {session.title}')
                    Notifier._open_meet_url(session.meet_url)
                    return True
                else:
                    logger.info(f'Пользователь отменил встречу {session.title}')
                    session.status = 'cancelled'
                    return False
            else:
                logger.warning('Диалог был закрыт или произошла ошибка')
                return False

        except subprocess.TimeoutExpired:
            logger.warning('Таймаут при ожидании ответа на уведомление')
            return False
        except Exception as e:
            logger.error(f'Ошибка при показе уведомления: {e}')
            return False

    @staticmethod
    def _open_meet_url(meet_url: str):
        """
        Открыть URL встречи в браузере

        Args:
            meet_url: URL встречи
        """
        if not meet_url:
            logger.warning('URL встречи не доступен')
            return

        try:
            subprocess.run(['open', meet_url], check=True)
            logger.info(f'Открыт URL встречи: {meet_url}')
        except Exception as e:
            logger.error(f'Ошибка при открытии URL: {e}')

    @staticmethod
    def notify_recording_started(session: Session):
        """
        Показать простое уведомление о начале записи

        Args:
            session: Объект сессии
        """
        notify(title="BeeDott", message=f"Запись встречи '{session.title}' началась")

    @staticmethod
    def notify_processing(session: Session):
        """
        Показать уведомление о обработке встречи

        Args:
            session: Объект сессии
        """
        notify(title="BeeDott", message=f"Обработка встречи '{session.title}'...")

    @staticmethod
    def notify_done(session: Session):
        """
        Показать уведомление об завершении

        Args:
            session: Объект сессии
        """
        notify(
            title="BeeDott",
            message=f"Фоллоу-ап готов для '{session.title}'",
            subtitle="Отправлено в Telegram",
        )
