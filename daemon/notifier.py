"""
Системные уведомления на Mac через osascript (AppleScript).

Экранирование: AppleScript string-литералы ломаются на встроенных кавычках
и бэкслешах — v2 подставлял title/message в f-строку без экранирования
(баг). Порядок важен: сперва бэкслеши, потом кавычки — иначе кавычка,
добавленная экранированием бэкслеша, сама попала бы под второй replace.
"""
import logging
import subprocess
from typing import Optional

from daemon.session import Session

logger = logging.getLogger(__name__)


def _escape_applescript(s: str) -> str:
    """Экранировать строку для подстановки в AppleScript string-литерал."""
    return s.replace('\\', '\\\\').replace('"', '\\"')


def notify(title: str, message: str, subtitle: Optional[str] = None) -> bool:
    """Показать системное уведомление (display notification) через osascript.

    Общая точка входа для всех предохранителей (duration-limit/disk-guard/
    silence) и для Notifier-методов ниже — единственное место, где строится
    osascript-скрипт, поэтому единственное место, где нужно экранировать.

    Возвращает True при успешном вызове osascript, False при ошибке/таймауте.
    """
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
        notify(title="MeetMind", message=f"Запись встречи '{session.title}' началась")

    @staticmethod
    def notify_processing(session: Session):
        """
        Показать уведомление о обработке встречи

        Args:
            session: Объект сессии
        """
        notify(title="MeetMind", message=f"Обработка встречи '{session.title}'...")

    @staticmethod
    def notify_done(session: Session):
        """
        Показать уведомление об завершении

        Args:
            session: Объект сессии
        """
        notify(
            title="MeetMind",
            message=f"Фоллоу-ап готов для '{session.title}'",
            subtitle="Отправлено в Telegram",
        )
