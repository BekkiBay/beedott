from daemon import db, memguard
from daemon.states import State
from daemon.orchestrator import Orchestrator


def _no_memory_limit(monkeypatch):
    # Тесты гоняют тривиальные lambda-стадии, а не реальные transcribe/summarize —
    # реальный психsutil.available() машины (может быть <10GB) не должен решать,
    # проходит тест или нет. memguard.require() как факт вызывается (см. вручную
    # ниже), но порог всегда пройден.
    monkeypatch.setattr(memguard, "available", lambda: 64 * 2**30)


def test_pipeline_runs_all_stages(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    _no_memory_limit(monkeypatch)
    con = db.connect()
    sid = db.create_session(con, source="manual")
    for st in (State.RECORDING, State.RECORDED):
        db.set_state(con, sid, st)
    db.enqueue_job(con, sid, "transcribe")
    calls = []
    orch = Orchestrator(con, stages={
        "transcribe": lambda s: calls.append("t"),
        "summarize":  lambda s: calls.append("s"),
        "deliver":    lambda s: calls.append("d")})
    orch.run_once_until_idle()
    assert calls == ["t", "s", "d"]
    assert db.get_session(con, sid)["state"] == State.DELIVERED


def test_stage_failure_sets_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    _no_memory_limit(monkeypatch)
    con = db.connect()
    sid = db.create_session(con, source="manual")
    for st in (State.RECORDING, State.RECORDED):
        db.set_state(con, sid, st)
    db.enqueue_job(con, sid, "transcribe")
    orch = Orchestrator(con, stages={"transcribe": lambda s: 1 / 0})
    orch.run_once_until_idle()
    job = con.execute("SELECT * FROM pipeline_jobs WHERE session_id=?", (sid,)).fetchone()
    assert job["status"] == "failed_retry" and job["attempts"] == 1
    assert job["next_retry_at"] is not None
    # сессия остаётся в *_ING, пока джоба ретраится (SPEC_v3 §3)
    assert db.get_session(con, sid)["state"] == State.TRANSCRIBING


def test_stage_permanently_fails_after_max_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    _no_memory_limit(monkeypatch)
    con = db.connect()
    sid = db.create_session(con, source="manual")
    for st in (State.RECORDING, State.RECORDED):
        db.set_state(con, sid, st)
    db.enqueue_job(con, sid, "transcribe")

    notified = []
    orch = Orchestrator(con, stages={"transcribe": lambda s: 1 / 0},
                        notifier=lambda sid, stage, err: notified.append((sid, stage)))

    # прогоняем 8 попыток вручную, обнуляя next_retry_at (без реального backoff-сна)
    for _ in range(8):
        orch.run_once_until_idle()
        con.execute("UPDATE pipeline_jobs SET next_retry_at=NULL WHERE session_id=?", (sid,))
        con.commit()

    job = con.execute("SELECT * FROM pipeline_jobs WHERE session_id=?", (sid,)).fetchone()
    assert job["status"] == "failed" and job["attempts"] == 8
    assert db.get_session(con, sid)["state"] == State.FAILED
    assert notified == [(sid, "transcribe")]


def test_nonchain_job_leaves_state(tmp_path, monkeypatch):
    """diarize (chain=0) на уже DELIVERED-сессии — выполняется, sessions.state
    не трогается вовсе (SPEC_v3 §3: non-chain джобы)."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    _no_memory_limit(monkeypatch)
    con = db.connect()
    sid = db.create_session(con, source="manual")
    for st in (State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED,
               State.SUMMARIZING, State.SUMMARIZED, State.DELIVERING, State.DELIVERED):
        db.set_state(con, sid, st)
    db.enqueue_job(con, sid, "diarize", chain=0)

    calls = []
    orch = Orchestrator(con, stages={"diarize": lambda s: calls.append("diarize")})
    orch.run_once_until_idle()

    assert calls == ["diarize"]
    assert db.get_session(con, sid)["state"] == State.DELIVERED
    job = con.execute(
        "SELECT * FROM pipeline_jobs WHERE session_id=? AND stage='diarize'", (sid,)
    ).fetchone()
    assert job["status"] == "done"


def test_cancel_race(tmp_path, monkeypatch):
    """Стадия выполняется; в это время сессию отменили через db.set_state
    (эмулируем гонку прямо внутри обработчика стадии). Стадия завершилась успешно,
    но orchestrator не должен падать и не должен помечать job как done — файловый
    инвариант (удаление только в DELIVERED/CANCELLED) не нарушается."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    _no_memory_limit(monkeypatch)
    con = db.connect()
    sid = db.create_session(con, source="manual")
    for st in (State.RECORDING, State.RECORDED):
        db.set_state(con, sid, st)
    db.enqueue_job(con, sid, "transcribe")

    def _transcribe_then_cancel(session):
        db.set_state(con, session["id"], State.CANCELLED)

    orch = Orchestrator(con, stages={"transcribe": _transcribe_then_cancel})
    orch.run_once_until_idle()  # не должен упасть

    assert db.get_session(con, sid)["state"] == State.CANCELLED
    job = con.execute(
        "SELECT * FROM pipeline_jobs WHERE session_id=? AND stage='transcribe'", (sid,)
    ).fetchone()
    assert job["status"] == "skipped"
