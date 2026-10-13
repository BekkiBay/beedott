"""
Рендер JSON-фоллоу-апа (схема SPEC_v3 §7) в markdown для sinks.

Разделы: Коротко / Что обсуждалось / Договорённости / Задачи (`• что — кто, до
пт, 16.10`) / Открытые вопросы — пустые не показываются (правка 2026-10-12:
заметку должно хотеться читать). Плюс подпись движка (`☁️ claude-code:opus`,
SPEC §7) и дисклеймер о локальном хранении (перенос daemon/sinks/base.py:62-63).
"""
from datetime import date, datetime
from typing import Optional

# Перенос дословно из daemon/sinks/base.py:62-63.
DISCLAIMER = (
    "_Запись хранится локально. Запись разговора без согласия собеседников "
    "может нарушать местные законы — ответственность на пользователе._"
)


def _engine_signature(engine: str) -> str:
    icon = "☁️" if "claude" in engine.lower() else "🖥"
    return f"{icon} {engine}"


_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _bullets(items: list[str]) -> str:
    return "\n".join(f"• {item}" for item in items)


def _human_deadline(deadline: Optional[str]) -> str:
    """«2026-10-16» → «до пт, 16.10»; «asap» → «срочно»; свободный текст — как
    есть. Продукт P9: без срока — пусто, а не ложная срочность."""
    text = (deadline or "").strip()
    if not text:
        return ""
    if text.lower() == "asap":
        return "срочно"
    try:
        d = date.fromisoformat(text)
    except ValueError:
        return text
    return f"до {_WEEKDAYS[d.weekday()]}, {d:%d.%m}"


def _action_items_block(items: list[dict]) -> str:
    lines = []
    for it in items:
        owner = it.get("owner") or "не назначен"
        deadline = _human_deadline(it.get("deadline"))
        lines.append(f"• {it.get('task', '')} — {owner}" + (f", {deadline}" if deadline else ""))
    return "\n".join(lines)


def render_followup(
    followup: dict,
    engine: str,
    title: str,
    duration_min: float,
    when: Optional[datetime] = None,
    revision: int = 1,
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
    if revision > 1:
        # Продукт P12: пересобранное саммари не должно выглядеть как дубль.
        parts = [f"🔄 Обновлённая версия фоллоу-апа (№{revision})", ""] + parts

    if followup.get("degraded"):
        parts.append(
            "> ⚠️ Не удалось разобрать структурированный ответ модели — "
            "ниже сырой текст без разметки по разделам."
        )
        parts.append("")

    parts += ["**Коротко**", (followup.get("context") or "").strip() or "нет", ""]
    sections = [
        ("Что обсуждалось", _bullets(followup.get("discussed") or [])),
        ("Договорённости", _bullets(followup.get("decisions") or [])),
        ("Задачи", _action_items_block(followup.get("action_items") or [])),
        ("Открытые вопросы", _bullets(followup.get("open_questions") or [])),
    ]
    for heading, body in sections:
        if body:
            parts += [f"**{heading}**", body, ""]
    parts += ["---", _engine_signature(engine), DISCLAIMER]
    return "\n".join(parts)
