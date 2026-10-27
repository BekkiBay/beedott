"""
Транскрипция через MLX-Whisper (Metal на Apple Silicon).

mlx-whisper в 5-10× быстрее openai-whisper и использует Metal/Apple Neural Engine.
При отсутствии mlx-whisper в системе используется openai-whisper как fallback.

Анти-галлюцинационные дефолты (DEFAULT_KWARGS) и дедуп повторов (dedupe_lines) —
перенос из первого прототипа пайплайна (флаги, накопленные вручную на реальных
русскоязычных встречах), см. SPEC_v3 §8 и PLAN_v3 Task 1.4.

Перед Whisper тишина вырезается Silero VAD: на тишине модель выдумывает
«Продолжение следует…» и «Спасибо.». Речь склеивается в куски до 30 с, и
каждый кусок декодируется отдельно со словарём терминов (terms.txt в папке
приложения). Голоса собеседников, попавшие в микрофон из динамиков, убирает
drop_echo. Замеры на рабочих встречах (2026-10-27): с VAD выдуманных фраз
0 вместо 17 на 40 минут, со словарём в каждом куске термины в правильном
написании встречаются втрое чаще.
"""
import difflib
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)


# ─── анти-галлюцинационные дефолты ─────────────────────────────────────────
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

# ─── VAD ─────────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
# Кусок речи на один вызов Whisper — одно окно модели (30 с). Отдельный вызов
# на кусок нужен, чтобы словарь попадал в каждое окно: внутри одного вызова
# mlx-whisper при condition_on_previous_text=False отдаёт initial_prompt
# только первому окну.
MAX_CHUNK_SEC = 30
# Параметры faster-whisper, подобранные под Whisper: режем на паузах от 2 с,
# 400 мс поля вокруг речи. max_speech_duration_s на секунду меньше окна, чтобы
# кусок с полями не вылезал во второе, почти пустое окно.
VAD_KWARGS = dict(
    threshold=0.5,
    min_speech_duration_ms=250,
    min_silence_duration_ms=2000,
    speech_pad_ms=400,
    max_speech_duration_s=MAX_CHUNK_SEC - 1,
)

# ─── словарь терминов ────────────────────────────────────────────────────────
GLOSSARY_FILENAME = "terms.txt"  # в app_dir(): одна строка — одно написание


# ─── дедуп повторов ──────────────────────────────────────────────────────────


def _dedupe_keep_one_repeat(items: list, key: Callable = lambda x: x) -> list:
    """Общий алгоритм дедупа: из подряд идущих одинаковых (по `key`) элементов
    оставляем максимум два (т.е. одно повторение), остальные дропаем. Элементы
    с пустым `key` (после strip у строк) отбрасываются целиком.
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


# ─── словарь терминов ────────────────────────────────────────────────────────


def load_glossary(path: Path) -> list[str]:
    """Термины из файла: одна строка — одно каноническое написание, строки с
    # — комментарии. Нет файла — пустой список."""
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


def glossary_prompt(terms: list[str]) -> Optional[str]:
    """initial_prompt для Whisper. Только сами термины: служебные слова вроде
    «Термины:» модель дописывает в начало расшифровки. Whisper читает не
    больше 223 токенов подсказки — длинный словарь обрежется с начала."""
    return ", ".join(terms) + "." if terms else None


# ─── VAD: вырезать тишину, склеить речь, вернуть таймкоды ───────────────────

_VAD_MODEL = None


def speech_spans(audio: np.ndarray) -> list[tuple[int, int]]:
    """Куски речи [(начало, конец)] в сэмплах по Silero VAD."""
    global _VAD_MODEL
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad
    if _VAD_MODEL is None:
        _VAD_MODEL = load_silero_vad()
    ts = get_speech_timestamps(torch.from_numpy(audio), _VAD_MODEL,
                               sampling_rate=SAMPLE_RATE, **VAD_KWARGS)
    return [(int(t["start"]), int(t["end"])) for t in ts]


def group_spans(spans: list[tuple[int, int]], max_len: int) -> list[list[tuple[int, int]]]:
    """Сложить подряд идущие куски речи в группы, где речи не больше `max_len`
    сэмплов. Кусок длиннее лимита идёт отдельной группой."""
    groups: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    cur_len = 0
    for s, e in spans:
        if cur and cur_len + (e - s) > max_len:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append((s, e))
        cur_len += e - s
    if cur:
        groups.append(cur)
    return groups


