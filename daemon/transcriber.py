"""
Транскрипция через MLX-Whisper (Metal на Apple Silicon).

mlx-whisper в 5-10× быстрее openai-whisper и использует Metal/Apple Neural Engine.
При отсутствии mlx-whisper в системе используется openai-whisper как fallback.

Анти-галлюцинационные дефолты (DEFAULT_KWARGS) и дедуп повторов (dedupe_lines) —
перенос из `test_one_meeting.sh` (флаги, накопленные вручную на реальных
русскоязычных встречах), см. SPEC_v3 §8 и PLAN_v3 Task 1.4.
"""
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

import soundfile as sf

logger = logging.getLogger(__name__)


# ─── анти-галлюцинационные дефолты (test_one_meeting.sh:89-101) ────────────
DEFAULT_KWARGS = dict(
    language="ru",
    temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),  # fallback-лестница при лупах
    compression_ratio_threshold=2.4,
    logprob_threshold=-1.0,
    no_speech_threshold=0.6,
    condition_on_previous_text=False,  # v2 было True — источник лупов
)
MODEL_REPO = "mlx-community/whisper-large-v3-turbo"

# ─── чанкинг длинных записей ─────────────────────────────────────────────────
LONG_AUDIO_THRESHOLD_SEC = 60 * 60  # > 60 мин → резать
CHUNK_SEC = 30 * 60  # куски по 30 мин
OVERLAP_SEC = 5  # перекрытие между соседними чанками


# ─── дедуп повторов (test_one_meeting.sh:107-126) ───────────────────────────


def _dedupe_keep_one_repeat(items: list, key: Callable = lambda x: x) -> list:
    """Общий алгоритм дедупа: из подряд идущих одинаковых (по `key`) элементов
    оставляем максимум два (т.е. одно повторение), остальные дропаем. Элементы
    с пустым `key` (после strip у строк) отбрасываются целиком.

    Прямой перенос python-цикла из test_one_meeting.sh:107-126.
    """
    result: list = []
    prev = None
    dup_count = 0
    for item in items:
        k = key(item)
        if isinstance(k, str):
            k = k.strip()
        if not k:
            continue
        if k == prev:
            dup_count += 1
            if dup_count < 2:
                result.append(item)
            continue
        dup_count = 0
        result.append(item)
        prev = k
    return result


def dedupe_lines(text: str) -> str:
    """Дедуп подряд идущих одинаковых строк транскрипта (анти-галлюцинация).

    Если Whisper зациклился и повторяет одну и ту же строку — оставляем
    максимум одно повторение. Пустые строки отбрасываются.

    >>> dedupe_lines("а\\nа\\nа\\nб")
    'а\\nа\\nб'
    """
    lines = text.splitlines()
    kept = _dedupe_keep_one_repeat(lines, key=lambda s: s.strip())
    return "\n".join(kept)


def _dedupe_segments(segments: list[dict]) -> list[dict]:
    """Тот же алгоритм дедупа, но на уровне сегментов (сохраняет таймкоды
    первого/оставленного вхождения)."""
    return _dedupe_keep_one_repeat(segments, key=lambda s: (s.get("text") or "").strip())


# ─── склейка чанков ──────────────────────────────────────────────────────────


def merge_chunks(chunk_results: list[dict], overlap_sec: float) -> list[dict]:
    """Слить сегменты нескольких транскрибированных чанков в один список
    сегментов с глобальными таймкодами (чистая функция, без ffmpeg).

    Args:
        chunk_results: список `{"start": <секунда начала чанка в исходном
            файле>, "segments": [{"start", "end", "text"}, ...]}` — тайминги
            сегментов внутри `segments` локальны для своего чанка (0 —
            начало чанка).
        overlap_sec: длительность перекрытия между соседними чанками (сек).
            Сегменты чанка (кроме первого), целиком попадающие в первые
            `overlap_sec` секунд чанка, отбрасываются как дубликат хвоста
            предыдущего чанка.

    Returns:
        Список сегментов `{"start", "end", "text"}` с таймкодами в системе
        координат исходного (несечённого) файла.
    """
    merged: list[dict] = []
    for i, chunk in enumerate(chunk_results):
        offset = chunk["start"]
        for seg in chunk["segments"]:
            if i > 0 and seg["end"] <= overlap_sec:
                continue  # целиком в зоне перекрытия — уже покрыто предыдущим чанком
            merged.append(
                {
                    "start": seg["start"] + offset,
                    "end": seg["end"] + offset,
                    "text": seg["text"],
                }
            )
    return merged


