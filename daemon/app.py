"""DaemonApp — связывает Recorder, ControlServer и Orchestrator в один процесс (M1).

Потоки: main = orchestrator.run_forever (своё sqlite-подключение);
control-server = asyncio-thread (своё подключение). Recorder — общий, под Lock.
"""
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import soundfile as sf

from daemon import db, notifier, reconcile, retention
from daemon.app_guard import AppRecordingGuard
from daemon.control import ControlError, ControlServer
from daemon.notifier import notify
from daemon.orchestrator import Orchestrator
from daemon.paths import app_dir, session_dir
from daemon.recorder import DEFAULT_MIN_DISK_START_GB, Recorder, _env_num
from daemon.stages import make_stages
from daemon.states import State

logger = logging.getLogger(__name__)

# Каталоги CLI-инструментов, которых нет в PATH LaunchServices/launchd.
# Демон, запущенный Swift-супервизором из .app, без них не находит ffmpeg
# (mlx-whisper декодирует аудио через ffmpeg subprocess) и claude.
# Встроенный режим кладёт ffmpeg в app_dir()/runtime/bin — он идёт первым.
_TOOL_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")

# Как часто проверять предохранители записи owner=app (app_guard.py).
GUARD_INTERVAL_SEC = 5
MAINTENANCE_INTERVAL_SEC = 3600
GAP_NOTE_APP_GONE = "[запись прервана: приложение перестало писать звук]"
_STOP_REASON_HUMAN = {
    "duration_limit": "превышен лимит длительности",
    "disk_full": "мало места на диске",
    "file_limit": "файл записи достиг предела в 4 GB",
}


def _cancel_message(reason: str) -> str:
    if reason == "empty":
        return "Запись пустая — звук не записался, сессия отменена"
    return (f"Запись короче {reconcile.min_recording_sec():g} с — похоже на случайное "
            "нажатие, сессия отменена")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # httpx пишет на INFO полный URL запроса, а в URL Telegram Bot API — токен бота.
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _ensure_tool_path() -> None:
    """Дополнить PATH каталогами инструментов (идемпотентно, в начало):
    runtime/bin встроенного режима (ffmpeg из imageio-ffmpeg), затем Homebrew."""
    import os
    dirs = (str(app_dir() / "runtime" / "bin"), *_TOOL_DIRS)
    current = os.environ.get("PATH", "").split(":")
    missing = [d for d in dirs if d not in current]
    if missing:
        os.environ["PATH"] = ":".join(missing + current)


