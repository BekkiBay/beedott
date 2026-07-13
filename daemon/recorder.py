"""
Запись аудио с macOS — Aggregate Device, BlackHole, или системный микрофон.

Fallback chain:
    1) AGGREGATE_DEVICE_NAME (например "MeetMind In" — связка BlackHole+мик)
    2) BlackHole 2ch (только системный звук, мик не пишется)
    3) Системный микрофон по умолчанию (мик есть, системного звука нет)

Если первое в цепочке не нашлось — переходим к следующему и логируем warning.
"""
import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd
from scipy.io import wavfile

logger = logging.getLogger(__name__)


class AudioRecorder:
    """Запись аудио с приоритетным выбором устройства."""

    def __init__(
        self,
        aggregate_device_name: str = "MeetMind In",
        blackhole_device_name: str = "BlackHole 2ch",
        sample_rate: int = 16000,  # 16k достаточно для Whisper и в 3× меньше файл
    ):
        self.aggregate_name = aggregate_device_name
        self.blackhole_name = blackhole_device_name
        self.sample_rate = sample_rate

        self.device_id, self.device_label, self.channels = self._pick_device()

        self.is_recording = False
        self.audio_data: list[np.ndarray] = []
        self.audio_file: Optional[Path] = None
        self.recording_thread: Optional[threading.Thread] = None

    # ----------------------------- выбор устройства ----------------------------

    def _pick_device(self) -> tuple[int, str, int]:
        """Выбрать устройство по fallback chain. Возвращает (id, label, channels)."""
        try:
            devices = sd.query_devices()
        except Exception as e:
            logger.error(f"Не могу получить список аудио-устройств: {e}")
            return (-1, "none", 1)

        # 1) Aggregate device — приоритет
        agg = self._find_input_device(devices, self.aggregate_name)
        if agg is not None:
            idx, dev = agg
            ch = max(1, min(int(dev.get("max_input_channels", 2) or 2), 2))
            logger.info(
                f"🎙️ Aggregate device найден: «{dev['name']}» (id={idx}, channels={ch})"
            )
            return (idx, dev["name"], ch)

        # 2) BlackHole — только системный звук
        bh = self._find_input_device(devices, self.blackhole_name)
        if bh is not None:
            idx, dev = bh
            ch = max(1, min(int(dev.get("max_input_channels", 2) or 2), 2))
            logger.warning(
                f"⚠️  Aggregate «{self.aggregate_name}» не найден, использую "
                f"BlackHole — будет писаться ТОЛЬКО системный звук, без микрофона. "
                f"Создай Aggregate Device в Audio MIDI Setup чтобы захватывать оба."
            )
            return (idx, dev["name"], ch)

        # 3) Дефолтный микрофон
        try:
            default_in = sd.default.device[0]
            dev = devices[default_in]
            ch = max(1, min(int(dev.get("max_input_channels", 1) or 1), 2))
            logger.warning(
                f"⚠️  Ни Aggregate, ни BlackHole не найдены — пишу с дефолтного "
                f"микрофона «{dev['name']}». Установи BlackHole для записи "
                f"системного звука: brew install blackhole-2ch"
            )
            return (default_in, dev["name"], ch)
        except Exception as e:
            logger.error(f"Не могу определить дефолтное устройство: {e}")
            return (-1, "none", 1)

    @staticmethod
    def _find_input_device(devices, name_substring: str):
        needle = name_substring.lower().strip()
        for i, d in enumerate(devices):
            name = (d.get("name") or "").lower()
            if needle in name and int(d.get("max_input_channels") or 0) > 0:
                return i, d
        return None

    # ----------------------------- API записи -----------------------------------

    def start(self, session_id: str, output_dir: Path) -> str:
        if self.is_recording:
            logger.warning("Запись уже идёт")
            return ""

        if self.device_id < 0:
            logger.error("Нет рабочего устройства записи")
            return ""

        output_dir.mkdir(parents=True, exist_ok=True)
        self.audio_file = output_dir / f"{session_id}.wav"
        self.audio_data = []
        self.is_recording = True

        logger.info(
            f"▶️  Старт записи: device={self.device_label}, "
            f"rate={self.sample_rate}Hz, ch={self.channels}, → {self.audio_file}"
        )

        self.recording_thread = threading.Thread(target=self._record_loop, daemon=True)
        self.recording_thread.start()
        return str(self.audio_file)

    def _record_loop(self):
        try:
            def cb(indata, frames, time_info, status):
                if status:
                    logger.debug(f"audio status: {status}")
                # копируем во избежание гонок при дозаписи
                self.audio_data.append(indata.copy())

            with sd.InputStream(
                device=self.device_id,
                samplerate=self.sample_rate,
                channels=self.channels,
                callback=cb,
                blocksize=2048,
                dtype="float32",
            ):
                while self.is_recording:
                    sd.sleep(100)
        except Exception as e:
            logger.error(f"Ошибка записи: {e}", exc_info=True)
            self.is_recording = False

    def stop(self) -> Optional[str]:
        if not self.is_recording:
            return None

        self.is_recording = False
        if self.recording_thread:
            self.recording_thread.join(timeout=5)

        if not self.audio_data:
            logger.warning("Нет аудио-данных для сохранения")
            return None

        try:
            audio = np.concatenate(self.audio_data, axis=0)
            # mix в моно если многоканал — Whisper и так схлопывает,
            # но так файл в 2 раза меньше
            if audio.ndim > 1 and audio.shape[1] > 1:
                audio = audio.mean(axis=1)

            audio_int16 = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
            wavfile.write(str(self.audio_file), self.sample_rate, audio_int16)
            seconds = len(audio_int16) / self.sample_rate
            logger.info(
                f"⏹️  Запись остановлена: {self.audio_file} "
                f"({seconds:.1f} сек, {self.audio_file.stat().st_size / 1024:.0f} KB)"
            )
            return str(self.audio_file)
        except Exception as e:
            logger.error(f"Ошибка сохранения аудио: {e}", exc_info=True)
            return None
