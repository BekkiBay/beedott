"""Правка фоллоу-апа из Claude (MCP update_followup): daemon/followup_edit.py."""
import json

import pytest

import config
from daemon import db, followup_edit
from daemon.followup_edit import EditError
from daemon.paths import session_dir
from daemon.states import State
from tests.conftest import GOOD_FOLLOWUP as GOOD

CHAIN = [State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED,
         State.SUMMARIZING, State.SUMMARIZED, State.DELIVERING, State.DELIVERED]
EDITED = dict(GOOD, action_items=[{"task": "Собрать отчёт", "owner": "Стас", "deadline": None}])
EMPTY = {"context": "", "discussed": [], "decisions": [], "action_items": [], "open_questions": []}


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    return db.connect()


def _session(con, *, state=State.DELIVERED, followup=True, transcript=True):
    sid = db.create_session(con, source="manual", title="Синк")
    for st in CHAIN:
        db.set_state(con, sid, st)
        if st == state:
            break
    sdir = session_dir(sid)
    if transcript:
        (sdir / "transcript.txt").write_text("Спикер 1: обсуждаем отчёт", encoding="utf-8")
    if followup:
        (sdir / "followup.json").write_text(json.dumps(
            dict(GOOD, engine="anthropic-api:claude-opus-5-5", revision=1),
            ensure_ascii=False), encoding="utf-8")
    for stage in ("transcribe", "summarize", "deliver"):
        db.enqueue_job(con, sid, stage)
    con.execute("UPDATE pipeline_jobs SET status='done' WHERE session_id=?", (sid,))
    for sink in ("telegram", "note", "obsidian"):
        con.execute("INSERT INTO deliveries(session_id, sink, status, delivered_at)"
                    " VALUES(?,?,?,?)", (sid, sink, "delivered", db.now()))
    con.commit()
    return sid


def _telegram_configured(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")


def _jobs(con, sid):
    return {r["stage"]: dict(r) for r in con.execute(
        "SELECT * FROM pipeline_jobs WHERE session_id=?", (sid,))}


def _deliveries(con, sid):
    return {r["sink"]: r["status"] for r in con.execute(
        "SELECT sink, status FROM deliveries WHERE session_id=?", (sid,))}


def test_edit_writes_new_revision_and_requeues_delivery(con, monkeypatch):
    _telegram_configured(monkeypatch)
    sid = _session(con)
    assert followup_edit.apply(con, sid, EDITED, resend=False) == {
        "session_id": sid, "revision": 2, "resend": False, "telegram": "unchanged"}
    saved = json.loads((session_dir(sid) / "followup.json").read_text(encoding="utf-8"))
    assert saved["action_items"] == EDITED["action_items"]
    assert (saved["engine"], saved["revision"]) == ("anthropic-api:claude-opus-5-5", 2)
    md = (session_dir(sid) / "followup.md").read_text(encoding="utf-8")
    assert "Собрать отчёт — Стас" in md and "№2" in md
    assert db.get_session(con, sid)["state"] == State.SUMMARIZED
    deliver = _jobs(con, sid)["deliver"]
    assert (deliver["status"], deliver["attempts"]) == ("queued", 0)
    assert _deliveries(con, sid) == {"telegram": "delivered", "note": "pending", "obsidian": "pending"}
    hit = con.execute("SELECT session_id FROM notes_fts WHERE notes_fts MATCH 'Стас'").fetchone()
    assert hit["session_id"] == sid


def test_no_resend_leaves_telegram_progress_untouched(con):
    sid = _session(con)
    progress = session_dir(sid) / "telegram_progress.json"
    progress.write_text('{"hash": "x", "sent": ["555"]}')
    followup_edit.apply(con, sid, EDITED, resend=False)
    assert progress.read_text() == '{"hash": "x", "sent": ["555"]}'
    assert not (session_dir(sid) / "telegram_reply_to").exists()


def test_previous_revision_is_kept_on_disk(con):
    sid = _session(con)
    sdir = session_dir(sid)
    original = (sdir / "followup.json").read_text(encoding="utf-8")
    followup_edit.apply(con, sid, EDITED, resend=False)
    first_edit = (sdir / "followup.json").read_text(encoding="utf-8")
    assert (sdir / "followup.r1.json").read_text(encoding="utf-8") == original

    # демон бы довёл deliver обратно до DELIVERED; тест эмулирует это вручную
    db.set_state(con, sid, State.DELIVERING)
    db.set_state(con, sid, State.DELIVERED)
    edited_again = dict(GOOD, action_items=[{"task": "Вторая правка", "owner": "Стас", "deadline": None}])
    followup_edit.apply(con, sid, edited_again, resend=False)
    assert (sdir / "followup.r2.json").read_text(encoding="utf-8") == first_edit


def test_empty_followup_is_bad_params(con):
    sid = _session(con)
    before = (session_dir(sid) / "followup.json").read_text(encoding="utf-8")
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, EMPTY, resend=False)
    assert exc.value.code == "bad_params"
    assert (session_dir(sid) / "followup.json").read_text(encoding="utf-8") == before
    assert db.get_session(con, sid)["state"] == State.DELIVERED


