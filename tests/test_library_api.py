"""Тесты API «Библиотеки встреч» (экраны 05-06 design_handoff_meetmind).

Daemon-слой под новый UI: engine в followup.json, FTS-индексация заметок,
socket-команды sessions_list / session_detail / search / resummarize /
claude_set, плюс recording_started_at|title в status (таймер в дропдауне).

DaemonApp вызывается напрямую, без сокета (паттерн test_app_owner.py).
"""
import json

import pytest

import config
from daemon import db, stages
from daemon.app import DaemonApp
from daemon.paths import session_dir
from daemon.states import State
from tests.conftest import GOOD_FOLLOWUP as GOOD

SEGMENTS = [
    {"start": 0.0, "end": 4.2, "text": "обсуждаем горение выше порога"},
    {"start": 4.2, "end": 9.0, "text": "решили катить релиз в пятницу"},
]


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    return DaemonApp()


def _mk_session(con, *, title="Синк", state=State.DELIVERED, transcript=True,
                followup=True, engine="vikhr-nemo:12b-instruct-q4"):
    sid = db.create_session(con, source="manual", title=title)
    chain = [State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED,
             State.SUMMARIZING, State.SUMMARIZED, State.DELIVERING, State.DELIVERED]
    for st in chain:
        db.set_state(con, sid, st)
        if st == state:
            break
    sdir = session_dir(sid)
    if transcript:
        (sdir / "transcript.txt").write_text(
            "\n".join(s["text"] for s in SEGMENTS), encoding="utf-8")
        (sdir / "transcript.json").write_text(
            json.dumps(SEGMENTS, ensure_ascii=False), encoding="utf-8")
    if followup:
        (sdir / "followup.json").write_text(
            json.dumps(dict(GOOD, engine=engine), ensure_ascii=False),
            encoding="utf-8")
    return sid


# ──────────────────── stages.summarize: engine + FTS-индексация ──────────────


def _summarized(tmp_path, monkeypatch, *, use_claude=False):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = _mk_session(con, state=State.TRANSCRIBED, followup=False)
    monkeypatch.setattr(stages, "summarize_ollama",
                        lambda transcript, meta: dict(GOOD, degraded=False))
    monkeypatch.setattr(config, "USE_CLAUDE_CODE", use_claude)
    stages.summarize(db.get_session(con, sid))
    return con, sid


def test_summarize_writes_engine_into_followup_json(tmp_path, monkeypatch):
    con, sid = _summarized(tmp_path, monkeypatch)
    followup = json.loads(
        (session_dir(sid) / "followup.json").read_text(encoding="utf-8"))
    assert followup["engine"] == config.OLLAMA_MODEL


def test_summarize_indexes_session_into_fts(tmp_path, monkeypatch):
    con, sid = _summarized(tmp_path, monkeypatch)
    row = con.execute(
        "SELECT session_id FROM notes_fts WHERE notes_fts MATCH 'горение'").fetchone()
    assert row is not None and row["session_id"] == sid


def test_summarize_reindex_replaces_fts_row(tmp_path, monkeypatch):
    con, sid = _summarized(tmp_path, monkeypatch)
    # повторный прогон (реген) не должен плодить дубликаты
    stages.summarize(db.get_session(con, sid))
    n = con.execute("SELECT COUNT(*) n FROM notes_fts WHERE session_id=?",
                    (sid,)).fetchone()["n"]
    assert n == 1


# ─────────────────────────────── sessions_list ───────────────────────────────


def test_sessions_list_fields_and_order(app):
    con = app._con
    sid_old = _mk_session(con, title="Старая", engine="claude-code:opus")
    con.execute("INSERT INTO deliveries(session_id, sink, status, delivered_at)"
                " VALUES(?,?,?,?)", (sid_old, "telegram", "delivered", db.now()))
    con.commit()
    sid_new = _mk_session(con, title="Новая", state=State.TRANSCRIBING,
                          followup=False)

    rows = app.sessions_list({})["sessions"]
    assert [r["id"] for r in rows][:2] == [sid_new, sid_old]  # новые сверху

    new, old = rows[0], rows[1]
    assert new["status"] == "processing"
    assert new["state"] == "TRANSCRIBING"
    assert old["status"] == "delivered"
    assert old["title"] == "Старая"
    assert old["action_items"] == len(GOOD["action_items"])
    assert old["engine"] == "claude-code:opus"
    assert old["sinks"] == [{"sink": "telegram", "status": "delivered"}]
    assert "started_at" in old and "duration_min" in old


def test_sessions_list_failed_carries_error(app):
    con = app._con
    sid = _mk_session(con, state=State.TRANSCRIBED, followup=False)
    db.enqueue_job(con, sid, "summarize")
    con.execute("UPDATE pipeline_jobs SET status='failed', last_error='Ollama down'"
                " WHERE session_id=?", (sid,))
    db.set_state(con, sid, State.FAILED)

    row = next(r for r in app.sessions_list({})["sessions"] if r["id"] == sid)
    assert row["status"] == "error"
    assert "Ollama down" in row["error"]


def test_sessions_list_excludes_cancelled(app):
    con = app._con
    sid = db.create_session(con, source="manual", title="Отменённая")
    db.set_state(con, sid, State.CANCELLED)
    assert all(r["id"] != sid for r in app.sessions_list({})["sessions"])


# ─────────────────────────────── session_detail ──────────────────────────────


