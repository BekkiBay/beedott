"""Предохранители записи (SPEC_v3 §10, PLAN_v3 Task 2.3): duration-limit,
disk-guard, silence-эвристика.

Все тесты в этом файле подменяют sounddevice целиком (никакого реального
устройства/TCC-разрешения не требуется — не помечены `hardware`), но
writer-поток настоящий (реальный threading.Thread + реальный soundfile.SoundFile
на tmp_path), поэтому таймауты держим короткими ("не дольше пары секунд" —
ограничение из PLAN_v3), опрашивая состояние в цикле с мелким сном вместо
одного длинного time.sleep().
"""
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from daemon import recorder


class _FakeInputStream:
    """Замена sd.InputStream — не открывает реальное устройство/TCC."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fake_hardware(monkeypatch):
    """Ни один тест в этом файле не должен трогать реальное аудио-железо."""
    monkeypatch.setattr(recorder.sd, "InputStream", _FakeInputStream)
    monkeypatch.setattr(
        recorder.sd, "query_devices",
        lambda idx: {"name": "Fake Device", "max_input_channels": 2},
    )


def _wait_until(predicate, timeout=2.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


# --------------------------------------------------------- block_rms() ---

def test_block_rms_zero_for_silence():
    block = np.zeros((100, 2), dtype=np.float32)
    assert recorder.block_rms(block) == 0.0


def test_block_rms_positive_for_constant_signal():
    block = np.full((100, 2), 0.5, dtype=np.float32)
    assert recorder.block_rms(block) == pytest.approx(0.5, rel=1e-5)


def test_block_rms_empty_block_is_zero():
    assert recorder.block_rms(np.zeros((0, 2), dtype=np.float32)) == 0.0


def test_block_rms_handles_mono_1d_array():
    block = np.array([1.0, -1.0, 1.0, -1.0], dtype=np.float32)
    assert recorder.block_rms(block) == pytest.approx(1.0, rel=1e-5)


# ----------------------------------------------------- SilenceDetector ---

def test_silence_detector_below_threshold_never_fires():
    det = recorder.SilenceDetector(rms_threshold=1e-4, threshold_sec=5.0)
    assert det.feed(0.0, 2.0) is False
    assert det.feed(0.0, 2.0) is False
    # суммарно 4с < 5с порога — ещё не сработало


def test_silence_detector_fires_exactly_once_edge_triggered():
    det = recorder.SilenceDetector(rms_threshold=1e-4, threshold_sec=5.0)
    assert det.feed(0.0, 2.0) is False
    assert det.feed(0.0, 2.0) is False
    assert det.feed(0.0, 2.0) is True  # 4 + 2 = 6с >= 5с порога — впервые
    assert det.feed(0.0, 2.0) is False  # тишина продолжается — молчим дальше
    assert det.feed(0.0, 2.0) is False


def test_silence_detector_resets_on_loud_block_and_can_fire_again():
    det = recorder.SilenceDetector(rms_threshold=1e-4, threshold_sec=3.0)
    assert det.feed(0.0, 3.0) is True
    assert det.feed(0.5, 1.0) is False  # звук выше порога — сброс счётчика
    assert det.feed(0.0, 2.0) is False  # новый отрезок тишины, ещё не набрали
    assert det.feed(0.0, 1.0) is True  # снова перешли порог — срабатывает опять


def test_silence_detector_loud_audio_never_fires():
    det = recorder.SilenceDetector(rms_threshold=1e-4, threshold_sec=1.0)
    for _ in range(10):
        assert det.feed(0.5, 10.0) is False


def test_silence_detector_reset_clears_state():
    det = recorder.SilenceDetector(rms_threshold=1e-4, threshold_sec=2.0)
    det.feed(0.0, 1.5)
    det.reset()
    assert det.feed(0.0, 1.0) is False  # после reset() счётчик снова с нуля


# --------------------------------------------------- duration-limit ------

def test_duration_limit_autostops_recording(tmp_path):
    """env-override MEETMIND_MAX_RECORDING_SEC подменяется здесь через явный
    kwarg конструктора — короткий предохранитель без реального 6-часового сна."""
    events = []
    rec = recorder.Recorder(
        out_path=tmp_path / "a.wav",
        device=0,
        max_recording_sec=0.3,
        min_disk_start_gb=-1,   # выключаем disk-предупреждение в этом тесте
        min_disk_stop_mb=-1,    # выключаем disk-авто-стоп
        silence_min=999,
        on_autostop=events.append,
    )
    rec.start()

    assert _wait_until(lambda: rec.stopped_reason is not None, timeout=2.0)

    assert rec.stopped_reason == "duration_limit"
    assert events == ["duration_limit"]

    # writer сам себя обнулил при авто-стопе — повторный публичный stop()
    # должен быть идемпотентен и не пытаться join() поток изнутри себя.
    path = rec.stop()
    assert path == str(rec.out_path)
    assert Path(path).exists()


def test_normal_stop_leaves_stopped_reason_none(tmp_path):
    rec = recorder.Recorder(
        out_path=tmp_path / "a.wav",
        device=0,
        max_recording_sec=999,
        min_disk_start_gb=-1,
        min_disk_stop_mb=-1,
        silence_min=999,
    )
    rec.start()
    time.sleep(0.1)
    path = rec.stop()
    assert path == str(rec.out_path)
    assert rec.stopped_reason is None


def test_max_recording_sec_reads_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MEETMIND_MAX_RECORDING_SEC", "123")
    rec = recorder.Recorder(out_path=tmp_path / "a.wav", device=0)
    assert rec.max_recording_sec == 123


def test_max_recording_sec_default_is_six_hours(tmp_path):
    rec = recorder.Recorder(out_path=tmp_path / "a.wav", device=0)
    assert rec.max_recording_sec == 6 * 3600


# ---------------------------------------------------------- disk-guard ---

def test_disk_guard_autostops_when_free_space_drops(tmp_path, monkeypatch):
    monkeypatch.setattr(
        recorder.shutil, "disk_usage",
        lambda path: SimpleNamespace(total=0, used=0, free=100 * 2**20),  # 100MB
    )
    events = []
    rec = recorder.Recorder(
        out_path=tmp_path / "a.wav",
        device=0,
        max_recording_sec=999,
        min_disk_stop_mb=500,
        disk_check_interval_sec=0,  # проверяем на каждой итерации writer-цикла
        silence_min=999,
        on_autostop=events.append,
    )
    rec.start()

    assert _wait_until(lambda: rec.stopped_reason is not None, timeout=2.0)

    assert rec.stopped_reason == "disk_full"
    assert events == ["disk_full"]
    rec.stop()  # идемпотентно, файл (пустой, но валидный) сохранён


def test_disk_guard_preflight_warns_but_recording_still_starts(tmp_path, monkeypatch, caplog):
    """SPEC_v3 §10: <2GB на старте — только предупреждение, запись не блокируется."""
    monkeypatch.setattr(
        recorder.shutil, "disk_usage",
        lambda path: SimpleNamespace(total=0, used=0, free=1 * 2**30),  # 1GB < default 2GB старта
    )
    rec = recorder.Recorder(
        out_path=tmp_path / "a.wav",
        device=0,
        max_recording_sec=999,
        min_disk_stop_mb=500,  # 1GB free > 500MB стоп-порога — не остановит
        disk_check_interval_sec=999,
        silence_min=999,
    )
    with caplog.at_level(logging.WARNING, logger="daemon.recorder"):
        rec.start()

    assert rec._stream is not None  # реально стартовала, несмотря на мало места
    assert any("мало места" in r.message for r in caplog.records)

    rec.stop()
    assert rec.stopped_reason is None


def test_disk_guard_does_not_fire_when_space_is_plentiful(tmp_path, monkeypatch):
    monkeypatch.setattr(
        recorder.shutil, "disk_usage",
        lambda path: SimpleNamespace(total=0, used=0, free=100 * 2**30),  # 100GB
    )
    rec = recorder.Recorder(
        out_path=tmp_path / "a.wav",
        device=0,
        max_recording_sec=999,
        min_disk_stop_mb=500,
        disk_check_interval_sec=0,
        silence_min=999,
    )
    rec.start()
    time.sleep(0.3)
    assert rec.stopped_reason is None
    rec.stop()


# -------------------------------------------------- silence в Recorder ---

def test_silence_callback_fires_without_stopping_recording(tmp_path):
    silence_events = []
    rec = recorder.Recorder(
        out_path=tmp_path / "a.wav",
        device=0,
        max_recording_sec=999,
        min_disk_start_gb=-1,
        min_disk_stop_mb=-1,
        disk_check_interval_sec=999,
        silence_min=0.02,  # 1.2 "виртуальных" секунды тишины — порог
        on_silence=lambda: silence_events.append(True),
    )
    rec.start()

    block = np.zeros((int(rec.samplerate * 0.3), rec.channels), dtype=np.float32)
    for _ in range(8):  # 8 * 0.3 = 2.4с виртуальной тишины > 1.2с порога
        rec._q.put(block)

    assert _wait_until(lambda: len(silence_events) > 0, timeout=2.0)

    # даём writer-у ещё немного прокрутиться, чтобы убедиться что коллбек
    # не спамит повторно, пока тишина продолжается
    time.sleep(0.2)
    rec.stop()

    assert silence_events == [True]
    assert rec.stopped_reason is None  # тишина НЕ останавливает запись
