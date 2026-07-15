"""DaemonApp — связывает Recorder, ControlServer и Orchestrator в один процесс (M1).

Потоки: main = orchestrator.run_forever (своё sqlite-подключение);
control-server = asyncio-thread (своё подключение). Recorder — общий, под Lock.
"""
import json
import logging
import shutil
import threading
from datetime import datetime

import config
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
        rec_title = rec_started = None
        if self._active_sid is not None:
            rec = db.get_session(self._con, self._active_sid)
            if rec is not None:
                rec_title, rec_started = rec["title"], rec["started_at"]
        return {
            "recording": self._active_sid,
            "recording_title": rec_title,
            "recording_started_at": rec_started,
            "claude_enabled": bool(config.USE_CLAUDE_CODE),
            "sessions": {r["state"]: r["n"] for r in rows},
            "queue": [dict(r) for r in jobs],
            "last_error": dict(last_err) if last_err else None,
        }

    # -------------------------------------- библиотека встреч (экраны 05-06)

    def _followup_meta(self, sid: str) -> dict | None:
        path = session_dir(sid) / "followup.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _duration_min(row) -> float | None:
        if not row["ended_at"]:
            return None
        try:
            start = datetime.fromisoformat(row["started_at"])
            end = datetime.fromisoformat(row["ended_at"])
        except ValueError:
            return None
        return round((end - start).total_seconds() / 60, 1)

    def _last_error(self, sid: str) -> str | None:
        r = self._con.execute(
            "SELECT stage, last_error FROM pipeline_jobs WHERE session_id=?"
            " AND last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
            (sid,)).fetchone()
        return f"{r['stage']}: {r['last_error']}" if r else None

    def sessions_list(self, params: dict) -> dict:
        """Список встреч для Библиотеки: новые сверху, CANCELLED скрыты.
        status: delivered | processing | error (детали ошибки — из pipeline_jobs)."""
        limit = int(params.get("limit") or 200)
        rows = self._con.execute(
            "SELECT * FROM sessions WHERE state != ?"
            " ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (State.CANCELLED, limit)).fetchall()
        out = []
        for row in rows:
            if row["state"] == State.DELIVERED:
                status, error = "delivered", None
            elif row["state"] == State.FAILED:
                status, error = "error", self._last_error(row["id"])
            else:
                status, error = "processing", None
            followup = self._followup_meta(row["id"])
            sinks = [
                {"sink": d["sink"], "status": d["status"]}
                for d in self._con.execute(
                    "SELECT sink, status FROM deliveries WHERE session_id=?"
                    " ORDER BY sink", (row["id"],))]
            out.append({
                "id": row["id"], "title": row["title"],
                "started_at": row["started_at"],
                "duration_min": self._duration_min(row),
                "state": row["state"], "status": status, "error": error,
                "engine": followup.get("engine") if followup else None,
                "action_items": (len(followup.get("action_items") or [])
                                 if followup else None),
                "sinks": sinks,
            })
        return {"sessions": out}

    def session_detail(self, params: dict) -> dict:
        """Карточка встречи (экран 06): followup + транскрипт с таймкодами +
        статус-лента пайплайна. ok: True пройден / False упал / None ещё нет."""
        sid = params.get("session_id")
        if not sid:
            raise ControlError("bad_params", "нужен session_id")
        row = db.get_session(self._con, sid)
        if row is None:
            raise ControlError("not_found", f"нет сессии {sid}")
        sdir = session_dir(sid)
        followup = self._followup_meta(sid)
        transcript = None
        tpath = sdir / "transcript.json"
        if tpath.exists():
            try:
                transcript = json.loads(tpath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                transcript = None
        jobs = {r["stage"]: r for r in self._con.execute(
            "SELECT * FROM pipeline_jobs WHERE session_id=?", (sid,))}

        def job_step(name: str, stage: str, evidence: bool) -> dict:
            # evidence — файловое доказательство пройденной стадии: job-строка
            # могла не создаваться (прямые db-манипуляции, старые сессии).
            job = jobs.get(stage)
            if job is not None and job["status"] in ("failed", "failed_retry"):
                return {"step": name, "ok": False, "at": job["updated_at"],
                        "error": job["last_error"] or "неизвестная ошибка"}
            if evidence or (job is not None and job["status"] == "done"):
                return {"step": name, "ok": True,
                        "at": job["updated_at"] if job else None, "error": None}
            return {"step": name, "ok": None, "at": None, "error": None}

        recorded_ok = row["state"] not in (State.DETECTED, State.RECORDING)
        pipeline = [
            {"step": "recorded", "ok": True if recorded_ok else None,
             "at": row["ended_at"], "error": None},
            job_step("transcribed", "transcribe", tpath.exists()),
            job_step("summarized", "summarize", followup is not None),
            job_step("delivered", "deliver", row["state"] == State.DELIVERED),
        ]
        sinks = [dict(r) for r in self._con.execute(
            "SELECT sink, status, delivered_at, detail FROM deliveries"
            " WHERE session_id=? ORDER BY sink", (sid,))]
        return {"session": dict(row), "followup": followup,
                "transcript": transcript, "pipeline": pipeline, "sinks": sinks}

    def search(self, params: dict) -> dict:
        """FTS5-поиск по транскриптам и саммари (экран 05)."""
        return {"results": db.search_notes(self._con, params.get("query") or "")}

    def resummarize(self, params: dict) -> dict:
        """«Пересобрать саммари» (экран 06): engine=local форсит чистую Ollama,
        claude — бустер; без engine — по текущему конфигу."""
        sid = params.get("session_id")
        if not sid:
            raise ControlError("bad_params", "нужен session_id")
        engine = params.get("engine")
        if engine not in (None, "local", "claude"):
            raise ControlError("bad_params", f"engine: {engine!r} (local|claude)")
        if db.get_session(self._con, sid) is None:
            raise ControlError("not_found", f"нет сессии {sid}")
        if engine:
            (session_dir(sid) / "engine_override").write_text(
                engine, encoding="utf-8")
        db.resummarize(self._con, sid)
        return {"session_id": sid,
                "state": db.get_session(self._con, sid)["state"]}

    def claude_set(self, params: dict) -> dict:
        """Тоггл Claude-бустера из дропдауна: рантайм-флаг + персист в config.env."""
        enabled = bool(params.get("enabled"))
        config.USE_CLAUDE_CODE = enabled
        config.update_config_env("USE_CLAUDE_CODE",
                                 "true" if enabled else "false")
        logger.info("claude_set: бустер %s", "включён" if enabled else "выключен")
        return {"enabled": enabled}

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
            "sessions_list": self.sessions_list,
            "session_detail": self.session_detail,
            "search": self.search,
            "resummarize": self.resummarize,
            "claude_set": self.claude_set,
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
        indexed = db.backfill_fts(orch_con)
        if indexed:
            logger.info("fts: доиндексировано %d старых сессий", indexed)

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
