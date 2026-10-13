"""Саммари через Anthropic API (ANTHROPIC_API_KEY, отдельный биллинг).

- structured outputs (output_config.format) — ответ всегда JSON по схеме §7;
  всё равно проходит parse_followup (normalize + validate), как и CLI-путь;
- стриминг — длинный транскрипт + adaptive thinking не упираются в HTTP-таймаут;
- fallbacks="default" — если классификатор безопасности отклонит запрос,
  API сам перезапустит его на рекомендованной модели, встреча не застрянет.

SDK `anthropic` импортируется лениво: для backend'а claude_cli он не нужен.
"""
import logging
from typing import Optional

import config
from daemon.orchestrator import PermanentStageError
from daemon.summarizer import (
    FOLLOWUP_SCHEMA,
    SYSTEM_PROMPT,
    SummaryAuthError,
    build_prompt,
    parse_followup,
)

logger = logging.getLogger(__name__)

MAX_TOKENS = 32000  # thinking + JSON; фоллоу-ап — единицы тысяч токенов
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def summarize(
    transcript: str,
    meta: dict,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict:
    """Фоллоу-ап по транскрипту через Anthropic API → валидный dict схемы §7.

    Raises:
        SummaryAuthError: ключ не задан или отклонён (401/403).
        PermanentStageError: модель отказалась обрабатывать транскрипт.
        RuntimeError: ответ обрезан по max_tokens.
        anthropic.APIError: сеть / 429 / 5xx после ретраев SDK — failed_retry.
    """
    api_key = api_key or config.ANTHROPIC_API_KEY
    if not api_key:
        raise SummaryAuthError("ANTHROPIC_API_KEY не задан — пропиши ключ в config.env")
    model = model or config.ANTHROPIC_MODEL

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    try:
        with client.beta.messages.stream(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(transcript, meta)}],
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": FOLLOWUP_SCHEMA}},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            message = stream.get_final_message()
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise SummaryAuthError(f"Anthropic API отклонил ключ: {exc}") from exc

    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) or "—"
        raise PermanentStageError(f"Claude отказался обрабатывать транскрипт (категория: {category})")
    if message.stop_reason == "max_tokens":
        raise RuntimeError(f"ответ Claude обрезан на max_tokens={MAX_TOKENS}")

    followup = parse_followup(_final_text(message))
    logger.info("anthropic_api: фоллоу-ап собран (%s)", message.model)
    return followup


def _final_text(message) -> str:
    """Текст ответа. Блок `fallback` = отказавшая модель передала запрос
    следующей — её частичный вывод до этого блока отбрасываем."""
    texts: list[str] = []
    for block in message.content:
        if block.type == "fallback":
            texts = []
        elif block.type == "text":
            texts.append(block.text)
    return "".join(texts)
