"""Startup reconciliation (SPEC_v3 S10 "Mac перезагрузился / приложение крашнулось
посреди RECORDING", PLAN_v3 Task 2.6).

reconcile() вызывается РОВНО ОДИН РАЗ при старте демона, ДО того как поднимается
живой Recorder или Orchestrator-поллинг — в этот момент гарантированно нет
процесса, который мог бы дописывать WAV или владеть running-джобой. Всё, что
осталось в нетерминальных состояниях без владельца, закрывается по данным на
диске:

  * sessions.state == RECORDING:
      - audio.wav есть и не пустой (> 44 байт, т.е. не только заголовок) ->
        чиним незакрытый RIFF-заголовок через ffmpeg (-ignore_length 1), сессия
        -> RECORDED с gap_note и ended_at = mtime файла, транскрайб-джоба в очередь;
      - иначе (нет файла / пустой) -> CANCELLED, session_dir удаляется целиком
        (файлового инварианта тут нет: записи не было).
  * pipeline_jobs.status == 'running' (владелец умер вместе с демоном) -> queued,
    next_retry_at сбрасывается.
  * sessions.state == DETECTED без файлов (крэш между create_session и стартом
    записи) -> CANCELLED.
"""
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from daemon import db
from daemon.paths import session_dir
from daemon.states import State

logger = logging.getLogger(__name__)

GAP_NOTE_DAEMON_CRASH = "[запись прервана: демон упал]"
MIN_WAV_SIZE = 44  # ровно заголовок RIFF/WAVE, без единого сэмпла = пустая запись


def _fix_wav_header(path: Path) -> None:
    """Незакрытый WAV (демон умер до Recorder.stop()) хранит либо нулевой, либо
    заведомо неверный размер в RIFF/data-чанках — обычные ридеры (soundfile)
    отказываются такое читать. ffmpeg с -ignore_length 1 читает "до конца файла",
    не доверяя объявленному размеру, и copy-транскодирует в новый файл с корректным
    заголовком. Проверено вживую (см. M1 verify note в PLAN_v3). Поднятое
    исключение (ffmpeg нет / битый файл) — ответственность вызывающего."""
    fixed = path.with_name(path.stem + "_fixed" + path.suffix)
    subprocess.run(
        ["ffmpeg", "-y", "-ignore_length", "1", "-i", str(path), "-c", "copy", str(fixed)],
        check=True,
        capture_output=True,
    )
    fixed.replace(path)


def _resolve_audio_path(row) -> Path:
    if row["audio_path"]:
        return Path(row["audio_path"])
    return session_dir(row["id"]) / "audio.wav"


def _notify(notifier: Optional[Callable[[str, str], None]], sid: str, message: str) -> None:
    if notifier is None:
        return
    try:
        notifier(sid, message)
    except Exception:
        logger.exception("reconcile: notifier упал (session %s)", sid)


def _reconcile_recording_sessions(con, notifier) -> list[dict]:
    actions: list[dict] = []
    rows = con.execute("SELECT * FROM sessions WHERE state=?", (State.RECORDING,)).fetchall()
    for row in rows:
        sid = row["id"]
        path = _resolve_audio_path(row)

        if path.exists() and path.stat().st_size > MIN_WAV_SIZE:
            ended_at = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
            try:
                _fix_wav_header(path)
            except (subprocess.CalledProcessError, OSError, FileNotFoundError):
                logger.exception(
                    "reconcile: не смог починить WAV header %s (session %s) —"
                    " ставлю на транскрайб как есть", path, sid,
                )
            db.set_state(
                con, sid, State.RECORDED,
                ended_at=ended_at, gap_note=GAP_NOTE_DAEMON_CRASH, audio_path=str(path),
            )
            db.enqueue_job(con, sid, "transcribe")
            actions.append({
                "session_id": sid, "action": "recovered_recording", "audio_path": str(path),
            })
            _notify(notifier, sid, "запись восстановлена после сбоя демона, отправлена на обработку")
        else:
            db.set_state(con, sid, State.CANCELLED)
            shutil.rmtree(session_dir(sid), ignore_errors=True)
            actions.append({"session_id": sid, "action": "cancelled_empty_recording"})
            _notify(notifier, sid, "запись отменена: аудио не найдено после сбоя демона")
    return actions


def _requeue_orphaned_jobs(con) -> list[dict]:
    actions: list[dict] = []
    rows = con.execute("SELECT * FROM pipeline_jobs WHERE status='running'").fetchall()
    for row in rows:
        con.execute(
            "UPDATE pipeline_jobs SET status='queued', next_retry_at=NULL, updated_at=?"
            " WHERE id=?",
            (db.now(), row["id"]),
        )
        actions.append({
            "session_id": row["session_id"], "stage": row["stage"], "action": "requeued_job",
        })
    if actions:
        con.commit()
    return actions


def _cancel_orphaned_detected(con) -> list[dict]:
    actions: list[dict] = []
    rows = con.execute("SELECT * FROM sessions WHERE state=?", (State.DETECTED,)).fetchall()
    for row in rows:
        sid = row["id"]
        db.set_state(con, sid, State.CANCELLED)
        shutil.rmtree(session_dir(sid), ignore_errors=True)
        actions.append({"session_id": sid, "action": "cancelled_detected"})
    return actions


def reconcile(con, notifier: Optional[Callable[[str, str], None]] = None) -> list[dict]:
    """Закрыть все сессии/джобы, зависшие без владельца после ребута/крэша.
    Вызывать один раз при старте демона, до Recorder/Orchestrator. Возвращает
    список выполненных действий (для логов/уведомлений вызывающей стороны)."""
    actions: list[dict] = []
    actions += _reconcile_recording_sessions(con, notifier)
    actions += _requeue_orphaned_jobs(con)
    actions += _cancel_orphaned_detected(con)
    con.commit()
    return actions
