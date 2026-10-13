"""Предохранители записи, которую пишет Swift CaptureEngine (audio_path_owner=app).

Python Recorder проверяет лимит длительности, диск и тишину сам, в своём
writer-потоке. Для нативной записи через Process Tap (основной путь) этих
проверок не было вовсе (аудит T7): забытая запись шла до заполнения диска, а
WAV упирался в лимит RIFF в 4 GiB.

Политика живёт здесь, в демоне, — в одном месте с порогами Recorder (те же
env-переменные). Остановить чужой CaptureEngine демон не может, поэтому
DaemonApp только выставляет stop_requested в status, а Swift, увидев его,
останавливает движок и зовёт stop_recording. Исключение — app_gone: файл давно
не меняется, значит писателя уже нет, и демон закрывает запись сам.
"""
import os
import shutil
import struct
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from daemon.recorder import (
    DEFAULT_MAX_RECORDING_SEC,
    DEFAULT_MIN_DISK_STOP_MB,
    DEFAULT_SILENCE_MIN,
    SILENCE_RMS_THRESHOLD,
    SilenceDetector,
    _env_num,
    block_rms,
)

# Swift пишет WAV непрерывно; файл, не менявшийся дольше этого, — брошенный
# (приложение упало или устройство перестало отдавать звук).
DEFAULT_STALE_SEC = 120
# Лимит RIFF — 4 GiB (32-битные размеры в заголовке). Останавливаемся с запасом.
DEFAULT_MAX_WAV_BYTES = int(3.9 * 2**30)
# Сколько последних секунд WAV слушаем для детектора тишины.
TAIL_SEC = 5.0
# Пауза между проверками длиннее этого — система спала (или процесс стоял).
# Файл не менялся, пока все спали, — это не «приложение умерло».
SLEEP_GAP_SEC = 30.0


def wav_tail_rms(path: Path, seconds: float) -> Optional[float]:
    """RMS последних `seconds` PCM16-WAV (0…1). Размер data-чанка из заголовка
    не используется — у пишущегося AVAudioFile он ещё не финализирован; читаем
    до конца файла. None — не WAV / не PCM16 / не удалось прочитать."""
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            pos, channels, rate, block_align, bits, data_off = 12, 0, 0, 0, 0, None
            while pos + 8 <= len(head):
                cid = head[pos:pos + 4]
                size = struct.unpack("<I", head[pos + 4:pos + 8])[0]
                if cid == b"fmt " and pos + 24 <= len(head):
                    _, channels, rate, _, block_align, bits = struct.unpack(
                        "<HHIIHH", head[pos + 8:pos + 24])
                elif cid == b"data":
                    data_off = pos + 8
                    break
                pos += 8 + size + (size & 1)
            if data_off is None or bits != 16 or not channels or not rate or not block_align:
                return None
            file_size = os.path.getsize(path)
            want = int(seconds * rate) * block_align
            start = max(data_off, file_size - want)
            start = data_off + ((start - data_off) // block_align) * block_align
            f.seek(start)
            raw = f.read(file_size - start)
    except OSError:
        return None
    usable = len(raw) - len(raw) % block_align
    if usable <= 0:
        return 0.0
    samples = np.frombuffer(raw[:usable], dtype="<i2").astype(np.float32) / 32768.0
    return block_rms(samples.reshape(-1, channels))


def _disk_free(path: Path) -> Optional[int]:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class AppRecordingGuard:
    """Одна активная запись owner=app. check() зовётся периодически (раз в
    несколько секунд) и возвращает (stop_reason | None, silence_fired)."""

    def __init__(
        self,
        *,
        max_sec: Optional[float] = None,
        min_disk_stop_mb: Optional[float] = None,
        silence_min: Optional[float] = None,
        stale_sec: float = DEFAULT_STALE_SEC,
        max_bytes: int = DEFAULT_MAX_WAV_BYTES,
        clock: Callable[[], float] = time.time,
        disk_free: Callable[[Path], Optional[int]] = _disk_free,
        tail_rms: Callable[[Path, float], Optional[float]] = wav_tail_rms,
        mtime: Callable[[Path], Optional[float]] = _mtime,
    ):
        self.max_sec = (max_sec if max_sec is not None else
                        _env_num("MEETMIND_MAX_RECORDING_SEC", DEFAULT_MAX_RECORDING_SEC, cast=int))
        self.min_disk_stop_mb = (min_disk_stop_mb if min_disk_stop_mb is not None else
                                 _env_num("MEETMIND_MIN_DISK_STOP_MB", DEFAULT_MIN_DISK_STOP_MB))
        silence_min = (silence_min if silence_min is not None else
                       _env_num("MEETMIND_SILENCE_MIN", DEFAULT_SILENCE_MIN))
        self.stale_sec = stale_sec
        self.max_bytes = max_bytes
        self._clock = clock
        self._disk_free = disk_free
        self._tail_rms = tail_rms
        self._mtime = mtime
        self._silence = SilenceDetector(
            rms_threshold=SILENCE_RMS_THRESHOLD, threshold_sec=silence_min * 60)
        self._last_check: Optional[float] = None
        self._grace_until = 0.0

    def check(self, *, started_at: float, audio_path: Path) -> tuple[Optional[str], bool]:
        now = self._clock()
        dt = 0.0 if self._last_check is None else max(0.0, now - self._last_check)
        self._last_check = now
        if dt > SLEEP_GAP_SEC:
            # Проснулись: даём Swift stale_sec, чтобы запись снова пошла, а сон
            # не засчитываем в тишину.
            self._grace_until = now + self.stale_sec
            dt = 0.0

        mtime = self._mtime(audio_path)
        last_activity = mtime if mtime is not None else started_at
        if now - last_activity > self.stale_sec and now >= self._grace_until:
            return "app_gone", False

        if now - started_at > self.max_sec:
            return "duration_limit", False

        free = self._disk_free(audio_path.parent)
        if free is not None and free < self.min_disk_stop_mb * 2**20:
            return "disk_full", False

        try:
            if audio_path.stat().st_size >= self.max_bytes:
                return "file_limit", False
        except OSError:
            pass

        rms = self._tail_rms(audio_path, TAIL_SEC)
        silent = rms is not None and self._silence.feed(rms, dt)
        return None, silent
