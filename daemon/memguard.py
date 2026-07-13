"""Guard: не грузить тяжёлую модель, если доступной памяти мало (иначе swap/OOM)."""
import psutil

# summarize: ~5GB весов Vikhr-8B Q4 + KV-cache при num_ctx=16384 + запас.
# Цифры — стартовые: в M2 Verify замерить реальный пик RSS ollama и скорректировать.
REQUIRED = {"transcribe": 4 * 2**30, "summarize": 10 * 2**30, "diarize": 2 * 2**30}


def available() -> int: return psutil.virtual_memory().available


class NotEnoughMemory(RuntimeError): ...


def require(stage: str) -> None:
    need = REQUIRED.get(stage, 0)
    if available() < need:
        raise NotEnoughMemory(f"{stage}: need {need>>30}GB, have {available()>>30}GB")
