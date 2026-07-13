"""Guard: не грузить тяжёлую модель, если доступной памяти мало (иначе swap/OOM)."""
import os

import psutil

# summarize: ~5GB весов Vikhr-8B Q4 + KV-cache при num_ctx=16384 + запас.
# Цифры — стартовые: в M2 Verify замерить реальный пик RSS ollama и скорректировать.
# Override: MEETMIND_MEMGUARD_GB_SUMMARIZE=4 (аналогично _TRANSCRIBE/_DIARIZE) —
# ollama может быть уже резидентна, тогда порог завышен.
_DEFAULT_GB = {"transcribe": 4, "summarize": 10, "diarize": 2}


def _required(stage: str) -> int:
    gb = os.environ.get(f"MEETMIND_MEMGUARD_GB_{stage.upper()}")
    return int(gb if gb is not None else _DEFAULT_GB.get(stage, 0)) * 2**30


def available() -> int:
    return psutil.virtual_memory().available


class NotEnoughMemory(RuntimeError): ...


def require(stage: str) -> None:
    need = _required(stage)
    if available() < need:
        raise NotEnoughMemory(f"{stage}: need {need >> 30}GB, have {available() >> 30}GB")
