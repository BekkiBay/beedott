"""
WebSocket сервер для приёма событий от Firefox расширения
"""
import asyncio
import json
import logging
from typing import Callable, Optional

import websockets
from websockets.server import WebSocketServerProtocol

logger = logging.getLogger(__name__)


class WebSocketServer:
    """WebSocket сервер для приёма событий от браузера"""

    def __init__(self, host: str = 'localhost', port: int = 8765):
        """
        Инициализация WebSocket сервера

        Args:
            host: Хост для прослушивания
            port: Порт для прослушивания
        """
        self.host = host
        self.port = port
        self.server = None
        self.clients = set()
        self.event_callbacks = []
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def add_event_callback(self, callback: Callable):
        """
        Добавить callback для событий

        Args:
            callback: Функция которая будет вызвана при событии
        """
        self.event_callbacks.append(callback)

    async def _handle_connection(self, websocket: WebSocketServerProtocol, path: str):
        """
        Обработчик нового подключения

        Args:
            websocket: WebSocket соединение
            path: Путь подключения
        """
        self.clients.add(websocket)
        logger.info(f'Новое подключение от {websocket.remote_address}')

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self._process_event(data)
                except json.JSONDecodeError:
                    logger.warning(f'Невалидный JSON: {message}')
                except Exception as e:
                    logger.error(f'Ошибка при обработке события: {e}')

        except websockets.exceptions.ConnectionClosed:
            logger.info(f'Соединение закрыто от {websocket.remote_address}')
        except Exception as e:
            logger.error(f'Ошибка в соединении: {e}')
        finally:
            self.clients.discard(websocket)

    async def _process_event(self, event_data: dict):
        """
        Обработить событие от браузера

        Args:
            event_data: Данные события
        """
        event_type = event_data.get('type')
        logger.debug(f'Получено событие: {event_type}')

        # Вызываем все зарегистрированные callbacks
        for callback in self.event_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event_data)
                else:
                    callback(event_data)
            except Exception as e:
                logger.error(f'Ошибка в callback: {e}')

    async def start(self):
        """Запустить WebSocket сервер"""
        try:
            logger.info(f'Запуск WebSocket сервера на ws://{self.host}:{self.port}')
            self.server = await websockets.serve(
                self._handle_connection,
                self.host,
                self.port
            )
            logger.info(f'WebSocket сервер запущен успешно')
        except Exception as e:
            logger.error(f'Ошибка при запуске WebSocket сервера: {e}')

    async def stop(self):
        """Остановить WebSocket сервер"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info('WebSocket сервер остановлен')

    def run(self):
        """Запустить сервер в асинхронном цикле"""
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            # Запускаем сервер и ждём его завершения
            self.loop.run_until_complete(self.start())
            self.loop.run_forever()

        except KeyboardInterrupt:
            logger.info('Получена сигнал KeyboardInterrupt')
        except Exception as e:
            logger.error(f'Ошибка при работе сервера: {e}')
        finally:
            if self.loop:
                self.loop.run_until_complete(self.stop())
                self.loop.close()

    async def broadcast_message(self, message: dict):
        """
        Отправить сообщение всем подключённым клиентам

        Args:
            message: Словарь для отправки
        """
        if not self.clients:
            return

        message_json = json.dumps(message)
        for client in self.clients:
            try:
                await client.send(message_json)
            except Exception as e:
                logger.warning(f'Ошибка при отправке сообщения: {e}')
