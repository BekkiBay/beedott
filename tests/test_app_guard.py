"""Аудит T7: предохранители записи, которую пишет Swift (owner=app) —
лимит длительности, диск, размер WAV, брошенная запись, тишина."""
import os
import struct
import time

import numpy as np
import soundfile as sf

from daemon.app_guard import AppRecordingGuard, wav_tail_rms


def _wav(path, samples, rate=48000):
    sf.write(str(path), samples, rate, subtype="PCM_16")
    return path


def test_tail_rms_is_zero_for_digital_silence(tmp_path):
    p = _wav(tmp_path / "a.wav", np.zeros((48000 * 2, 2), dtype="int16"))
    assert wav_tail_rms(p, 1.0) == 0.0


def test_tail_rms_is_positive_for_tone(tmp_path):
    t = np.arange(48000) / 48000
    tone = (np.sin(2 * np.pi * 440 * t) * 16000).astype("int16")
    p = _wav(tmp_path / "a.wav", np.stack([tone, tone], axis=1))
    assert wav_tail_rms(p, 0.5) > 0.1


def test_tail_rms_reads_file_with_unfinalized_header(tmp_path):
    t = np.arange(48000) / 48000
    tone = (np.sin(2 * np.pi * 440 * t) * 16000).astype("int16")
    p = _wav(tmp_path / "a.wav", np.stack([tone, tone], axis=1))
    raw = bytearray(p.read_bytes())
    struct.pack_into("<I", raw, 40, 0)  # data size = 0, как у незакрытого AVAudioFile
    p.write_bytes(bytes(raw))
    assert wav_tail_rms(p, 0.5) > 0.1


def test_tail_rms_none_for_garbage(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"not a wav at all")
    assert wav_tail_rms(p, 1.0) is None


def _guard(**kw):
    defaults = dict(max_sec=3600, min_disk_stop_mb=500, silence_min=30,
                    stale_sec=120, max_bytes=10**9,
                    disk_free=lambda p: 10**12, tail_rms=lambda p, s: 0.5)
    defaults.update(kw)
    return AppRecordingGuard(**defaults)


def _fresh_file(tmp_path, size=1000):
    p = tmp_path / "audio.wav"
    p.write_bytes(b"\0" * size)
    return p


def test_guard_quiet_when_everything_is_fine(tmp_path):
    p = _fresh_file(tmp_path)
    assert _guard().check(started_at=time.time() - 60, audio_path=p) == (None, False)


def test_guard_duration_limit(tmp_path):
    p = _fresh_file(tmp_path)
    reason, _ = _guard(max_sec=10).check(started_at=time.time() - 11, audio_path=p)
    assert reason == "duration_limit"


def test_guard_disk_full(tmp_path):
    p = _fresh_file(tmp_path)
    reason, _ = _guard(disk_free=lambda path: 100 * 2**20).check(
        started_at=time.time() - 60, audio_path=p)
    assert reason == "disk_full"


def test_guard_file_size_limit(tmp_path):
    p = _fresh_file(tmp_path, size=2000)
    reason, _ = _guard(max_bytes=1000).check(started_at=time.time() - 60, audio_path=p)
    assert reason == "file_limit"


def test_guard_app_gone_when_file_stops_changing(tmp_path):
    p = _fresh_file(tmp_path)
    old = time.time() - 600
    os.utime(p, (old, old))
    reason, _ = _guard(stale_sec=120).check(started_at=old - 60, audio_path=p)
    assert reason == "app_gone"


def test_guard_app_gone_when_file_never_appeared(tmp_path):
    reason, _ = _guard(stale_sec=120).check(
        started_at=time.time() - 600, audio_path=tmp_path / "missing.wav")
    assert reason == "app_gone"


def test_guard_silence_fires_once_after_threshold(tmp_path):
    p = _fresh_file(tmp_path)
    now = [1000.0]
    g = _guard(silence_min=1, tail_rms=lambda path, s: 0.0, clock=lambda: now[0],
               mtime=lambda path: now[0])
    fired = []
    for _ in range(20):  # 20 проверок по 5 с = 100 с тишины
        now[0] += 5
        fired.append(g.check(started_at=900.0, audio_path=p)[1])
    assert fired.count(True) == 1


def test_guard_gives_grace_after_system_sleep(tmp_path):
    """Mac уснул посреди записи: файл не менялся, пока система спала. Сразу
    после пробуждения это не «приложение умерло» — даём Swift время продолжить
    запись; если файл так и не ожил — тогда app_gone."""
    p = _fresh_file(tmp_path)
    now = [1000.0]
    mtime = [1000.0]
    g = _guard(stale_sec=120, max_sec=10**6, clock=lambda: now[0], mtime=lambda path: mtime[0])
    assert g.check(started_at=990.0, audio_path=p)[0] is None

    now[0] = 1000.0 + 3600  # проснулись через час
    assert g.check(started_at=990.0, audio_path=p)[0] is None

    now[0] += 60  # Swift продолжил писать
    mtime[0] = now[0]
    assert g.check(started_at=990.0, audio_path=p)[0] is None


def test_guard_app_gone_after_grace_if_file_stays_stale(tmp_path):
    p = _fresh_file(tmp_path)
    now = [1000.0]
    g = _guard(stale_sec=120, max_sec=10**6, clock=lambda: now[0], mtime=lambda path: 1000.0)
    g.check(started_at=990.0, audio_path=p)
    now[0] = 1000.0 + 3600
    assert g.check(started_at=990.0, audio_path=p)[0] is None
    reasons = []
    for _ in range(25):  # проверки каждые 5 с, как в DaemonApp._guard_loop
        now[0] += 5
        reasons.append(g.check(started_at=990.0, audio_path=p)[0])
    assert reasons[:23] == [None] * 23  # 115 с окна после сна
    assert reasons[-1] == "app_gone"


def test_guard_silence_does_not_count_sleep_as_silence(tmp_path):
    p = _fresh_file(tmp_path)
    now = [1000.0]
    g = _guard(silence_min=30, max_sec=10**6, tail_rms=lambda path, s: 0.0,
               clock=lambda: now[0], mtime=lambda path: now[0])
    g.check(started_at=990.0, audio_path=p)
    now[0] += 3600  # час сна — не «час тишины»
    assert g.check(started_at=990.0, audio_path=p)[1] is False
