#!/usr/bin/env python3
"""
MeetMind v2 — главный entry-point демона.

Поднимает:
- Recorder (aggregate device на Mac)
- Transcriber (MLX-Whisper)
- Summarizer (Ollama + Claude параллельно)
- Sinks (Telegram / Markdown / Obsidian / Notion)
- Triggers: Calendar / Process / Menubar+Hotkey
- WebSocket-сервер для опционального Firefox extension enrichment

Запуск:
    python main.py                  # фон + все триггеры
    python main.py --no-menubar     # без menubar
    python main.py --tray-only      # только menubar (для отладки UI)
"""
import argparse
import logging
import signal
import threading
import time

import config
from daemon.calendar_client import CalendarClient
from daemon.enricher import Enricher
from daemon.notifier import Notifier
from daemon.process_watcher import ProcessWatcher
from daemon.recorder import AudioRecorder
from daemon.scheduler import MeetingScheduler
from daemon.session_manager import SessionManager
from daemon.sinks import (
    MarkdownSink,
    NotionSink,
    ObsidianSink,
    TelegramSink,
)
from daemon.storage import Storage
from daemon.summarizer import Summarizer
from daemon.transcriber import Transcriber
from daemon.websocket_server import WebSocketServer

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format="%(asctime)s · %(name)s · %(levelname)s · %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("meetmind.log")],
)
logger = logging.getLogger("meetmind")


