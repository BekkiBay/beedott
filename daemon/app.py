"""DaemonApp — связывает Recorder, ControlServer и Orchestrator в один процесс (M1).

Потоки: main = orchestrator.run_forever (своё sqlite-подключение);
control-server = asyncio-thread (своё подключение). Recorder — общий, под Lock.
"""
import logging
import shutil
import threading

from daemon import db, reconcile, retention
from daemon.control import ControlError, ControlServer
from daemon.notifier import notify
from daemon.orchestrator import Orchestrator
from daemon.paths import session_dir
from daemon.recorder import Recorder
from daemon.stages import make_stages
from daemon.states import State

logger = logging.getLogger(__name__)


class DaemonApp:
    def __init__(self):
        self._lock = threading.Lock()
        self._recorder: Recorder | None = None
        self._active_sid: str | None = None
        self._owner: str | None = None  # "daemon" | "app" — кто пишет audio.wav активной сессии
        self._tls = threading.local()  # sqlite-подключение на поток

    @property
    def _con(self):
        con = getattr(self._tls, "con", None)
        if con is None:
            con = self._tls.con = db.connect()
        return con

    # ------------------------------------------------------------- handlers

    def start_recording(self, params: dict) -> dict:
        with self._lock:
            if self._active_sid is not None:
                raise ControlError("busy", f"уже идёт запись сессии {self._active_sid}")
            owner = params.get("audio_path_owner") or "daemon"
            if owner not in ("daemon", "app"):
                raise ControlError(
                    "bad_params", f"audio_path_owner: {owner!r} (ожидается daemon|app)")
            sid = db.create_session(
                self._con,
                source=params.get("source", "manual"),
                title=params.get("title"),
                meeting_key=params.get("meeting_key"),
            )
            wav = session_dir(sid) / "audio.wav"

            if owner == "app":
                # Swift CaptureEngine пишет WAV сам (Process Tap, SPEC_v3 §6
                # audio_path_owner=app) — демон только заводит сессию/каталог и
                # возвращает путь, Python-Recorder не стартует.
                db.set_state(self._con, sid, State.RECORDING, audio_path=str(wav))
                self._recorder, self._active_sid, self._owner = None, sid, "app"
                logger.info(
                    "REC start (owner=app): %s (%s)", sid, params.get("title") or "без названия")
                return {"session_id": sid, "audio_path": str(wav)}

            rec = Recorder(
                out_path=wav,
                on_autostop=lambda reason, _sid=sid: self._on_autostop(_sid, reason),
                on_silence=lambda _sid=sid: notify(
                    "MeetMind", "Долгая тишина — остановить запись? ⌘ record stop"),
            )
            try:
                rec.start()
            except Exception:
                db.set_state(self._con, sid, State.CANCELLED)
                shutil.rmtree(session_dir(sid), ignore_errors=True)
                raise
            db.set_state(self._con, sid, State.RECORDING, audio_path=str(wav))
            self._recorder, self._active_sid, self._owner = rec, sid, "daemon"
            logger.info("REC start: %s (%s)", sid, params.get("title") or "без названия")
            return {"session_id": sid}

    def stop_recording(self, params: dict) -> dict:
        with self._lock:
            sid = params.get("session_id") or self._active_sid
            if self._active_sid is None or sid != self._active_sid:
                raise ControlError("no_active_session", "нет активной записи")
            if self._owner == "daemon":
                if self._recorder is None:
                    raise ControlError("no_active_session", "нет активной записи")
                self._recorder.stop()
            # owner == "app": Swift CaptureEngine уже сам остановил и закрыл WAV
            # (engine.stop() до stop_recording, см. app/Sources/MeetMind/MeetMindApp.swift)
            # — Recorder тут нет и не было, просто финализируем сессию.
            self._finalize_recording(sid)
            return {"session_id": sid}

    def _finalize_recording(self, sid: str) -> None:
        """Под self._lock. Закрывает запись и ставит транскрипцию в очередь."""
        self._recorder = None
        self._active_sid = None
        self._owner = None
        db.set_state(self._con, sid, State.RECORDED, ended_at=db.now())
        db.enqueue_job(self._con, sid, "transcribe")
        logger.info("REC stop: %s -> очередь transcribe", sid)

    def _on_autostop(self, sid: str, reason: str) -> None:
        """Вызывается writer-потоком Recorder при duration_limit/disk_full.
        Recorder уже остановил себя и закрыл файл — только финализируем сессию."""
        with self._lock:
            if sid != self._active_sid:
                return
            self._finalize_recording(sid)
        human = {"duration_limit": "превышен лимит длительности",
                 "disk_full": "мало места на диске"}.get(reason, reason)
        notify("MeetMind", f"Запись остановлена автоматически: {human}. "
                           "Записанное будет обработано.")

    def cancel_recording(self, params: dict) -> dict:
        with self._lock:
            sid = params.get("session_id") or self._active_sid
            if sid == self._active_sid and self._active_sid is not None:
                # owner == "app": рекордера нет (Swift CaptureEngine уже остановлен
                # вызывающим до cancel_recording) — только сбрасываем внутреннее состояние.
                if self._owner == "daemon" and self._recorder is not None:
                    self._recorder.stop()
                self._recorder = None
                self._active_sid = None
                self._owner = None
            if sid is None:
                raise ControlError("no_active_session", "нечего отменять")
            db.set_state(self._con, sid, State.CANCELLED)
            shutil.rmtree(session_dir(sid), ignore_errors=True)
            logger.info("REC cancel: %s — файлы удалены", sid)
            return {"session_id": sid}

    def status(self, params: dict) -> dict:
        rows = self._con.execute(
            "SELECT state, COUNT(*) n FROM sessions GROUP BY state").fetchall()
        jobs = self._con.execute(
            "SELECT stage, status, COUNT(*) n FROM pipeline_jobs"
            " WHERE status NOT IN ('done') GROUP BY stage, status").fetchall()
        last_err = self._con.execute(
            "SELECT session_id, stage, last_error FROM pipeline_jobs"
            " WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 1").fetchone()
        return {
            "recording": self._active_sid,
            "sessions": {r["state"]: r["n"] for r in rows},
            "queue": [dict(r) for r in jobs],
            "last_error": dict(last_err) if last_err else None,
        }

    def reprocess(self, params: dict) -> dict:
        sid = params.get("session_id")
        if not sid:
            raise ControlError("bad_params", "нужен session_id")
        db.reprocess(self._con, sid)
        return {"session_id": sid, "state": db.get_session(self._con, sid)["state"]}

    def health(self, params: dict) -> dict:
        return {"ok": True, "version": "3.0.0"}

    # ----------------------------------------------------------------- run

    def handlers(self) -> dict:
        return {
            "start_recording": self.start_recording,
            "stop_recording": self.stop_recording,
            "cancel_recording": self.cancel_recording,
            "status": self.status,
            "reprocess": self.reprocess,
            "health": self.health,
        }

    def run(self) -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        orch_con = db.connect()

        # Startup reconciliation: осиротевшие RECORDING-сессии и running-джобы
        # после краша/ребута (SPEC_v3 §10). Рекордер в этот момент точно не жив.
        for action in reconcile.reconcile(
                orch_con, notifier=lambda sid, msg: notify("MeetMind", msg)):
            logger.info("reconcile: %s", action)
        removed = retention.sweep(orch_con)
        if removed:
            logger.info("retention: удалено аудио %d старых сессий", len(removed))

        server = ControlServer(self.handlers())
        server.start()
        orch = Orchestrator(
            orch_con, make_stages(),
            notifier=lambda sid, stage, err: notify(
                "MeetMind", f"Встреча {sid} не обработана ({stage}): {err[:120]} — "
                            f"аудио сохранено, reprocess доступен"),
            progress_notifier=lambda sid, minutes: notify(
                "MeetMind", f"Обработка длинной встречи продолжается ({minutes:.0f} мин)"),
        )
        logger.info("MeetMind daemon v3 запущен (control socket + orchestrator)")
        orch.run_forever()
