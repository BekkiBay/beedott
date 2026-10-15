"""
Конфигурация MeetMind v3.

Все настройки читаются из .env (см. .env.example). Любую можно переопределить
переменной окружения. Способ вызова Claude для саммари (SUMMARY_BACKEND)
настраивается в одном месте.

Секреты живут в `app_dir()/config.env` (SPEC_v3 D14), вне репозитория, с
правами 0600. Для разработки — fallback на `./.env` в корне проекта, если
app-level config.env ещё не создан.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

from daemon.paths import app_dir

# .env: приоритет — app_dir()/config.env (прод, секреты, chmod 600),
# fallback — ./.env в корне проекта (разработка).
DEV_ENV_PATH = Path(__file__).parent / ".env"
env_path = app_dir() / "config.env"
if not env_path.exists():
    env_path = DEV_ENV_PATH
load_dotenv(env_path)


def update_config_env(key: str, value: str) -> Path:
    """Обновить один ключ в app_dir()/config.env, сохранив остальные строки
    (read-modify-write), и выставить права 0600 (SPEC_v3 D14: секреты не
    должны быть читаемы другими пользователями).

    Если config.env ещё нет, а настройки живут в dev-файле ./.env — сначала
    переносим его содержимое целиком: после появления config.env модуль читает
    только его, и без переноса пропали бы токены Telegram (аудит T4)."""
    path = app_dir() / "config.env"
    if path.exists():
        lines = path.read_text().splitlines()
    elif DEV_ENV_PATH.exists():
        lines = DEV_ENV_PATH.read_text().splitlines()
    else:
        lines = []
    new_line = f"{key}={value}"
    idx = next((i for i, ln in enumerate(lines) if ln.startswith(f"{key}=")), None)
    if idx is None:
        lines.append(new_line)
    else:
        lines[idx] = new_line
        lines = lines[:idx + 1] + [ln for ln in lines[idx + 1:]
                                   if not ln.startswith(f"{key}=")]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return path


# ─── LLM / суммаризация ─────────────────────────────────────────────────────
# Саммари делает Claude (модель — ANTHROPIC_MODEL, выбирается в «Настройке…»).
# Способ вызова — SUMMARY_BACKEND:
#   claude_cli — `claude -p` по Max-подписке, API-ключ не нужен (по умолчанию)
#   api        — Anthropic API по ANTHROPIC_API_KEY, отдельный биллинг
SUMMARY_BACKEND = os.getenv("SUMMARY_BACKEND", "claude_cli").strip().lower()

# ── claude_cli ──
# Алиас модели Claude Code (opus = актуальный Opus) или полное имя модели.
# Путь к claude, если его нет в PATH демона, — MEETMIND_CLAUDE_BIN.
CLAUDE_CODE_MODEL = os.getenv("CLAUDE_CODE_MODEL", "opus")

# ── api ──
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)

# ─── Sinks (куда отправлять фоллоу-ап) ──────────────────────────────────────
# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Заметка .md пишется всегда; если задан OBSIDIAN_VAULT_PATH — ещё и в vault
MARKDOWN_BASE_DIR = os.getenv("MARKDOWN_BASE_DIR", "~/MeetMind")
OBSIDIAN_VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "")
OBSIDIAN_SUBFOLDER = os.getenv("OBSIDIAN_SUBFOLDER", "Meetings")
