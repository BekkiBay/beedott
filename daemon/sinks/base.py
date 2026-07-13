"""Базовый интерфейс sink — точка доставки готового фоллоу-апа."""
from dataclasses import dataclass
from typing import Optional, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from daemon.session import Session
    from daemon.summarizer import FollowupResult


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

    def send(self, followup: "FollowupResult", session: "Session") -> SinkResult: ...


def render_combined_markdown(followup: "FollowupResult", session: "Session") -> str:
    """Собрать единый markdown-документ с обоими вариантами фоллоу-апа.

    Используется большинством sinks для записи на диск/Obsidian/Notion.
    """
    parts: list[str] = []

    # Шапка
    parts.append(f"# {session.title or 'Без названия'}")
    parts.append("")
    parts.append(
        f"_Сгенерировано MeetMind · {session.start_time.strftime('%d.%m.%Y %H:%M')} · "
        f"{session.get_duration_minutes()} мин_"
    )
    parts.append("")
    if followup.errors:
        parts.append("> ⚠️  Ошибки при генерации:")
        for err in followup.errors:
            parts.append(f"> - {err}")
        parts.append("")

    if followup.cloud:
        parts.append(f"## ☁️  Claude ({followup.cloud_engine}, {followup.cloud_duration_s:.1f}с)")
        parts.append("")
        parts.append(followup.cloud)
        parts.append("")

    if followup.local:
        parts.append(f"## 🖥️  Локально ({followup.local_engine}, {followup.local_duration_s:.1f}с)")
        parts.append("")
        parts.append(followup.local)
        parts.append("")

    parts.append("---")
    parts.append("_Запись хранится локально. Запись разговора без согласия собеседников "
                 "может нарушать местные законы — ответственность на пользователе._")
    return "\n".join(parts)
