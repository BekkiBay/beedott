"""Task 2.6: startup reconciliation. SPEC_v3 S10 "Mac перезагрузился / приложение
крашнулось посреди RECORDING" — ни одна сессия не должна зависнуть навсегда."""
import struct
import subprocess

import numpy as np
import soundfile as sf

from daemon import db, reconcile
from daemon.paths import session_dir
from daemon.states import State


def _make_recording_session(con, *, with_audio: bool, audio_bytes: bytes | None = None) -> tuple:
    sid = db.create_session(con, source="manual")
    sdir = session_dir(sid)
    wav = sdir / "audio.wav"
    if with_audio:
        wav.write_bytes(audio_bytes if audio_bytes is not None else b"RIFF" + b"\x00" * 100)
    db.set_state(con, sid, State.RECORDING, audio_path=str(wav))
    return sid, wav


def test_recovers_recording_session_with_audio(tmp_path, monkeypatch):
    """WAV непустой -> RECORDED + gap_note + ended_at из mtime + transcribe в очереди.
    ffmpeg замокан — проверяем логику реконсиляции, не сам ffmpeg (для этого есть
    отдельный интеграционный тест ниже)."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid, wav = _make_recording_session(con, with_audio=True, audio_bytes=b"RIFF" + b"\x00" * 200)

    calls = []

    def fake_run(cmd, check, capture_output):
        calls.append(cmd)
        # эмулируем ffmpeg: создаёт *_fixed.wav рядом с исходником
        fixed = wav.with_name(wav.stem + "_fixed" + wav.suffix)
        fixed.write_bytes(b"RIFF-FIXED" + b"\x00" * 200)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(reconcile.subprocess, "run", fake_run)

    notified = []
    actions = reconcile.reconcile(con, notifier=lambda sid_, msg: notified.append((sid_, msg)))

    assert calls and calls[0][0] == "ffmpeg" and "-ignore_length" in calls[0]
    assert wav.read_bytes().startswith(b"RIFF-FIXED")  # заменён починенным файлом

    row = db.get_session(con, sid)
    assert row["state"] == State.RECORDED
    assert row["gap_note"] == "[запись прервана: демон упал]"
    assert row["ended_at"] is not None

    job = con.execute(
        "SELECT * FROM pipeline_jobs WHERE session_id=? AND stage='transcribe'", (sid,)
    ).fetchone()
    assert job["stage"] == "transcribe" and job["status"] == "queued"

    assert {"session_id": sid, "action": "recovered_recording", "audio_path": str(wav)} in actions
    assert notified == [(sid, "запись восстановлена после сбоя демона, отправлена на обработку")]


def test_cancels_recording_session_without_audio_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid, wav = _make_recording_session(con, with_audio=False)
    assert not wav.exists()

    notified = []
    actions = reconcile.reconcile(con, notifier=lambda sid_, msg: notified.append((sid_, msg)))

    assert db.get_session(con, sid)["state"] == State.CANCELLED
    assert not session_dir(sid).exists() or list(session_dir(sid).iterdir()) == []
    assert {"session_id": sid, "action": "cancelled_empty_recording"} in actions
    assert notified == [(sid, "запись отменена: аудио не найдено после сбоя демона")]


def test_cancels_recording_session_with_empty_audio_file(tmp_path, monkeypatch):
    """Файл существует, но <= 44 байт (голый RIFF-заголовок, ни одного сэмпла) —
    трактуется как пустая запись, не как "есть что транскрибировать"."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid, wav = _make_recording_session(con, with_audio=True, audio_bytes=b"R" * 44)
    assert wav.stat().st_size == 44

    actions = reconcile.reconcile(con)

    assert db.get_session(con, sid)["state"] == State.CANCELLED
    assert {"session_id": sid, "action": "cancelled_empty_recording"} in actions


def test_requeues_orphaned_running_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    db.enqueue_job(con, sid, "transcribe")
    con.execute(
        "UPDATE pipeline_jobs SET status='running', next_retry_at=? WHERE session_id=?",
        ("2026-01-01T00:00:00+00:00", sid),
    )
    con.commit()

    actions = reconcile.reconcile(con)

    job = con.execute(
        "SELECT * FROM pipeline_jobs WHERE session_id=? AND stage='transcribe'", (sid,)
    ).fetchone()
    assert job["status"] == "queued"
    assert job["next_retry_at"] is None
    assert {"session_id": sid, "stage": "transcribe", "action": "requeued_job"} in actions


def test_cancels_detected_sessions_without_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    assert db.get_session(con, sid)["state"] == State.DETECTED

    actions = reconcile.reconcile(con)

    assert db.get_session(con, sid)["state"] == State.CANCELLED
    assert {"session_id": sid, "action": "cancelled_detected"} in actions


def test_reconcile_is_a_noop_on_clean_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    assert reconcile.reconcile(con) == []


def test_fixes_unclosed_wav_header_with_real_ffmpeg(tmp_path, monkeypatch):
    """Интеграционный тест: настоящий ffmpeg. Сгенерировать WAV через soundfile,
    вручную обнулить RIFF/data chunk size (как если бы Recorder не успел
    финализировать заголовок при kill -9 демона — soundfile пишет 0 в размер при
    открытии файла и патчит его только на close()), убедиться что soundfile ДО
    починки не может прочитать сэмплы, прогнать reconcile() (реальный
    subprocess.run, ffmpeg не мокан), убедиться что после починки soundfile читает
    все сэмплы обратно."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    wav = session_dir(sid) / "audio.wav"

    samplerate = 16000
    n_frames = samplerate * 3  # 3 секунды стерео
    samples = np.random.randint(-1000, 1000, size=(n_frames, 2)).astype("int16")
    sf.write(str(wav), samples, samplerate, subtype="PCM_16")

    # "Обрезаем" незакрытый WAV: обнуляем RIFF chunk size (байты 4:8) и data chunk
    # size (байты 40:44) — ровно то, что происходит, если процесс умер до того как
    # soundfile успел допатчить заголовок при закрытии файла.
    raw = bytearray(wav.read_bytes())
    struct.pack_into("<I", raw, 4, 0)
    struct.pack_into("<I", raw, 40, 0)
    wav.write_bytes(bytes(raw))

    # до починки: soundfile считает, что во "объявленном" файле 0 сэмплов
    broken, _sr = sf.read(str(wav), dtype="int16")
    assert broken.shape[0] == 0

    db.set_state(con, sid, State.RECORDING, audio_path=str(wav))

    actions = reconcile.reconcile(con)

    assert db.get_session(con, sid)["state"] == State.RECORDED
    assert {"session_id": sid, "action": "recovered_recording", "audio_path": str(wav)} in actions

    fixed_data, sr = sf.read(str(wav), dtype="int16")
    assert sr == samplerate
    assert fixed_data.shape[0] == n_frames
    np.testing.assert_array_equal(fixed_data, samples)
