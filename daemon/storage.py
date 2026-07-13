"""
Класс для сохранения данных встреч на диск
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from daemon.session import Session

logger = logging.getLogger(__name__)


class Storage:
    """Управление сохранением и загрузкой данных сессий"""

    def __init__(self, sessions_dir: Path):
        """
        Инициализация хранилища

        Args:
            sessions_dir: Директория для сохранения сессий
        """
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(exist_ok=True)

    def save_session(self, session: Session, followup_text: str, enriched_transcript: list[dict]) -> Optional[Path]:
        """
        Сохранить сессию с фоллоу-апом

        Args:
            session: Объект сессии
            followup_text: Текст фоллоу-апа
            enriched_transcript: Обогащённый транскрипт

        Returns:
            Путь к папке сессии или None при ошибке
        """
        try:
            # Создаём папку с именем дата_slug
            date_str = session.start_time.strftime('%Y-%m-%d')
            slug = self._slugify(session.title)
            session_dir = self.sessions_dir / f'{date_str}_{slug}_{session.id[:8]}'
            session_dir.mkdir(exist_ok=True)

            # Сохраняем метаданные
            meta = {
                'id': session.id,
                'title': session.title,
                'start_time': session.start_time.isoformat(),
                'end_time': session.end_time.isoformat() if session.end_time else None,
                'attendees': session.attendees,
                'meet_url': session.meet_url,
                'duration_minutes': session.get_duration_minutes(),
                'created_at': session.created_at.isoformat(),
            }

            meta_file = session_dir / 'meta.json'
            with open(meta_file, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            # Сохраняем обогащённый транскрипт
            enriched_file = session_dir / 'enriched.json'
            with open(enriched_file, 'w', encoding='utf-8') as f:
                json.dump(enriched_transcript, f, ensure_ascii=False, indent=2)

            # Сохраняем фоллоу-ап
            followup_file = session_dir / 'followup.md'
            with open(followup_file, 'w', encoding='utf-8') as f:
                f.write(followup_text)

            logger.info(f'Сессия сохранена в {session_dir}')
            return session_dir

        except Exception as e:
            logger.error(f'Ошибка при сохранении сессии: {e}')
            return None

    def list_sessions(self) -> list[dict]:
        """
        Получить список всех сохранённых сессий

        Returns:
            Список сессий отсортированный по дате (самые новые первыми)
        """
        sessions = []

        try:
            for session_dir in sorted(self.sessions_dir.iterdir(), reverse=True):
                if not session_dir.is_dir():
                    continue

                meta_file = session_dir / 'meta.json'
                if meta_file.exists():
                    try:
                        with open(meta_file, 'r', encoding='utf-8') as f:
                            meta = json.load(f)

                        sessions.append({
                            'path': str(session_dir),
                            'id': meta.get('id'),
                            'title': meta.get('title'),
                            'start_time': meta.get('start_time'),
                            'duration': meta.get('duration_minutes'),
                        })
                    except Exception as e:
                        logger.warning(f'Ошибка при чтении {meta_file}: {e}')

        except Exception as e:
            logger.error(f'Ошибка при получении списка сессий: {e}')

        return sessions

    def get_session(self, session_id: str) -> Optional[dict]:
        """
        Получить полные данные сессии

        Args:
            session_id: ID сессии

        Returns:
            Данные сессии или None
        """
        try:
            # Ищем папку по полному ID внутри meta.json
            for session_dir in sorted(self.sessions_dir.iterdir(), reverse=True):
                if not session_dir.is_dir():
                    continue

                meta_file = session_dir / 'meta.json'
                if meta_file.exists():
                    with open(meta_file, 'r', encoding='utf-8') as f:
                        meta = json.load(f)

                    # Сравниваем по полному ID из meta.json
                    if meta.get('id') != session_id:
                        continue

                    if True:  # найдено

                        # Читаем остальные файлы
                        enriched_file = session_dir / 'enriched.json'
                        followup_file = session_dir / 'followup.md'

                        enriched = []
                        followup = ''

                        if enriched_file.exists():
                            with open(enriched_file, 'r', encoding='utf-8') as f:
                                enriched = json.load(f)

                        if followup_file.exists():
                            with open(followup_file, 'r', encoding='utf-8') as f:
                                followup = f.read()

                        return {
                            'meta': meta,
                            'enriched': enriched,
                            'followup': followup,
                        }

        except Exception as e:
            logger.error(f'Ошибка при получении сессии: {e}')

        return None

    @staticmethod
    def _slugify(text: str) -> str:
        """
        Преобразовать текст в безопасное имя файла

        Args:
            text: Исходный текст

        Returns:
            Slug для использования в имени файла
        """
        # Берём только ASCII символы и дефисы
        slug = ''
        for char in text.lower():
            if char.isalnum() or char in '-_ ':
                slug += char
            else:
                slug += '_'

        # Заменяем пробелы на дефисы
        slug = slug.replace(' ', '-').replace('_', '-')

        # Удаляем множественные дефисы
        while '--' in slug:
            slug = slug.replace('--', '-')

        # Обрезаем до 50 символов
        return slug[:50]