def concat_spans(audio: np.ndarray, spans: list[tuple[int, int]]):
    """Склеить куски речи подряд. Возвращает сигнал и карту для пересчёта
    таймкодов: [(начало в склейке, начало в оригинале, длина)] в сэмплах."""
    pieces, mapping, pos = [], [], 0
    for s, e in spans:
        pieces.append(audio[s:e])
        mapping.append((pos, s, e - s))
        pos += e - s
    out = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
    return out, mapping


def restore_time(t: float, mapping) -> float:
    """Секунда в склейке → секунда в исходном файле."""
    x = t * SAMPLE_RATE
    for cat_start, orig_start, length in mapping:
        if x <= cat_start + length:
            return (orig_start + max(0.0, x - cat_start)) / SAMPLE_RATE
    if mapping:
        _, orig_start, length = mapping[-1]
        return (orig_start + length) / SAMPLE_RATE
    return t


def _load_audio(path: str) -> np.ndarray:
    """Моно 16 кГц float32 через ffmpeg (любой формат на входе)."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path,
         "-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        check=True, capture_output=True).stdout
    return np.frombuffer(out, np.float32).copy()


# ─── фильтр выдуманных фраз ──────────────────────────────────────────────────

# Титры из обучающих субтитров, которые Whisper выдаёт на тишине и шуме.
HALLUCINATION_RE = re.compile(
    r"(?i)^\W*(продолжение следует|субтитры\b.*|редактор субтитров.*|"
    r"спасибо за (просмотр|внимание)|подписывайтесь на (наш )?канал.*|"
    r".*dimatorzok.*|.*amara\.org.*)\W*$")
THANKS_RE = re.compile(r"(?i)^\W*спасибо\W*$")
REPEAT_RE = re.compile(r"(?i)\b(\w+)(?:[\s,.!?…-]+\1\b){3,}")
# Буква не из латиницы и не из кириллицы: каша на шуме («государству석 144ほど»).
FOREIGN_LETTER_RE = re.compile(r"[^\W\d_\u0000-\u024F\u0400-\u04FF]")


def _echoes_glossary(text: str, terms: list[str]) -> bool:
    """Сегмент — пересказ словаря: 4+ термина и почти ничего кроме них."""
    low = text.lower()
    hits = [t for t in terms if t.lower() in low]
    if len(hits) < 4:
        return False
    for t in sorted(hits, key=len, reverse=True):
        low = low.replace(t.lower(), " ")
    return len(re.findall(r"\w+", low)) <= 4


def filter_hallucinations(segments: list[dict], terms: list[str]) -> list[dict]:
    """Убрать выдуманные фразы: титры, «Спасибо.» на тишине, пересказ
    словаря, кашу из чужих алфавитов; слово, повторённое 4+ раза подряд, оставить дважды."""
    out = []
    for s in segments:
        text = s["text"]
        if HALLUCINATION_RE.match(text) or FOREIGN_LETTER_RE.search(text):
            continue
        # «Спасибо.» одним сегментом — частая выдумка на тишине; оставляем,
        # только если модель уверена, что там речь.
        if THANKS_RE.match(text) and s.get("no_speech_prob", 0.0) > 0.3:
            continue
        if terms and _echoes_glossary(text, terms):
            continue
        text = REPEAT_RE.sub(r"\1 \1", text).strip()
        if text:
            out.append(dict(s, text=text))
    return out


# ─── эхо: собеседники из динамиков попадают в микрофон ───────────────────────

ECHO_SLACK_SEC = 1.5  # эхо звучит одновременно с оригиналом; ответ, повторяющий
                      # слова вопроса, начинается позже
ECHO_MIN_SHARE = 0.6  # доля слов микрофонной реплики, найденных в системном канале
ECHO_MIN_MATCHED = 3  # и не меньше стольких слов: «да … это» совпадает случайно


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def _matched_in_window(mic_words: list[str], sys_words: list[str]) -> int:
    """Сколько слов микрофонной реплики по порядку нашлось в самом похожем
    компактном отрывке системного текста (окно чуть длиннее реплики). Окно
    нужно, чтобы слова, разбросанные по длинной чужой реплике, не сложились
    в ложное совпадение."""
    n = len(mic_words)
    width = n + max(2, n // 2)
    best = 0
    for i in range(max(1, len(sys_words) - width + 1)):
        window = sys_words[i:i + width]
        blocks = difflib.SequenceMatcher(None, mic_words, window, autojunk=False).get_matching_blocks()
        best = max(best, sum(b.size for b in blocks))
        if best == n:
            break
    return best


def drop_echo(mic_segments: list[dict], sys_segments: list[dict]) -> list[dict]:
    """Убрать из микрофонного канала реплики, которые повторяют системный
    канал в то же время: без наушников микрофон слышит собеседников из
    динамиков, и каждая их фраза попадала в расшифровку дважды. Оставляем
    версию системного канала — она чище.

    Реплика от 3 слов — эхо, если не меньше ECHO_MIN_SHARE её слов (и не
    меньше ECHO_MIN_MATCHED) по порядку нашлись в компактном отрывке
    системных реплик рядом (±ECHO_SLACK_SEC). Короткая («Да.», «Вот.») —
    только при точном совпадении текста и начала (±1 с): своё «да» в ответ
    на чужое не должно пропасть."""
    kept = []
    for m in mic_segments:
        mw = _words(m["text"])
        near = [s for s in sys_segments
                if s["start"] < m["end"] + ECHO_SLACK_SEC
                and s["end"] > m["start"] - ECHO_SLACK_SEC]
        if mw and near:
            if len(mw) >= 3:
                sw = [w for s in near for w in _words(s["text"])]
                matched = _matched_in_window(mw, sw)
                if matched >= ECHO_MIN_MATCHED and matched / len(mw) >= ECHO_MIN_SHARE:
                    continue
            elif any(_words(s["text"]) == mw and abs(s["start"] - m["start"]) <= 1.0
                     for s in near):
                continue
        kept.append(m)
    return kept


class Transcriber:
    """Транскрибирование аудио. По умолчанию MLX, fallback на openai-whisper."""

    def __init__(
        self,
        model_repo: str = MODEL_REPO,
        language: str = DEFAULT_KWARGS["language"],
        initial_prompt: Optional[str] = None,
        openai_model_name: str = "large-v3",
        terms: Optional[list[str]] = None,
    ):
        """
        Args:
            model_repo: HF repo для mlx-whisper (см. MODEL_REPO).
            language: ISO 639-1, для русского "ru".
            initial_prompt: подсказка модели; по умолчанию собирается из
                словаря терминов.
            openai_model_name: имя модели для openai-whisper fallback (если
                mlx-whisper недоступен, например не Apple Silicon).
            terms: словарь терминов; по умолчанию app_dir()/terms.txt.
        """
        from daemon.paths import app_dir

        self.model_repo = model_repo
        self.language = language
        self.terms = terms if terms is not None else load_glossary(app_dir() / GLOSSARY_FILENAME)
        self.initial_prompt = (initial_prompt if initial_prompt is not None
                               else glossary_prompt(self.terms))
        if self.terms:
            logger.info(f"Transcriber: словарь из {len(self.terms)} терминов")

        try:
            import silero_vad  # type: ignore  # noqa: F401
            self.use_vad = True
        except ImportError:
            self.use_vad = False
            logger.warning("silero-vad не установлен — расшифровка без VAD, "
                           "на тишине возможны выдуманные фразы")

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

    def transcribe(self, audio_file: str) -> list[dict]:
        """Транскрибировать файл. Возвращает [{start, end, text}, ...]; пустой
        список — речи нет. Ошибки (нет файла, нет ffmpeg, сбой модели) не
        глушатся, а летят наверх с причиной: раньше они превращались в None, и в
        интерфейсе было «'NoneType' object is not iterable» (продукт P6).

        Длинные записи (> LONG_AUDIO_THRESHOLD_SEC) режутся ffmpeg-ом на
        чанки по CHUNK_SEC с перекрытием OVERLAP_SEC и склеиваются через
        merge_chunks(). Длительность читается через soundfile.info — без
        полной загрузки файла в память.
        """
        path = Path(audio_file)
        if not path.exists():
            raise FileNotFoundError(f"аудиофайл не найден: {audio_file}")

        channels = 1
        try:
            info = sf.info(str(path))
            duration = float(info.duration)
            channels = int(info.channels)
        except Exception as e:
            logger.warning(f"Не удалось прочитать длительность {audio_file}: {e}")
            duration = 0.0

        if channels == 2:
            # Стерео от CaptureEngine: ch0 = микрофон, ch1 = системный звук
            # — раздельная расшифровка каналов даёт деление на спикеров
            # без диаризации («Спикер 1» = я, «Спикер 2» = собеседники).
            return self._transcribe_stereo(str(path), duration)
        return _dedupe_segments(self._transcribe_mono(str(path), duration))

    def _transcribe_mono(self, audio_path: str, duration: float) -> list[dict]:
        if duration > LONG_AUDIO_THRESHOLD_SEC:
            return self._transcribe_chunked(audio_path, duration)
        return self._transcribe_one(audio_path)

    # Метки каналов стерео-записи (порядок = порядок каналов в WAV).
    STEREO_SPEAKERS = ("Спикер 1", "Спикер 2")

    def _transcribe_stereo(self, audio_path: str, duration: float) -> list[dict]:
        """Разложить стерео на mic.wav/sys.wav (ffmpeg channelsplit), расшифровать
        каждый канал отдельно и слить сегменты по общему таймлайну (каналы
        синхронны — таймкоды не сдвигаются). Эхо собеседников в микрофоне
        убирается drop_echo."""
        with tempfile.TemporaryDirectory(prefix="meetmind_stereo_") as tmp:
            mic_path = Path(tmp) / "mic.wav"
            sys_path = Path(tmp) / "sys.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", audio_path,
                 "-filter_complex", "[0:a]channelsplit=channel_layout=stereo[l][r]",
                 "-map", "[l]", "-ar", "16000", str(mic_path),
                 "-map", "[r]", "-ar", "16000", str(sys_path)],
                check=True, capture_output=True)
            mic, sys_ = (_dedupe_segments(self._transcribe_mono(str(p), duration))
                         for p in (mic_path, sys_path))
        mic_kept = drop_echo(mic, sys_)
        if len(mic_kept) < len(mic):
            logger.info(f"Эхо: из микрофона убрано {len(mic) - len(mic_kept)} "
                        f"из {len(mic)} реплик, повторяющих системный звук")
        mic_speaker, sys_speaker = self.STEREO_SPEAKERS
        merged = ([dict(s, speaker=mic_speaker) for s in mic_kept]
                  + [dict(s, speaker=sys_speaker) for s in sys_])
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

    def _transcribe_one(self, audio_path: str) -> list[dict]:
        """Один канал/файл: VAD → куски речи до MAX_CHUNK_SEC → Whisper на
        каждый кусок со словарём → таймкоды обратно на шкалу файла → фильтр
        выдуманных фраз."""
        audio = _load_audio(audio_path)
        if getattr(self, "use_vad", False):
            groups = group_spans(speech_spans(audio), MAX_CHUNK_SEC * SAMPLE_RATE)
            speech = sum(e - s for g in groups for s, e in g) / SAMPLE_RATE
            logger.info(f"VAD {Path(audio_path).name}: речи {speech:.0f} с из "
                        f"{len(audio) / SAMPLE_RATE:.0f} с, {len(groups)} кусков")
        else:
            groups = [[(0, len(audio))]] if len(audio) else []
        segments = []
        for group in groups:
            chunk, mapping = concat_spans(audio, group)
            for s in self._decode(chunk, self.initial_prompt):
                segments.append(dict(s, start=restore_time(s["start"], mapping),
                                     end=restore_time(s["end"], mapping)))
        segments = filter_hallucinations(segments, getattr(self, "terms", []))
        logger.info(f"Whisper {Path(audio_path).name}: {len(segments)} сегментов")
        return [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segments]

    def _decode(self, audio: np.ndarray, prompt: Optional[str]) -> list[dict]:
        """Whisper на сигнале 16 кГц: [{start, end, text, no_speech_prob}]."""
        kwargs = dict(DEFAULT_KWARGS)
        kwargs["language"] = self.language
        if prompt:
            kwargs["initial_prompt"] = prompt
        if self._mlx is not None:
            # verbose=None — без прогресс-бара: вызовов теперь десятки на файл
            result = self._mlx.transcribe(
                audio, path_or_hf_repo=self.model_repo, verbose=None, **kwargs)
        elif getattr(self, "_openai_model", None) is not None:
            result = self._openai_model.transcribe(
                audio, fp16=False, **kwargs)  # на Mac fp16 нестабилен
        else:
            raise RuntimeError("нет backend для транскрипции: не установлены ни "
                               "mlx-whisper, ни openai-whisper")
        return [{"start": float(seg["start"]), "end": float(seg["end"]),
                 "text": str(seg["text"]).strip(),
                 "no_speech_prob": float(seg.get("no_speech_prob", 0.0))}
                for seg in result.get("segments", [])]

    @staticmethod
    def get_full_text(segments: list[dict]) -> str:
        return "\n".join(s["text"] for s in segments if s.get("text"))