def test_session_detail_followup_transcript_pipeline(app):
    con = app._con
    sid = _mk_session(con, title="Детали")
    for stage in ("transcribe", "summarize", "deliver"):
        db.enqueue_job(con, sid, stage)
        con.execute("UPDATE pipeline_jobs SET status='done' WHERE session_id=?"
                    " AND stage=?", (sid, stage))
    con.execute("INSERT INTO deliveries(session_id, sink, status, delivered_at)"
                " VALUES(?,?,?,?)", (sid, "telegram", "delivered", db.now()))
    con.commit()

    d = app.session_detail({"session_id": sid})
    assert d["session"]["id"] == sid
    assert d["followup"]["decisions"] == GOOD["decisions"]
    assert d["transcript"][1]["text"] == SEGMENTS[1]["text"]
    steps = {p["step"]: p for p in d["pipeline"]}
    assert set(steps) == {"recorded", "transcribed", "summarized", "delivered"}
    assert all(p["ok"] for p in d["pipeline"])
    assert d["sinks"][0]["sink"] == "telegram"


def test_session_detail_pipeline_error_step(app):
    con = app._con
    sid = _mk_session(con, state=State.TRANSCRIBED, followup=False)
    db.enqueue_job(con, sid, "summarize")
    con.execute("UPDATE pipeline_jobs SET status='failed', last_error='boom'"
                " WHERE session_id=? AND stage='summarize'", (sid,))
    db.set_state(con, sid, State.FAILED)

    steps = {p["step"]: p for p in app.session_detail({"session_id": sid})["pipeline"]}
    assert steps["summarized"]["ok"] is False
    assert "boom" in steps["summarized"]["error"]
    assert steps["transcribed"]["ok"] is True


# ─────────────────────────────────── search ──────────────────────────────────


def test_search_returns_snippets_with_markers(app):
    con = app._con
    sid = _mk_session(con, title="Поисковая")
    db.index_note(con, sid, title="Поисковая",
                  transcript="обсуждали пид-регулятор и горение",
                  summary="решение: настроить пид-регулятор")

    hits = app.search({"query": "пид"})["results"]
    assert len(hits) == 2  # транскрипт + саммари
    sources = {h["source"] for h in hits}
    assert sources == {"transcript", "summary"}
    hit = hits[0]
    assert hit["session_id"] == sid
    assert "[[" in hit["snippet"] and "]]" in hit["snippet"]


def test_search_empty_query_returns_nothing(app):
    assert app.search({"query": "  "})["results"] == []


# ─────────────────────────────── resummarize ─────────────────────────────────


def test_resummarize_delivered_session_requeues_and_sets_engine(app):
    con = app._con
    sid = _mk_session(con)
    for stage in ("transcribe", "summarize", "deliver"):
        db.enqueue_job(con, sid, stage)
        con.execute("UPDATE pipeline_jobs SET status='done' WHERE session_id=?"
                    " AND stage=?", (sid, stage))
    con.execute("INSERT INTO deliveries(session_id, sink, status, delivered_at)"
                " VALUES(?,?,?,?)", (sid, "telegram", "delivered", db.now()))
    con.commit()

    app.resummarize({"session_id": sid, "engine": "claude"})

    assert db.get_session(con, sid)["state"] == State.TRANSCRIBED
    jobs = {r["stage"]: r["status"] for r in con.execute(
        "SELECT stage, status FROM pipeline_jobs WHERE session_id=?", (sid,))}
    assert jobs["summarize"] == "queued"
    assert jobs["deliver"] == "queued"
    assert jobs["transcribe"] == "done"  # транскрипцию не перегоняем
    deliv = con.execute("SELECT status, delivered_at FROM deliveries"
                        " WHERE session_id=?", (sid,)).fetchone()
    assert deliv["status"] == "pending" and deliv["delivered_at"] is None
    assert (session_dir(sid) / "engine_override").read_text() == "claude"


def test_summarize_respects_engine_override_local(tmp_path, monkeypatch):
    """override=local: бустер не зовётся даже при USE_CLAUDE_CODE=True;
    файл override съедается (одноразовый)."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = _mk_session(con, state=State.TRANSCRIBED, followup=False)
    (session_dir(sid) / "engine_override").write_text("local")
    monkeypatch.setattr(stages, "summarize_ollama",
                        lambda transcript, meta: dict(GOOD, degraded=False))
    monkeypatch.setattr(config, "USE_CLAUDE_CODE", True)
    from daemon import claude_booster
    monkeypatch.setattr(claude_booster, "boost",
                        lambda *a, **k: pytest.fail("boost не должен вызываться"))

    stages.summarize(db.get_session(con, sid))

    followup = json.loads(
        (session_dir(sid) / "followup.json").read_text(encoding="utf-8"))
    assert followup["engine"] == config.OLLAMA_MODEL
    assert not (session_dir(sid) / "engine_override").exists()


# ─────────────────────────────── claude_set ──────────────────────────────────


def test_claude_set_flips_runtime_flag_and_persists(app, tmp_path, monkeypatch):
    from daemon.paths import app_dir
    (app_dir() / "config.env").write_text("TELEGRAM_BOT_TOKEN=tok\n")
    monkeypatch.setattr(config, "USE_CLAUDE_CODE", True)

    out = app.claude_set({"enabled": False})

    assert out["enabled"] is False
    assert config.USE_CLAUDE_CODE is False
    content = (app_dir() / "config.env").read_text()
    assert "USE_CLAUDE_CODE=false" in content
    assert "TELEGRAM_BOT_TOKEN=tok" in content  # чужие ключи не потеряны


# ───────────────────────── status: метаданные записи ─────────────────────────


def test_status_reports_recording_title_and_started_at(app):
    app.start_recording({"audio_path_owner": "app", "title": "Планёрка"})
    st = app.status({})
    assert st["recording_title"] == "Планёрка"
    assert st["recording_started_at"] is not None
