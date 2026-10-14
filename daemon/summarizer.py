"""
Фоллоу-ап встречи: промпт, схема и валидация JSON по SPEC_v3 §7.

Саммари делает Claude (модель — в «Настройке…»), способ вызова — config.SUMMARY_BACKEND:
    claude_cli — `claude -p` по Max-подписке (daemon/claude_cli.py)
    api        — Anthropic API по ANTHROPIC_API_KEY (daemon/anthropic_api.py)

Здесь — общее для обоих: промпт, JSON-схема ответа, разбор и проверка.
Транскрипт уходит в модель целиком одним запросом (контекст — 1M токенов),
без map-reduce. Инструкция фоллоу-апа — своя из followup_instructions.md в
папке приложения или стандартная; формат JSON добавляется всегда. Рендер в markdown — отдельно, в daemon/render.py.
"""
import json
import logging
from pathlib import Path

from daemon.paths import app_dir

logger = logging.getLogger(__name__)

# Роль — фиксированная. Всё, что определяет фоллоу-ап (стиль, разделы, правила),
# — в инструкции после транскрипта: на длинном контексте указания после
# документа соблюдаются лучше. Инструкцию пользователь правит в «Настройке…».
SYSTEM_PROMPT = "Ты пишешь фоллоу-ап по рабочей встрече по инструкции из сообщения пользователя."

DEFAULT_INSTRUCTIONS = """Ты пишешь фоллоу-ап по рабочей встрече для участника, который хочет за минуту понять главное: о чём договорились, что делать ему и другим, что осталось открытым. Фоллоу-ап приходит в Telegram — его должно хотеться дочитать.

Пиши по-русски простым живым языком, как толковый коллега пишет в рабочий чат: короткие законченные предложения, без канцелярита и телеграфного стиля. Не пересказывай встречу подряд — отбирай важное и ставь его первым. Одну мысль пиши один раз: если пункт уже есть в решениях или задачах, не повторяй его в обсуждении. Термины и сленг команды оставляй как есть — их все знают, но сам слова не сокращай и не перегружай текст цифрами: число нужно, только если без него непонятно решение.

Разделы фоллоу-апа:
- "context": 2–3 предложения — о чём встреча и главный итог. Человек, который её пропустил, должен понять суть только из этого абзаца.
- "discussed": самые важные темы — не больше 6 пунктов, каждый в одно-два коротких предложения: что обсуждали и к чему пришли или что выяснилось. Мелочи и то, что уже есть в решениях и задачах, не включай.
- "decisions": принятые решения, каждое — одной фразой.
- "action_items": конкретные поручения, у которых есть исполнитель. task начинается с глагола и понятна без контекста встречи. Близкие поручения одному человеку объединяй в одно; мелочи вроде «ответить в чате» пропускай. deadline — дата, если срок назвали («завтра», «к пятнице» считай от даты встречи), "asap" — если сказали «срочно», иначе null. owner — имя человека, который взял задачу (по обращениям и «я сделаю»); если имя не понять — его роль («ведущий встречи»). «Спикер 2» исполнителем не пиши — это голоса всех собеседников сразу.
- "open_questions": что действительно осталось нерешённым и важно — обычно не больше 3–4 пунктов; не повторяй в них задачи.

Правила:
- Не выдумывай факты, которых нет в транскрипте.
- В транскрипте «Спикер 1» — микрофон автора записи, «Спикер 2» — остальные участники звонка. Имена бери из разговора или списка участников; если неразличимы — так и пиши «Спикер 1», «Спикер 2».
- Даты в тексте пиши по-человечески: «ср, 14.10», а не 2026-10-14."""

INSTRUCTIONS_FILE = "followup_instructions.md"


def instructions_path() -> Path:
    return app_dir() / INSTRUCTIONS_FILE


def custom_instructions() -> str | None:
    """Своя инструкция пользователя или None: файла нет, он пустой или не
    читается (тогда — предупреждение в лог, саммари идёт по стандартной)."""
    try:
        text = instructions_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("инструкция фоллоу-апа не читается (%s) — беру стандартную", exc)
        return None
    return text or None


