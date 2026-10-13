"""Тесты API «Библиотеки встреч» (экраны 05-06 design_handoff_meetmind).

Daemon-слой под новый UI: engine в followup.json, FTS-индексация заметок,
socket-команды sessions_list / session_detail / search / resummarize,
плюс recording_started_at|title в status (таймер в дропдауне).

DaemonApp вызывается напрямую, без сокета (паттерн test_app_owner.py).
"""
import json

import pytest

import config
from daemon import claude_cli, db, stages
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


def _summarized(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = _mk_session(con, state=State.TRANSCRIBED, followup=False)
    monkeypatch.setattr(config, "SUMMARY_BACKEND", "claude_cli")
    monkeypatch.setattr(config, "CLAUDE_CODE_MODEL", "opus")
    monkeypatch.setattr(claude_cli, "summarize", lambda transcript, meta: dict(GOOD))
    stages.summarize(db.get_session(con, sid))
    return con, sid


def test_summarize_writes_engine_into_followup_json(tmp_path, monkeypatch):
    con, sid = _summarized(tmp_path, monkeypatch)
    followup = json.loads(
        (session_dir(sid) / "followup.json").read_text(encoding="utf-8"))
    assert followup["engine"] == "claude-code:opus"


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


def test_resummarize_delivered_session_requeues(app):
    con = app._con
    sid = _mk_session(con)
    for stage in ("transcribe", "summarize", "deliver"):
        db.enqueue_job(con, sid, stage)
        con.execute("UPDATE pipeline_jobs SET status='done' WHERE session_id=?"
                    " AND stage=?", (sid, stage))
    con.execute("INSERT INTO deliveries(session_id, sink, status, delivered_at)"
                " VALUES(?,?,?,?)", (sid, "telegram", "delivered", db.now()))
    con.commit()

    app.resummarize({"session_id": sid})

    assert db.get_session(con, sid)["state"] == State.TRANSCRIBED
    jobs = {r["stage"]: r["status"] for r in con.execute(
        "SELECT stage, status FROM pipeline_jobs WHERE session_id=?", (sid,))}
    assert jobs["summarize"] == "queued"
    assert jobs["deliver"] == "queued"
    assert jobs["transcribe"] == "done"  # транскрипцию не перегоняем
    deliv = con.execute("SELECT status, delivered_at FROM deliveries"
                        " WHERE session_id=?", (sid,)).fetchone()
    assert deliv["status"] == "pending" and deliv["delivered_at"] is None


def test_resummarize_forgets_telegram_progress(app):
    """Пересборка сбрасывает доставки — значит, и прогресс Telegram-отправки:
    иначе при побайтно том же тексте sink решит, что всё уже отправлено, и
    сообщение не придёт."""
    sid = _mk_session(app._con)
    progress = session_dir(sid) / "telegram_progress.json"
    progress.write_text('{"hash": "x", "sent": ["1"]}')

    app.resummarize({"session_id": sid})

    assert not progress.exists()


# ───────────────────────── status: метаданные записи ─────────────────────────


def test_status_reports_recording_title_and_started_at(app):
    app.start_recording({"audio_path_owner": "app", "title": "Планёрка"})
    st = app.status({})
    assert st["recording_title"] == "Планёрка"
    assert st["recording_started_at"] is not None


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


def test_ensure_tool_path_puts_runtime_bin_first(monkeypatch, tmp_path):
    """Встроенный режим (чистый Mac): ffmpeg — симлинк в app_dir()/runtime/bin
    (daemon/setup.py link-ffmpeg). Он должен идти раньше Homebrew."""
    from daemon.app import _ensure_tool_path
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _ensure_tool_path()
    import os
    parts = os.environ["PATH"].split(":")
    assert parts[0] == str(tmp_path / "runtime" / "bin")
    _ensure_tool_path()
    assert os.environ["PATH"].split(":").count(str(tmp_path / "runtime" / "bin")) == 1


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
    не ValueError, а requeue (лог 2026-07-15: illegal
    transition SUMMARIZING -> TRANSCRIBED)."""
    con = app._con
    sid = _mk_session(con, state=State.SUMMARIZING, followup=False)
    db.enqueue_job(con, sid, "summarize")
    con.execute(
        "UPDATE pipeline_jobs SET status='failed_retry', attempts=3,"
        " next_retry_at='2099-01-01T00:00:00+00:00' WHERE session_id=?", (sid,))
    con.commit()

    app.resummarize({"session_id": sid})  # не должен бросить

    job = con.execute("SELECT status, next_retry_at FROM pipeline_jobs"
                      " WHERE session_id=? AND stage='summarize'", (sid,)).fetchone()
    assert job["status"] == "queued" and job["next_retry_at"] is None


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


def test_transcribe_stage_without_speech_is_permanent_error(tmp_path, monkeypatch):
    from daemon.orchestrator import PermanentStageError
    from daemon.transcriber import Transcriber
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = _mk_session(con)
    monkeypatch.setattr(Transcriber, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(Transcriber, "transcribe", lambda self, p: [])
    with pytest.raises(PermanentStageError):
        stages.transcribe(db.get_session(con, sid))


# ───── продукт P10: «Хранить аудио» и сжатие в FLAC после транскрипции ───────


def test_set_keep_audio_persists_flag(app):
    sid = _mk_session(app._con)
    assert app.set_keep_audio({"session_id": sid, "keep": True}) == {
        "session_id": sid, "keep_audio": True}
    assert db.get_session(app._con, sid)["keep_audio"] == 1
    row = next(r for r in app.sessions_list({})["sessions"] if r["id"] == sid)
    assert row["keep_audio"] is True
    assert row["source"] == "manual"
    assert "has_audio" in row


def _stage_ready_for_transcribe(tmp_path, monkeypatch, seconds=2):
    import numpy as np
    import soundfile as sf
    from daemon.transcriber import Transcriber
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    wav = session_dir(sid) / "audio.wav"
    rng = np.random.default_rng(1)
    sf.write(str(wav), (rng.standard_normal((16000 * seconds, 2)) * 0.1), 16000,
             subtype="PCM_16")
    db.set_state(con, sid, State.RECORDING, audio_path=str(wav))
    db.set_state(con, sid, State.RECORDED)
    monkeypatch.setattr(Transcriber, "__init__", lambda self, **kw: None)
    monkeypatch.setattr(Transcriber, "transcribe",
                        lambda self, p: [{"start": 0.0, "end": 1.0, "text": "привет"}])
    return con, sid, wav


def test_transcribe_compresses_audio_to_flac(tmp_path, monkeypatch):
    import soundfile as sf
    con, sid, wav = _stage_ready_for_transcribe(tmp_path, monkeypatch)
    frames = sf.info(str(wav)).frames

    stages.transcribe(db.get_session(con, sid))

    flac = wav.with_suffix(".flac")
    assert flac.exists() and not wav.exists()
    assert db.get_session(con, sid)["audio_path"] == str(flac)
    info = sf.info(str(flac))
    assert (info.frames, info.channels) == (frames, 2)


def test_transcribe_keeps_wav_when_compression_fails(tmp_path, monkeypatch):
    con, sid, wav = _stage_ready_for_transcribe(tmp_path, monkeypatch)

    def broken_ffmpeg(*a, **kw):
        raise OSError("ffmpeg сломался")

    monkeypatch.setattr(stages.subprocess, "run", broken_ffmpeg)
    stages.transcribe(db.get_session(con, sid))  # стадия не падает

    assert wav.exists()
    assert db.get_session(con, sid)["audio_path"] == str(wav)


def test_maintenance_tick_runs_retention(app, monkeypatch):
    """Продукт P10: retention раньше запускался только при старте демона —
    демон, работающий неделями, аудио не чистил."""
    calls = []
    monkeypatch.setattr("daemon.app.retention.sweep", lambda con: calls.append(con) or ["x"])
    app.maintenance_tick()
    assert len(calls) == 1


# ───── продукт P12: пересборка саммари помечена и отвечает на старое сообщение ─


def test_resummarized_followup_is_marked_as_update(tmp_path, monkeypatch):
    con, sid = _summarized(tmp_path, monkeypatch)
    first = (session_dir(sid) / "followup.md").read_text(encoding="utf-8")
    assert "Обновлённая версия" not in first

    stages.summarize(db.get_session(con, sid))  # «Пересобрать саммари»

    followup = json.loads((session_dir(sid) / "followup.json").read_text(encoding="utf-8"))
    assert followup["revision"] == 2
    md = (session_dir(sid) / "followup.md").read_text(encoding="utf-8")
    assert md.startswith("🔄 Обновлённая версия фоллоу-апа (№2)")


def test_resummarize_remembers_previous_telegram_message(app):
    sid = _mk_session(app._con)
    (session_dir(sid) / "telegram_progress.json").write_text(
        '{"hash": "x", "sent": ["555", "556"]}')

    app.resummarize({"session_id": sid})

    assert (session_dir(sid) / "telegram_reply_to").read_text() == "555"
    assert not (session_dir(sid) / "telegram_progress.json").exists()


# ───── продукт P14: удаление встречи ──────────────────────────────────────────


def test_delete_session_removes_everything_local(app):
    con = app._con
    sid = _mk_session(con)
    db.enqueue_job(con, sid, "transcribe")
    con.execute("INSERT INTO deliveries(session_id, sink, status) VALUES(?,?,?)",
                (sid, "telegram", "delivered"))
    db.index_note(con, sid, title="t", transcript="секретный разговор", summary="s")
    con.commit()
    sdir = session_dir(sid)

    assert app.delete_session({"session_id": sid}) == {"session_id": sid, "deleted": True}

    assert db.get_session(con, sid) is None
    for table in ("pipeline_jobs", "deliveries", "notes_fts"):
        assert con.execute(f"SELECT COUNT(*) n FROM {table} WHERE session_id=?",
                           (sid,)).fetchone()["n"] == 0
    assert not sdir.exists()
    assert app.search({"query": "секретный"})["results"] == []


def test_delete_session_refuses_active_recording(app):
    from daemon.control import ControlError
    sid = app.start_recording({"audio_path_owner": "app"})["session_id"]
    with pytest.raises(ControlError) as exc:
        app.delete_session({"session_id": sid})
    assert exc.value.code == "busy"


def test_delete_session_refuses_while_stage_running(app):
    from daemon.control import ControlError
    con = app._con
    sid = _mk_session(con, state=State.TRANSCRIBING)
    db.enqueue_job(con, sid, "transcribe")
    con.execute("UPDATE pipeline_jobs SET status='running' WHERE session_id=?", (sid,))
    con.commit()
    with pytest.raises(ControlError) as exc:
        app.delete_session({"session_id": sid})
    assert exc.value.code == "busy"


def test_delete_session_unknown_id(app):
    from daemon.control import ControlError
    with pytest.raises(ControlError) as exc:
        app.delete_session({"session_id": "nope"})
    assert exc.value.code == "not_found"


# ───── продукт P13: импорт готового аудиофайла ────────────────────────────────


@pytest.fixture()
def sync_app(app):
    """import_audio конвертирует в фоне — в тестах выполняем сразу."""
    app._run_async = lambda fn, *args: fn(*args)
    return app


def test_import_audio_creates_session_and_queues_transcription(sync_app, tmp_path):
    import os
    import numpy as np
    import soundfile as sf
    src = tmp_path / "Созвон с клиентом.wav"
    sf.write(str(src), np.full((44100 * 3, 2), 0.1), 44100)
    os.utime(src, (1_780_000_000, 1_780_000_000))

    sid = sync_app.import_audio({"path": str(src)})["session_id"]

    row = db.get_session(sync_app._con, sid)
    assert row["state"] == State.RECORDED
    assert row["source"] == "import"
    assert row["title"] == "Созвон с клиентом"
    assert row["audio_path"].endswith("audio.flac")
    info = sf.info(row["audio_path"])
    assert (info.channels, info.samplerate) == (1, 16000)  # моно: без ложного деления на спикеров
    assert abs(info.frames / info.samplerate - 3.0) < 0.05
    from datetime import datetime
    started = datetime.fromisoformat(row["started_at"]).timestamp()
    assert abs(started - (1_780_000_000 - 3)) < 1  # начало = конец файла минус длительность
    job = sync_app._con.execute(
        "SELECT status FROM pipeline_jobs WHERE session_id=? AND stage='transcribe'",
        (sid,)).fetchone()
    assert job["status"] == "queued"
    assert src.exists()  # исходник не трогаем


def test_import_audio_missing_file_is_bad_params(sync_app, tmp_path):
    from daemon.control import ControlError
    with pytest.raises(ControlError) as exc:
        sync_app.import_audio({"path": str(tmp_path / "нет.m4a")})
    assert exc.value.code == "bad_params"


def test_import_audio_not_audio_cancels_with_notice(sync_app, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr("daemon.app.notify", lambda t, m, *a, **k: sent.append(m))
    src = tmp_path / "заметки.txt"
    src.write_text("это не звук")

    sid = sync_app.import_audio({"path": str(src)})["session_id"]

    assert db.get_session(sync_app._con, sid)["state"] == State.CANCELLED
    assert any("импорт" in m.lower() for m in sent)


# ───── продукт P8: участники из календаря и переименование ────────────────────


def test_start_recording_stores_attendees(app):
    sid = app.start_recording({"audio_path_owner": "app", "title": "Синк",
                               "attendees": ["Аня", "Борис", 5, ""]})["session_id"]
    assert json.loads(db.get_session(app._con, sid)["attendees"]) == ["Аня", "Борис"]


def test_rename_session_updates_title_and_search(app):
    con = app._con
    sid = _mk_session(con, title="Встреча 10:10")
    db.index_note(con, sid, title="Встреча 10:10", transcript="текст", summary="s")

    assert app.rename_session({"session_id": sid, "title": "  Ретро спринта  "}) == {
        "session_id": sid, "title": "Ретро спринта"}

    assert db.get_session(con, sid)["title"] == "Ретро спринта"
    assert con.execute("SELECT title FROM notes_fts WHERE session_id=?",
                       (sid,)).fetchone()["title"] == "Ретро спринта"


def test_rename_session_rejects_empty_title(app):
    from daemon.control import ControlError
    sid = _mk_session(app._con)
    with pytest.raises(ControlError) as exc:
        app.rename_session({"session_id": sid, "title": "   "})
    assert exc.value.code == "bad_params"


def test_summarize_passes_attendees_to_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = _mk_session(con, state=State.TRANSCRIBED, followup=False)
    con.execute("UPDATE sessions SET attendees=? WHERE id=?", ('["Аня", "Борис"]', sid))
    con.commit()
    seen = {}

    def fake_claude(transcript, meta):
        seen.update(meta)
        return dict(GOOD)

    monkeypatch.setattr(config, "SUMMARY_BACKEND", "claude_cli")
    monkeypatch.setattr(claude_cli, "summarize", fake_claude)
    stages.summarize(db.get_session(con, sid))
    assert seen["attendees"] == ["Аня", "Борис"]


def test_prompt_mentions_attendees():
    from daemon.summarizer import build_prompt
    meta = {"title": "Синк", "duration_min": 30, "attendees": ["Аня", "Борис"]}
    assert "Аня, Борис" in build_prompt("текст", meta)
    assert "Участники" not in build_prompt("текст", {"title": "Синк"})
