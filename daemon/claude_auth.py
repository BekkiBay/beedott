"""Auth-обвязка для headless-вызовов claude CLI по Max-подписке (SPEC D15).

Факты (verified 2026-07, code.claude.com/docs):
- CLAUDE_CODE_OAUTH_TOKEN: env var для headless -p; создаётся `claude setup-token`
  (браузерный OAuth, живёт 1 год, биллинг = подписка, нужен Pro/Max).
- Прецеденс: ANTHROPIC_AUTH_TOKEN > ANTHROPIC_API_KEY > apiKeyHelper >
  CLAUDE_CODE_OAUTH_TOKEN > keychain (/login). В -p режиме API-ключ применяется
  БЕЗ подтверждения → тихий биллинг API-кредитов. Поэтому чистим env.
- --bare НЕ читает CLAUDE_CODE_OAUTH_TOKEN — не использовать с токеном.
- launchd/.app: минимальный PATH → claude ищется и в типовых местах установки
  (nvm, ~/.local/bin), явный путь — MEETMIND_CLAUDE_BIN в config.env.
- Несколько аккаунтов: какой логин брать, решает CLAUDE_CONFIG_DIR (claude_env
  передаёт его из окружения/config.env как есть).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

AUTH_STATUS_TIMEOUT_S = 30

# env-ключи, которые перехватывают биллинг у OAuth-токена (см. прецеденс выше)
_BILLING_OVERRIDE_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _node_version(bin_path: Path) -> tuple[int, ...]:
    """~/.nvm/versions/node/v26.1.0/bin/claude → (26, 1, 0)."""
    return tuple(int(n) for n in re.findall(r"\d+", bin_path.parent.parent.name))


def claude_bin() -> str | None:
    """Полный путь к claude CLI: MEETMIND_CLAUDE_BIN, PATH, затем типовые места
    установки. Демон из .app получает урезанный PATH без nvm и ~/.local/bin —
    без этого поиска саммари молча вставали (2026-10-12)."""
    found = os.environ.get("MEETMIND_CLAUDE_BIN") or shutil.which("claude")
    if found:
        return found
    home = Path.home()
    nvm = sorted(home.glob(".nvm/versions/node/*/bin/claude"), key=_node_version, reverse=True)
    for path in [home / ".local/bin/claude", home / ".claude/local/claude", *nvm]:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def claude_env(oauth_token: str | None) -> dict:
    """Копия окружения без ANTHROPIC_*-ключей, с инъекцией OAuth-токена."""
    env = {k: v for k, v in os.environ.items() if k not in _BILLING_OVERRIDE_KEYS}
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def auth_ok(oauth_token: str | None) -> bool:
    """Self-check при старте демона: exit 0 = залогинен."""
    binp = claude_bin()
    if not binp:
        return False
    try:
        r = subprocess.run(
            [binp, "auth", "status"],
            env=claude_env(oauth_token),
            capture_output=True,
            timeout=AUTH_STATUS_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0