def test_old_followup_json_not_a_dict_is_tolerated(con):
    sid = _session(con, followup=False)
    (session_dir(sid) / "followup.json").write_text("[]", encoding="utf-8")
    followup_edit.apply(con, sid, EDITED, resend=False)
    saved = json.loads((session_dir(sid) / "followup.json").read_text(encoding="utf-8"))
    assert (saved["engine"], saved["revision"]) == ("claude", 2)


def test_db_failure_rolls_back(con, monkeypatch):
    sid = _session(con)

    def boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(followup_edit.db, "redeliver", boom)
    with pytest.raises(RuntimeError):
        followup_edit.apply(con, sid, EDITED, resend=False)
    assert _deliveries(con, sid)["note"] == "delivered"
    assert db.get_session(con, sid)["state"] == State.DELIVERED


def test_telegram_status_off_when_not_configured(con, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    sid = _session(con)
    assert followup_edit.apply(con, sid, EDITED, resend=False)["telegram"] == "off"


def test_telegram_status_pending_on_resend(con, monkeypatch):
    _telegram_configured(monkeypatch)
    sid = _session(con)
    assert followup_edit.apply(con, sid, EDITED, resend=True)["telegram"] == "pending"


@pytest.mark.parametrize("missing", [True, False])
def test_telegram_status_pending_when_row_not_delivered(con, monkeypatch, missing):
    _telegram_configured(monkeypatch)
    sid = _session(con)
    if missing:
        con.execute("DELETE FROM deliveries WHERE session_id=? AND sink='telegram'", (sid,))
    else:
        con.execute("UPDATE deliveries SET status='failed_retry'"
                    " WHERE session_id=? AND sink='telegram'", (sid,))
    con.commit()
    assert followup_edit.apply(con, sid, EDITED, resend=False)["telegram"] == "pending"


def test_resend_resets_telegram_and_replies_to_first_message(con):
    sid = _session(con)
    (session_dir(sid) / "telegram_progress.json").write_text('{"hash": "x", "sent": ["555", "556"]}')
    followup_edit.apply(con, sid, EDITED, resend=True)
    assert _deliveries(con, sid)["telegram"] == "pending"
    assert (session_dir(sid) / "telegram_reply_to").read_text() == "555"
    assert not (session_dir(sid) / "telegram_progress.json").exists()


def test_failed_delivery_gets_fresh_retries(con):
    sid = _session(con, state=State.DELIVERING)
    db.set_state(con, sid, State.FAILED)
    con.execute("UPDATE pipeline_jobs SET status='failed', attempts=8, last_error='telegram: 502'"
                " WHERE session_id=? AND stage='deliver'", (sid,))
    con.commit()
    followup_edit.apply(con, sid, EDITED, resend=False)
    deliver = _jobs(con, sid)["deliver"]
    assert (deliver["status"], deliver["attempts"], deliver["last_error"]) == ("queued", 0, None)
    assert db.get_session(con, sid)["state"] == State.SUMMARIZED


def test_failed_resummarize_marks_summarize_done(con):
    sid = _session(con, state=State.SUMMARIZING)
    db.set_state(con, sid, State.FAILED)
    con.execute("UPDATE pipeline_jobs SET status='failed', last_error='529'"
                " WHERE session_id=? AND stage='summarize'", (sid,))
    con.commit()
    followup_edit.apply(con, sid, EDITED, resend=False)
    assert _jobs(con, sid)["summarize"]["status"] == "done"


def test_invalid_followup_is_bad_params_and_files_untouched(con):
    sid = _session(con)
    before = (session_dir(sid) / "followup.json").read_text(encoding="utf-8")
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, {"context": "x"}, resend=False)
    assert exc.value.code == "bad_params"
    assert (session_dir(sid) / "followup.json").read_text(encoding="utf-8") == before
    assert db.get_session(con, sid)["state"] == State.DELIVERED


