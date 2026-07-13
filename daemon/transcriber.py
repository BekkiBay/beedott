"""
Транскрипция через MLX-Whisper (Metal на Apple Silicon).

mlx-whisper в 5-10× быстрее openai-whisper и использует Metal/Apple Neural Engine.
При отсутствии mlx-whisper в системе используется openai-whisper как fallback.
"""
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# Мапа "красивое имя модели" → "путь к mlx модели" / "имя openai-whisper модели"
_MLX_MODEL_MAP = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large": "mlx-community/whisper-large-v3-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}


class Transcriber:
    """Транскрибирование аудио. По умолчанию MLX, fallback на openai-whisper."""

    def __init__(self, model_name: str = "large-v3", language: str = "ru"):
        """
        Args:
            model_name: tiny / base / small / medium / large / large-v3 / turbo
            language: ISO 639-1, для русского "ru"
        """
        self.model_name = model_name
        self.language = language

        # пробуем mlx сначала
        self._mlx = None
        try:
            import mlx_whisper  # type: ignore

            self._mlx = mlx_whisper
            self._mlx_path = _MLX_MODEL_MAP.get(model_name, model_name)
            logger.info(
                f"Transcriber: MLX-Whisper, модель {self._mlx_path}, язык {language}"
            )
            return
        except ImportError:
            logger.warning(
                "mlx-whisper не установлен — fallback на openai-whisper "
                "(будет в 5-10× медленнее). Установи: pip install mlx-whisper"
            )

        # fallback: openai-whisper
        try:
            import whisper  # type: ignore

            self._openai = whisper
            logger.info(f"Загрузка openai-whisper model={model_name}…")
            self._openai_model = whisper.load_model(model_name)
            logger.info("openai-whisper готов")
        except ImportError:
            logger.error(
                "Ни mlx-whisper, ни openai-whisper не установлены. "
                "Транскрипция работать НЕ будет."
            )
            self._openai = None
            self._openai_model = None

    def transcribe(self, audio_file: str) -> Optional[list[dict]]:
        """Транскрибировать файл. Возвращает [{start, end, text}, ...] или None."""
        path = Path(audio_file)
        if not path.exists():
            logger.error(f"Файл не найден: {audio_file}")
            return None

        try:
            if self._mlx is not None:
                return self._transcribe_mlx(str(path))
            elif getattr(self, "_openai_model", None) is not None:
                return self._transcribe_openai(str(path))
            else:
                logger.error("Нет рабочего backend для транскрипции")
                return None
        except Exception as e:
            logger.error(f"Ошибка транскрипции: {e}", exc_info=True)
            return None

    def _transcribe_mlx(self, audio_path: str) -> list[dict]:
        logger.info(f"MLX-Whisper: транскрибирую {audio_path}…")
        # mlx_whisper.transcribe умеет word_timestamps, но для followup нам нужны
        # только сегментные таймкоды — экономим время
        result = self._mlx.transcribe(
            audio_path,
            path_or_hf_repo=self._mlx_path,
            language=self.language,
            verbose=False,
            condition_on_previous_text=True,
            temperature=0.0,
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
        result = self._openai_model.transcribe(
            audio_path,
            language=self.language,
            fp16=False,  # на Mac fp16 нестабилен
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
        return " ".join(s["text"] for s in segments if s.get("text"))
