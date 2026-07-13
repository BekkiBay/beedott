"""
Класс для обогащения транскрипта данными из других источников
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class Enricher:
    """Объединение аудио, событий спикеров и сообщений чата"""

    @staticmethod
    def enrich(
        transcript_segments: list[dict],
        speaker_events: list[dict],
        chat_messages: list[dict]
    ) -> list[dict]:
        """
        Обогатить транскрипт данными о спикерах и чате

        Args:
            transcript_segments: Сегменты из Whisper
            speaker_events: События о смене спикера из расширения
            chat_messages: Сообщения чата из расширения

        Returns:
            Список обогащённых элементов встречи
        """
        enriched = []

        # Сначала добавляем аудио сегменты с привязкой к спикерам
        for segment in transcript_segments:
            start_time = segment['start']
            speaker_name = Enricher._find_speaker_at_time(start_time, speaker_events)

            enriched.append({
                'ts': start_time,
                'end_ts': segment['end'],
                'speaker': speaker_name or 'Неизвестно',
                'text': segment['text'],
                'source': 'audio',
            })

        # Добавляем сообщения чата
        for msg in chat_messages:
            try:
                # Парсим время если оно в формате строки
                if isinstance(msg.get('ts'), str):
                    ts_float = float(msg['ts'])
                else:
                    ts_float = msg.get('ts', 0)

                enriched.append({
                    'ts': ts_float,
                    'speaker': msg.get('name', 'Неизвестно'),
                    'text': msg.get('text', ''),
                    'source': 'chat',
                })
            except Exception as e:
                logger.warning(f'Ошибка при обработке сообщения чата: {e}')

        # Сортируем по времени
        enriched.sort(key=lambda x: x['ts'])

        logger.debug(f'Обогащено {len(enriched)} элементов встречи')
        return enriched

    @staticmethod
    def _find_speaker_at_time(timestamp: float, speaker_events: list[dict]) -> str:
        """
        Найти спикера для заданного времени

        Args:
            timestamp: Время в секундах от начала встречи
            speaker_events: События о смене спикера

        Returns:
            Имя спикера или пустая строка
        """
        # Ищем событие спикера ближайшее к заданному времени
        closest_event = None
        closest_distance = float('inf')

        for event in speaker_events:
            try:
                event_ts = event.get('ts')
                if isinstance(event_ts, str):
                    event_ts = float(event_ts)

                # Ищем событие, которое произошло ДО или В момент текущего timestamp
                if event_ts <= timestamp:
                    distance = timestamp - event_ts
                    if distance < closest_distance:
                        closest_distance = distance
                        closest_event = event
            except Exception as e:
                logger.debug(f'Ошибка при обработке события спикера: {e}')

        if closest_event:
            return closest_event.get('name', '')

        return ''
