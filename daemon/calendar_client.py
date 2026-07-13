"""
Клиент для работы с Google Calendar
"""
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient import discovery

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']


class CalendarClient:
    """Клиент для работы с Google Calendar"""

    def __init__(self, credentials_path: str = 'credentials.json'):
        """
        Инициализация клиента календаря

        Args:
            credentials_path: Путь к credentials.json файлу от Google
        """
        self.credentials_path = Path(credentials_path)
        self.token_path = Path('token.json')
        self.service = None
        self._authenticate()

    def _authenticate(self):
        """Аутентификация через OAuth2"""
        creds = None

        # Загружаем сохранённый token если существует
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)

        # Если нет валидного токена - делаем OAuth flow
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.credentials_path.exists():
                    logger.error(f'Файл {self.credentials_path} не найден. '
                               f'Скачайте credentials.json из Google Cloud Console')
                    return

                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), SCOPES)
                creds = flow.run_local_server(port=0)

            # Сохраняем токен для следующего раза
            with open(self.token_path, 'w') as token_file:
                token_file.write(creds.to_json())

        self.service = discovery.build('calendar', 'v3', credentials=creds)
        logger.info('Успешно подключены к Google Calendar')

    def get_upcoming_events(self, hours_ahead: int = 24) -> list[dict]:
        """
        Получить предстоящие встречи

        Args:
            hours_ahead: Сколько часов вперёд смотреть

        Returns:
            Список встреч с полями: id, title, start, end, attendees, meet_url
        """
        try:
            now = datetime.utcnow().isoformat() + 'Z'
            time_max = (datetime.utcnow() + timedelta(hours=hours_ahead)).isoformat() + 'Z'

            events_result = self.service.events().list(
                calendarId='primary',
                timeMin=now,
                timeMax=time_max,
                maxResults=50,
                singleEvents=True,
                orderBy='startTime'
            ).execute()

            events = events_result.get('items', [])
            result = []

            for event in events:
                # Пропускаем отменённые события
                if event.get('status') == 'cancelled':
                    continue

                parsed_event = {
                    'id': event.get('id'),
                    'title': event.get('summary', 'Без названия'),
                    'start': event.get('start', {}).get('dateTime'),
                    'end': event.get('end', {}).get('dateTime'),
                    'attendees': self._extract_attendees(event),
                    'meet_url': self._extract_meet_url(event),
                    'description': event.get('description', ''),
                }

                result.append(parsed_event)

            logger.debug(f'Получено {len(result)} предстоящих встреч')
            return result

        except Exception as e:
            logger.error(f'Ошибка при получении встреч: {e}')
            return []

    def _extract_attendees(self, event: dict) -> list[str]:
        """Извлечь список участников из события"""
        attendees = []
        for attendee in event.get('attendees', []):
            email = attendee.get('email')
            if email:
                attendees.append(email)
        return attendees

    def _extract_meet_url(self, event: dict) -> Optional[str]:
        """Извлечь URL встречи из события"""
        # Проверяем conferenceData
        conference_data = event.get('conferenceData', {})
        entry_points = conference_data.get('entryPoints', [])

        for entry in entry_points:
            if entry.get('entryPointType') == 'video':
                return entry.get('uri')

        # Ищем в описании
        description = event.get('description', '')
        import re
        match = re.search(r'(https://meet\.google\.com/[a-z-]+)', description)
        if match:
            return match.group(1)

        return None

    def get_event(self, event_id: str) -> Optional[dict]:
        """
        Получить информацию о конкретной встрече

        Args:
            event_id: ID встречи

        Returns:
            Данные встречи или None
        """
        try:
            event = self.service.events().get(
                calendarId='primary',
                eventId=event_id
            ).execute()

            return {
                'id': event.get('id'),
                'title': event.get('summary'),
                'start': event.get('start', {}).get('dateTime'),
                'end': event.get('end', {}).get('dateTime'),
                'attendees': self._extract_attendees(event),
                'meet_url': self._extract_meet_url(event),
            }
        except Exception as e:
            logger.error(f'Ошибка при получении встречи {event_id}: {e}')
            return None
