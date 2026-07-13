"""
Process-watcher: триггер записи при появлении процесса zoom.us / Meet / Teams.

Слежение идёт лёгкое (psutil раз в 5 сек), без нагрузки на CPU.
Когда находим процесс из списка MEETING_APP_PROCESSES — вызываем callback.
Когда процесс пропал — вызываем callback завершения.

Для веб-встреч (Google Meet / Whereby) этот watcher не сработает —
у них процесс это просто браузер. Для них используется Firefox extension или
ручной старт через menubar/hotkey.
"""
import logging
import threading
import time
from datetime import datetime
from typing import Callable, Optional

import psutil

from daemon.session import Session

logger = logging.getLogger(__name__)


class ProcessWatcher:
    """Опрос psutil для детектирования запуска приложения встреч."""

    def __init__(
        self,
        meeting_processes: list[str],
        poll_interval_s: float = 5.0,
        on_meeting_start: Optional[Callable[[Session], None]] = None,
        on_meeting_end: Optional[Callable[[], None]] = None,
    ):
        # имена процессов в lower-case
        self.processes = [p.lower() for p in meeting_processes if p]
        self.poll_interval_s = poll_interval_s
        self.on_meeting_start = on_meeting_start
        self.on_meeting_end = on_meeting_end

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._active_proc: Optional[str] = None  # имя процесса который сейчас идёт

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ProcessWatcher")
        self._thread.start()
        logger.info(
            f"ProcessWatcher запущен (poll={self.poll_interval_s}s, watch={self.processes})"
        )

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        logger.info("ProcessWatcher остановлен")

    def _loop(self):
        while self._running:
            try:
                found = self._scan()
                if found and self._active_proc is None:
                    # новый процесс
                    self._active_proc = found
                    logger.info(f"🎯 Обнаружен процесс встречи: {found}")
                    if self.on_meeting_start:
                        session = Session(
                            title=f"Встреча ({found})",
                            start_time=datetime.now(),
                        )
                        self.on_meeting_start(session)
                elif not found and self._active_proc is not None:
                    # процесс пропал
                    logger.info(f"🛑 Процесс встречи закрыт: {self._active_proc}")
                    self._active_proc = None
                    if self.on_meeting_end:
                        self.on_meeting_end()
            except Exception as e:
                logger.error(f"ProcessWatcher: {e}", exc_info=True)

            time.sleep(self.poll_interval_s)

    def _scan(self) -> Optional[str]:
        """Вернуть имя процесса встречи если идёт, иначе None."""
        try:
            for proc in psutil.process_iter(["name"]):
                name = (proc.info.get("name") or "").lower()
                if not name:
                    continue
                for needle in self.processes:
                    if needle in name:
                        return name
        except Exception as e:
            logger.debug(f"psutil.process_iter упал: {e}")
        return None
