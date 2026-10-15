import pytest

from daemon import db
from daemon.states import State


def test_schema_and_session_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual", title="тест")
    s = db.get_session(con, sid)
    assert s["state"] == State.DETECTED and s["title"] == "тест"
    db.set_state(con, sid, State.RECORDING)
    assert db.get_session(con, sid)["state"] == State.RECORDING


def test_illegal_transition_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    with pytest.raises(ValueError):
        db.set_state(con, sid, State.DELIVERED)


def test_jobs_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    db.enqueue_job(con, sid, "transcribe")
    job = db.next_job(con)
    assert job["stage"] == "transcribe" and job["status"] == "queued"


def test_reprocess_from_failed_resets_session_and_job(tmp_path, monkeypatch):
    # Сессия провалилась на стадии transcribe: state=FAILED руками через UPDATE
    # (как если бы оркестратор пометил её так после исчерпания ретраев),
    # job transcribe помечена failed. reprocess() должен откатить сессию к
    # RECORDED (состояние перед transcribe) и вернуть job в очередь.
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    db.enqueue_job(con, sid, "transcribe")

    con.execute("UPDATE sessions SET state=? WHERE id=?", (State.FAILED, sid))
    con.execute(
        "UPDATE pipeline_jobs SET status='failed', last_error='boom'"
        " WHERE session_id=? AND stage='transcribe'", (sid,))
    con.commit()

    db.reprocess(con, sid)

    assert db.get_session(con, sid)["state"] == State.RECORDED
    job = db.next_job(con)
    assert job["stage"] == "transcribe" and job["status"] == "queued"


def test_session_title_falls_back_when_started_at_unparseable():
    """Битая/старая запись started_at не должна ронять session_title (его
    зовут Telegram, заметка, поиск и Claude через MCP)."""
    assert db.session_title({"title": None, "started_at": "не-дата"}) == "Встреча ??:??"
    assert db.session_title({"title": None, "started_at": None}) == "Встреча ??:??"


def test_migration_survives_concurrent_add_column(tmp_path, monkeypatch):
    """Два процесса открыли старую БД одновременно: оба увидели, что колонки
    нет, второй ALTER падает с «duplicate column». connect() не должен падать."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()  # колонка уже есть
    monkeypatch.setattr(db, "_session_columns", lambda c: {"id", "state"})
    db._migrate(con)  # «устаревший» снимок колонок → ALTER на существующую колонку


def test_search_notes_orders_by_relevance_not_insertion(tmp_path, monkeypatch):
    """search_notes без ORDER BY возвращал LIMIT строк в порядке rowid (т.е.
    самые старые по вставке), а не самые релевантные. Слабое совпадение
    вставлено первым — при limit=1 должна остаться сильная строка (термин
    повторяется много раз), а не первая по rowid."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    weak_sid = db.create_session(con, source="manual", title="слабое")
    db.index_note(con, weak_sid, title="слабое",
                  transcript="отчёт один раз мелькнул в начале текста", summary="")
    strong_sid = db.create_session(con, source="manual", title="сильное")
    db.index_note(con, strong_sid, title="сильное",
                  transcript="отчёт отчёт отчёт отчёт отчёт", summary="")

    hits = db.search_notes(con, "отчёт", limit=1)
    assert len(hits) == 1
    assert hits[0]["session_id"] == strong_sid


def test_redeliver_resets_deliver_job_in_one_go(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual", title="Синк")
    for st in (State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED,
               State.SUMMARIZING, State.FAILED):
        db.set_state(con, sid, st)
    for stage in ("transcribe", "summarize", "deliver"):
        db.enqueue_job(con, sid, stage)
    con.execute("UPDATE pipeline_jobs SET status='done' WHERE session_id=? AND stage='transcribe'", (sid,))
    con.execute("UPDATE pipeline_jobs SET status='failed', attempts=8, last_error='x'"
                " WHERE session_id=? AND stage IN ('summarize', 'deliver')", (sid,))
    con.commit()

    db.redeliver(con, sid)

    assert db.get_session(con, sid)["state"] == State.SUMMARIZED
    jobs = {r["stage"]: r for r in con.execute(
        "SELECT * FROM pipeline_jobs WHERE session_id=?", (sid,))}
    assert (jobs["deliver"]["status"], jobs["deliver"]["attempts"],
            jobs["deliver"]["last_error"], jobs["deliver"]["next_retry_at"]) == ("queued", 0, None, None)
    assert (jobs["summarize"]["status"], jobs["summarize"]["last_error"]) == ("done", None)


def test_redeliver_inserts_missing_deliver_job(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    for st in (State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED,
               State.SUMMARIZING, State.SUMMARIZED, State.DELIVERING, State.DELIVERED):
        db.set_state(con, sid, st)
    db.redeliver(con, sid)
    row = con.execute("SELECT status FROM pipeline_jobs WHERE session_id=? AND stage='deliver'",
                      (sid,)).fetchone()
    assert row["status"] == "queued"
