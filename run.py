#\!/usr/bin/env python3
"""
Главный скрипт для запуска MeetMind локально на Mac
Запускает Flask web server в отдельном потоке и основной демон
"""

import os
import sys
import subprocess
import threading
import time
import logging
from pathlib import Path

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Получаем корневую директорию проекта
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# Импортируем конфиг
from config import WEB_PORT


def run_web_server():
    """Запускает Flask web server в отдельном потоке"""
    logger.info("=" * 60)
    logger.info("🌐 Запуск Flask Web Server...")
    logger.info("=" * 60)
    
    try:
        # Добавляем папку web в sys.path, чтобы Python нашёл web_server
        web_dir = PROJECT_ROOT / "web"
        sys.path.insert(0, str(web_dir))
        
        # Импортируем и запускаем Flask приложение
        from web_server import app
        app.run(host="0.0.0.0", port=WEB_PORT, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"Ошибка запуска Flask server: {e}", exc_info=True)


def run_websocket_server():
    """Запускает WebSocket сервер для Firefox Add-on"""
    import asyncio
    import websockets
    import json

    connected_clients = set()

    async def handler(websocket):
        connected_clients.add(websocket)
        logger.info(f"🔌 Firefox Add-on подключился ({len(connected_clients)} клиентов)")
        try:
            async for message in websocket:
                try:
                    event = json.loads(message)
                    logger.info(f"📨 Событие от Add-on: {event.get('type')} — {event.get('name', '')}")
                except Exception:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            connected_clients.discard(websocket)
            logger.info(f"🔌 Add-on отключился ({len(connected_clients)} клиентов)")

    async def serve():
        from config import WS_PORT
        async with websockets.serve(handler, "localhost", WS_PORT):
            logger.info(f"✅ WebSocket сервер запущен на ws://localhost:{WS_PORT}")
            await asyncio.Future()  # держим живым

    asyncio.run(serve())


def run_daemon():
    """Запускает WebSocket сервер в отдельном потоке"""
    logger.info("=" * 60)
    logger.info("🤖 Запуск MeetMind Daemon...")
    logger.info("=" * 60)

    try:
        ws_thread = threading.Thread(target=run_websocket_server, daemon=True)
        ws_thread.start()
        logger.info("✅ Демон и WebSocket сервер запущены")
    except Exception as e:
        logger.error(f"Ошибка запуска демона: {e}", exc_info=True)


def open_browser():
    """Открывает браузер на localhost:5000 через несколько секунд"""
    time.sleep(2)  # Даем серверу время на запуск
    try:
        url = f"http://localhost:{WEB_PORT}"
        logger.info(f"🌍 Открываю браузер: {url}")
        subprocess.run(["open", url], check=False)
    except Exception as e:
        logger.warning(f"Не удалось открыть браузер: {e}")
        logger.info(f"Откройте вручную: http://localhost:{WEB_PORT}")


def main():
    """Главная функция - запускает всё"""
    logger.info("")
    logger.info("╔" + "=" * 58 + "╗")
    logger.info("║" + " " * 58 + "║")
    logger.info("║" + "  🚀 MeetMind - Meeting Intelligence Platform".center(58) + "║")
    logger.info("║" + " " * 58 + "║")
    logger.info("╚" + "=" * 58 + "╝")
    logger.info("")
    
    logger.info(f"📍 Рабочая директория: {PROJECT_ROOT}")
    logger.info(f"🔌 Web Server будет запущен на: http://localhost:{WEB_PORT}")
    logger.info("")
    
    # Запускаем Flask server в отдельном потоке
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()
    logger.info("✅ Web server запущен в фоновом потоке")
    
    # Запускаем открытие браузера в отдельном потоке
    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()
    
    # Запускаем демон в основном потоке (блокирующий)
    logger.info("")
    run_daemon()
    
    # Если демон завершился, сохраняем процесс живым
    try:
        logger.info("")
        logger.info("=" * 60)
        logger.info("✨ MeetMind запущен\! Нажмите Ctrl+C для остановки")
        logger.info("=" * 60)
        logger.info("")
        
        # Блокируем основной поток
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("")
        logger.info("=" * 60)
        logger.info("👋 Остановка MeetMind...")
        logger.info("=" * 60)
        sys.exit(0)


if __name__ == "__main__":
    main()
