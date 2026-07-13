#\!/usr/bin/env python3
"""
MeetMind Web Server
Flask приложение для просмотра истории фоллоу-апов и управления сессиями.
Запускается на http://localhost:5000
"""

import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify, request
from flask_cors import CORS

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Добавляем корневую папку проекта в sys.path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Импортируем Storage и конфиг
try:
    from daemon.storage import Storage
    from config import SESSIONS_DIR
    logger.info("Модули daemon.storage и config загружены успешно")
except ImportError as e:
    logger.error(f"Ошибка импорта: {e}")
    Storage = None
    SESSIONS_DIR = Path('sessions')

app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)

# Инициализируем хранилище
try:
    storage = Storage(SESSIONS_DIR)
    logger.info(f"Storage инициализирован с директорией: {SESSIONS_DIR}")
except Exception as e:
    logger.error(f"Ошибка инициализации Storage: {e}")
    storage = None


@app.route("/")
def index():
    """Главная страница - список всех сессий"""
    return render_template("index.html")


@app.route("/session/<session_id>")
def session_detail(session_id):
    """Страница просмотра одного фоллоу-апа"""
    return render_template("session.html", session_id=session_id)


@app.route("/api/sessions", methods=["GET"])
def get_sessions():
    """Возвращает список всех сессий с метаданными"""
    if not storage:
        logger.error("Storage не инициализирован")
        return jsonify({"error": "Storage недоступен"}), 503

    try:
        sessions = storage.list_sessions()
        logger.info(f"Загружено {len(sessions)} сессий")
        return jsonify(sessions)
    except Exception as e:
        logger.error(f"Ошибка при получении сессий: {e}", exc_info=True)
        return jsonify({"error": "Ошибка загрузки сессий"}), 500


@app.route("/api/session/<session_id>", methods=["GET"])
def get_session(session_id):
    """Возвращает полные данные сессии"""
    if not storage:
        logger.error("Storage не инициализирован")
        return jsonify({"error": "Storage недоступен"}), 503

    try:
        session_data = storage.get_session(session_id)

        if not session_data:
            logger.warning(f"Сессия {session_id} не найдена")
            return jsonify({"error": "Сессия не найдена"}), 404

        logger.info(f"Загружена сессия {session_id}")
        return jsonify({
            "id": session_id,
            "meta": session_data.get('meta'),
            "followup": session_data.get('followup'),
            "enriched": session_data.get('enriched'),
        })
    except Exception as e:
        logger.error(f"Ошибка при получении сессии {session_id}: {e}", exc_info=True)
        return jsonify({"error": "Ошибка загрузки сессии"}), 500


@app.route("/api/status", methods=["GET"])
def get_status():
    """Возвращает статус текущей активной сессии"""
    try:
        return jsonify({"active_session": None})
    except Exception as e:
        logger.error(f"Ошибка при получении статуса: {e}", exc_info=True)
        return jsonify({"active_session": None})


@app.route("/api/start-manual", methods=["POST"])
def start_manual():
    """Создаёт новую сессию вручную из ссылки на встречу"""
    try:
        data = request.get_json()
        url = data.get("url")
        title = data.get("title", "Встреча вручную")

        if not url:
            logger.warning("Попытка создания сессии без URL")
            return jsonify({"error": "URL встречи обязателен"}), 400

        logger.info(f"Запрос создания сессии вручную: {title}")

        # Создаём реальную сессию в storage
        import uuid
        from datetime import datetime
        session_id = str(uuid.uuid4())
        session_dir = storage.sessions_dir / f"{datetime.now().strftime('%Y-%m-%d')}_{session_id[:8]}"
        session_dir.mkdir(exist_ok=True)

        meta = {
            "id": session_id,
            "title": title,
            "meet_url": url,
            "start_time": datetime.now().isoformat(),
            "end_time": None,
            "attendees": [],
            "duration_minutes": 0,
            "status": "manual",
            "created_at": datetime.now().isoformat(),
        }
        import json as _json
        with open(session_dir / "meta.json", "w", encoding="utf-8") as f:
            _json.dump(meta, f, ensure_ascii=False, indent=2)

        # Создаём пустой followup.md чтобы страница открылась
        with open(session_dir / "followup.md", "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\nЗапись ещё не завершена. Фоллоу-ап появится после окончания встречи.\n")

        logger.info(f"Создана сессия вручную: {session_id}")
        return jsonify({
            "success": True,
            "session_id": session_id,
            "title": title,
        })
    except Exception as e:
        logger.error(f"Ошибка при создании сессии вручную: {e}", exc_info=True)
        return jsonify({"error": "Ошибка создания сессии"}), 500


@app.route("/api/send-telegram/<session_id>", methods=["POST"])
def send_telegram(session_id):
    """Отправляет фоллоу-ап в Telegram"""
    try:
        session_data = storage.get_session(session_id)
        if not session_data:
            return jsonify({"error": "Сессия не найдена"}), 404

        followup = session_data.get('followup', '')
        meta = session_data.get('meta', {})
        title = meta.get('title', 'Встреча')

        if not followup:
            return jsonify({"error": "Фоллоу-ап пустой"}), 404

        # Отправляем через python-telegram-bot
        import requests as _req
        token = os.environ.get('TELEGRAM_BOT_TOKEN') or ''
        chat_id = os.environ.get('TELEGRAM_CHAT_ID') or ''

        if not token or not chat_id:
            return jsonify({"error": "Telegram не настроен в .env"}), 500

        # Разбиваем на части если больше 4096 символов
        max_len = 4000
        parts = [followup[i:i+max_len] for i in range(0, len(followup), max_len)]

        for part in parts:
            resp = _req.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": part, "parse_mode": "Markdown"},
                timeout=10
            )
            if not resp.ok:
                # Пробуем без markdown если ошибка парсинга
                _req.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": part},
                    timeout=10
                )

        logger.info(f"Фоллоу-ап отправлен в TG: {title}")
        return jsonify({"success": True, "message": "Отправлено в Telegram ✅"})

    except Exception as e:
        logger.error(f"Ошибка отправки в TG: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/daemon-status", methods=["GET"])
def daemon_status():
    """Проверяет статус демона"""
    try:
        # Проверяем, существует ли директория сессий
        daemon_active = SESSIONS_DIR.exists()
        return jsonify({"daemon_active": daemon_active})
    except Exception as e:
        logger.warning(f"Ошибка проверки статуса демона: {e}")
        return jsonify({"daemon_active": False})


# Обработчик ошибок 404
@app.errorhandler(404)
def not_found(error):
    """Обработка 404 ошибок"""
    logger.warning(f"404 Not Found: {request.path}")
    return jsonify({"error": "Страница не найдена"}), 404


# Обработчик ошибок 500
@app.errorhandler(500)
def internal_error(error):
    """Обработка 500 ошибок"""
    logger.error(f"500 Internal Server Error: {error}", exc_info=True)
    return jsonify({"error": "Внутренняя ошибка сервера"}), 500


if __name__ == "__main__":
    logger.info("Запуск MeetMind Web Server на 0.0.0.0:5000")

    if not storage:
        logger.warning("⚠️  Storage не инициализирован\!")
        logger.warning("Убедитесь, что daemon/storage.py доступен")
    else:
        logger.info("✅ Storage инициализирован успешно")

    # Запускаем Flask на 0.0.0.0:5000
    app.run(host="0.0.0.0", port=8080, debug=True, use_reloader=False)
