"""
Суммаризация через Ollama, строгий JSON по схеме SPEC_v3 §7.

Модель по умолчанию — из config.OLLAMA_MODEL (Vikhr-YandexGPT-5-Lite-8B-it,
см. SPEC_v3 D8). LLM возвращает JSON (`format=json`), рендер в markdown —
отдельно, в daemon/render.py. Claude-бустер (opt-in, поверх готового
Ollama-результата) приезжает отдельно в M7 (daemon/claude_booster.py) — тут
НЕ реализован.

Map-reduce: транскрипты длиннее MAP_REDUCE_CHUNK_CHARS режутся на части,
каждая часть суммируется в JSON той же схемы, финальный проход через ту же
модель сливает частичные JSON в один. Невалидный JSON после MAX_JSON_RETRIES
ретраев на каком-либо LLM-вызове → деградированный fallback:
`{"context": <сырой текст>, ...остальное пустое}` + `degraded=True`.
"""
import json
import logging
from typing import Optional

import requests

from config import OLLAMA_MODEL, OLLAMA_URL

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "Ты — продакт-менеджер, который слушает встречи и пишет краткие, "
    "конкретные фоллоу-апы на русском языке. Никакой воды, никаких общих фраз. "
    "Только факты, договорённости, дедлайны, ответственные."
)

# ─── схема фоллоу-апа (SPEC_v3 §7) ───────────────────────────────────────────
FOLLOWUP_KEYS = {"context", "discussed", "decisions", "action_items", "open_questions"}
ACTION_ITEM_KEYS = {"task", "owner", "deadline"}

# ─── map-reduce / ретраи ──────────────────────────────────────────────────────
MAP_REDUCE_CHUNK_CHARS = 48_000  # ~12k токенов, см. PLAN_v3 Task 1.5
MAX_JSON_RETRIES = 2  # итого до 3 попыток на один LLM-вызов
OLLAMA_NUM_CTX = 16384
OLLAMA_TIMEOUT_S = 600


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


def _degraded_followup(raw_text: str) -> dict:
    """Fallback после исчерпанных ретраев: сырой текст в context, остальное
    пусто, degraded=True (SPEC не требует его в схеме — это внутренний флаг
    для render.py/оркестратора)."""
    return {
        "context": (raw_text or "").strip(),
        "discussed": [],
        "decisions": [],
        "action_items": [],
        "open_questions": [],
        "degraded": True,
    }


# ─────────────────────────────── промпты ─────────────────────────────────────


def _build_user_prompt(transcript: str, meta: dict, *, is_chunk: bool = False) -> str:
    title = meta.get("title") or "Без названия"
    duration = meta.get("duration_min")
    duration_str = f"{duration} минут" if duration is not None else "неизвестна"

    chunk_note = ""
    if is_chunk:
        chunk_info = meta.get("_chunk_info", "часть длинной записи")
        chunk_note = (
            f"\nЭто {chunk_info} длинной встречи. Суммируй только то, что "
            "реально есть в этом фрагменте — не додумывай, что было до или "
            "после него.\n"
        )

    return f"""Проанализируй транскрипт встречи «{title}» (длительность {duration_str}).
{chunk_note}
Транскрипт:
---
{transcript}
---

Верни ОТВЕТ СТРОГО в виде одного JSON-объекта (никакого текста вне JSON) со
следующими ключами:
- "context": строка — 2-3 предложения о чём встреча, к чему пришли
- "discussed": список строк — пункты обсуждения ("кто что сказал/предложил")
- "decisions": список строк — конкретные принятые решения
- "action_items": список объектов вида
  {{"task": "что сделать", "owner": "кто отвечает", "deadline": "2026-07-20" | "asap" | null}}
- "open_questions": список строк — что осталось нерешённым

Правила:
- Не выдумывай факты, которых нет в транскрипте. Если по разделу сказать
  нечего — верни пустой список [] (или пустую строку "" для context), а не
  придуманный текст.
- Если имена спикеров в транскрипте неразличимы — называй их "Спикер 1",
  "Спикер 2" и т.д., не выдумывай имена.
- Набор ключей и типы должны СТРОГО совпадать со схемой выше, без
  дополнительных ключей.
- Ответ — ТОЛЬКО валидный JSON: без markdown-обёртки ```json, без преамбулы
  вроде "Вот фоллоу-ап:", без комментариев после JSON.
"""


def _build_messages(transcript: str, meta: dict, *, is_chunk: bool = False) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(transcript, meta, is_chunk=is_chunk)},
    ]


