"""Тесты API «Библиотеки встреч» (экраны 05-06 design_handoff_meetmind).

Daemon-слой под новый UI: engine в followup.json, FTS-индексация заметок,
socket-команды sessions_list / session_detail / search / resummarize /
claude_set, плюс recording_started_at|title в status (таймер в дропдауне).

DaemonApp вызывается напрямую, без сокета (паттерн test_app_owner.py).
"""
import json

import pytest

import config
from daemon import db, memguard, stages
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
    # RAM-гвард внутри stages.summarize не должен зависеть от машины
    monkeypatch.setattr(memguard, "available", lambda: 64 * 2**30)
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
    monkeypatch.setattr(memguard, "available", lambda: 64 * 2**30)
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


def test_status_reports_claude_enabled(app, monkeypatch):
    monkeypatch.setattr(config, "USE_CLAUDE_CODE", True)
    assert app.status({})["claude_enabled"] is True
    monkeypatch.setattr(config, "USE_CLAUDE_CODE", False)
    assert app.status({})["claude_enabled"] is False


# ─────────────────────────────── FTS backfill ────────────────────────────────


def test_backfill_fts_indexes_old_sessions(tmp_path, monkeypatch):
    """Сессии, обработанные до появления FTS-индексации (файлы на диске есть,
    строки в notes_fts нет), доиндексируются при старте демона."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = _mk_session(con, title="Старая встреча")
    (session_dir(sid) / "followup.md").write_text("решили про горение", encoding="utf-8")

    n = db.backfill_fts(con)

    assert n == 1
    row = con.execute(
        "SELECT session_id FROM notes_fts WHERE notes_fts MATCH 'горение'").fetchone()
    assert row is not None and row["session_id"] == sid
    assert db.backfill_fts(con) == 0  # повторный вызов — идемпотентен


# ──────────────── PATH для subprocess-зависимостей (ffmpeg) ──────────────────


def test_ensure_tool_path_adds_homebrew(monkeypatch):
    """Демон из .app получает PATH LaunchServices без /opt/homebrew/bin —
    mlx-whisper не находит ffmpeg (FileNotFoundError). Хелпер дополняет PATH."""
    from daemon.app import _ensure_tool_path
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _ensure_tool_path()
    import os
    parts = os.environ["PATH"].split(":")
    assert "/opt/homebrew/bin" in parts
    assert "/usr/local/bin" in parts
    assert parts[-2:] == ["/usr/bin", "/bin"]  # системные пути не потеряны

    _ensure_tool_path()  # идемпотентен — без дублей
    assert os.environ["PATH"].split(":").count("/opt/homebrew/bin") == 1


# ───────────── reprocess во время failed_retry (кнопка «Повторить») ──────────


def test_reprocess_during_failed_retry_requeues_without_crash(app):
    """Сессия ещё TRANSCRIBING (оркестратор в failed_retry-ретраях), юзер жмёт
    «Повторить»: не ValueError (illegal transition), а мгновенный requeue."""
    con = app._con
    sid = _mk_session(con, state=State.TRANSCRIBING, transcript=False,
                      followup=False)
    db.enqueue_job(con, sid, "transcribe")
    con.execute(
        "UPDATE pipeline_jobs SET status='failed_retry', attempts=4,"
        " next_retry_at='2099-01-01T00:00:00+00:00', last_error='ffmpeg'"
        " WHERE session_id=?", (sid,))
    con.commit()

    app.reprocess({"session_id": sid})  # не должен бросить

    job = con.execute("SELECT status, next_retry_at FROM pipeline_jobs"
                      " WHERE session_id=?", (sid,)).fetchone()
    assert job["status"] == "queued"
    assert job["next_retry_at"] is None
    assert db.get_session(con, sid)["state"] == State.TRANSCRIBING  # не тронут


def test_resummarize_during_summarizing_requeues_without_crash(app):
    """«Пересобрать саммари» пока стадия в failed_retry (state=SUMMARIZING):
    не ValueError, а requeue + engine_override (лог 2026-07-15: illegal
    transition SUMMARIZING -> TRANSCRIBED)."""
    con = app._con
    sid = _mk_session(con, state=State.SUMMARIZING, followup=False)
    db.enqueue_job(con, sid, "summarize")
    con.execute(
        "UPDATE pipeline_jobs SET status='failed_retry', attempts=3,"
        " next_retry_at='2099-01-01T00:00:00+00:00' WHERE session_id=?", (sid,))
    con.commit()

    app.resummarize({"session_id": sid, "engine": "claude"})  # не должен бросить

    job = con.execute("SELECT status, next_retry_at FROM pipeline_jobs"
                      " WHERE session_id=? AND stage='summarize'", (sid,)).fetchone()
    assert job["status"] == "queued" and job["next_retry_at"] is None
    assert (session_dir(sid) / "engine_override").read_text() == "claude"


def test_transcribe_stage_writes_speaker_prefixed_lines(tmp_path, monkeypatch):
    """Стерео-сегменты со speaker → transcript.txt в виде «Спикер N: текст»."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = _mk_session(con, state=State.RECORDED, transcript=False, followup=False)
    con.execute("UPDATE sessions SET audio_path=? WHERE id=?",
                (str(session_dir(sid) / "audio.wav"), sid))
    con.commit()

    class FakeTranscriber:
        def transcribe(self, audio_path):
            return [
                {"start": 0.0, "end": 2.0, "text": "привет", "speaker": "Спикер 1"},
                {"start": 2.5, "end": 4.0, "text": "по релизу", "speaker": "Спикер 2"},
            ]

    monkeypatch.setattr(stages, "Transcriber", FakeTranscriber)
    stages.transcribe(db.get_session(con, sid))

    text = (session_dir(sid) / "transcript.txt").read_text(encoding="utf-8")
    assert text == "Спикер 1: привет\nСпикер 2: по релизу"
    segs = json.loads((session_dir(sid) / "transcript.json").read_text("utf-8"))
    assert segs[0]["speaker"] == "Спикер 1"
