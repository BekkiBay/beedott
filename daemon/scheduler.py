"""
Планировщик встреч с APScheduler
"""
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from daemon.session import Session

logger = logging.getLogger(__name__)


class MeetingScheduler:
    """Планирование записи встреч из Google Calendar"""

    def __init__(self):
        """Инициализация планировщика"""
        self.scheduler = BackgroundScheduler()
        self.scheduled_events = {}  # event_id -> Session
        self.on_meeting_start: Optional[Callable] = None
        self.on_calendar_update: Optional[Callable] = None

    def start(self):
        """Запустить планировщик"""
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info('Планировщик встреч запущен')

            # Планируем проверку календаря каждые 2 минуты
            self.scheduler.add_job(
                self.check_calendar,
                IntervalTrigger(minutes=2),
                id='calendar_checker',
                replace_existing=True
            )

    def stop(self):
        """Остановить планировщик"""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info('Планировщик встреч остановлен')

    def set_on_meeting_start(self, callback: Callable):
        """
        Установить callback для начала встречи

        Args:
            callback: Функция callback(session)
        """
        self.on_meeting_start = callback

    def set_on_calendar_update(self, callback: Callable):
        """
        Установить callback для обновления календаря

        Args:
            callback: Функция callback(upcoming_events)
        """
        self.on_calendar_update = callback

    def check_calendar(self, calendar_client) -> list[dict]:
        """
        Проверить календарь и запланировать встречи

        Args:
            calendar_client: Объект CalendarClient

        Returns:
            Список предстоящих встреч
        """
        try:
            # Получаем встречи на следующие 10 минут
            upcoming = calendar_client.get_upcoming_events(hours_ahead=0.3)  # ~18 минут

            now = datetime.now()

            for event in upcoming:
                event_id = event['id']

                # Парсим время начала
                try:
                    start_time = datetime.fromisoformat(
                        event['start'].replace('Z', '+00:00')
                    ).replace(tzinfo=None)
                except:
                    continue

                time_until = (start_time - now).total_seconds() / 60

                # Если встреча уже запланирована - пропускаем
                if event_id in self.scheduled_events:
                    continue

                logger.debug(f'Встреча "{event["title"]}" начинается через {time_until:.1f} минут')

                # Если встреча в ближайшие 10-1 минут - немедленно запускаем
                if 0 <= time_until <= 10:
                    self._schedule_meeting_start(event, time_until)

                # Если встреча дальше - планируем на 5 минут до начала
                elif time_until > 10:
                    delay_minutes = time_until - 5
                    if delay_minutes > 0:
                        logger.debug(f'Планируем начало записи через {delay_minutes:.1f} минут')
                        self.scheduler.add_job(
                            self._trigger_meeting_start,
                            args=[event],
                            id=f'meeting_{event_id}',
                            replace_existing=True,
                            seconds=delay_minutes * 60
                        )

            # Уведомляем об обновлении календаря
            if self.on_calendar_update:
                self.on_calendar_update(upcoming)

            return upcoming

        except Exception as e:
            logger.error(f'Ошибка при проверке календаря: {e}')
            return []

    def _schedule_meeting_start(self, event: dict, time_until: float):
        """
        Запланировать начало записи встречи

        Args:
            event: Данные события из календаря
            time_until: Минут до начала встречи
        """
        event_id = event['id']

        # Создаём объект сессии
        start_time = datetime.fromisoformat(
            event['start'].replace('Z', '+00:00')
        ).replace(tzinfo=None)

        session = Session(
            title=event['title'],
            start_time=start_time,
            attendees=event.get('attendees', []),
            meet_url=event.get('meet_url'),
        )

        self.scheduled_events[event_id] = session
        logger.info(f'Сессия создана: {session.title} (через {time_until:.1f} мин)')

        # Планируем начало записи в зависимости от времени
        if time_until <= 1:
            # Если меньше 1 минуты - сразу
            self._trigger_meeting_start(event)
        elif time_until <= 5:
            # Если 1-5 минут - запускаем немедленно
            logger.info(f'Запуск записи встречи "{event["title"]}" (начало через {time_until:.1f} мин)')
            if self.on_meeting_start:
                self.on_meeting_start(session)
        else:
            # Иначе планируем на нужное время
            delay = max(0, time_until - 5) * 60  # За 5 минут до начала
            logger.debug(f'Планируем запуск через {delay} секунд')
            self.scheduler.add_job(
                self._trigger_meeting_start,
                args=[event],
                id=f'meeting_{event_id}',
                replace_existing=True,
                seconds=int(delay)
            )

    def _trigger_meeting_start(self, event: dict):
        """
        Триггер начала встречи

        Args:
            event: Данные события
        """
        event_id = event['id']

        if event_id not in self.scheduled_events:
            # Создаём новую сессию
            start_time = datetime.fromisoformat(
                event['start'].replace('Z', '+00:00')
            ).replace(tzinfo=None)

            session = Session(
                title=event['title'],
                start_time=start_time,
                attendees=event.get('attendees', []),
                meet_url=event.get('meet_url'),
            )
            self.scheduled_events[event_id] = session
        else:
            session = self.scheduled_events[event_id]

        logger.info(f'Запуск встречи: {session.title}')

        if self.on_meeting_start:
            self.on_meeting_start(session)

    def remove_event(self, event_id: str):
        """
        Удалить встречу из расписания

        Args:
            event_id: ID события
        """
        if event_id in self.scheduled_events:
            del self.scheduled_events[event_id]
            logger.debug(f'Встреча {event_id} удалена')
