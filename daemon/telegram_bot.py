"""
Класс для отправки сообщений в Telegram
"""
import logging

from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Отправка фоллоу-апов в Telegram"""

    def __init__(self, bot_token: str, chat_id: str):
        """
        Инициализация Telegram бота

        Args:
            bot_token: Токен бота
            chat_id: ID чата для отправки
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.bot = Bot(token=bot_token)

    def send_followup(self, text: str, session_title: str) -> bool:
        """
        Отправить фоллоу-ап в Telegram

        Args:
            text: Текст фоллоу-апа в Markdown
            session_title: Название встречи

        Returns:
            True если отправлено успешно
        """
        if not self.bot_token or not self.chat_id:
            logger.error('Telegram конфигурация не полная')
            return False

        try:
            # Разбиваем текст если больше 4096 символов
            if len(text) <= 4096:
                messages = [text]
            else:
                messages = self._split_message(text)

            # Отправляем каждую часть
            for i, message in enumerate(messages):
                # Добавляем header к первому сообщению
                if i == 0:
                    message = f'*Фоллоу-ап встречи:*\n{message}'

                self.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    parse_mode='Markdown',
                    disable_web_page_preview=True
                )
                logger.debug(f'Отправлено сообщение {i+1}/{len(messages)}')

            logger.info(f'Фоллоу-ап встречи "{session_title}" отправлен в Telegram')
            return True

        except TelegramError as e:
            logger.error(f'Ошибка Telegram: {e}')
            return False
        except Exception as e:
            logger.error(f'Ошибка при отправке в Telegram: {e}')
            return False

    @staticmethod
    def _split_message(text: str, max_length: int = 4000) -> list[str]:
        """
        Разбить длинное сообщение на части

        Args:
            text: Исходный текст
            max_length: Максимальная длина части

        Returns:
            Список частей сообщения
        """
        messages = []
        current = ''

        # Разбиваем по абзацам (двойные переносы)
        paragraphs = text.split('\n\n')

        for paragraph in paragraphs:
            if len(current) + len(paragraph) + 2 <= max_length:
                current += paragraph + '\n\n'
            else:
                if current:
                    messages.append(current.strip())
                current = paragraph + '\n\n'

        if current:
            messages.append(current.strip())

        return messages

    def send_notification(self, message: str) -> bool:
        """
        Отправить простое уведомление

        Args:
            message: Текст уведомления

        Returns:
            True если отправлено
        """
        try:
            self.bot.send_message(
                chat_id=self.chat_id,
                text=message
            )
            logger.debug(f'Уведомление отправлено: {message[:50]}...')
            return True
        except Exception as e:
            logger.error(f'Ошибка при отправке уведомления: {e}')
            return False
