"""
Конфигурация MeetMind v2.

Все настройки читаются из .env (см. .env.example). Любую можно переопределить
переменной окружения. Сложные настройки выбора движка (Ollama vs Claude)
доступны в одном месте.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# .env берётся из корня проекта
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name, "")
    if not val:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on", "y"}


# ─── LLM / суммаризация ─────────────────────────────────────────────────────
# Три backend'а, любой можно включить независимо. Все включённые работают
# параллельно, но «cloud» слот занимает один (Claude Code если включён,
# иначе Anthropic API). «local» слот — всегда Ollama если включена.
#
# Рекомендуемые конфигурации:
#   Есть Max-подписка → USE_OLLAMA=true + USE_CLAUDE_CODE=true (без API ключа)
#   Нет Max, есть API → USE_OLLAMA=true + USE_ANTHROPIC=true
#   Только локально   → USE_OLLAMA=true, остальные false

USE_OLLAMA = _bool("USE_OLLAMA", True)
USE_CLAUDE_CODE = _bool("USE_CLAUDE_CODE", True)  # через Max-подписку
USE_ANTHROPIC = _bool("USE_ANTHROPIC", False)     # отдельный API-биллинг

# ── Ollama ──
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
# Дефолт: Vikhr-Nemo 12B — русский тюн поверх Mistral Nemo, 128K context, ~7.5GB Q4.
# Альтернативы:
#   qwen3.5:14b               — свежий мейнстрим (Feb 2026), 9GB
#   t-tech/t-pro-it-2.0       — русская премиум 32B, требует sysctl tweak
#   gemma3:12b                — Google long-context champion
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "vikhr-nemo:12b-instruct-q4")

# ── Claude Code CLI (рекомендуется для Max-подписчиков) ──
CLAUDE_CODE_BINARY = os.getenv("CLAUDE_CODE_BINARY", "claude")
# Алиасы Claude Code: opus | sonnet | haiku. Можно указать полное имя модели.
CLAUDE_CODE_MODEL = os.getenv("CLAUDE_CODE_MODEL", "opus")

# ── Anthropic API (если нет Max, нужен API ключ) ──
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

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

# ─── Сервера ────────────────────────────────────────────────────────────────
WS_PORT = int(os.getenv("WS_PORT", "8765"))
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

# ─── Логи ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def summary() -> str:
    """Текстовая сводка по конфигу — для логирования при старте."""
    lines = ["━" * 60, "MeetMind v2 config:"]
    cloud_info = "off"
    if USE_CLAUDE_CODE:
        cloud_info = f"claude-code:{CLAUDE_CODE_MODEL} (Max)"
    elif USE_ANTHROPIC:
        cloud_info = f"anthropic-api:{ANTHROPIC_MODEL}"
    lines.append(f"  LLM:        ollama={USE_OLLAMA}({OLLAMA_MODEL})")
    lines.append(f"              cloud={cloud_info}")
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
