"""Команды мастера первичной настройки BeeDott (app/Sources/MeetMind/SetupWizard.swift).

Запуск: `python -m daemon.setup <команда>`. Каждое событие — одна JSON-строка
в stdout:

    {"event": "progress", "stage": "model", "done": N, "total": M}
    {"event": "ok", ...поля результата}
    {"event": "error", "code": "no_network", "message": "..."}

Код выхода: 0 — ok, 1 — error, 2 — неизвестная команда. Секреты (ключ
Anthropic, токен бота) приходят только через stdin — argv виден в `ps`.
Тексты для человека рисует Swift по коду ошибки.
"""
import errno
import json
import os
import sys

# hf-xet обновляет tqdm-бар один раз в конце загрузки — прогресса в мастере не
# было бы. По HTTP бар растёт шагами по 10 МБ. Должно стоять до импорта
# huggingface_hub (константы читаются при импорте).
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from dotenv import dotenv_values  # noqa: E402

from daemon.paths import app_dir  # noqa: E402

# Тот же репозиторий, что грузит Transcriber (daemon/transcriber.py) — импорт
# transcriber тянет soundfile, поэтому копия; совпадение проверяет тест.
MODEL_REPO = "mlx-community/whisper-large-v3-turbo"
WEIGHT_FILES = ("weights.safetensors", "weights.npz")

_NETWORK_ERRORS = {"ConnectionError", "TransportError", "ConnectError", "TimeoutException",
                   "ConnectTimeout", "ReadTimeout", "LocalEntryNotFoundError",
                   "APIConnectionError"}


class SetupError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def emit(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def _setting(key: str) -> str:
    """Значение как его увидит демон: env, иначе config.env (иначе dev ./.env).
    Читаем с диска заново — уже импортированный config мог устареть."""
    import config
    path = app_dir() / "config.env"
    if not path.exists():
        path = config.DEV_ENV_PATH
    values = dotenv_values(path) if path.exists() else {}
    return (os.environ.get(key) or values.get(key) or "").strip()


def model_cached(repo: str = MODEL_REPO) -> bool:
    """Модель целиком в кэше HF: config.json и веса. Офлайн, сеть не трогает."""
    from huggingface_hub import try_to_load_from_cache

    def cached(name: str) -> bool:
        return isinstance(try_to_load_from_cache(repo, name), str)

    return cached("config.json") and any(cached(w) for w in WEIGHT_FILES)


def cmd_status() -> dict:
    backend = _setting("SUMMARY_BACKEND").lower() or "claude_cli"
    summary = None
    if backend == "api" and _setting("ANTHROPIC_API_KEY"):
        summary = "api"
    elif backend == "claude_cli":
        from daemon import claude_auth
        if claude_auth.claude_bin():
            summary = "claude_cli"
    telegram = bool(_setting("TELEGRAM_BOT_TOKEN") and _setting("TELEGRAM_CHAT_ID"))
    return {"model": model_cached(), "summary": summary, "telegram": telegram}


def _classify(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "disk_full"
    if {cls.__name__ for cls in type(exc).__mro__} & _NETWORK_ERRORS:
        return "no_network"
    return "internal"


def _last_line(exc: BaseException) -> str:
    lines = str(exc).strip().splitlines()
    return lines[-1] if lines else type(exc).__name__


def cmd_check_key(key: str) -> dict:
    """Проверить ключ Anthropic настоящим запросом (список моделей — без
    расхода токенов) и включить саммари через API."""
    if not key:
        raise SetupError("invalid_key", "пустой ключ")
    import anthropic
    try:
        anthropic.Anthropic(api_key=key, max_retries=1, timeout=20).models.list(limit=1)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise SetupError("invalid_key", _last_line(exc)) from exc
    except anthropic.APIConnectionError as exc:
        raise SetupError("no_network", _last_line(exc)) from exc
    import config
    config.update_config_env("ANTHROPIC_API_KEY", key)
    config.update_config_env("SUMMARY_BACKEND", "api")
    return {}


COMMANDS = {
    "status": cmd_status,
    "check-key": cmd_check_key,
}
# Команды, которым нужен секрет из stdin (первая строка).
SECRET_COMMANDS = {"check-key"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in COMMANDS:
        emit("error", code="internal", message="команды: " + ", ".join(COMMANDS))
        return 2
    name = argv[0]
    secret = sys.stdin.readline().strip() if name in SECRET_COMMANDS else None
    try:
        result = COMMANDS[name](secret) if name in SECRET_COMMANDS else COMMANDS[name]()
    except Exception as exc:  # noqa: BLE001 — любой сбой → событие error для мастера
        code = exc.code if isinstance(exc, SetupError) else _classify(exc)
        message = str(exc) if isinstance(exc, SetupError) else _last_line(exc)
        if secret:
            message = message.replace(secret, "<скрыто>")
        emit("error", code=code, message=message)
        return 1
    emit("ok", **result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
