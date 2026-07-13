"""
Класс для отправки системных уведомлений на Mac
"""
import logging
import subprocess
from pathlib import Path

from daemon.session import Session

logger = logging.getLogger(__name__)


class Notifier:
    """Система уведомлений для Mac через osascript"""

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

        # AppleScript для диалога с двумя кнопками
        script = f"""
        display dialog "Встреча '{title}' начинается в {start_time}" \\
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
        script = f"""
        display notification "Запись встречи '{session.title}' началась" \\
            with title "MeetMind"
        """

        try:
            subprocess.run(['osascript', '-e', script], check=True)
        except Exception as e:
            logger.error(f'Ошибка при отправке уведомления: {e}')

    @staticmethod
    def notify_processing(session: Session):
        """
        Показать уведомление о обработке встречи

        Args:
            session: Объект сессии
        """
        script = f"""
        display notification "Обработка встречи '{session.title}'..." \\
            with title "MeetMind"
        """

        try:
            subprocess.run(['osascript', '-e', script], check=True)
        except Exception as e:
            logger.error(f'Ошибка при отправке уведомления: {e}')

    @staticmethod
    def notify_done(session: Session):
        """
        Показать уведомление об завершении

        Args:
            session: Объект сессии
        """
        script = f"""
        display notification "Фоллоу-ап готов для '{session.title}'" \\
            with title "MeetMind" \\
            subtitle "Отправлено в Telegram"
        """

        try:
            subprocess.run(['osascript', '-e', script], check=True)
        except Exception as e:
            logger.error(f'Ошибка при отправке уведомления: {e}')
