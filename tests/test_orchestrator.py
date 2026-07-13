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


# ------------------------------------------------------------------------
# Task 2.6: уведомление о долгой обработке (SPEC_v3 §9/§10). Клок инжектируется
# через параметр Orchestrator(clock=...) — тесты не трогают глобальный
# time.monotonic, стадии сами двигают фейковый клок вперёд, эмулируя, сколько
# "заняла" обработка.
# ------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _recorded_session_with_transcribe_job(con):
    sid = db.create_session(con, source="manual")
    for st in (State.RECORDING, State.RECORDED):
        db.set_state(con, sid, st)
    db.enqueue_job(con, sid, "transcribe")
    return sid


def test_long_processing_notifies_once_after_threshold(tmp_path, monkeypatch):
    """Суммарное время TRANSCRIBING+SUMMARIZING превышает порог -> ровно одно
    уведомление, вызванное на переходе в следующую chain-стадию (deliver)."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    monkeypatch.setenv("MEETMIND_LONG_PROCESSING_MIN", "15")
    _no_memory_limit(monkeypatch)
    con = db.connect()
    sid = _recorded_session_with_transcribe_job(con)

    clock = _FakeClock()
    notified = []
    orch = Orchestrator(
        con,
        stages={
            "transcribe": lambda s: clock.advance(10 * 60),  # 10 мин
            "summarize": lambda s: clock.advance(10 * 60),  # +10 мин = 20 > 15
            "deliver": lambda s: None,
        },
        progress_notifier=lambda sid_, minutes: notified.append((sid_, minutes)),
        clock=clock,
    )
    orch.run_once_until_idle()

    assert db.get_session(con, sid)["state"] == State.DELIVERED
    assert notified == [(sid, 20.0)]
    # таймер убран из памяти после DELIVERED (не течёт бесконечно)
    assert sid not in orch._processing_started


def test_long_processing_not_notified_under_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    monkeypatch.setenv("MEETMIND_LONG_PROCESSING_MIN", "15")
    _no_memory_limit(monkeypatch)
    con = db.connect()
    sid = _recorded_session_with_transcribe_job(con)

    clock = _FakeClock()
    notified = []
    orch = Orchestrator(
        con,
        stages={
            "transcribe": lambda s: clock.advance(60),
            "summarize": lambda s: clock.advance(60),
            "deliver": lambda s: clock.advance(60),
        },
        progress_notifier=lambda sid_, minutes: notified.append((sid_, minutes)),
        clock=clock,
    )
    orch.run_once_until_idle()

    assert db.get_session(con, sid)["state"] == State.DELIVERED
    assert notified == []


def test_long_processing_env_threshold_override_and_no_duplicate(tmp_path, monkeypatch):
    """MEETMIND_LONG_PROCESSING_MIN=1 понижает порог; после первого срабатывания
    дальнейшие стадии той же сессии уведомление не дублируют."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    monkeypatch.setenv("MEETMIND_LONG_PROCESSING_MIN", "1")
    _no_memory_limit(monkeypatch)
    con = db.connect()
    sid = _recorded_session_with_transcribe_job(con)

    clock = _FakeClock()
    notified = []
    orch = Orchestrator(
        con,
        stages={
            "transcribe": lambda s: clock.advance(2 * 60),
            "summarize": lambda s: clock.advance(2 * 60),
            "deliver": lambda s: clock.advance(2 * 60),
        },
        progress_notifier=lambda sid_, minutes: notified.append((sid_, minutes)),
        clock=clock,
    )
    orch.run_once_until_idle()

    assert len(notified) == 1
    assert notified[0][0] == sid


def test_long_processing_without_progress_notifier_does_not_crash(tmp_path, monkeypatch):
    """progress_notifier не передан (None) — таймер считается молча, без ошибок."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    monkeypatch.setenv("MEETMIND_LONG_PROCESSING_MIN", "1")
    _no_memory_limit(monkeypatch)
    con = db.connect()
    sid = _recorded_session_with_transcribe_job(con)

    clock = _FakeClock()
    orch = Orchestrator(
        con,
        stages={
            "transcribe": lambda s: clock.advance(5 * 60),
            "summarize": lambda s: None,
            "deliver": lambda s: None,
        },
        clock=clock,
    )
    orch.run_once_until_idle()  # не должен упасть без progress_notifier

    assert db.get_session(con, sid)["state"] == State.DELIVERED


def test_long_processing_default_clock_is_time_monotonic(tmp_path, monkeypatch):
    """Без явного clock= используется настоящий time.monotonic — sanity check,
    что дефолт не ломается (реальная длительность теста ничтожна, порог не
    достигается)."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    _no_memory_limit(monkeypatch)
    con = db.connect()
    sid = _recorded_session_with_transcribe_job(con)

    notified = []
    orch = Orchestrator(
        con,
        stages={
            "transcribe": lambda s: None,
            "summarize": lambda s: None,
            "deliver": lambda s: None,
        },
        progress_notifier=lambda sid_, minutes: notified.append((sid_, minutes)),
    )
    orch.run_once_until_idle()

    assert db.get_session(con, sid)["state"] == State.DELIVERED
    assert notified == []
