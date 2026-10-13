"""Guard: не грузить тяжёлую модель, если доступной памяти мало (иначе swap/OOM)."""
import os

import psutil

# summarize в списке нет: саммари делает Claude в облаке, локальной памяти не ест.
# Override: MEETMIND_MEMGUARD_GB_TRANSCRIBE=6 (аналогично _DIARIZE).
_DEFAULT_GB = {"transcribe": 4, "diarize": 2}


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
