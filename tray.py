"""
Menubar-приложение для MeetMind: иконка в строке меню Mac + глобальный hotkey.

Запускается отдельно от main.py (но может общаться с ним через файловый сигнал
или, если запущены вместе — через colocated `MeetMindDaemon`).

Минимальный режим: запуск/остановка ручной записи, отображение состояния.
"""
import logging
import threading
from datetime import datetime
from pathlib import Path

import rumps  # type: ignore

logger = logging.getLogger(__name__)


class MeetMindTray(rumps.App):
    """Menubar-приложение MeetMind."""

    def __init__(self, daemon=None, hotkey: str = "<cmd>+<shift>+r"):
        super().__init__("MeetMind", title="🎙️", quit_button=None)
        self.daemon = daemon  # ссылка на MeetMindDaemon, опционально
        self.hotkey = hotkey
        self.is_recording = False

        # Menu items
        self.menu = [
            rumps.MenuItem("Начать запись", callback=self._on_start),
            rumps.MenuItem("Остановить запись", callback=self._on_stop),
            None,  # separator
            rumps.MenuItem("Открыть папку записей", callback=self._on_open_folder),
            rumps.MenuItem("Статус", callback=None),
            None,
            rumps.MenuItem("Выход", callback=self._on_quit),
        ]
        self.menu["Остановить запись"].set_callback(None)  # disabled initially

        # Регистрируем hotkey в отдельном потоке (pynput блокирует)
        self._hotkey_listener = None
        self._start_hotkey_listener()

    # ------------------------------ hotkey -------------------------------------

    def _start_hotkey_listener(self):
        try:
            from pynput import keyboard
        except ImportError:
            logger.warning("pynput не установлен — hotkey недоступен")
            return

        def _on_press():
            logger.info(f"🔥 Hotkey: {self.hotkey}")
            if self.is_recording:
                self._on_stop(None)
            else:
                self._on_start(None)

        try:
            hk = keyboard.GlobalHotKeys({self.hotkey: _on_press})
            t = threading.Thread(target=hk.start, daemon=True, name="hotkey")
            t.start()
            self._hotkey_listener = hk
            logger.info(f"Hotkey зарегистрирован: {self.hotkey}")
        except Exception as e:
            logger.error(f"Не удалось зарегистрировать hotkey: {e}")

    # ------------------------------ callbacks ----------------------------------

    def _on_start(self, _):
        if self.is_recording:
            return
        if self.daemon is None:
            rumps.notification(
                "MeetMind", "Демон не запущен",
                "Сначала запусти main.py в фоне"
            )
            return
        try:
            from daemon.session import Session
            session = Session(
                title=f"Ручная запись {datetime.now().strftime('%H:%M')}",
                start_time=datetime.now(),
            )
            self.daemon._on_meeting_start(session)  # noqa: SLF001
            self.is_recording = True
            self.title = "🔴"
            self.menu["Начать запись"].set_callback(None)
            self.menu["Остановить запись"].set_callback(self._on_stop)
            rumps.notification("MeetMind", "Запись начата", session.title)
        except Exception as e:
            logger.error(f"start: {e}", exc_info=True)
            rumps.notification("MeetMind", "Ошибка старта", str(e))

    def _on_stop(self, _):
        if not self.is_recording or self.daemon is None:
            return
        try:
            self.daemon._on_meeting_end()  # noqa: SLF001
            self.is_recording = False
            self.title = "🎙️"
            self.menu["Начать запись"].set_callback(self._on_start)
            self.menu["Остановить запись"].set_callback(None)
            rumps.notification("MeetMind", "Запись остановлена",
                               "Обработка запущена в фоне")
        except Exception as e:
            logger.error(f"stop: {e}", exc_info=True)
            rumps.notification("MeetMind", "Ошибка стопа", str(e))

    def _on_open_folder(self, _):
        import subprocess
        from config import SESSIONS_DIR
        subprocess.run(["open", str(SESSIONS_DIR)])

    def _on_quit(self, _):
        if self.daemon is not None:
            try:
                self.daemon.stop()
            except Exception:
                pass
        rumps.quit_application()


def run_tray(daemon=None, hotkey: str = "<cmd>+<shift>+r"):
    """Запустить menubar-приложение (блокирует основной поток)."""
    app = MeetMindTray(daemon=daemon, hotkey=hotkey)
    app.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # standalone-режим: без демона, только показать иконку
    run_tray(daemon=None)
