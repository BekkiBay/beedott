"""
Рендер JSON-фоллоу-апа (схема SPEC_v3 §7) в markdown для sinks.

Формат — перенос v2 `USER_PROMPT_TEMPLATE` (daemon/summarizer.py v2): Контекст /
Что обсуждалось / Договорённости / Action items (`📅 дедлайн — что — кто`) /
Открытые вопросы, плюс подпись движка (`🖥 vikhr-8b` / `☁️ claude sonnet`, SPEC §7)
и дисклеймер о локальном хранении (перенос daemon/sinks/base.py:62-63).
"""
from datetime import datetime
from typing import Optional

# Перенос дословно из daemon/sinks/base.py:62-63.
DISCLAIMER = (
    "_Запись хранится локально. Запись разговора без согласия собеседников "
    "может нарушать местные законы — ответственность на пользователе._"
)


def _engine_signature(engine: str) -> str:
    icon = "☁️" if "claude" in engine.lower() else "🖥"
    return f"{icon} {engine}"


def _bullets(items: list[str]) -> str:
    if not items:
        return "нет"
    return "\n".join(f"• {item}" for item in items)


def _action_items_block(items: list[dict]) -> str:
    if not items:
        return "нет"
    lines = []
    for it in items:
        deadline = it.get("deadline") or "asap"
        task = it.get("task", "")
        owner = it.get("owner", "")
        lines.append(f"📅 {deadline} — {task} — {owner}")
    return "\n".join(lines)


def render_followup(
    followup: dict,
    engine: str,
    title: str,
    duration_min: float,
    when: Optional[datetime] = None,
) -> str:
    """Собрать markdown-заметку из JSON-фоллоу-апа (SPEC_v3 §7).

    Args:
        followup: dict со схемой §7 (context/discussed/decisions/
            action_items/open_questions); опциональный ключ `degraded` (см.
            daemon/summarizer.py) добавляет предупреждение в начало.
        engine: подпись движка, например "vikhr-8b" или "claude-sonnet".
        title: заголовок встречи.
        duration_min: длительность встречи в минутах.
        when: время начала встречи (для шапки); если не задано — не рендерится.
    """
    header = f"**{title}**"
    if when is not None:
        header += f" · {when.strftime('%d.%m.%Y, %H:%M')}"
    header += f" ({duration_min} мин)"

    parts: list[str] = [header, ""]

    if followup.get("degraded"):
        parts.append(
            "> ⚠️ Не удалось разобрать структурированный ответ модели — "
            "ниже сырой текст без разметки по разделам."
        )
        parts.append("")

    parts += [
        "**Контекст**",
        (followup.get("context") or "").strip() or "нет",
        "",
        "**Что обсуждалось**",
        _bullets(followup.get("discussed") or []),
        "",
        "**Договорённости**",
        _bullets(followup.get("decisions") or []),
        "",
        "**Action items**",
        _action_items_block(followup.get("action_items") or []),
        "",
        "**Открытые вопросы**",
        _bullets(followup.get("open_questions") or []),
        "",
        "---",
        _engine_signature(engine),
        DISCLAIMER,
    ]
    return "\n".join(parts)