def _build_reduce_messages(partials: list[dict], meta: dict) -> list[dict]:
    title = meta.get("title") or "Без названия"
    parts_json = "\n\n".join(
        f"Часть {i + 1}:\n{json.dumps(p, ensure_ascii=False, indent=2)}"
        for i, p in enumerate(partials)
    )
    user_prompt = f"""Ниже — JSON-саммари нескольких последовательных фрагментов одной
встречи «{title}», в хронологическом порядке. Слей их в ОДИН итоговый JSON-объект
той же схемы: убери дубликаты между частями, сохрани хронологию, объедини
близкие action items с одинаковыми task/owner.

{parts_json}

Верни СТРОГО один JSON-объект (никакого текста вне JSON) с ключами:
- "context": строка — 2-3 предложения о встрече целиком
- "discussed": список строк
- "decisions": список строк
- "action_items": список объектов {{"task", "owner", "deadline"}}
- "open_questions": список строк

Не выдумывай факты, которых нет в частях выше. Никаких дополнительных ключей,
никакой markdown-обёртки, никакой преамбулы.
"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


# ─────────────────────────────── Ollama I/O ──────────────────────────────────


def _chat_json(
    messages: list[dict], *, model: str, ollama_url: str, num_ctx: int = OLLAMA_NUM_CTX,
    timeout: int = OLLAMA_TIMEOUT_S,
) -> str:
    """Один POST /api/chat с format=json. Возвращает сырой content (строка).
    Кидает requests.RequestException при сетевой ошибке/не-2xx."""
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": messages,
        "options": {"temperature": 0.2, "num_ctx": num_ctx},
    }
    r = requests.post(f"{ollama_url.rstrip('/')}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return (data.get("message") or {}).get("content", "") or ""


def _chat_json_with_retries(
    messages: list[dict], *, model: str, ollama_url: str, retries: int = MAX_JSON_RETRIES
) -> tuple[Optional[dict], str]:
    """Вызвать Ollama и распарсить+провалидировать JSON-ответ, с ретраями при
    невалидном JSON. Возвращает (followup_dict_или_None, последний_сырой_текст)."""
    last_raw = ""
    for attempt in range(retries + 1):
        try:
            raw = _chat_json(messages, model=model, ollama_url=ollama_url)
        except requests.RequestException as e:
            logger.error(f"Ollama запрос упал (попытка {attempt + 1}/{retries + 1}): {e}")
            last_raw = ""
            continue

        last_raw = raw
        try:
            obj = json.loads(raw)
            validated = validate_followup(obj)
            return validated, raw
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(
                f"Ollama JSON невалиден (попытка {attempt + 1}/{retries + 1}): {e}"
            )
            continue

    return None, last_raw


def _chunk_text(text: str, chunk_chars: int) -> list[str]:
    if len(text) <= chunk_chars:
        return [text]
    return [text[i : i + chunk_chars] for i in range(0, len(text), chunk_chars)]


def _summarize_map_reduce(transcript: str, meta: dict, *, model: str, ollama_url: str) -> dict:
    chunks = _chunk_text(transcript, MAP_REDUCE_CHUNK_CHARS)
    logger.info(
        f"Транскрипт {len(transcript)} символов > {MAP_REDUCE_CHUNK_CHARS} → "
        f"map-reduce, {len(chunks)} чанков"
    )

    partials: list[dict] = []
    for i, chunk in enumerate(chunks):
        chunk_meta = dict(meta)
        chunk_meta["_chunk_info"] = f"часть {i + 1}/{len(chunks)}"
        messages = _build_messages(chunk, chunk_meta, is_chunk=True)
        result, raw = _chat_json_with_retries(messages, model=model, ollama_url=ollama_url)
        if result is not None:
            partials.append(result)
        else:
            logger.error(f"Map-шаг {i + 1}/{len(chunks)}: JSON не распарсился, деградирую часть")
            partials.append(_degraded_followup(raw or chunk))

    reduce_messages = _build_reduce_messages(partials, meta)
    result, raw = _chat_json_with_retries(reduce_messages, model=model, ollama_url=ollama_url)
    if result is not None:
        followup = dict(result)
        # если хоть один map-шаг деградировал — итог помечаем как частично
        # деградированный, даже если reduce-проход прошёл успешно
        followup["degraded"] = any(p.get("degraded") for p in partials)
        return followup

    logger.error("Reduce-шаг: JSON не распарсился после ретраев, собираю fallback из частей")
    combined_context = "\n\n".join(p.get("context", "") for p in partials if p.get("context"))
    return _degraded_followup(combined_context or transcript[:MAP_REDUCE_CHUNK_CHARS])


# ─────────────────────────────── публичный API ───────────────────────────────


def summarize_ollama(
    transcript: str,
    meta: dict,
    *,
    model: Optional[str] = None,
    ollama_url: Optional[str] = None,
) -> dict:
    """Суммаризировать транскрипт встречи через Ollama в строгий JSON (SPEC_v3 §7).

    Args:
        transcript: полный текст транскрипта (реплики, разделённые строками).
        meta: контекст встречи для промпта — как минимум `title` (str) и
            `duration_min` (число); лишние ключи игнорируются.
        model: имя модели Ollama; по умолчанию config.OLLAMA_MODEL.
        ollama_url: базовый URL Ollama; по умолчанию config.OLLAMA_URL.

    Returns:
        dict со схемой SPEC_v3 §7 (context/discussed/decisions/action_items/
        open_questions) + служебный ключ `degraded: bool` (True, если хотя бы
        часть JSON не удалось распарсить и пришлось идти в fallback).
    """
    model = model or OLLAMA_MODEL
    ollama_url = ollama_url or OLLAMA_URL
    transcript = transcript or ""

    if len(transcript) > MAP_REDUCE_CHUNK_CHARS:
        return _summarize_map_reduce(transcript, meta, model=model, ollama_url=ollama_url)

    messages = _build_messages(transcript, meta)
    result, raw = _chat_json_with_retries(messages, model=model, ollama_url=ollama_url)
    if result is not None:
        followup = dict(result)
        followup["degraded"] = False
        return followup

    logger.error("Ollama: JSON не распарсился после ретраев, деградированный fallback")
    return _degraded_followup(raw or transcript)
