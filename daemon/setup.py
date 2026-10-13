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
import shutil
import sys
import time
from pathlib import Path

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


PROGRESS_EVERY_SEC = 0.5
DISK_RESERVE = 300 * 2**20  # запас сверх размера модели


def _cached_file(repo: str, name: str) -> bool:
    from huggingface_hub import try_to_load_from_cache
    return isinstance(try_to_load_from_cache(repo, name), str)


def model_cached(repo: str = MODEL_REPO) -> bool:
    """Модель целиком в кэше HF: config.json и веса. Офлайн, сеть не трогает."""
    return _cached_file(repo, "config.json") and any(_cached_file(repo, w) for w in WEIGHT_FILES)


def _free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def _remove_stale_partials(cache: Path, repo: str) -> None:
    """huggingface_hub 1.23 не докачивает: прерванный файл начинается заново, а
    после SIGKILL в blobs/ остаются *.incomplete — убираем, чтобы не копились."""
    blobs = cache / f"models--{repo.replace('/', '--')}" / "blobs"
    if blobs.is_dir():
        for part in blobs.glob("*.incomplete"):
            part.unlink(missing_ok=True)


def cmd_install_model(repo: str = MODEL_REPO) -> dict:
    import huggingface_hub
    import tqdm
    from huggingface_hub import constants

    cache = Path(constants.HF_HUB_CACHE)
    files = [(s.rfilename, s.size or 0)
             for s in huggingface_hub.HfApi().model_info(repo, files_metadata=True).siblings]
    total = sum(size for _, size in files)
    todo = [(name, size) for name, size in files if not _cached_file(repo, name)]
    need = sum(size for _, size in todo)
    free = _free_bytes(cache)
    if todo and free < need + DISK_RESERVE:
        raise SetupError("disk_full",
                         f"нужно ещё {(need + DISK_RESERVE - free) / 2**30:.1f} ГБ")
    _remove_stale_partials(cache, repo)

    state = {"before": total - need, "last": None}

    def report(current: int, force: bool = False) -> None:
        now = time.monotonic()
        if force or state["last"] is None or now - state["last"] >= PROGRESS_EVERY_SEC:
            state["last"] = now
            emit("progress", stage="model", done=state["before"] + current, total=total)

    devnull = open(os.devnull, "w")  # noqa: SIM115 — живёт до конца процесса

    class Bar(tqdm.tqdm):
        # Обычный tqdm, не huggingface_hub.utils.tqdm: тот без терминала
        # выключается (disable=None), и n не растёт.
        def __init__(self, *args, **kwargs):
            kwargs["disable"] = False
            kwargs["file"] = devnull
            super().__init__(*args, **kwargs)

        def update(self, n=1):
            out = super().update(n)
            report(self.n)
            return out

    report(0, force=True)
    for name, size in todo:
        huggingface_hub.hf_hub_download(repo, name, tqdm_class=Bar)
        state["before"] += size
    report(0, force=True)
    return {"model": repo}


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


def cmd_link_ffmpeg() -> dict:
    """runtime/bin/ffmpeg → статический ffmpeg из imageio-ffmpeg: mlx-whisper и
    reconcile зовут его по имени, а на чистом Mac Homebrew нет."""
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    bin_dir = app_dir() / "runtime" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / "ffmpeg"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(exe)
    return {"ffmpeg": str(link)}


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
TELEGRAM_HELLO = "BeeDott подключён ✓ Сюда будут приходить фоллоу-апы встреч."
CONNECT_WAIT_SEC = 120
LONG_POLL_SEC = 50  # Telegram держит getUpdates не дольше ~50 с


def _tg(token: str, method: str, params: dict | None = None, http_timeout: float = 15):
    """Вызов Bot API. Токен в URL — вычищаем его из любых текстов ошибок."""
    import requests
    url = TELEGRAM_API.format(token=token, method=method)
    try:
        resp = requests.post(url, json=params or {}, timeout=http_timeout)
    except requests.RequestException as exc:
        raise SetupError("no_network", _last_line(exc).replace(token, "<токен>")) from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise SetupError("internal", f"Telegram ответил {resp.status_code}") from exc
    description = str(data.get("description", "")).replace(token, "<токен>")
    if resp.status_code in (401, 404):
        raise SetupError("invalid_token", description or "токен не подходит")
    if resp.status_code == 409:
        raise SetupError("bot_busy", description)
    if not data.get("ok"):
        raise SetupError("internal", description or f"Telegram ответил {resp.status_code}")
    return data["result"]


def cmd_telegram_check(token: str) -> dict:
    if not token:
        raise SetupError("invalid_token", "пустой токен")
    return {"bot": _tg(token, "getMe")["username"]}


def cmd_telegram_connect(token: str) -> dict:
    """Ждём /start от пользователя, запоминаем его чат, шлём тестовое сообщение."""
    if not token:
        raise SetupError("invalid_token", "пустой токен")
    deadline = time.monotonic() + CONNECT_WAIT_SEC
    offset = None
    while time.monotonic() < deadline:
        wait = max(1, min(LONG_POLL_SEC, int(deadline - time.monotonic())))
        params = {"timeout": wait, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        for update in _tg(token, "getUpdates", params, http_timeout=wait + 10):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            if (message.get("text") or "").strip().startswith("/start"):
                chat_id = message["chat"]["id"]
                _tg(token, "getUpdates", {"offset": offset, "timeout": 0})  # подтвердить
                _tg(token, "sendMessage", {"chat_id": chat_id, "text": TELEGRAM_HELLO})
                import config
                config.update_config_env("TELEGRAM_BOT_TOKEN", token)
                config.update_config_env("TELEGRAM_CHAT_ID", str(chat_id))
                return {"chat_id": chat_id}
    raise SetupError("timeout", "не дождались /start")


COMMANDS = {
    "status": cmd_status,
    "install-model": cmd_install_model,
    "link-ffmpeg": cmd_link_ffmpeg,
    "check-key": cmd_check_key,
    "telegram-check": cmd_telegram_check,
    "telegram-connect": cmd_telegram_connect,
}
# Команды, которым нужен секрет из stdin (первая строка).
SECRET_COMMANDS = {"check-key", "telegram-check", "telegram-connect"}


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
