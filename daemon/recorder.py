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

Предохранители записи (SPEC_v3 §10 «диск полон», «забыл выключить») — все
живут в writer-потоке, НЕ в audio-callback:
    - duration-limit: жёсткий лимит длительности (MEETMIND_MAX_RECORDING_SEC,
      default 6ч) — авто-стоп, self.stopped_reason = "duration_limit".
    - disk-guard: преflight-предупреждение при старте (<MEETMIND_MIN_DISK_START_GB,
      default 2GB, запись всё равно стартует) + авто-стоп во время записи
      (<MEETMIND_MIN_DISK_STOP_MB, default 500MB), self.stopped_reason = "disk_full",
      уже записанное сохраняется как есть.
    - silence-эвристика: RMS блоков ниже порога непрерывно >MEETMIND_SILENCE_MIN
      (default 30 минут) — запись НЕ останавливается, только один раз (edge-trigger)
      вызывается on_silence-коллбек с предложением остановиться вручную.

Оба авто-стопа сообщаются владельцу (DaemonApp) через on_autostop-коллбек,
вызываемый из writer-потока синхронно с обнаружением условия — не из
audio-callback, чтобы не тормозить realtime-путь.
"""
import logging
import os
import queue
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

logger = logging.getLogger(__name__)

AGGREGATE_DEVICE_NAME = "MeetMind In"
BLACKHOLE_DEVICE_NAME = "BlackHole 2ch"

# Предохранители (SPEC_v3 §10) — дефолты, каждый переопределим через env
# (или явным kwarg-ом в конструкторе Recorder — удобно для тестов).
DEFAULT_MAX_RECORDING_SEC = 6 * 3600
DEFAULT_MIN_DISK_START_GB = 2
DEFAULT_MIN_DISK_STOP_MB = 500
DEFAULT_SILENCE_MIN = 30
SILENCE_RMS_THRESHOLD = 1e-4
DISK_CHECK_INTERVAL_SEC = 30


def _env_num(name: str, default, cast=float):
    val = os.environ.get(name)
    return cast(val) if val is not None else cast(default)


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


# --------------------------------------------------------------- guards ---

def block_rms(block) -> float:
    """RMS амплитуды одного блока сэмплов.

    Чистая функция — принимает numpy-массив (или что-то np.asarray-совместимое)
    любой формы (mono или frames x channels), возвращает скаляр. Пустой блок → 0.0.
    """
    arr = np.asarray(block, dtype=np.float64)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(arr))))


class SilenceDetector:
    """Детектор состояния: подряд идущая тишина (по RMS блоков), в секундах.

    Чистая логика без I/O — легко тестировать на синтетических последовательностях.
    feed() вызывается на каждый блок аудио с его RMS и длительностью (сек):
        - RMS >= порога → счётчик подряд-тишины обнуляется, флаг "уже сработал"
          снимается, возвращается False;
        - RMS < порога → счётчик накапливается; как только он впервые пересёк
          threshold_sec — feed() возвращает True РОВНО ОДИН РАЗ (edge-trigger),
          дальше, пока тишина продолжается без перерыва, — False (чтобы не
          спамить коллбек). Новый непрерывный отрезок тишины (после звука)
          может сработать снова.
    """

    def __init__(
        self,
        rms_threshold: float = SILENCE_RMS_THRESHOLD,
        threshold_sec: float = DEFAULT_SILENCE_MIN * 60,
    ):
        self.rms_threshold = rms_threshold
        self.threshold_sec = threshold_sec
        self._silent_sec = 0.0
        self._fired = False

    def feed(self, rms_value: float, duration_sec: float) -> bool:
        if rms_value >= self.rms_threshold:
            self._silent_sec = 0.0
            self._fired = False
            return False

        self._silent_sec += max(0.0, duration_sec)
        if self._silent_sec >= self.threshold_sec and not self._fired:
            self._fired = True
            return True
        return False

    def reset(self) -> None:
        self._silent_sec = 0.0
        self._fired = False


class Recorder:
    """Потоковая запись в WAV: callback -> queue -> writer-поток -> SoundFile.

    Предохранители (см. модульный docstring) живут в writer-потоке и опираются
    только на данные, уже прошедшие через очередь — audio-callback их не касается.
    """

    def __init__(
        self,
        out_path,
        device: Optional[int] = None,
        samplerate: int = 16000,
        channels: int = 2,
        max_recording_sec: Optional[float] = None,
        min_disk_start_gb: Optional[float] = None,
        min_disk_stop_mb: Optional[float] = None,
        silence_min: Optional[float] = None,
        disk_check_interval_sec: Optional[float] = None,
        on_autostop: Optional[Callable[[str], None]] = None,
        on_silence: Optional[Callable[[], None]] = None,
    ):
        self.out_path = Path(out_path)
        self.device = device
        self.samplerate = samplerate
        self.channels = channels

        # Предохранители: явный kwarg побеждает, иначе — env, иначе — дефолт.
        # env читаем в момент конструирования (не на импорте модуля), чтобы
        # MEETMIND_MAX_RECORDING_SEC и т.п. можно было переопределить перед
        # стартом демона/теста без перезагрузки модуля.
        self.max_recording_sec = (
            max_recording_sec
            if max_recording_sec is not None
            else _env_num("MEETMIND_MAX_RECORDING_SEC", DEFAULT_MAX_RECORDING_SEC, cast=int)
        )
        self.min_disk_start_gb = (
            min_disk_start_gb
            if min_disk_start_gb is not None
            else _env_num("MEETMIND_MIN_DISK_START_GB", DEFAULT_MIN_DISK_START_GB)
        )
        self.min_disk_stop_mb = (
            min_disk_stop_mb
            if min_disk_stop_mb is not None
            else _env_num("MEETMIND_MIN_DISK_STOP_MB", DEFAULT_MIN_DISK_STOP_MB)
        )
        self.silence_min = (
            silence_min
            if silence_min is not None
            else _env_num("MEETMIND_SILENCE_MIN", DEFAULT_SILENCE_MIN)
        )
        self.disk_check_interval_sec = (
            disk_check_interval_sec
            if disk_check_interval_sec is not None
            else DISK_CHECK_INTERVAL_SEC
        )
        self.on_autostop = on_autostop
        self.on_silence = on_silence
        # None при обычном stop(); "duration_limit" | "disk_full" при авто-стопе.
        self.stopped_reason: Optional[str] = None

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

    def _disk_free_bytes(self) -> Optional[int]:
        try:
            return shutil.disk_usage(self.out_path.parent).free
        except OSError as e:
            logger.warning(f"Recorder: не удалось проверить свободное место: {e}")
            return None

    def _invoke_callback(self, cb: Callable, *args) -> None:
        try:
            cb(*args)
        except Exception:
            logger.exception(f"Recorder: ошибка в колбэке {cb!r}")

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                logger.error(f"Ошибка остановки InputStream: {e}", exc_info=True)
            finally:
                self._stream = None

    def _write_loop(self):
        start_mono = time.monotonic()
        last_disk_check = start_mono
        silence = SilenceDetector(
            rms_threshold=SILENCE_RMS_THRESHOLD,
            threshold_sec=self.silence_min * 60,
        )
        autostop_reason: Optional[str] = None

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
                        block = self._q.get(timeout=0.5)
                    except queue.Empty:
                        block = None

                    if block is not None:
                        f.write(block)
                        duration_sec = (
                            len(block) / self.samplerate if self.samplerate else 0.0
                        )
                        if (
                            silence.feed(block_rms(block), duration_sec)
                            and self.on_silence is not None
                        ):
                            self._invoke_callback(self.on_silence)

                    if self._stop.is_set():
                        continue

                    now = time.monotonic()

                    if now - start_mono >= self.max_recording_sec:
                        autostop_reason = "duration_limit"
                        break

                    if now - last_disk_check >= self.disk_check_interval_sec:
                        last_disk_check = now
                        free = self._disk_free_bytes()
                        if (
                            free is not None
                            and free < self.min_disk_stop_mb * 2**20
                        ):
                            autostop_reason = "disk_full"
                            break
        except Exception as e:
            self._write_error = e
            logger.error(f"Ошибка записи в файл {self.out_path}: {e}", exc_info=True)
            return

        if autostop_reason is not None:
            self.stopped_reason = autostop_reason
            logger.warning(
                f"⏱️  Recorder: авто-стоп ({autostop_reason}) — {self.out_path}"
            )
            self._close_stream()
            self._stop.set()
            # Обнуляем ДО коллбека: если on_autostop реентрантно дёрнет stop(),
            # тот не должен пытаться join() поток изнутри самого себя.
            self._writer = None
            if self.on_autostop is not None:
                self._invoke_callback(self.on_autostop, self.stopped_reason)

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
        self.stopped_reason = None

        # Disk-guard preflight: предупреждаем, но запись всё равно стартует
        # (SPEC_v3 §10 — «диск полон» на старте не блокирует запись).
        free = self._disk_free_bytes()
        if free is not None and free < self.min_disk_start_gb * 2**30:
            logger.warning(
                f"⚠️  Recorder.start: мало места на диске — "
                f"{free / 2**30:.2f}GB свободно на {self.out_path.parent} "
                f"(порог {self.min_disk_start_gb}GB). Запись всё равно стартует."
            )

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
        """Остановить поток захвата, дождаться writer-потока и закрыть файл.

        Идемпотентно относительно авто-стопа: если writer-поток уже сам себя
        остановил (duration_limit/disk_full), просто возвращает путь к файлу.
        """
        self._close_stream()

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
