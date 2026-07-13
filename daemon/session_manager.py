"""
Менеджер активных сессий встреч
"""
import logging
from datetime import datetime
from typing import Optional

from daemon.session import Session

logger = logging.getLogger(__name__)


class SessionManager:
    """Управление активными сессиями встреч"""

    def __init__(self):
        """Инициализация менеджера"""
        self.active_sessions = {}  # session_id -> Session
        self.completed_sessions = []

    def create_session(self, title: str, start_time: datetime, attendees: list[str] = None, meet_url: str = None) -> Session:
        """
        Создать новую сессию

        Args:
            title: Название встречи
            start_time: Время начала
            attendees: Список участников
            meet_url: URL встречи

        Returns:
            Новый объект Session
        """
        session = Session(
            title=title,
            start_time=start_time,
            attendees=attendees or [],
            meet_url=meet_url,
        )
        self.active_sessions[session.id] = session
        logger.info(f'Создана новая сессия: {session.id} ({title})')
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """
        Получить сессию по ID

        Args:
            session_id: ID сессии

        Returns:
            Объект Session или None
        """
        return self.active_sessions.get(session_id)

    def update_session_status(self, session_id: str, status: str):
        """
        Обновить статус сессии

        Args:
            session_id: ID сессии
            status: Новый статус
        """
        session = self.get_session(session_id)
        if session:
            old_status = session.status
            session.status = status
            logger.debug(f'Статус сессии {session_id} изменился: {old_status} -> {status}')

    def add_speaker_event(self, session_id: str, name: str, ts: float):
        """
        Добавить событие о смене спикера

        Args:
            session_id: ID сессии
            name: Имя спикера
            ts: Время события (секунды от начала встречи)
        """
        session = self.get_session(session_id)
        if session:
            session.speaker_events.append({
                'name': name,
                'ts': ts,
            })
            logger.debug(f'Добавлено событие спикера: {name} в {ts}с')

    def add_chat_message(self, session_id: str, name: str, text: str, ts: float):
        """
        Добавить сообщение чата

        Args:
            session_id: ID сессии
            name: Отправитель
            text: Текст сообщения
            ts: Время события
        """
        session = self.get_session(session_id)
        if session:
            session.chat_messages.append({
                'name': name,
                'text': text,
                'ts': ts,
            })
            logger.debug(f'Добавлено сообщение чата от {name}')

    def end_session(self, session_id: str) -> Optional[Session]:
        """
        Завершить сессию

        Args:
            session_id: ID сессии

        Returns:
            Завершённая сессия или None
        """
        session = self.active_sessions.pop(session_id, None)
        if session:
            session.end_time = datetime.now()
            session.status = 'done'
            self.completed_sessions.append(session)
            logger.info(f'Сессия завершена: {session.id} ({session.title})')
            return session
        return None

    def cancel_session(self, session_id: str) -> Optional[Session]:
        """
        Отменить сессию

        Args:
            session_id: ID сессии

        Returns:
            Отменённая сессия или None
        """
        session = self.active_sessions.pop(session_id, None)
        if session:
            session.status = 'cancelled'
            self.completed_sessions.append(session)
            logger.info(f'Сессия отменена: {session.id} ({session.title})')
            return session
        return None

    def get_active_session(self) -> Optional[Session]:
        """
        Получить текущую активную сессию (если она одна)

        Returns:
            Активная сессия или None
        """
        if len(self.active_sessions) == 1:
            return list(self.active_sessions.values())[0]
        return None

    def has_active_session(self) -> bool:
        """
        Проверить есть ли активная сессия

        Returns:
            True если есть активная сессия
        """
        return len(self.active_sessions) > 0

    def get_all_active(self) -> list[Session]:
        """
        Получить все активные сессии

        Returns:
            Список активных сессий
        """
        return list(self.active_sessions.values())
