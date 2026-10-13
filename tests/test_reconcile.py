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
        # эмулируем ffmpeg: создаёт *_fixed.wav рядом с исходником (настоящий
        # WAV на 2 с — пустые/короткие записи reconcile теперь отменяет, P6)
        fixed = wav.with_name(wav.stem + "_fixed" + wav.suffix)
        sf.write(str(fixed), np.full((32000, 1), 0.1), 16000)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(reconcile.subprocess, "run", fake_run)

    notified = []
    actions = reconcile.reconcile(con, notifier=lambda sid_, msg: notified.append((sid_, msg)))

    assert calls and calls[0][0] == "ffmpeg" and "-ignore_length" in calls[0]
    assert sf.info(str(wav)).frames == 32000  # заменён починенным файлом

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


# ───── аудит T3: живую запись Swift (owner=app) reconcile не трогает ─────────


def _make_app_recording(con, *, age_sec: float):
    import os
    import time
    sid = db.create_session(con, source="manual")
    wav = session_dir(sid) / "audio.wav"
    sf.write(str(wav), np.full((32000, 2), 0.1), 16000)
    db.set_state(con, sid, State.RECORDING, audio_path=str(wav), audio_owner="app")
    t = time.time() - age_sec
    os.utime(wav, (t, t))
    return sid, wav


def test_live_app_recording_is_adopted_not_touched(tmp_path, monkeypatch):
    """Демон рестартовал посреди записи, Swift продолжает писать WAV (свежий
    mtime). reconcile не должен его переписывать ffmpeg-ом и закрывать сессию —
    иначе всё, что Swift допишет после рестарта, уйдёт в удалённый inode."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid, wav = _make_app_recording(con, age_sec=5)
    before = wav.read_bytes()

    def boom(*a, **kw):
        raise AssertionError("ffmpeg не должен вызываться для живой записи")

    monkeypatch.setattr(reconcile.subprocess, "run", boom)

    actions = reconcile.reconcile(con)

    assert {"session_id": sid, "action": "adopted_app_recording"} in actions
    assert db.get_session(con, sid)["state"] == State.RECORDING
    assert wav.read_bytes() == before


def test_stale_app_recording_is_recovered_as_before(tmp_path, monkeypatch):
    """Swift давно перестал писать (приложение упало) — обычное восстановление."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid, wav = _make_app_recording(con, age_sec=600)
    monkeypatch.setattr(reconcile, "_fix_wav_header", lambda p: None)

    actions = reconcile.reconcile(con)

    assert db.get_session(con, sid)["state"] == State.RECORDED
    assert any(a["action"] == "recovered_recording" for a in actions)


def test_only_newest_live_app_recording_is_adopted(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    old_sid, _ = _make_app_recording(con, age_sec=10)
    con.execute("UPDATE sessions SET started_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                (old_sid,))
    con.commit()
    new_sid, _ = _make_app_recording(con, age_sec=3)
    monkeypatch.setattr(reconcile, "_fix_wav_header", lambda p: None)

    actions = reconcile.reconcile(con)

    assert {"session_id": new_sid, "action": "adopted_app_recording"} in actions
    assert db.get_session(con, old_sid)["state"] == State.RECORDED



def test_recovered_recording_without_frames_is_cancelled(tmp_path, monkeypatch):
    """Продукт P6: после краша пустой WAV (только заголовок AVAudioFile, 4096
    байт) больше не уходит в транскрипцию — сессия отменяется."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    wav = session_dir(sid) / "audio.wav"
    sf.write(str(wav), np.zeros((0, 2)), 48000)
    wav.write_bytes(wav.read_bytes() + b"\x00" * 4000)  # как JUNK-чанк AVAudioFile
    db.set_state(con, sid, State.RECORDING, audio_path=str(wav))
    monkeypatch.setattr(reconcile, "_fix_wav_header", lambda p: None)

    reconcile.reconcile(con)

    assert db.get_session(con, sid)["state"] == State.CANCELLED