def _split_wav_ffmpeg(
    audio_path: str, out_dir: Path, chunk_sec: int, overlap_sec: int, total_duration: float
) -> list[tuple[float, str]]:
    """Нарезать WAV на чанки по `chunk_sec` с перекрытием `overlap_sec` через
    ffmpeg. Возвращает список `(chunk_start_sec, chunk_file_path)`."""
    step = chunk_sec - overlap_sec
    chunks: list[tuple[float, str]] = []
    start = 0.0
    idx = 0
    while start < total_duration:
        length = min(chunk_sec, total_duration - start)
        out_path = out_dir / f"chunk_{idx:03d}.wav"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", str(start), "-i", str(audio_path),
            "-t", str(length), str(out_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        chunks.append((start, str(out_path)))
        idx += 1
        start += step
    return chunks


class Transcriber:
    """Транскрибирование аудио. По умолчанию MLX, fallback на openai-whisper."""

    def __init__(
        self,
        model_repo: str = MODEL_REPO,
        language: str = DEFAULT_KWARGS["language"],
        initial_prompt: Optional[str] = None,
        openai_model_name: str = "large-v3",
    ):
        """
        Args:
            model_repo: HF repo для mlx-whisper (см. MODEL_REPO).
            language: ISO 639-1, для русского "ru".
            initial_prompt: опциональная подсказка модели (глоссарий/имена);
                в v2 не использовался, оставлен как расширение.
            openai_model_name: имя модели для openai-whisper fallback (если
                mlx-whisper недоступен, например не Apple Silicon).
        """
        self.model_repo = model_repo
        self.language = language
        self.initial_prompt = initial_prompt

        # пробуем mlx сначала
        self._mlx = None
        try:
            import mlx_whisper  # type: ignore

            self._mlx = mlx_whisper
            logger.info(f"Transcriber: MLX-Whisper, модель {self.model_repo}, язык {language}")
            return
        except ImportError:
            logger.warning(
                "mlx-whisper не установлен — fallback на openai-whisper "
                "(будет в 5-10× медленнее). Установи: pip install mlx-whisper"
            )

        # fallback: openai-whisper
        self._openai = None
        self._openai_model = None
        try:
            import whisper  # type: ignore

            self._openai = whisper
            logger.info(f"Загрузка openai-whisper model={openai_model_name}…")
            self._openai_model = whisper.load_model(openai_model_name)
            logger.info("openai-whisper готов")
        except ImportError:
            logger.error(
                "Ни mlx-whisper, ни openai-whisper не установлены. "
                "Транскрипция работать НЕ будет."
            )

    def transcribe(self, audio_file: str) -> Optional[list[dict]]:
        """Транскрибировать файл. Возвращает [{start, end, text}, ...] или None.

        Длинные записи (> LONG_AUDIO_THRESHOLD_SEC) режутся ffmpeg-ом на
        чанки по CHUNK_SEC с перекрытием OVERLAP_SEC и склеиваются через
        merge_chunks(). Длительность читается через soundfile.info — без
        полной загрузки файла в память.
        """
        path = Path(audio_file)
        if not path.exists():
            logger.error(f"Файл не найден: {audio_file}")
            return None

        channels = 1
        try:
            info = sf.info(str(path))
            duration = float(info.duration)
            channels = int(info.channels)
        except Exception as e:
            logger.warning(f"Не удалось прочитать длительность {audio_file}: {e}")
            duration = 0.0

        try:
            if channels == 2:
                # Стерео от CaptureEngine: ch0 = микрофон, ch1 = системный звук
                # — раздельная расшифровка каналов даёт деление на спикеров
                # без диаризации («Спикер 1» = я, «Спикер 2» = собеседники).
                return self._transcribe_stereo(str(path), duration)
            segments = self._transcribe_mono(str(path), duration)
            if segments is None:
                return None
            return _dedupe_segments(segments)
        except Exception as e:
            logger.error(f"Ошибка транскрипции: {e}", exc_info=True)
            return None

    def _transcribe_mono(self, audio_path: str, duration: float) -> Optional[list[dict]]:
        if duration > LONG_AUDIO_THRESHOLD_SEC:
            return self._transcribe_chunked(audio_path, duration)
        return self._transcribe_one(audio_path)

    # Метки каналов стерео-записи (порядок = порядок каналов в WAV).
    STEREO_SPEAKERS = ("Спикер 1", "Спикер 2")

    def _transcribe_stereo(self, audio_path: str, duration: float) -> Optional[list[dict]]:
        """Разложить стерео на mic.wav/sys.wav (ffmpeg channelsplit), расшифровать
        каждый канал отдельно и слить сегменты по общему таймлайну (каналы
        синхронны — таймкоды не сдвигаются)."""
        with tempfile.TemporaryDirectory(prefix="meetmind_stereo_") as tmp:
            mic_path = Path(tmp) / "mic.wav"
            sys_path = Path(tmp) / "sys.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", audio_path,
                 "-filter_complex", "[0:a]channelsplit=channel_layout=stereo[l][r]",
                 "-map", "[l]", "-ar", "16000", str(mic_path),
                 "-map", "[r]", "-ar", "16000", str(sys_path)],
                check=True, capture_output=True)
            merged: list[dict] = []
            for speaker, channel_path in zip(self.STEREO_SPEAKERS,
                                             (mic_path, sys_path)):
                segments = self._transcribe_mono(str(channel_path), duration)
                if segments is None:
                    continue
                merged.extend(
                    dict(s, speaker=speaker) for s in _dedupe_segments(segments))
        if not merged:
            return None
        merged.sort(key=lambda s: s.get("start") or 0.0)
        return merged

    def _transcribe_chunked(self, audio_path: str, duration: float) -> list[dict]:
        with tempfile.TemporaryDirectory(prefix="meetmind_chunks_") as tmp:
            tmp_dir = Path(tmp)
            pieces = _split_wav_ffmpeg(audio_path, tmp_dir, CHUNK_SEC, OVERLAP_SEC, duration)
            logger.info(
                f"Долгая запись ({duration / 60:.1f} мин) → {len(pieces)} чанков "
                f"по {CHUNK_SEC // 60} мин, перекрытие {OVERLAP_SEC}с"
            )
            chunk_results = []
            for i, (start, chunk_path) in enumerate(pieces):
                logger.info(f"Транскрибирую чанк {i + 1}/{len(pieces)} (start={start:.0f}с)…")
                segs = self._transcribe_one(chunk_path) or []
                chunk_results.append({"start": start, "segments": segs})
            return merge_chunks(chunk_results, OVERLAP_SEC)

    def _transcribe_one(self, audio_path: str) -> Optional[list[dict]]:
        if self._mlx is not None:
            return self._transcribe_mlx(audio_path)
        elif getattr(self, "_openai_model", None) is not None:
            return self._transcribe_openai(audio_path)
        else:
            logger.error("Нет рабочего backend для транскрипции")
            return None

    def _transcribe_mlx(self, audio_path: str) -> list[dict]:
        logger.info(f"MLX-Whisper: транскрибирую {audio_path}…")
        kwargs = dict(DEFAULT_KWARGS)
        kwargs["language"] = self.language
        if self.initial_prompt:
            kwargs["initial_prompt"] = self.initial_prompt
        result = self._mlx.transcribe(
            audio_path,
            path_or_hf_repo=self.model_repo,
            verbose=False,
            **kwargs,
        )
        segments = []
        for seg in result.get("segments", []):
            segments.append(
                {
                    "start": float(seg["start"]),
                    "end": float(seg["end"]),
                    "text": str(seg["text"]).strip(),
                }
            )
        logger.info(f"MLX-Whisper готов: {len(segments)} сегментов")
        return segments

    def _transcribe_openai(self, audio_path: str) -> list[dict]:
        logger.info(f"openai-whisper: транскрибирую {audio_path}…")
        kwargs = dict(DEFAULT_KWARGS)
        kwargs["language"] = self.language
        if self.initial_prompt:
            kwargs["initial_prompt"] = self.initial_prompt
        result = self._openai_model.transcribe(
            audio_path,
            fp16=False,  # на Mac fp16 нестабилен
            **kwargs,
        )
        segments = []
        for seg in result.get("segments", []):
            segments.append(
                {
                    "start": float(seg["start"]),
                    "end": float(seg["end"]),
                    "text": str(seg["text"]).strip(),
                }
            )
        logger.info(f"openai-whisper готов: {len(segments)} сегментов")
        return segments

    @staticmethod
    def get_full_text(segments: list[dict]) -> str:
        return "\n".join(s["text"] for s in segments if s.get("text"))
