"""Саммари через claude CLI headless (`claude -p`, биллинг = Max-подписка, SPEC D15).

Паттерн вызова — из test_one_meeting.sh:184-189: --print --model …
--append-system-prompt … --output-format text, промпт в stdin. БЕЗ --bare
(не читает CLAUDE_CODE_OAUTH_TOKEN). Любой сбой — исключение с причиной:
оркестратор переводит стадию в failed_retry, причина видна в last_error.
"""
import logging
import subprocess
from typing import Optional

import config
from daemon import claude_auth
from daemon.summarizer import SYSTEM_PROMPT, SummaryAuthError, build_prompt, parse_followup

logger = logging.getLogger(__name__)

CLAUDE_TIMEOUT_S = 600


def summarize(
    transcript: str,
    meta: dict,
    *,
    model: Optional[str] = None,
    oauth_token: Optional[str] = None,
) -> dict:
    """Фоллоу-ап по транскрипту через claude CLI → валидный dict схемы §7.

    Args:
        transcript: полный текст транскрипта.
        meta: `title` / `duration_min` / `attendees` для промпта.
        model: алиас/имя модели claude CLI; по умолчанию config.CLAUDE_CODE_MODEL.
        oauth_token: CLAUDE_CODE_OAUTH_TOKEN; по умолчанию из окружения
            (config.env уже загружен в os.environ через load_dotenv).

    Raises:
        SummaryAuthError: claude не найден или не залогинен.
        RuntimeError: таймаут или ненулевой exit code.
        ValueError: ответ не JSON или не по схеме.
    """
    binp = claude_auth.claude_bin()
    if not binp:
        raise SummaryAuthError("claude CLI не найден — укажи путь в MEETMIND_CLAUDE_BIN")
    if not claude_auth.auth_ok(oauth_token):
        raise SummaryAuthError("claude CLI не залогинен — выполни claude login / setup-token")

    model = model or config.CLAUDE_CODE_MODEL
    cmd = [
        binp,
        "--print",
        # без плагинов/хуков/CLAUDE.md пользователя — их SessionStart-хуки
        # иначе попадают в контекст и меняют стиль фоллоу-апа; логин работает
        "--safe-mode",
        "--no-session-persistence",
        "--model", model,
        "--append-system-prompt", SYSTEM_PROMPT,
        "--output-format", "text",
    ]
    try:
        r = subprocess.run(
            cmd,
            input=build_prompt(transcript, meta).encode("utf-8"),
            env=claude_auth.claude_env(oauth_token),
            capture_output=True,
            timeout=CLAUDE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude не ответил за {CLAUDE_TIMEOUT_S} с") from exc

    if r.returncode != 0:
        stderr = (r.stderr or b"").decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"claude exit {r.returncode}: {stderr}")

    followup = parse_followup((r.stdout or b"").decode("utf-8", errors="replace"))
    logger.info("claude_cli: фоллоу-ап собран (%s)", model)
    return followup
