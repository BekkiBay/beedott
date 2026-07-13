"""Task 2.4: retention sweep. Инвариант N1 (SPEC_v3 S3) — файлы удаляются только
в DELIVERED (тут, по возрасту) или CANCELLED (немедленно, отдельный флоу).
sweep() трогает только audio.wav — транскрипт/заметки в session_dir остаются."""
from datetime import datetime, timedelta, timezone

from daemon import db, retention
from daemon.paths import session_dir
from daemon.states import State


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _write_session_files(sid: str) -> tuple:
    sdir = session_dir(sid)
    wav = sdir / "audio.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 40)
    transcript = sdir / "transcript.txt"
    transcript.write_text("привет, это транскрипт")
    return wav, transcript


def _advance_to_delivered(con, sid: str, *, wav, ended_at: str) -> None:
    db.set_state(con, sid, State.RECORDING, audio_path=str(wav))
    db.set_state(con, sid, State.RECORDED, ended_at=ended_at)
    db.set_state(con, sid, State.TRANSCRIBING)
    db.set_state(con, sid, State.TRANSCRIBED)
    db.set_state(con, sid, State.SUMMARIZING)
    db.set_state(con, sid, State.SUMMARIZED)
    db.set_state(con, sid, State.DELIVERING)
    db.set_state(con, sid, State.DELIVERED)


def test_sweep_deletes_wav_for_old_delivered_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    wav, transcript = _write_session_files(sid)
    _advance_to_delivered(con, sid, wav=wav, ended_at=_iso_days_ago(20))

    swept = retention.sweep(con, keep_days=14)

    assert swept == [sid]
    assert not wav.exists()
    assert transcript.exists() and transcript.read_text() == "привет, это транскрипт"
    assert db.get_session(con, sid)["audio_path"] is None
    assert db.get_session(con, sid)["state"] == State.DELIVERED


def test_sweep_keeps_recent_delivered_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    wav, _transcript = _write_session_files(sid)
    _advance_to_delivered(con, sid, wav=wav, ended_at=_iso_days_ago(1))

    swept = retention.sweep(con, keep_days=14)

    assert swept == []
    assert wav.exists()
    assert db.get_session(con, sid)["audio_path"] == str(wav)


def test_sweep_ignores_failed_session_any_age(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    wav, _transcript = _write_session_files(sid)
    db.set_state(con, sid, State.RECORDING, audio_path=str(wav))
    db.set_state(con, sid, State.RECORDED, ended_at=_iso_days_ago(999))
    # эмулируем терминальный FAILED напрямую (как это делает orchestrator после
    # исчерпания ретраев, см. tests/test_db.py::test_reprocess_from_failed_...)
    con.execute("UPDATE sessions SET state=? WHERE id=?", (State.FAILED, sid))
    con.commit()

    swept = retention.sweep(con, keep_days=14)

    assert swept == []
    assert wav.exists()
    assert db.get_session(con, sid)["audio_path"] == str(wav)


def test_sweep_ignores_recorded_session_any_age(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    wav, _transcript = _write_session_files(sid)
    db.set_state(con, sid, State.RECORDING, audio_path=str(wav))
    db.set_state(con, sid, State.RECORDED, ended_at=_iso_days_ago(999))

    swept = retention.sweep(con, keep_days=14)

    assert swept == []
    assert wav.exists()
    assert db.get_session(con, sid)["audio_path"] == str(wav)


def test_sweep_missing_wav_still_clears_audio_path(tmp_path, monkeypatch):
    """audio_path указывает на уже отсутствующий файл (например, ручная чистка
    диска) — sweep не должен падать, просто обнуляет audio_path."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    wav, _transcript = _write_session_files(sid)
    _advance_to_delivered(con, sid, wav=wav, ended_at=_iso_days_ago(20))
    wav.unlink()  # файла уже нет на диске

    swept = retention.sweep(con, keep_days=14)

    assert swept == [sid]
    assert db.get_session(con, sid)["audio_path"] is None
