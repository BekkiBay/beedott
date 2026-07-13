"""Poll-loop оркестратор: строго один job одновременно (24GB бюджет), backoff-ретраи,
memory-guard перед тяжёлыми стадиями. См. SPEC_v3 §3 (инварианты, non-chain джобы) и
PLAN_v3 Task 1.7."""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from daemon import db, memguard
from daemon.states import State

logger = logging.getLogger(__name__)

# stage -> (running_state, done_state, next_stage). Действует ТОЛЬКО для джобов с
# chain=1 (основная цепочка, двигает sessions.state). non-chain джобы (напр. diarize
# из M7) выполняются тем же циклом, но sessions.state не трогают вовсе — см.
# _on_stage_success/run_once ниже.
CHAIN: dict[str, tuple[State, State, Optional[str]]] = {
    "transcribe": (State.TRANSCRIBING, State.TRANSCRIBED, "summarize"),
    "summarize": (State.SUMMARIZING, State.SUMMARIZED, "deliver"),
    "deliver": (State.DELIVERING, State.DELIVERED, None),
}

MAX_ATTEMPTS = 8


class Orchestrator:
    """stages: {"transcribe": fn(session_row) -> None, "summarize": ..., "deliver": ...,
    "diarize": ...} — обработчики стадий; любое исключение внутри = failed_retry.

    notifier: опциональный callable(session_id, stage, error_text), вызывается когда
    джоба окончательно проваливается (attempts >= 8) и сессия помечается FAILED.
    """

    def __init__(self, con, stages: dict[str, Callable], notifier: Optional[Callable] = None):
        self.con = con
        self.stages = stages
        self.notifier = notifier

    # ------------------------------------------------------------------ loop

    def run_once(self) -> bool:
        """Забрать и обработать один job. True — job был найден и обработан
        (независимо от результата), False — очередь пуста (idle)."""
        job = db.next_job(self.con)
        if job is None:
            return False

        job_id = job["id"]
        sid = job["session_id"]
        stage = job["stage"]
        is_chain_stage = bool(job["chain"]) and stage in CHAIN

        self._mark_running(job_id)

        if is_chain_stage:
            running_state, _, _ = CHAIN[stage]
            if not db.try_set_state(self.con, sid, running_state):
                # Сессию отменили (или иначе увели из ожидаемого состояния) ещё до
                # старта стадии — не запускаем обработчик вовсе.
                logger.warning(
                    "orchestrator: session %s не в состоянии для старта %s — job skipped",
                    sid, stage)
                self._mark_skipped(job_id)
                return True

        session = db.get_session(self.con, sid)

        try:
            memguard.require(stage)
            fn = self.stages[stage]
            fn(session)
        except Exception as exc:  # noqa: BLE001 — любой сбой стадии = failed_retry
            self._on_stage_failure(job, exc)
            return True

        self._on_stage_success(job_id, sid, stage, is_chain_stage)
        return True

    def run_once_until_idle(self) -> None:
        """Молотит run_once(), пока очередь не опустеет. Для тестов — без sleep."""
        while self.run_once():
            pass

    def run_forever(self, poll_sec: float = 3) -> None:
        """Бесконечный poll-цикл демона."""
        while True:
            if not self.run_once():
                time.sleep(poll_sec)

    # --------------------------------------------------------------- helpers

    def _mark_running(self, job_id: int) -> None:
        self.con.execute(
            "UPDATE pipeline_jobs SET status='running', updated_at=? WHERE id=?",
            (db.now(), job_id))
        self.con.commit()

    def _mark_done(self, job_id: int) -> None:
        self.con.execute(
            "UPDATE pipeline_jobs SET status='done', updated_at=? WHERE id=?",
            (db.now(), job_id))
        self.con.commit()

    def _mark_skipped(self, job_id: int) -> None:
        """Сессию отменили (гонка) до старта стадии или пока она выполнялась.
        Результат стадии выброшен (файлы чистит cancel-флоу), job — не done, а
        skipped: файловый инвариант (SPEC_v3 §3, "удаление только в
        DELIVERED/CANCELLED") не нарушается, демон не падает."""
        self.con.execute(
            "UPDATE pipeline_jobs SET status='skipped', updated_at=? WHERE id=?",
            (db.now(), job_id))
        self.con.commit()

    def _on_stage_success(self, job_id: int, sid: str, stage: str,
                           is_chain_stage: bool) -> None:
        if not is_chain_stage:
            # non-chain джоба (напр. diarize): sessions.state не трогаем вовсе.
            self._mark_done(job_id)
            return

        _, done_state, next_stage = CHAIN[stage]
        if not db.try_set_state(self.con, sid, done_state):
            logger.warning(
                "orchestrator: session %s отменили во время %s — результат выброшен",
                sid, stage)
            self._mark_skipped(job_id)
            return

        self._mark_done(job_id)
        if next_stage is not None:
            db.enqueue_job(self.con, sid, next_stage)

    def _on_stage_failure(self, job, exc: Exception) -> None:
        job_id = job["id"]
        sid = job["session_id"]
        stage = job["stage"]
        attempts = job["attempts"] + 1
        logger.error(
            "orchestrator: стадия %s провалилась (session %s, попытка %d/%d): %s",
            stage, sid, attempts, MAX_ATTEMPTS, exc)

        if attempts >= MAX_ATTEMPTS:
            self.con.execute(
                "UPDATE pipeline_jobs SET status='failed', attempts=?, last_error=?,"
                " next_retry_at=NULL, updated_at=? WHERE id=?",
                (attempts, str(exc), db.now(), job_id))
            self.con.commit()
            db.try_set_state(self.con, sid, State.FAILED)
            if self.notifier is not None:
                try:
                    self.notifier(sid, stage, str(exc))
                except Exception:
                    logger.exception("orchestrator: notifier упал")
            return

        delay = min(60 * 2 ** attempts, 3600)
        next_retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)) \
            .isoformat(timespec="seconds")
        self.con.execute(
            "UPDATE pipeline_jobs SET status='failed_retry', attempts=?, last_error=?,"
            " next_retry_at=?, updated_at=? WHERE id=?",
            (attempts, str(exc), next_retry_at, db.now(), job_id))
        self.con.commit()