class MeetMindDaemon:
    """Главный класс демона MeetMind v2."""

    def __init__(self, enable_menubar: bool = True):
        logger.info("\n" + config.summary())
        self.enable_menubar = enable_menubar

        # Core processing
        self.recorder = AudioRecorder(
            aggregate_device_name=config.AGGREGATE_DEVICE_NAME,
            blackhole_device_name=config.BLACKHOLE_DEVICE_NAME,
            sample_rate=config.AUDIO_SAMPLE_RATE,
        )
        self.transcriber = Transcriber(
            model_name=config.WHISPER_MODEL,
            language=config.WHISPER_LANGUAGE,
        )
        self.summarizer = Summarizer(
            # Anthropic API
            anthropic_api_key=config.ANTHROPIC_API_KEY,
            anthropic_model=config.ANTHROPIC_MODEL,
            use_anthropic=config.USE_ANTHROPIC,
            # Claude Code CLI (через Max-подписку)
            use_claude_code=config.USE_CLAUDE_CODE,
            claude_code_model=config.CLAUDE_CODE_MODEL,
            claude_code_binary=config.CLAUDE_CODE_BINARY,
            # Ollama
            ollama_url=config.OLLAMA_URL,
            ollama_model=config.OLLAMA_MODEL,
            use_ollama=config.USE_OLLAMA,
        )

        # Sinks
        self.sinks = self._build_sinks()
        logger.info(f"Sinks активны: {[s.name for s in self.sinks]}")

        # Сессии и хранилище
        self.session_manager = SessionManager()
        self.storage = Storage(config.SESSIONS_DIR)

        # Уведомления
        self.notifier = Notifier()

        # WebSocket для Firefox extension (опционально, для enrichment)
        self.ws_server = WebSocketServer("localhost", config.WS_PORT)
        self.ws_server.add_event_callback(self._handle_websocket_event)

        # Триггеры
        self.calendar_client = None
        self.scheduler = None
        if config.ENABLE_CALENDAR_TRIGGER:
            try:
                self.calendar_client = CalendarClient(config.GOOGLE_CREDENTIALS_PATH)
                self.scheduler = MeetingScheduler()
                self.scheduler.set_on_meeting_start(self._on_meeting_start)
                logger.info("Calendar trigger: OK")
            except Exception as e:
                logger.warning(f"Calendar trigger выключен: {e}")

        self.process_watcher = None
        if config.ENABLE_PROCESS_TRIGGER:
            self.process_watcher = ProcessWatcher(
                meeting_processes=config.MEETING_APP_PROCESSES,
                on_meeting_start=self._on_meeting_start,
                on_meeting_end=self._on_meeting_end,
            )

        self._stop_flag = threading.Event()

    # --------------------------------- sinks -----------------------------------

    def _build_sinks(self) -> list:
        sinks = []
        if config.ENABLE_MARKDOWN_SINK:
            sinks.append(MarkdownSink(base_dir=config.MARKDOWN_BASE_DIR))
        if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            sinks.append(TelegramSink(
                bot_token=config.TELEGRAM_BOT_TOKEN,
                chat_id=config.TELEGRAM_CHAT_ID,
            ))
        if config.ENABLE_OBSIDIAN_SINK:
            sinks.append(ObsidianSink(
                vault_path=config.OBSIDIAN_VAULT_PATH,
                subfolder=config.OBSIDIAN_SUBFOLDER,
            ))
        if config.ENABLE_NOTION_SINK:
            sinks.append(NotionSink(
                token=config.NOTION_TOKEN,
                database_id=config.NOTION_DATABASE_ID,
            ))
        return sinks

    # ------------------------------ event handlers -----------------------------

    def _handle_websocket_event(self, event_data: dict):
        active = self.session_manager.get_active_session()
        if not active:
            return
        t = event_data.get("type")
        try:
            if t == "speaker":
                self.session_manager.add_speaker_event(
                    active.id,
                    event_data.get("name", "Unknown"),
                    float(event_data.get("ts", 0)),
                )
            elif t == "chat":
                self.session_manager.add_chat_message(
                    active.id,
                    event_data.get("name", "Unknown"),
                    event_data.get("text", ""),
                    float(event_data.get("ts", 0)),
                )
            elif t == "meeting_end":
                self._on_meeting_end()
        except Exception as e:
            logger.error(f"WS event: {e}", exc_info=True)

    def _on_meeting_start(self, session):
        """Стартовать запись для встречи. Может приходить из любого триггера."""
        # Если уже идёт активная сессия — пропускаем
        if self.session_manager.has_active_session():
            logger.warning(
                f"Запрос на старт встречи «{session.title}», "
                f"но сессия уже активна — игнор"
            )
            return

        logger.info(f"=== Старт встречи: {session.title} ===")
        self.session_manager.active_sessions[session.id] = session
        session.status = "recording"

        session_dir = config.SESSIONS_DIR / session.id
        session_dir.mkdir(exist_ok=True)
        audio_file = self.recorder.start(session.id, session_dir)
        if audio_file:
            session.audio_file = audio_file
            try:
                Notifier.notify_recording_started(session)
            except Exception:
                pass
        else:
            logger.error("Recorder не стартовал")
            self.session_manager.cancel_session(session.id)

    def _on_meeting_end(self):
        active = self.session_manager.get_active_session()
        if not active:
            return
        logger.info(f"=== Стоп встречи: {active.title} ===")
        self.recorder.stop()
        session = self.session_manager.end_session(active.id)
        if not session:
            return

        # Обработка в отдельном потоке — чтобы не блокировать триггеры
        threading.Thread(
            target=self._process_session, args=(session,), daemon=True
        ).start()

    def _process_session(self, session):
        try:
            logger.info(f"Обработка сессии: {session.title}")
            try:
                Notifier.notify_processing(session)
            except Exception:
                pass

            if not session.audio_file:
                logger.warning("Нет аудио-файла")
                return

            # 1) Транскрипция
            segments = self.transcriber.transcribe(session.audio_file)
            if not segments:
                logger.error("Транскрипция не дала результата")
                return
            logger.info(f"Транскрипция: {len(segments)} сегментов")

            # 2) Enrichment (опционально, из Firefox extension)
            enriched = Enricher.enrich(
                segments, session.speaker_events, session.chat_messages
            )

            # 3) Суммаризация (Ollama + Claude параллельно)
            followup = self.summarizer.summarize(enriched, session)
            if not followup.has_any:
                logger.error("Суммаризация не дала результата")
                if followup.errors:
                    logger.error(f"Ошибки: {followup.errors}")
                return

            logger.info(
                f"Followup готов: "
                f"local={'yes' if followup.local else 'no'}, "
                f"cloud={'yes' if followup.cloud else 'no'}"
            )

            # 4) Sinks (все параллельно)
            results = self._dispatch_to_sinks(followup, session)
            for r in results:
                emoji = "✅" if r.success else "❌"
                logger.info(f"  {emoji} {r.name}: {r.detail}")

            # 5) Локальное хранилище (raw audio + transcript + followup)
            self.storage.save_session(
                session, followup.primary or "", enriched
            )

            try:
                Notifier.notify_done(session)
            except Exception:
                pass

            logger.info(f"✅ Сессия обработана: {session.title}")

        except Exception as e:
            logger.error(f"Ошибка обработки сессии: {e}", exc_info=True)

    def _dispatch_to_sinks(self, followup, session) -> list:
        """Запустить все sinks параллельно, вернуть SinkResult'ы."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not self.sinks:
            return []
        results = []
        with ThreadPoolExecutor(max_workers=len(self.sinks)) as pool:
            futures = {
                pool.submit(s.send, followup, session): s.name for s in self.sinks
            }
            for fut in as_completed(futures):
                try:
                    results.append(fut.result())
                except Exception as e:
                    from daemon.sinks.base import SinkResult
                    results.append(SinkResult(futures[fut], False, detail=str(e)))
        return results

    # -------------------------------- lifecycle --------------------------------

    def _calendar_loop(self):
        if not (self.scheduler and self.calendar_client):
            return
        while not self._stop_flag.is_set():
            try:
                self.scheduler.check_calendar(self.calendar_client)
            except Exception as e:
                logger.error(f"calendar loop: {e}", exc_info=True)
            self._stop_flag.wait(timeout=120)

    def start(self):
        logger.info("Запуск MeetMind v2 daemon…")

        # WS server (для Firefox extension; ни на что не опираемся жёстко)
        threading.Thread(target=self.ws_server.run, daemon=True).start()
        logger.info(f"WebSocket server: ws://localhost:{config.WS_PORT}")

        # Calendar trigger
        if self.scheduler:
            self.scheduler.start()
            threading.Thread(target=self._calendar_loop, daemon=True).start()

        # Process trigger
        if self.process_watcher:
            self.process_watcher.start()

        logger.info("=== MeetMind готов ===")

        # Menubar — блокирующий, должен запускаться в main thread на macOS
        if self.enable_menubar and config.ENABLE_MENUBAR:
            try:
                from tray import run_tray
                run_tray(daemon=self, hotkey=config.HOTKEY_START_STOP)
                return  # rumps.quit_application выйдет из процесса
            except ImportError as e:
                logger.warning(f"Menubar недоступен: {e}")

        # Иначе — обычный фоновый цикл с обработкой сигналов
        def _signal_handler(sig, frame):
            logger.info(f"Получен сигнал {sig}, останавливаемся")
            self.stop()

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        try:
            while not self._stop_flag.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        logger.info("Остановка MeetMind…")
        self._stop_flag.set()
        try:
            self.recorder.stop()
        except Exception:
            pass
        if self.process_watcher:
            self.process_watcher.stop()
        if self.scheduler:
            try:
                self.scheduler.stop()
            except Exception:
                pass
        for sess in self.session_manager.get_all_active():
            self.session_manager.end_session(sess.id)
        logger.info("MeetMind остановлен")


def main():
    parser = argparse.ArgumentParser(description="MeetMind v2 — auto-followupper")
    parser.add_argument("--no-menubar", action="store_true", help="без menubar")
    parser.add_argument("--tray-only", action="store_true", help="только menubar")
    args = parser.parse_args()

    if args.tray_only:
        from tray import run_tray
        logging.basicConfig(level=logging.INFO)
        run_tray()
        return

    daemon = MeetMindDaemon(enable_menubar=not args.no_menubar)
    try:
        daemon.start()
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
        daemon.stop()
        raise


if __name__ == "__main__":
    main()
