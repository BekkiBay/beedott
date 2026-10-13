"""Retention sweep (SPEC_v3 S4, PLAN_v3 Task 2.4).

Инвариант N1 (SPEC_v3 S3): файлы удаляются только в DELIVERED (retention) или
CANCELLED (немедленно, см. daemon/app.py). sweep() трогает ТОЛЬКО сам audio.wav —
транскрипт/заметки в session_dir и на sinks не удаляются никогда, история и
FTS-поиск остаются доступны после чистки аудио.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daemon import db
from daemon.states import State

logger = logging.getLogger(__name__)


DEFAULT_RETENTION_DAYS = 14


def retention_days() -> int:
    """MEETMIND_RETENTION_DAYS, 0 — аудио не удаляется никогда (продукт P10)."""
    return int(os.environ.get("MEETMIND_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))


def sweep(con, keep_days: int | None = None) -> list[str]:
    """Удалить аудио у DELIVERED-сессий старше keep_days (по ended_at), кроме
    помеченных «Хранить аудио» (keep_audio=1, продукт P10). keep_days по
    умолчанию — из MEETMIND_RETENTION_DAYS; 0 — ничего не удалять.

    Возвращает список session_id, у которых WAV был удалён/обнулён. FAILED,
    RECORDED и любые другие нетерминальные-для-удаления состояния не трогаются
    (may_delete_files() в states.py допускает только DELIVERED/CANCELLED; здесь
    отфильтровано жёстко в SQL, а не через may_delete_files, потому что CANCELLED
    чистится немедленно самим cancel-флоу, а не retention-свипом).
    """
    keep_days = retention_days() if keep_days is None else keep_days
    if keep_days <= 0:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(
        timespec="seconds"
    )
    rows = con.execute(
        "SELECT id, audio_path FROM sessions WHERE state=? AND ended_at IS NOT NULL"
        " AND ended_at < ? AND audio_path IS NOT NULL AND keep_audio=0",
        (State.DELIVERED, cutoff),
    ).fetchall()

    swept: list[str] = []
    for row in rows:
        sid = row["id"]
        audio_path = Path(row["audio_path"])
        try:
            audio_path.unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "retention: не смог удалить %s (session %s) — пропускаю", audio_path, sid
            )
            continue
        con.execute(
            "UPDATE sessions SET audio_path=NULL, updated_at=? WHERE id=?",
            (db.now(), sid),
        )
        swept.append(sid)
        logger.info("retention: удалён %s (session %s)", audio_path, sid)

    if swept:
        con.commit()
    return swept