@pytest.mark.parametrize("state, message", [
    (State.RECORDING, "идёт запись"),
    (State.TRANSCRIBING, "обрабатывается"),
])
def test_busy_states(con, state, message):
    sid = _session(con, state=state, followup=False)
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, EDITED, resend=False)
    assert exc.value.code == "busy" and message in str(exc.value)


def test_second_edit_before_delivery_is_allowed(con):
    """SUMMARIZED (правка уже прошла, deliver ещё в очереди — оркестратор не
    бежал) — вторая правка не должна упираться в busy: SUMMARIZED -> SUMMARIZED
    разрешённый самопереход, redeliver его отработает."""
    sid = _session(con)
    second = dict(GOOD, action_items=[{"task": "Вторая правка", "owner": "Стас", "deadline": None}])
    assert followup_edit.apply(con, sid, EDITED, resend=False) == {
        "session_id": sid, "revision": 2, "resend": False, "telegram": "off"}
    assert db.get_session(con, sid)["state"] == State.SUMMARIZED
    assert followup_edit.apply(con, sid, second, resend=False) == {
        "session_id": sid, "revision": 3, "resend": False, "telegram": "off"}
    sdir = session_dir(sid)
    assert json.loads((sdir / "followup.r1.json").read_text(encoding="utf-8"))["revision"] == 1
    assert json.loads((sdir / "followup.r2.json").read_text(encoding="utf-8"))["revision"] == 2
    saved = json.loads((sdir / "followup.json").read_text(encoding="utf-8"))
    assert saved["revision"] == 3 and saved["action_items"] == second["action_items"]
    deliver = _jobs(con, sid)["deliver"]
    assert (deliver["status"], deliver["attempts"]) == ("queued", 0)


def test_running_job_is_busy(con):
    sid = _session(con)
    con.execute("UPDATE pipeline_jobs SET status='running' WHERE session_id=? AND stage='deliver'",
                (sid,))
    con.commit()
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, EDITED, resend=False)
    assert exc.value.code == "busy"


def test_failed_without_followup(con):
    sid = _session(con, state=State.TRANSCRIBING, followup=False)
    db.set_state(con, sid, State.FAILED)
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, EDITED, resend=False)
    assert exc.value.code == "no_followup" and "пересобери саммари" in str(exc.value)
    (session_dir(sid) / "transcript.txt").unlink()
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, EDITED, resend=False)
    assert "повтори обработку" in str(exc.value)


def test_unknown_or_cancelled_is_not_found(con):
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, "nope", EDITED, resend=False)
    assert exc.value.code == "not_found"
    sid = _session(con, state=State.RECORDING, followup=False)
    db.set_state(con, sid, State.CANCELLED)
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, EDITED, resend=False)
    assert exc.value.code == "not_found"
