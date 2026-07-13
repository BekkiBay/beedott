"""DaemonApp — связывает Recorder, ControlServer и Orchestrator в один процесс (M1).

Потоки: main = orchestrator.run_forever (своё sqlite-подключение);
control-server = asyncio-thread (своё подключение). Recorder — общий, под Lock.
"""
import logging
import shutil
import threading

from daemon import db
from daemon.control import ControlError, ControlServer
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
        self._con = db.connect()  # подключение control-потока

    # ------------------------------------------------------------- handlers

    def start_recording(self, params: dict) -> dict:
        with self._lock:
            if self._recorder is not None:
                raise ControlError("busy", f"уже идёт запись сессии {self._active_sid}")
            sid = db.create_session(
                self._con,
                source=params.get("source", "manual"),
                title=params.get("title"),
                meeting_key=params.get("meeting_key"),
            )
            wav = session_dir(sid) / "audio.wav"
            rec = Recorder(out_path=wav)
            try:
                rec.start()
            except Exception:
                db.set_state(self._con, sid, State.CANCELLED)
                shutil.rmtree(session_dir(sid), ignore_errors=True)
                raise
            db.set_state(self._con, sid, State.RECORDING, audio_path=str(wav))
            self._recorder, self._active_sid = rec, sid
            logger.info("REC start: %s (%s)", sid, params.get("title") or "без названия")
            return {"session_id": sid}

    def stop_recording(self, params: dict) -> dict:
        with self._lock:
            sid = params.get("session_id") or self._active_sid
            if self._recorder is None or sid != self._active_sid:
                raise ControlError("no_active_session", "нет активной записи")
            self._recorder.stop()
            self._recorder = None
            self._active_sid = None
            db.set_state(self._con, sid, State.RECORDED, ended_at=db.now())
            db.enqueue_job(self._con, sid, "transcribe")
            logger.info("REC stop: %s -> очередь transcribe", sid)
            return {"session_id": sid}

    def cancel_recording(self, params: dict) -> dict:
        with self._lock:
            sid = params.get("session_id") or self._active_sid
            if sid == self._active_sid and self._recorder is not None:
                self._recorder.stop()
                self._recorder = None
                self._active_sid = None
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
        server = ControlServer(self.handlers())
        server.start()
        orch_con = db.connect()
        orch = Orchestrator(orch_con, make_stages())
        logger.info("MeetMind daemon v3 запущен (control socket + orchestrator)")
        orch.run_forever()
