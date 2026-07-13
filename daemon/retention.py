"""Retention sweep (SPEC_v3 S4, PLAN_v3 Task 2.4).

Инвариант N1 (SPEC_v3 S3): файлы удаляются только в DELIVERED (retention) или
CANCELLED (немедленно, см. daemon/app.py). sweep() трогает ТОЛЬКО сам audio.wav —
транскрипт/заметки в session_dir и на sinks не удаляются никогда, история и
FTS-поиск остаются доступны после чистки аудио.
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from daemon import db
from daemon.states import State

logger = logging.getLogger(__name__)


def sweep(con, keep_days: int = 14) -> list[str]:
    """Удалить audio.wav у DELIVERED-сессий старше keep_days (по ended_at).

    Возвращает список session_id, у которых WAV был удалён/обнулён. FAILED,
    RECORDED и любые другие нетерминальные-для-удаления состояния не трогаются
    (may_delete_files() в states.py допускает только DELIVERED/CANCELLED; здесь
    отфильтровано жёстко в SQL, а не через may_delete_files, потому что CANCELLED
    чистится немедленно самим cancel-флоу, а не retention-свипом).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(
        timespec="seconds"
    )
    rows = con.execute(
        "SELECT id, audio_path FROM sessions WHERE state=? AND ended_at IS NOT NULL"
        " AND ended_at < ? AND audio_path IS NOT NULL",
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
