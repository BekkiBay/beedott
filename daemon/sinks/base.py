"""Базовый интерфейс sink — точка доставки готового (уже отрендеренного) markdown
фоллоу-апа. Рендер markdown из JSON-саммари делает daemon/render.py — sink получает
готовый текст и просто доставляет его."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol


@dataclass
class SinkResult:
    """Результат отправки в sink."""

    name: str
    success: bool
    detail: str = ""  # путь к файлу, ID сообщения, текст ошибки
    url: Optional[str] = None


class BaseSink(Protocol):
    """Интерфейс — все sinks реализуют этот метод."""

    name: str

    def send(self, text: str, *, title: str, when: datetime,
             session_id: str) -> SinkResult: ...