def load_instructions() -> str:
    return custom_instructions() or DEFAULT_INSTRUCTIONS

# ─── схема фоллоу-апа (SPEC_v3 §7) ───────────────────────────────────────────
FOLLOWUP_KEYS = {"context", "discussed", "decisions", "action_items", "open_questions"}
ACTION_ITEM_KEYS = {"task", "owner", "deadline"}

_STR_LIST = {"type": "array", "items": {"type": "string"}}

# Та же схема в виде JSON Schema — для structured outputs Anthropic API
# (output_config.format): модель физически не может вернуть другой набор ключей.
FOLLOWUP_SCHEMA = {
    "type": "object",
    "properties": {
        "context": {"type": "string"},
        "discussed": _STR_LIST,
        "decisions": _STR_LIST,
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "owner": {"type": "string"},
                    "deadline": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": ["task", "owner", "deadline"],
                "additionalProperties": False,
            },
        },
        "open_questions": _STR_LIST,
    },
    "required": ["context", "discussed", "decisions", "action_items", "open_questions"],
    "additionalProperties": False,
}


class SummaryAuthError(RuntimeError):
    """Claude недоступен из-за доступа: claude CLI не залогинен / не найден,
    ANTHROPIC_API_KEY пуст или отклонён. Ретрай сам не поможет — нужен человек;
    стадия всё равно уходит в failed_retry, чтобы встреча дособралась после
    исправления (stages.summarize шлёт одно уведомление на сессию)."""


# ─────────────────────────────── валидация ───────────────────────────────────


def validate_followup(obj: dict) -> dict:
    """Ручная проверка схемы SPEC_v3 §7 (без jsonschema-зависимости).

    Проверяет и типы, и точный набор ключей (без лишних/пропущенных). Кидает
    ValueError с описанием проблемы при любом несоответствии. Возвращает
    исходный объект без изменений при успехе.
    """
    if not isinstance(obj, dict):
        raise ValueError(f"followup должен быть dict, получено {type(obj).__name__}")

    keys = set(obj.keys())
    if keys != FOLLOWUP_KEYS:
        missing = FOLLOWUP_KEYS - keys
        extra = keys - FOLLOWUP_KEYS
        raise ValueError(
            f"followup: неверный набор ключей (missing={sorted(missing)}, "
            f"extra={sorted(extra)})"
        )

    if not isinstance(obj["context"], str):
        raise ValueError("context должен быть str")

    for field in ("discussed", "decisions", "open_questions"):
        val = obj[field]
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            raise ValueError(f"{field} должен быть list[str]")

    action_items = obj["action_items"]
    if not isinstance(action_items, list):
        raise ValueError("action_items должен быть list")
    for i, item in enumerate(action_items):
        if not isinstance(item, dict):
            raise ValueError(f"action_items[{i}] должен быть dict")
        item_keys = set(item.keys())
        if item_keys != ACTION_ITEM_KEYS:
            raise ValueError(
                f"action_items[{i}]: неверный набор ключей {sorted(item_keys)}"
            )
        if not isinstance(item.get("task"), str):
            raise ValueError(f"action_items[{i}].task должен быть str")
        if not isinstance(item.get("owner"), str):
            raise ValueError(f"action_items[{i}].owner должен быть str")
        deadline = item.get("deadline")
        if deadline is not None and not isinstance(deadline, str):
            raise ValueError(f"action_items[{i}].deadline должен быть str|None")

    return obj


NO_OWNER = "не назначен"


