"""Команды мастера первичной настройки BeeDott (app/Sources/MeetMind/SetupWizard.swift).

Запуск: `python -m daemon.setup <команда>`. Каждое событие — одна JSON-строка
в stdout:

    {"event": "progress", "stage": "model", "done": N, "total": M}
    {"event": "ok", ...поля результата}
    {"event": "error", "code": "no_network", "message": "..."}

Код выхода: 0 — ok, 1 — error, 2 — неизвестная команда. Секреты (ключ
Anthropic, токен бота) приходят только через stdin — argv виден в `ps`.
Тексты для человека рисует Swift по коду ошибки.
Команды claude-* подключают MCP-сервер BeeDott к Claude (спек 2026-10-14-claude-mcp-design.md).
"""
import errno
import json
import os
import shlex
import shutil
import subprocess
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
# Модели саммари в мастере. Тот же список с подписями — RuntimeLogic.summaryModels
# (Swift), совпадение проверяет tests/test_setup.py.
SUMMARY_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1")
MAX_INSTRUCTIONS = 20_000
WEIGHT_FILES = ("weights.safetensors", "weights.npz")
CLAUDE_CLI_TIMEOUT = 30

# MCP-сервер BeeDott в конфигах Claude (спек 2026-10-14-claude-mcp-design.md).
CLAUDE_SERVER_NAME = "beedott"
DESKTOP_CONFIG = (Path.home() / "Library" / "Application Support" / "Claude"
                  / "claude_desktop_config.json")

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
# *.incomplete старше — брошены убитым процессом; свежие может писать другая загрузка.
STALE_PARTIAL_SEC = 6 * 3600


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
    после SIGKILL в blobs/ остаются *.incomplete — убираем старые, чтобы не
    копились. Свежие не трогаем: их может писать параллельная загрузка."""
    blobs = cache / f"models--{repo.replace('/', '--')}" / "blobs"
    if blobs.is_dir():
        cutoff = time.time() - STALE_PARTIAL_SEC
        for part in blobs.glob("*.incomplete"):
            if part.stat().st_mtime < cutoff:
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
    from daemon import summarizer
    return {"model": model_cached(), "summary": summary, "telegram": telegram,
            "summary_model": _summary_model(),
            "summary_custom": summarizer.custom_instructions() is not None,
            "claude": claude_desktop_state()}


def _summary_model() -> str:
    import config
    return _setting("ANTHROPIC_MODEL") or config.DEFAULT_ANTHROPIC_MODEL


def cmd_summary_settings() -> dict:
    from daemon import summarizer
    custom = summarizer.custom_instructions()
    return {"model": _summary_model(),
            "instructions": custom or summarizer.DEFAULT_INSTRUCTIONS,
            "custom": custom is not None,
            "default_instructions": summarizer.DEFAULT_INSTRUCTIONS}


def cmd_set_model(model: str) -> dict:
    model = model.strip()
    if model not in SUMMARY_MODELS:
        raise SetupError("bad_model", f"неизвестная модель: {model}")
    import config
    config.update_config_env("ANTHROPIC_MODEL", model)
    return {"model": model}


def cmd_set_instructions(text: str) -> dict:
    """Пустая или стандартная инструкция — файл удаляется (сброс). Swift
    дописывает к stdin перевод строки — сравниваем после strip()."""
    from daemon import summarizer
    text = text.strip()
    path = summarizer.instructions_path()
    if not text or text == summarizer.DEFAULT_INSTRUCTIONS.strip():
        path.unlink(missing_ok=True)
        return {"custom": False}
    if len(text) > MAX_INSTRUCTIONS:
        raise SetupError("too_long", f"{len(text)} символов, предел {MAX_INSTRUCTIONS}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return {"custom": True}


# ─── MCP: Claude Desktop ──────────────────────────────────────────────────────


def desktop_config_path() -> Path:
    return Path(os.environ.get("MEETMIND_CLAUDE_DESKTOP_CONFIG") or DESKTOP_CONFIG)


def mcp_launch() -> dict:
    """Как Claude запускает MCP-сервер BeeDott: этот же Python, пакет daemon — по
    PYTHONPATH (у коллег — Resources/backend в .app, у автора — репо). Одна
    функция для status и подключения — сравнивают одно и то же.
    PYTHONPYCACHEPREFIX обязателен у коллег: иначе __pycache__ ляжет в .app и
    сломает подпись."""
    import daemon
    env = {"PYTHONPATH": str(Path(daemon.__file__).resolve().parent.parent)}
    for key in ("PYTHONPYCACHEPREFIX", "MEETMIND_APPDIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return {"command": sys.executable, "args": ["-m", "daemon.mcp_server"], "env": env}


def _read_desktop_config(path: Path) -> dict:
    """{} — файла нет или он пустой. bad_config — не JSON-объект или
    mcpServers не объект. Прочие OSError (нет прав) — наружу."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except ValueError as exc:  # UnicodeDecodeError
        raise SetupError("bad_config", f"{path}: {exc}") from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SetupError("bad_config", f"{path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise SetupError("bad_config", f"{path}: неожиданная структура")
    return data


def _write_desktop_config(path: Path, data: dict) -> None:
    """Файл только для чтения — не трогаем вовсе (ни .bak, ни .tmp): владелец
    явно запретил запись. Иначе — бэкап в .beedott.bak (старый .bak может быть
    read-only, поэтому unlink перед copy2, чтобы не упереться в Permission
    denied), затем временный файл с правами оригинала (shutil.copymode) и
    os.replace; .tmp подчищаем в finally при любом исходе. Текст собираем
    (allow_nan=False — не JSON-совместимые inf/nan не пишем) до того, как
    что-либо создаём на диске: сбой сериализации не должен оставлять мусор.
    Симлинк остаётся симлинком — пишем в файл, на который он указывает."""
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    exists = target.exists()
    if exists and not os.access(target, os.W_OK):
        raise SetupError("internal", f"{target}: файл только для чтения — BeeDott его не меняет")
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if exists:
        bak = target.with_name(target.name + ".beedott.bak")
        bak.unlink(missing_ok=True)
        shutil.copy2(target, bak)
    tmp = target.with_name(target.name + ".beedott.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        if exists:
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def cmd_claude_connect() -> dict:
    """Записать mcpServers.beedott; preferences, чужие серверы и прочее — как
    есть. Уже подключено тем же способом — не пишем вовсе (иначе повторный
    запуск мастера затирал бы свежий .bak копией уже изменённого файла).
    Пишется только при закрытом Claude Desktop (мастер закрывает его сам):
    открытый Claude при выходе перезаписал бы файл своей копией."""
    path = desktop_config_path()
    data = _read_desktop_config(path)
    servers = dict(data.get("mcpServers") or {})
    if servers.get(CLAUDE_SERVER_NAME) == mcp_launch():
        return {"config": str(path)}
    servers[CLAUDE_SERVER_NAME] = mcp_launch()
    data["mcpServers"] = servers
    _write_desktop_config(path, data)
    return {"config": str(path)}


def cmd_claude_disconnect() -> dict:
    path = desktop_config_path()
    data = _read_desktop_config(path)
    servers = data.get("mcpServers")
    if isinstance(servers, dict) and CLAUDE_SERVER_NAME in servers:
        data["mcpServers"] = {k: v for k, v in servers.items() if k != CLAUDE_SERVER_NAME}
        _write_desktop_config(path, data)
    return {"config": str(path)}


def claude_desktop_state() -> str | None:
    """connected — ключ есть и command/args/env совпадают с mcp_launch();
    stale — ключ есть, команда другая; bad_config — файл не читается; None — нет
    ключа или файла. Никогда не бросает: упавший status мастер счёл бы
    «нужна настройка»."""
    try:
        data = _read_desktop_config(desktop_config_path())
    except Exception:  # noqa: BLE001 — любой сбой чтения, не только ожидаемые
        return "bad_config"
    entry = (data.get("mcpServers") or {}).get(CLAUDE_SERVER_NAME)
    if entry is None:
        return None
    want = mcp_launch()
    if isinstance(entry, dict) and all(entry.get(k) == want[k] for k in ("command", "args", "env")):
        return "connected"
    return "stale"


# ─── MCP: Claude Code ─────────────────────────────────────────────────────────


def _claude_code_entry() -> str:
    return json.dumps({"type": "stdio", **mcp_launch()}, ensure_ascii=False)


def _claude_cli() -> str | None:
    """claude CLI так же, как его ищут status и демон. import config кладёт
    config.env в os.environ: MEETMIND_CLAUDE_BIN и CLAUDE_CONFIG_DIR (свой
    аккаунт Claude Code) учитываются."""
    import config  # noqa: F401
    from daemon import claude_auth
    return claude_auth.claude_bin()


def _claude_run(binp: str, *args: str) -> bool:
    from daemon import claude_auth
    try:
        r = subprocess.run([binp, *args], env=claude_auth.claude_env(None),
                           capture_output=True, timeout=CLAUDE_CLI_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def cmd_claude_code_connect() -> dict:
    """remove (ошибку игнорируем) + add-json --scope user. Неудача CLI — не
    ошибка команды, а "failed"; command — та же регистрация строкой для
    терминала (свой CLAUDE_CONFIG_DIR или CLI не нашёлся)."""
    entry = _claude_code_entry()
    command = " ".join(["claude", "mcp", "add-json", "--scope", "user",
                        CLAUDE_SERVER_NAME, shlex.quote(entry)])
    binp = _claude_cli()
    if not binp:
        return {"claude_code": "absent", "command": command}
    _claude_run(binp, "mcp", "remove", "--scope", "user", CLAUDE_SERVER_NAME)
    ok = _claude_run(binp, "mcp", "add-json", "--scope", "user", CLAUDE_SERVER_NAME, entry)
    return {"claude_code": "added" if ok else "failed", "command": command}


def cmd_claude_code_disconnect() -> dict:
    """Неудача remove — не всегда ошибка: если beedott и не был
    зарегистрирован, желаемое состояние (не зарегистрирован) уже достигнуто.
    Проверяем через `mcp get`: нашёлся — реально "failed", не нашёлся —
    считаем "removed"."""
    binp = _claude_cli()
    if not binp:
        return {"claude_code": "absent"}
    if _claude_run(binp, "mcp", "remove", "--scope", "user", CLAUDE_SERVER_NAME):
        return {"claude_code": "removed"}
    still_registered = _claude_run(binp, "mcp", "get", CLAUDE_SERVER_NAME)
    return {"claude_code": "failed" if still_registered else "removed"}


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


def cmd_telegram_connect(token: str, code: str = "") -> dict:
    """Ждём `/start <code>` — мастер открывает бота ссылкой t.me/<бот>?start=<code>
    с одноразовым кодом, — запоминаем этот чат, шлём тестовое сообщение. Голый
    /start не принимаем: в очереди бота может лежать /start чужого человека."""
    if not token:
        raise SetupError("invalid_token", "пустой токен")
    if not code:
        raise SetupError("internal", "нет одноразового кода подключения")
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
            if (message.get("text") or "").split() == ["/start", code]:
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
    "summary-settings": cmd_summary_settings,
    "set-model": cmd_set_model,
    "set-instructions": cmd_set_instructions,
    "claude-connect": cmd_claude_connect,
    "claude-disconnect": cmd_claude_disconnect,
    "claude-code-connect": cmd_claude_code_connect,
    "claude-code-disconnect": cmd_claude_code_disconnect,
}
# Команды, которым нужен секрет из stdin (первая строка).
SECRET_COMMANDS = {"check-key", "telegram-check", "telegram-connect"}
# Аргумент из первой строки stdin (секреты и id модели) или весь stdin (текст).
LINE_COMMANDS = SECRET_COMMANDS | {"set-model"}
TEXT_COMMANDS = {"set-instructions"}
# Второй строкой stdin — одноразовый код из ссылки на бота.
CODE_COMMANDS = {"telegram-connect"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in COMMANDS:
        emit("error", code="internal", message="команды: " + ", ".join(COMMANDS))
        return 2
    name = argv[0]
    if name in TEXT_COMMANDS:
        arg = sys.stdin.read()
    elif name in LINE_COMMANDS:
        arg = sys.stdin.readline().strip()
    else:
        arg = None
    secret = arg if name in SECRET_COMMANDS else None
    extra = [sys.stdin.readline().strip()] if name in CODE_COMMANDS else []
    try:
        result = COMMANDS[name](arg, *extra) if arg is not None else COMMANDS[name]()
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
