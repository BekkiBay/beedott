"""
Класс сессии встречи - хранит все данные о конкретной встрече
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import uuid4


@dataclass
class Session:
    """Представление одной встречи/сессии"""

    # Основные данные
    title: str
    start_time: datetime
    attendees: list[str] = field(default_factory=list)
    end_time: Optional[datetime] = None
    meet_url: Optional[str] = None

    # Статус
    status: str = "scheduled"  # scheduled, recording, processing, done, cancelled

    # Аудио и временные файлы
    audio_file: Optional[str] = None

    # События от расширений
    speaker_events: list[dict] = field(default_factory=list)
    chat_messages: list[dict] = field(default_factory=list)

    # Метаданные сессии
    id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        """Преобразовать сессию в словарь для JSON"""
        return {
            'id': self.id,
            'title': self.title,
            'start_time': self.start_time.isoformat(),
            'end_time': self.end_time.isoformat() if self.end_time else None,
            'attendees': self.attendees,
            'meet_url': self.meet_url,
            'status': self.status,
            'audio_file': self.audio_file,
            'speaker_events': self.speaker_events,
            'chat_messages': self.chat_messages,
            'created_at': self.created_at.isoformat(),
        }

    def get_duration_minutes(self) -> int:
        """Длительность встречи в минутах. Если ещё идёт — от старта до сейчас."""
        end = self.end_time or datetime.now()
        duration = end - self.start_time
        return max(0, int(duration.total_seconds() / 60))

    def is_active(self) -> bool:
        """Проверить, активна ли встреча в данный момент"""
        now = datetime.now()
        return self.start_time <= now and (self.end_time is None or now < self.end_time)
