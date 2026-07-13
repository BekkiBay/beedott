#\!/usr/bin/env python3
"""
MeetMind WebSocket Server - Пример реализации
Получает события от Firefox расширения и выводит их в консоль
"""

import asyncio
import websockets
import json
from datetime import datetime
from typing import Set
import sys

class MeetMindServer:
    def __init__(self, host: str = "localhost", port: int = 8765):
        self.host = host
        self.port = port
        self.clients: Set[websockets.WebSocketServerProtocol] = set()
        self.current_meeting = None
        self.participants = set()
        
    async def register(self, websocket: websockets.WebSocketServerProtocol):
        """Регистрирует новое подключение"""
        self.clients.add(websocket)
        print(f"[{self._timestamp()}] ✓ Клиент подключился. Всего клиентов: {len(self.clients)}")
        
    async def unregister(self, websocket: websockets.WebSocketServerProtocol):
        """Отменяет регистрацию подключения"""
        self.clients.discard(websocket)
        print(f"[{self._timestamp()}] ✗ Клиент отключился. Всего клиентов: {len(self.clients)}")
        
    async def handle_event(self, event: dict):
        """Обрабатывает событие от расширения"""
        event_type = event.get('type', 'unknown')
        timestamp = event.get('ts', self._timestamp())
        meeting_id = event.get('meeting_id', 'unknown')
        
        # Обновляем состояние текущей встречи
        if event_type == 'meeting_start':
            self.current_meeting = meeting_id
            self.participants.clear()
            print(f"\n{'='*70}")
            print(f"[{timestamp}] 🎤 ВСТРЕЧА НАЧАЛАСЬ: {meeting_id}")
            print(f"{'='*70}\n")
            
        elif event_type == 'meeting_end':
            print(f"\n{'='*70}")
            print(f"[{timestamp}] 📞 ВСТРЕЧА ЗАКОНЧИЛАСЬ: {meeting_id}")
            print(f"Участников было: {len(self.participants)}")
            print(f"{'='*70}\n")
            self.current_meeting = None
            self.participants.clear()
            
        elif event_type == 'speaker':
            name = event.get('name', 'Unknown')
            print(f"[{timestamp}] 🎤 ГОВОРИТ: {name}")
            
        elif event_type == 'chat':
            name = event.get('name', 'Unknown')
            text = event.get('text', '')
            print(f"[{timestamp}] 💬 {name}: {text}")
            
        elif event_type == 'participant_join':
            name = event.get('name', 'Unknown')
            self.participants.add(name)
            print(f"[{timestamp}] ✅ ПРИСОЕДИНИЛСЯ: {name} (всего: {len(self.participants)})")
            
        elif event_type == 'participant_leave':
            name = event.get('name', 'Unknown')
            self.participants.discard(name)
            print(f"[{timestamp}] ❌ ПОКИНУЛ: {name} (всего: {len(self.participants)})")
            
        else:
            print(f"[{timestamp}] ❓ НЕИЗВЕСТНОЕ СОБЫТИЕ: {event_type}")

    async def broadcast(self, event: dict):
        """Отправляет событие всем подключённым клиентам"""
        if not self.clients:
            return
            
        message = json.dumps(event)
        # Для примера можно здесь добавить рассылку событий другим клиентам
        for client in self.clients:
            try:
                await client.send(json.dumps({
                    'status': 'received',
                    'event_type': event.get('type')
                }))
            except websockets.exceptions.ConnectionClosed:
                pass
    
    async def handler(self, websocket: websockets.WebSocketServerProtocol, path: str):
        """Обработчик WebSocket соединения"""
        await self.register(websocket)
        try:
            async for message in websocket:
                try:
                    event = json.loads(message)
                    await self.handle_event(event)
                    await self.broadcast(event)
                except json.JSONDecodeError as e:
                    print(f"[{self._timestamp()}] ⚠️  Ошибка парсинга JSON: {e}")
                except Exception as e:
                    print(f"[{self._timestamp()}] ⚠️  Ошибка обработки события: {e}")
                    
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self.unregister(websocket)
    
    @staticmethod
    def _timestamp() -> str:
        """Возвращает текущее время в ISO формате"""
        return datetime.now().isoformat(timespec='milliseconds')
    
    async def start(self):
        """Запускает WebSocket сервер"""
        print(f"{'='*70}")
        print(f"🚀 MeetMind WebSocket Server")
        print(f"{'='*70}")
        print(f"Слушаем на ws://{self.host}:{self.port}")
        print(f"Ожидаем подключения от Firefox расширения...")
        print(f"\nПосле запуска встречи на meet.google.com события будут выводиться здесь")
        print(f"{'='*70}\n")
        
        async with websockets.serve(self.handler, self.host, self.port):
            await asyncio.Future()  # Бесконечный цикл


def main():
    """Главная функция"""
    server = MeetMindServer()
    
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print(f"\n\n[{server._timestamp()}] ⏹️  Сервер остановлен")
        sys.exit(0)
    except Exception as e:
        print(f"\n\n[{server._timestamp()}] ❌ Ошибка сервера: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