class DaemonApp:
    def __init__(self):
        self._lock = threading.Lock()
        self._recorder: Recorder | None = None
        self._active_sid: str | None = None
        self._owner: str | None = None  # "daemon" | "app" — кто пишет audio.wav активной сессии
        self._guard: AppRecordingGuard | None = None  # предохранители owner=app
        self._stop_request: str | None = None  # причина авто-стопа для Swift
        self._tls = threading.local()  # sqlite-подключение на поток
        # Фоновая работа (импорт аудио); тесты подменяют на синхронный вызов.
        self._run_async = lambda fn, *args: threading.Thread(
            target=fn, args=args, daemon=True, name="import").start()

    @property
    def _con(self):
        con = getattr(self._tls, "con", None)
        if con is None:
            con = self._tls.con = db.connect()
        return con

    # ------------------------------------------------------------- handlers

    def start_recording(self, params: dict) -> dict:
        low_disk_note = None
        # Участники из календаря (продукт P8) — имена для владельцев задач.
        raw_attendees = params.get("attendees") or []
        attendees = [a.strip() for a in raw_attendees if isinstance(a, str) and a.strip()][:50] \
            if isinstance(raw_attendees, list) else []
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
            if attendees:
                self._con.execute("UPDATE sessions SET attendees=? WHERE id=?",
                                  (json.dumps(attendees, ensure_ascii=False), sid))
                self._con.commit()

            if owner == "app":
                # Swift CaptureEngine пишет WAV сам (Process Tap, SPEC_v3 §6
                # audio_path_owner=app) — демон только заводит сессию/каталог и
                # возвращает путь, Python-Recorder не стартует. Владелец пишется
                # в БД: после рестарта демона запись надо узнать (аудит T3).
                db.set_state(self._con, sid, State.RECORDING, audio_path=str(wav),
                             audio_owner="app")
                self._recorder, self._active_sid, self._owner = None, sid, "app"
                self._guard, self._stop_request = AppRecordingGuard(), None
                logger.info(
                    "REC start (owner=app): %s (%s)", sid, params.get("title") or "без названия")
                free = shutil.disk_usage(wav.parent).free
                min_gb = _env_num("MEETMIND_MIN_DISK_START_GB", DEFAULT_MIN_DISK_START_GB)
                if free < min_gb * 2**30:
                    low_disk_note = (f"Мало места на диске ({free / 2**30:.1f} GB) — "
                                     "запись остановится, если место кончится")
                result = {"session_id": sid, "audio_path": str(wav)}
            else:
                result = self._start_daemon_recorder(sid, wav, params)
        if low_disk_note:
            notify("BeeDott", low_disk_note, session_id=result["session_id"])
        return result

    def _start_daemon_recorder(self, sid: str, wav: Path, params: dict) -> dict:
        """Под self._lock. Fallback-путь: пишет Python Recorder (owner=daemon)."""
        rec = Recorder(
            out_path=wav,
            on_autostop=lambda reason, _sid=sid: self._on_autostop(_sid, reason),
            on_silence=lambda _sid=sid: notify(
                "BeeDott", "Долгая тишина — остановить запись? ⌘⇧R", session_id=_sid),
        )
        try:
            rec.start()
        except Exception:
            db.set_state(self._con, sid, State.CANCELLED)
            shutil.rmtree(session_dir(sid), ignore_errors=True)
            raise
        db.set_state(self._con, sid, State.RECORDING, audio_path=str(wav),
                     audio_owner="daemon")
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
            # (engine.stop() до stop_recording, MeetMindApp.swift).
            cancelled = self._close_recording(sid)
            if cancelled is None:
                return {"session_id": sid}
        notify("BeeDott", _cancel_message(cancelled), session_id=sid)
        return {"session_id": sid, "cancelled": cancelled}

    def _close_recording(self, sid: str, gap_note: str | None = None) -> str | None:
        """Под self._lock. Закрыть запись: проверить файл (незакрытый заголовок
        чинится), пустую или случайную короткую — отменить (продукт P6).
        None — запись в очереди на обработку; "empty" | "too_short" — отменена."""
        row = db.get_session(self._con, sid)
        sec = reconcile.audio_seconds(Path(row["audio_path"]))
        reason = None
        if sec == 0.0:
            reason = "empty"
        elif sec is not None and sec < reconcile.min_recording_sec():
            reason = "too_short"
        if reason:
            self._reset_active()
            db.set_state(self._con, sid, State.CANCELLED)
            shutil.rmtree(session_dir(sid), ignore_errors=True)
            logger.info("REC stop: %s — запись отменена (%s, %.1f с)", sid, reason, sec or 0)
            return reason
        self._finalize_recording(sid, gap_note=gap_note)
        return None

    def _reset_active(self) -> None:
        self._recorder = None
        self._active_sid = None
        self._owner = None
        self._guard = None
        self._stop_request = None

    def _finalize_recording(self, sid: str, gap_note: str | None = None) -> None:
        """Под self._lock. Закрывает запись и ставит транскрипцию в очередь."""
        self._reset_active()
        fields = {"ended_at": db.now()}
        if gap_note:
            fields["gap_note"] = gap_note
        db.set_state(self._con, sid, State.RECORDED, **fields)
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
        notify("BeeDott", f"Запись остановлена автоматически: {human}. "
                           "Записанное будет обработано.", session_id=sid)

    def cancel_recording(self, params: dict) -> dict:
        with self._lock:
            sid = params.get("session_id") or self._active_sid
            if sid == self._active_sid and self._active_sid is not None:
                # owner == "app": рекордера нет (Swift CaptureEngine уже остановлен
                # вызывающим до cancel_recording) — только сбрасываем внутреннее состояние.
                if self._owner == "daemon" and self._recorder is not None:
                    self._recorder.stop()
                self._reset_active()
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
            # owner=app: Swift сверяет со своим движком (осиротевшая запись →
            # stop_recording), stop_requested — сигнал предохранителя (T3, T7).
            "recording_owner": self._owner,
            "stop_requested": self._stop_request,
            "recording_title": rec_title,
            "recording_started_at": rec_started,
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
                "source": row["source"],
                "keep_audio": bool(row["keep_audio"]),
                "has_audio": bool(row["audio_path"]) and Path(row["audio_path"]).exists(),
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
        """«Пересобрать саммари» (экран 06): заново через Claude, транскрипт
        не перегоняется."""
        sid = params.get("session_id")
        if not sid:
            raise ControlError("bad_params", "нужен session_id")
        if db.get_session(self._con, sid) is None:
            raise ControlError("not_found", f"нет сессии {sid}")
        # Доставки сбрасываются — прогресс Telegram тоже, иначе побайтно тот же
        # текст «уже отправлен» и сообщение не придёт. Первое старое сообщение
        # запоминаем: новая версия уйдёт ответом на него (продукт P12).
        sdir = session_dir(sid)
        progress = sdir / "telegram_progress.json"
        try:
            sent = json.loads(progress.read_text(encoding="utf-8")).get("sent") or []
        except (OSError, json.JSONDecodeError):
            sent = []
        if sent:
            (sdir / "telegram_reply_to").write_text(str(sent[0]), encoding="utf-8")
        progress.unlink(missing_ok=True)
        db.resummarize(self._con, sid)
        return {"session_id": sid,
                "state": db.get_session(self._con, sid)["state"]}

    def rename_session(self, params: dict) -> dict:
        """Переименовать встречу из карточки (продукт P8). Поиск видит новое имя;
        уже отправленные Telegram и заметка не меняются."""
        sid = params.get("session_id")
        if not sid or db.get_session(self._con, sid) is None:
            raise ControlError("not_found", f"нет сессии {sid}")
        title = (params.get("title") or "").strip()[:200]
        if not title:
            raise ControlError("bad_params", "пустое название")
        self._con.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                          (title, db.now(), sid))
        self._con.execute("UPDATE notes_fts SET title=? WHERE session_id=?", (title, sid))
        self._con.commit()
        return {"session_id": sid, "title": title}

    def delete_session(self, params: dict) -> dict:
        """«Удалить встречу» (продукт P14): аудио, транскрипт, саммари, поиск и
        строки в БД. Заметки в ~/MeetMind и сообщения в Telegram остаются."""
        sid = params.get("session_id")
        if not sid or db.get_session(self._con, sid) is None:
            raise ControlError("not_found", f"нет сессии {sid}")
        with self._lock:
            if sid == self._active_sid:
                raise ControlError("busy", "идёт запись этой встречи — сначала остановите её")
            running = self._con.execute(
                "SELECT 1 FROM pipeline_jobs WHERE session_id=? AND status='running'",
                (sid,)).fetchone()
            if running:
                raise ControlError("busy", "встреча сейчас обрабатывается — удалите её, "
                                           "когда обработка закончится")
            for table in ("pipeline_jobs", "deliveries", "notes_fts", "sessions"):
                column = "id" if table == "sessions" else "session_id"
                self._con.execute(f"DELETE FROM {table} WHERE {column}=?", (sid,))
            self._con.commit()
        shutil.rmtree(app_dir() / "sessions" / sid, ignore_errors=True)
        logger.info("delete: встреча %s удалена", sid)
        return {"session_id": sid, "deleted": True}

    def import_audio(self, params: dict) -> dict:
        """«Импортировать аудио» (продукт P13): запись с телефона или диктофона
        проходит тот же пайплайн. Конвертация в моно FLAC 16 kHz идёт в фоне —
        ответ сразу, сессия появляется в Библиотеке как «в обработке»."""
        src = Path(params.get("path") or "").expanduser()
        if not src.is_file():
            raise ControlError("bad_params", f"файл не найден: {src}")
        title = (params.get("title") or "").strip() or src.stem
        sid = db.create_session(self._con, source="import", title=title)
        self._run_async(self._import_worker, sid, src)
        return {"session_id": sid}

    def _import_worker(self, sid: str, src: Path) -> None:
        dst = session_dir(sid) / "audio.flac"
        part = dst.with_name("audio.flac.part")
        try:
            # Моно: стерео-файл с телефона — это не «мик/система», деление на
            # спикеров по каналам для него было бы ложным.
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1",
                 "-ar", "16000", "-c:a", "flac", "-f", "flac", str(part)],
                check=True, capture_output=True)
            part.replace(dst)
            info = sf.info(str(dst))
            duration = info.frames / info.samplerate if info.samplerate else 0
            if duration <= 0:
                raise ValueError("в файле нет звука")
            end_ts = src.stat().st_mtime  # файл дописан в конце записи
            con = self._con
            con.execute("UPDATE sessions SET started_at=? WHERE id=?", (
                datetime.fromtimestamp(end_ts - duration, tz=timezone.utc)
                .isoformat(timespec="seconds"), sid))
            con.commit()
            db.set_state(con, sid, State.RECORDING, audio_path=str(dst), audio_owner="import")
            db.set_state(con, sid, State.RECORDED, ended_at=datetime.fromtimestamp(
                end_ts, tz=timezone.utc).isoformat(timespec="seconds"))
            db.enqueue_job(con, sid, "transcribe")
            logger.info("import: %s → %s (%.0f с) -> очередь transcribe", src.name, sid, duration)
        except Exception as exc:  # noqa: BLE001
            logger.exception("import: не удалось импортировать %s", src)
            part.unlink(missing_ok=True)
            try:
                db.set_state(self._con, sid, State.CANCELLED)
            except ValueError:
                pass
            shutil.rmtree(app_dir() / "sessions" / sid, ignore_errors=True)
            reason = (exc.stderr.decode(errors="replace").strip().splitlines() or ["ffmpeg"])[-1] \
                if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            notify("BeeDott", f"Не удалось импортировать «{src.name}»: {reason[:120]}")

    def notifications_pop(self, params: dict) -> dict:
        """Уведомления для MeetMind.app (продукт P16), см. daemon/notifier.py."""
        return {"notifications": notifier.pop_for_app()}

    def set_keep_audio(self, params: dict) -> dict:
        """«Хранить аудио» в карточке встречи: retention его не удалит (P10)."""
        sid = params.get("session_id")
        if not sid or db.get_session(self._con, sid) is None:
            raise ControlError("not_found", f"нет сессии {sid}")
        keep = bool(params.get("keep"))
        self._con.execute("UPDATE sessions SET keep_audio=?, updated_at=? WHERE id=?",
                          (int(keep), db.now(), sid))
        self._con.commit()
        return {"session_id": sid, "keep_audio": keep}

    def reprocess(self, params: dict) -> dict:
        sid = params.get("session_id")
        if not sid:
            raise ControlError("bad_params", "нужен session_id")
        db.reprocess(self._con, sid)
        return {"session_id": sid, "state": db.get_session(self._con, sid)["state"]}

    def health(self, params: dict) -> dict:
        return {"ok": True, "version": "3.0.0"}

    def shutdown(self, params: dict) -> dict:
        """Завершить демон по просьбе приложения: оно перезапускает демон с новым
        config.env, а демон, оставшийся от упавшего приложения, — не его child,
        SIGTERM ему не послать. Во время записи отказываем."""
        with self._lock:
            if self._active_sid is not None:
                raise ControlError("busy", "идёт запись — демон не останавливаем")
        # Ответ должен уйти до выхода — SIGTERM чуть позже.
        threading.Timer(0.3, os.kill, args=(os.getpid(), signal.SIGTERM)).start()
        return {"ok": True}

    # ------------------------------------------- предохранители owner=app (T7)

    def guard_tick(self) -> None:
        """Одна проверка предохранителей активной записи owner=app. Лимиты →
        stop_requested (останавливает Swift); брошенный файл → демон закрывает
        запись сам."""
        messages: list[str] = []
        with self._lock:
            if self._owner != "app" or self._active_sid is None or self._guard is None:
                return
            sid = self._active_sid
            row = db.get_session(self._con, sid)
            started = datetime.fromisoformat(row["started_at"]).timestamp()
            reason, silent = self._guard.check(
                started_at=started, audio_path=Path(row["audio_path"]))
            if reason == "app_gone":
                cancelled = self._close_recording(sid, gap_note=GAP_NOTE_APP_GONE)
                if cancelled is None:
                    messages.append("Звук перестал записываться — запись закрыта, "
                                    "записанное будет обработано")
                else:
                    messages.append(_cancel_message(cancelled))
                logger.warning("guard: %s — файл записи не меняется, запись закрыта", sid)
            elif reason and self._stop_request is None:
                self._stop_request = reason
                messages.append(f"Запись останавливается автоматически: "
                                f"{_STOP_REASON_HUMAN.get(reason, reason)}. "
                                "Записанное будет обработано.")
                logger.warning("guard: %s — запрошен стоп (%s)", sid, reason)
            if silent:
                messages.append("Долгая тишина — остановить запись? ⌘⇧R")
        for m in messages:
            notify("BeeDott", m, session_id=sid)

    def maintenance_tick(self) -> None:
        """Retention по расписанию, а не только при старте демона (P10)."""
        removed = retention.sweep(self._con)
        if removed:
            logger.info("retention: удалено аудио %d старых сессий", len(removed))

    def _maintenance_loop(self) -> None:
        while True:
            time.sleep(MAINTENANCE_INTERVAL_SEC)
            try:
                self.maintenance_tick()
            except Exception:  # noqa: BLE001
                logger.exception("maintenance_tick упал")

    def _guard_loop(self) -> None:
        while True:
            time.sleep(GUARD_INTERVAL_SEC)
            try:
                self.guard_tick()
            except Exception:  # noqa: BLE001 — поток предохранителя не должен умирать
                logger.exception("guard_tick упал")

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
            "set_keep_audio": self.set_keep_audio,
            "delete_session": self.delete_session,
            "rename_session": self.rename_session,
            "notifications_pop": self.notifications_pop,
            "import_audio": self.import_audio,
            "health": self.health,
            "shutdown": self.shutdown,
        }

    def startup(self, con) -> None:
        """Startup reconciliation: осиротевшие RECORDING-сессии и running-джобы
        после краша/ребута (SPEC_v3 §10). Python Recorder в этот момент точно не
        жив, а вот Swift CaptureEngine может продолжать писать — такую запись
        reconcile не трогает, и демон снова считает её активной (аудит T3)."""
        for action in reconcile.reconcile(
                con, notifier=lambda sid, msg: notify("BeeDott", msg, session_id=sid)):
            logger.info("reconcile: %s", action)
            if action["action"] == "adopted_app_recording":
                with self._lock:
                    self._recorder, self._active_sid, self._owner = None, action["session_id"], "app"
                    self._guard, self._stop_request = AppRecordingGuard(), None
        removed = retention.sweep(con)
        if removed:
            logger.info("retention: удалено аудио %d старых сессий", len(removed))
        indexed = db.backfill_fts(con)
        if indexed:
            logger.info("fts: доиндексировано %d старых сессий", indexed)

    def run(self) -> None:
        _ensure_tool_path()
        _configure_logging()
        orch_con = db.connect()
        self.startup(orch_con)

        server = ControlServer(self.handlers())
        server.start()
        threading.Thread(target=self._guard_loop, daemon=True, name="guard").start()
        threading.Thread(target=self._maintenance_loop, daemon=True, name="maintenance").start()
        orch = Orchestrator(
            orch_con, make_stages(),
            notifier=lambda sid, stage, err: notify(
                "BeeDott", f"Встреча не обработана ({stage}): {err[:120]} — аудио "
                            "сохранено, «Повторить» — в карточке встречи", session_id=sid),
            progress_notifier=lambda sid, minutes: notify(
                "BeeDott", f"Обработка длинной встречи продолжается ({minutes:.0f} мин)",
                session_id=sid),
        )
        logger.info("MeetMind daemon v3 запущен (control socket + orchestrator)")
        orch.run_forever()