def normalize_followup(obj):
    """Привести ответ LLM к схеме §7 там, где смысл однозначен, вместо отказа
    (продукт P9: 15.07 Claude вернул `owner: null`, и весь улучшенный фоллоу-ап
    был выброшен). null → пусто / «не назначен», числа → строки, лишние ключи
    action item отбрасываются, пункты без задачи — тоже. Не-dict или ответ без
    какого-то из разделов схемы возвращается как есть — его отклонит
    validate_followup (обрезанный ответ не должен затирать готовый фоллоу-ап)."""
    if not isinstance(obj, dict) or not FOLLOWUP_KEYS <= set(obj):
        return obj

    def as_str(v) -> str:
        return "" if v is None else v if isinstance(v, str) else str(v)

    def as_list(v) -> list[str]:
        if v is None:
            return []
        if not isinstance(v, list):
            v = [v]
        return [as_str(x) for x in v if x is not None and as_str(x).strip()]

    out = dict(obj)
    out["context"] = as_str(obj.get("context"))
    for field in ("discussed", "decisions", "open_questions"):
        out[field] = as_list(obj.get(field))
    items = obj.get("action_items")
    norm_items = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not as_str(item.get("task")).strip():
            continue
        deadline = item.get("deadline")
        norm_items.append({
            "task": as_str(item["task"]),
            "owner": as_str(item.get("owner")).strip() or NO_OWNER,
            "deadline": None if deadline in (None, "") else as_str(deadline),
        })
    out["action_items"] = norm_items
    return out


def _strip_fence(text: str) -> str:
    """Убрать markdown-обёртку ```json … ``` если модель всё же её добавила."""
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def parse_followup(raw: str) -> dict:
    """Текст ответа модели → валидный followup-dict схемы §7. Битый JSON или
    ответ не по схеме — ValueError с причиной (стадия уйдёт в failed_retry)."""
    try:
        obj = json.loads(_strip_fence(raw or ""))
    except json.JSONDecodeError as exc:
        raise ValueError(f"невалидный JSON от Claude: {exc}") from exc
    return validate_followup(normalize_followup(obj))


# ─────────────────────────────── промпт ──────────────────────────────────────


def attendees_hint(meta: dict) -> str:
    """Строка про участников из календаря для промпта (продукт P8) или ""."""
    names = meta.get("attendees") or []
    if not names:
        return ""
    return (f"Участники встречи по календарю: {', '.join(names)}. Если из транскрипта "
            "понятно, кто говорит или кому поручена задача, используй эти имена; "
            "не приписывай задачи наугад.\n")


# Технический формат ответа — не редактируется: от него зависят валидация,
# рендер и доставка. Через API схему дополнительно держит structured outputs.
_FORMAT_RULES = """Формат ответа — один JSON-объект (никакого текста вне JSON) ровно с ключами:
- "context": строка;
- "discussed", "decisions", "open_questions": списки строк;
- "action_items": список объектов {"task": "что сделать", "owner": "кто", "deadline": "2026-07-20" | "asap" | null}, deadline — дата в формате YYYY-MM-DD.
Если по разделу сказать нечего — пустой список [] (или пустая строка "" для context). Набор ключей и типы строго по схеме, без лишних ключей. Ответ — только валидный JSON: без markdown-обёртки ```json, без преамбулы."""


_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _date_hint(meta: dict) -> str:
    """Дата встречи — чтобы «завтра», «к пятнице» стали датами (через API
    модель не знает текущей даты)."""
    d = meta.get("date")
    if not d:
        return ""
    return f"Дата встречи: {_WEEKDAYS[d.weekday()]}, {d:%d.%m.%Y}.\n"


def build_prompt(transcript: str, meta: dict) -> str:
    """Пользовательский промпт: мета встречи + транскрипт + инструкция
    (своя или стандартная) + технический формат ответа."""
    title = meta.get("title") or "Без названия"
    duration = meta.get("duration_min")
    duration_str = f"{duration} минут" if duration is not None else "неизвестна"
    return f"""{attendees_hint(meta)}{_date_hint(meta)}Ниже — транскрипт встречи «{title}» (длительность {duration_str}). Составь по нему фоллоу-ап по инструкции после транскрипта.

Транскрипт:
---
{transcript}
---

Инструкция:
{load_instructions()}

{_FORMAT_RULES}"""
