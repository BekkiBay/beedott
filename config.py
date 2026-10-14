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


def write_config_env(values: dict[str, str]) -> Path:
    """Записать пары key=value в app_dir()/config.env и выставить права 0600
    (SPEC_v3 D14: секреты не должны быть читаемы другими пользователями)."""
    path = app_dir() / "config.env"
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    path.chmod(0o600)
    return path


def update_config_env(key: str, value: str) -> Path:
    """Обновить один ключ в app_dir()/config.env, сохранив остальные строки
    (write_config_env перезаписывает файл целиком — тут read-modify-write).

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


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name, "")
    if not val:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "y"}


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

# ─── Транскрипция ───────────────────────────────────────────────────────────
# tiny / base / small / medium / large / large-v3 / turbo
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "ru")

# ─── Аудио ──────────────────────────────────────────────────────────────────
# Имя Aggregate Device, созданного в Audio MIDI Setup (BlackHole + Mic).
# Если не найден — fallback на BlackHole; если и его нет — на дефолтный мик.
AGGREGATE_DEVICE_NAME = os.getenv("AGGREGATE_DEVICE_NAME", "MeetMind In")
BLACKHOLE_DEVICE_NAME = os.getenv("BLACKHOLE_DEVICE_NAME", "BlackHole 2ch")
AUDIO_SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))

# ─── Sinks (куда отправлять фоллоу-ап) ──────────────────────────────────────
# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Локальные .md файлы
MARKDOWN_BASE_DIR = os.getenv("MARKDOWN_BASE_DIR", "~/MeetMind")
ENABLE_MARKDOWN_SINK = _bool("ENABLE_MARKDOWN_SINK", True)

# Obsidian
OBSIDIAN_VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "")
OBSIDIAN_SUBFOLDER = os.getenv("OBSIDIAN_SUBFOLDER", "Meetings")
ENABLE_OBSIDIAN_SINK = _bool("ENABLE_OBSIDIAN_SINK", False) and bool(OBSIDIAN_VAULT_PATH)

# Notion
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")
ENABLE_NOTION_SINK = _bool("ENABLE_NOTION_SINK", False) and bool(NOTION_TOKEN and NOTION_DATABASE_ID)

# ─── Триггеры ───────────────────────────────────────────────────────────────
ENABLE_CALENDAR_TRIGGER = _bool("ENABLE_CALENDAR_TRIGGER", True)
ENABLE_PROCESS_TRIGGER = _bool("ENABLE_PROCESS_TRIGGER", True)
ENABLE_MENUBAR = _bool("ENABLE_MENUBAR", True)
HOTKEY_START_STOP = os.getenv("HOTKEY_START_STOP", "<cmd>+<shift>+r")

# Какие имена процессов считать триггером для записи
MEETING_APP_PROCESSES = [
    p.strip()
    for p in os.getenv("MEETING_APP_PROCESSES", "zoom.us,zoom").split(",")
    if p.strip()
]
# URL-паттерны для веб-встреч (через активное окно браузера — определится отдельно)
MEETING_URL_PATTERNS = [
    p.strip()
    for p in os.getenv(
        "MEETING_URL_PATTERNS",
        "meet.google.com,zoom.us/j/,teams.microsoft.com,whereby.com",
    ).split(",")
    if p.strip()
]

# ─── Google Calendar ────────────────────────────────────────────────────────
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")

# ─── Пути ───────────────────────────────────────────────────────────────────
SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", "sessions"))
SESSIONS_DIR.mkdir(exist_ok=True)

# ─── Логи ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def summary() -> str:
    """Текстовая сводка по конфигу — для логирования при старте."""
    lines = ["━" * 60, "MeetMind v3 config:"]
    if SUMMARY_BACKEND == "api":
        llm = f"anthropic-api:{ANTHROPIC_MODEL}"
    else:
        llm = f"claude-code:{CLAUDE_CODE_MODEL} (Max)"
    lines.append(f"  LLM:        {SUMMARY_BACKEND} → {llm}")
    lines.append(f"  Whisper:    {WHISPER_MODEL} / {WHISPER_LANGUAGE}")
    lines.append(f"  Audio:      agg=«{AGGREGATE_DEVICE_NAME}»  "
                 f"bh=«{BLACKHOLE_DEVICE_NAME}»  rate={AUDIO_SAMPLE_RATE}Hz")
    sinks = []
    if ENABLE_MARKDOWN_SINK: sinks.append(f"md({MARKDOWN_BASE_DIR})")
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID: sinks.append("telegram")
    if ENABLE_OBSIDIAN_SINK: sinks.append(f"obsidian({OBSIDIAN_VAULT_PATH})")
    if ENABLE_NOTION_SINK: sinks.append("notion")
    lines.append(f"  Sinks:      {', '.join(sinks) or '(нет)'}")
    triggers = []
    if ENABLE_CALENDAR_TRIGGER: triggers.append("calendar")
    if ENABLE_PROCESS_TRIGGER: triggers.append("process")
    if ENABLE_MENUBAR: triggers.append(f"menubar/hotkey({HOTKEY_START_STOP})")
    lines.append(f"  Triggers:   {', '.join(triggers) or '(нет)'}")
    lines.append("━" * 60)
    return "\n".join(lines)
