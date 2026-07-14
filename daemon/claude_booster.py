"""Claude-бустер (opt-in, PLAN_v3 Task 7.4): улучшение Ollama-фоллоу-апа
через claude CLI headless (`--print`, биллинг = Max-подписка, SPEC D15).

Вход — транскрипт + готовый Ollama-JSON (SPEC_v3 §7), выход — JSON той же
схемы. Бустер строго best-effort: любой сбой (auth, exit code, невалидный
JSON, схема, таймаут) → None, в пайплайне остаётся Ollama-версия. При
auth-сбое — одно уведомление и отключение бустера до рестарта демона.

Паттерн вызова CLI — из test_one_meeting.sh:184-189: --print --model …
--append-system-prompt … --output-format text, промпт в stdin. БЕЗ --bare
(не читает CLAUDE_CODE_OAUTH_TOKEN).
"""
import json
import logging
import subprocess
from typing import Optional

import config
from daemon import claude_auth
from daemon.notifier import notify
from daemon.summarizer import SYSTEM_PROMPT, validate_followup

logger = logging.getLogger(__name__)

CLAUDE_TIMEOUT_S = 600

# auth-сбой отключает бустер до рестарта демона (не ретраим и не спамим)
_disabled = False


def is_disabled() -> bool:
    return _disabled


def reset() -> None:
    """Сброс флага «отключён до рестарта» (для тестов)."""
    global _disabled
    _disabled = False


def _build_prompt(transcript: str, ollama_followup: dict, meta: dict) -> str:
    title = meta.get("title") or "Без названия"
    duration = meta.get("duration_min")
    duration_str = f"{duration} минут" if duration is not None else "неизвестна"
    draft = {k: v for k, v in ollama_followup.items() if k != "degraded"}
    return f"""Ниже — транскрипт встречи «{title}» (длительность {duration_str}) и
черновик фоллоу-апа, сделанный локальной моделью. Улучши черновик: сверь его с
транскриптом, исправь ошибки, добавь пропущенные решения/задачи, убери выдумки.

Транскрипт:
---
{transcript}
---

Черновик фоллоу-апа (JSON):
{json.dumps(draft, ensure_ascii=False, indent=2)}

Верни ОТВЕТ СТРОГО в виде одного JSON-объекта (никакого текста вне JSON) со
следующими ключами:
- "context": строка — 2-3 предложения о чём встреча, к чему пришли
- "discussed": список строк — пункты обсуждения
- "decisions": список строк — конкретные принятые решения
- "action_items": список объектов вида
  {{"task": "что сделать", "owner": "кто отвечает", "deadline": "2026-07-20" | "asap" | null}}
- "open_questions": список строк — что осталось нерешённым

Правила:
- Не выдумывай факты, которых нет в транскрипте.
- Набор ключей и типы должны СТРОГО совпадать со схемой, без лишних ключей.
- Ответ — ТОЛЬКО валидный JSON: без markdown-обёртки ```json, без преамбулы.
"""


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


def _auth_failed() -> None:
    global _disabled
    _disabled = True
    notify(
        "MeetMind",
        "Claude недоступен, проверь claude login / setup-token — "
        "бустер отключён до рестарта, фоллоу-апы идут через Ollama",
    )
    logger.error("claude_booster: auth-сбой, бустер отключён до рестарта")


def boost(
    transcript: str,
    ollama_followup: dict,
    meta: dict,
    *,
    model: Optional[str] = None,
    oauth_token: Optional[str] = None,
) -> Optional[dict]:
    """Улучшить Ollama-фоллоу-ап через claude CLI. None = оставить Ollama-версию.

    Args:
        transcript: полный текст транскрипта.
        ollama_followup: валидный dict схемы §7 от summarize_ollama (ключ
            `degraded` игнорируется).
        meta: `title` / `duration_min` для промпта.
        model: алиас/имя модели claude CLI; по умолчанию config.CLAUDE_CODE_MODEL.
        oauth_token: CLAUDE_CODE_OAUTH_TOKEN; по умолчанию из окружения
            (config.env уже загружен в os.environ через load_dotenv).
    """
    if _disabled:
        return None

    if not claude_auth.auth_ok(oauth_token):
        _auth_failed()
        return None

    binp = claude_auth.claude_bin()
    if not binp:  # auth_ok уже вернул бы False, но защищаемся от гонки
        _auth_failed()
        return None

    model = model or config.CLAUDE_CODE_MODEL
    cmd = [
        binp,
        "--print",
        "--model", model,
        "--append-system-prompt", SYSTEM_PROMPT,
        "--output-format", "text",
    ]
    prompt = _build_prompt(transcript, ollama_followup, meta)

    try:
        r = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            env=claude_auth.claude_env(oauth_token),
            capture_output=True,
            timeout=CLAUDE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.error("claude_booster: вызов claude упал — %s", exc)
        return None

    if r.returncode != 0:
        stderr = (r.stderr or b"").decode("utf-8", errors="replace")[:500]
        logger.error("claude_booster: exit %d — %s", r.returncode, stderr)
        return None

    raw = _strip_fence((r.stdout or b"").decode("utf-8", errors="replace"))
    try:
        followup = validate_followup(json.loads(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("claude_booster: невалидный JSON от Claude — %s", exc)
        return None

    result = dict(followup)
    result["degraded"] = False
    logger.info("claude_booster: фоллоу-ап улучшен моделью %s", model)
    return result
