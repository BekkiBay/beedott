"""
Потоковая запись аудио с macOS — Aggregate Device, BlackHole, или системный микрофон.

Fallback chain выбора устройства (см. pick_device()):
    1) AGGREGATE_DEVICE_NAME (например "MeetMind In" — связка BlackHole+мик)
    2) BlackHole 2ch (только системный звук, мик не пишется)
    3) Системный микрофон по умолчанию (мик есть, системного звука нет)

Если первое в цепочке не нашлось — переходим к следующему и логируем warning
с указанием, какой путь выбран.

Realtime-safety: аудио-callback НЕ делает дисковый I/O (задержка диска —
Spotlight, Time Machine — дала бы overrun и потерю сэмплов). Callback только
кладёт копию буфера в queue.Queue; отдельный поток-писатель читает из очереди
и пишет в soundfile.SoundFile (PCM_16).
"""
import logging
import queue
import threading
from pathlib import Path
from typing import Optional

import sounddevice as sd
import soundfile as sf

logger = logging.getLogger(__name__)

AGGREGATE_DEVICE_NAME = "MeetMind In"
BLACKHOLE_DEVICE_NAME = "BlackHole 2ch"


def _find_input_device(devices, name_substring: str):
    needle = name_substring.lower().strip()
    for i, d in enumerate(devices):
        name = (d.get("name") or "").lower()
        if needle in name and int(d.get("max_input_channels") or 0) > 0:
            return i, d
    return None


def pick_device(
    aggregate_name: str = AGGREGATE_DEVICE_NAME,
    blackhole_name: str = BLACKHOLE_DEVICE_NAME,
) -> tuple[int, str, int]:
    """Выбрать входное устройство по fallback chain.

    Возвращает (device_index, device_name, max_input_channels).
    Логирует, какой путь в цепочке был выбран.
    """
    try:
        devices = sd.query_devices()
    except Exception as e:
        logger.error(f"Не могу получить список аудио-устройств: {e}")
        raise RuntimeError("Не могу получить список аудио-устройств") from e

    # 1) Aggregate device — приоритет
    agg = _find_input_device(devices, aggregate_name)
    if agg is not None:
        idx, dev = agg
        ch = int(dev.get("max_input_channels") or 0)
        logger.info(
            f"🎙️ pick_device: Aggregate найден — «{dev['name']}» "
            f"(id={idx}, max_input_channels={ch})"
        )
        return idx, dev["name"], ch

    # 2) BlackHole — только системный звук
    bh = _find_input_device(devices, blackhole_name)
    if bh is not None:
        idx, dev = bh
        ch = int(dev.get("max_input_channels") or 0)
        logger.warning(
            f"⚠️  pick_device: Aggregate «{aggregate_name}» не найден, использую "
            f"BlackHole «{dev['name']}» (id={idx}) — будет писаться ТОЛЬКО "
            f"системный звук, без микрофона. Создай Aggregate Device в Audio MIDI "
            f"Setup чтобы захватывать оба."
        )
        return idx, dev["name"], ch

    # 3) Дефолтный микрофон
    try:
        default_in = sd.default.device[0]
        if default_in is None or default_in < 0:
            raise RuntimeError("default input device не задан")
        dev = devices[default_in]
        ch = int(dev.get("max_input_channels") or 0)
        logger.warning(
            f"⚠️  pick_device: ни Aggregate, ни BlackHole не найдены — пишу с "
            f"дефолтного микрофона «{dev['name']}» (id={default_in}). Установи "
            f"BlackHole для записи системного звука: brew install blackhole-2ch"
        )
        return default_in, dev["name"], ch
    except Exception as e:
        logger.error(f"Не могу определить дефолтное устройство записи: {e}")
        raise RuntimeError("Нет рабочего устройства записи") from e


class Recorder:
    """Потоковая запись в WAV: callback -> queue -> writer-поток -> SoundFile."""

    def __init__(
        self,
        out_path,
        device: Optional[int] = None,
        samplerate: int = 16000,
        channels: int = 2,
    ):
        self.out_path = Path(out_path)
        self.device = device
        self.samplerate = samplerate
        self.channels = channels

        self._q: "queue.Queue" = queue.Queue()
        self._stream: Optional[sd.InputStream] = None
        self._writer: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._write_error: Optional[Exception] = None

    def _callback(self, indata, frames, time_info, status):
        if status:
            logger.debug(f"audio status: {status}")
        # копируем во избежание гонок — исходный буфер переиспользуется PortAudio
        self._q.put_nowait(indata.copy())

    def _write_loop(self):
        try:
            with sf.SoundFile(
                str(self.out_path),
                mode="w",
                samplerate=self.samplerate,
                channels=self.channels,
                subtype="PCM_16",
            ) as f:
                while not (self._stop.is_set() and self._q.empty()):
                    try:
                        f.write(self._q.get(timeout=0.5))
                    except queue.Empty:
                        continue
        except Exception as e:
            self._write_error = e
            logger.error(f"Ошибка записи в файл {self.out_path}: {e}", exc_info=True)

    def _resolve_device_and_channels(self) -> None:
        """Выбрать устройство (если не задано явно) и деградировать channels
        до максимума, поддерживаемого устройством, если нужно."""
        if self.device is None:
            idx, label, max_ch = pick_device()
            self.device = idx
        else:
            idx = self.device
            try:
                dev_info = sd.query_devices(idx)
                label = dev_info.get("name", str(idx))
                max_ch = int(dev_info.get("max_input_channels") or 0)
            except Exception as e:
                logger.warning(
                    f"Recorder: не удалось получить информацию об устройстве "
                    f"{idx}: {e} — использую как есть."
                )
                label, max_ch = str(idx), self.channels

        if 0 < max_ch < self.channels:
            logger.warning(
                f"⚠️  Recorder: устройство «{label}» поддерживает только "
                f"{max_ch} канал(ов), запрошено {self.channels} — деградирую "
                f"до {max_ch}."
            )
            self.channels = max_ch

        logger.info(
            f"▶️  Recorder.start: device=«{label}» (id={self.device}), "
            f"samplerate={self.samplerate}Hz, channels={self.channels} → "
            f"{self.out_path}"
        )

    def start(self) -> str:
        """Разрешить устройство, запустить writer-поток и InputStream.
        Возвращает путь к файлу записи (доступен сразу)."""
        self._resolve_device_and_channels()

        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._write_error = None

        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

        self._stream = sd.InputStream(
            device=self.device,
            samplerate=self.samplerate,
            channels=self.channels,
            callback=self._callback,
        )
        self._stream.start()
        return str(self.out_path)

    def stop(self) -> Optional[str]:
        """Остановить поток захвата, дождаться writer-потока и закрыть файл."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.error(f"Ошибка остановки InputStream: {e}", exc_info=True)
            finally:
                self._stream = None

        self._stop.set()
        if self._writer is not None:
            self._writer.join(timeout=10)
            if self._writer.is_alive():
                logger.error(
                    "⏹️  Recorder.stop: writer-поток не завершился за 10с — "
                    "файл может быть неполным"
                )
            self._writer = None

        if self._write_error is not None:
            logger.error(f"Recorder.stop: во время записи была ошибка: {self._write_error}")
            return None

        try:
            size_kb = self.out_path.stat().st_size / 1024
            logger.info(f"⏹️  Recorder.stop: файл закрыт — {self.out_path} ({size_kb:.0f} KB)")
        except OSError:
            pass

        return str(self.out_path)
