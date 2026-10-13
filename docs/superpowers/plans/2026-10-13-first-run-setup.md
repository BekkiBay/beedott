# Первичная настройка BeeDott — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** BeeDott.app на чистом Mac сам ставит Python-рантайм и модель Whisper, спрашивает ключ Anthropic и (по желанию) подключает Telegram — без терминала и репозитория.

**Architecture:** Исходники демона и бинарь `uv` едут внутри `.app`. Мастер (SwiftUI) запускает `uv sync` в `~/Library/Application Support/MeetMind/runtime` и Python-команды `python -m daemon.setup …`, которые печатают JSON-строки событий. Супервизор демона выбирает режим: dev (репо + uv, как сейчас) или bundled (рантайм из Application Support).

**Tech Stack:** Python 3.11 (pytest, huggingface_hub 1.23, anthropic, requests, imageio-ffmpeg), Swift 5 / SwiftUI / AppKit (голый swiftc, `app/build.sh`), uv 0.11.

Спека: `docs/superpowers/specs/2026-10-13-first-run-setup-design.md`.

Проверено заранее (спайк 2026-10-13):
- hf-xet обновляет `tqdm_class`-бар один раз в конце; с `HF_HUB_DISABLE_XET=1` загрузка идёт по HTTP и бар растёт шагами по 10 МБ. Поэтому `install-model` выставляет `HF_HUB_DISABLE_XET=1` до импорта huggingface_hub.
- ffmpeg 7.1 из `imageio-ffmpeg` (колесо arm64) умеет `flac`, `channelsplit`, `-ignore_length`, декодирует aac/mp3/opus.
- `~/.local/bin/uv` — arm64, линкуется только с системными библиотеками, подпись ad-hoc.

Команды тестов: Python — `.venv/bin/python -m pytest -q` из корня репо; Swift — `app/Tests/run.sh`; проверка компиляции приложения — `cd app && swiftc -target arm64-apple-macosx14.4 -o "${TMPDIR:-/tmp}/beedott-check" Sources/MeetMind/*.swift` (полная компиляция: `-typecheck` пропускает часть проверок).

---

## Карта файлов

| Файл | Что | Ответственность |
|---|---|---|
| `pyproject.toml`, `uv.lock` | изменить | + `imageio-ffmpeg`, `huggingface_hub` |
| `daemon/app.py` | изменить | `_ensure_tool_path` кладёт `app_dir()/runtime/bin` первым |
| `daemon/setup.py` | создать | CLI мастера: `status`, `install-model`, `link-ffmpeg`, `check-key`, `telegram-check`, `telegram-connect` |
| `tests/test_setup.py` | создать | тесты `daemon/setup.py` |
| `app/Sources/MeetMind/RuntimeLogic.swift` | создать | чистая логика без AppKit: события, режим, хэш, транслокация, тексты ошибок |
| `app/Sources/MeetMind/RuntimeManager.swift` | создать | процессы: копия uv, `uv sync`, `daemon.setup`, команда запуска демона |
| `app/Sources/MeetMind/DaemonSupervisor.swift` | изменить | запуск через RuntimeManager, `.notInstalled`/`.updating`, `runtimeReady()`, `restart()` |
| `app/Sources/MeetMind/SetupWizard.swift` | создать | `SetupModel`, вью мастера, `SetupWindow` |
| `app/Sources/MeetMind/MeetMindApp.swift` | изменить | проверка при запуске, транслокация, отложенный рестарт, `openSetup()`, завершение детей |
| `app/Sources/MeetMind/DropdownView.swift` | изменить | «Настройка…» в футере, текст состояния рантайма |
| `app/Tests/main.swift`, `app/Tests/run.sh` | изменить | тесты RuntimeLogic |
| `app/build.sh` | изменить | `Resources/backend` + `Resources/bin/uv` |
| `app/package.sh` | создать | zip для коллег |
| `app/Tools/bundled_smoke.sh` | создать | проверка bundled-режима без GUI |
| `docs/INSTALL.md` | создать | инструкция для коллеги |

---

### Task 1: Зависимости и PATH к ffmpeg рантайма

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Modify: `daemon/app.py:29-32` (`_TOOL_DIRS`), `daemon/app.py:61-67` (`_ensure_tool_path`)
- Test: `tests/test_library_api.py:271-283`

- [ ] **Step 1: Тест — runtime/bin первым в PATH**

В `tests/test_library_api.py` после `test_ensure_tool_path_adds_homebrew` добавить:

```python
def test_ensure_tool_path_puts_runtime_bin_first(monkeypatch, tmp_path):
    """Встроенный режим (чистый Mac): ffmpeg — симлинк в app_dir()/runtime/bin
    (daemon/setup.py link-ffmpeg). Он должен идти раньше Homebrew."""
    from daemon.app import _ensure_tool_path
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _ensure_tool_path()
    import os
    parts = os.environ["PATH"].split(":")
    assert parts[0] == str(tmp_path / "runtime" / "bin")
    _ensure_tool_path()
    assert os.environ["PATH"].split(":").count(str(tmp_path / "runtime" / "bin")) == 1
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/python -m pytest tests/test_library_api.py -q -k ensure_tool_path`
Expected: FAIL (`assert '/opt/homebrew/bin' == '.../runtime/bin'`)

- [ ] **Step 3: Реализация**

В `daemon/app.py` заменить функцию `_ensure_tool_path`:

```python
def _ensure_tool_path() -> None:
    """Дополнить PATH каталогами инструментов (идемпотентно, в начало):
    runtime/bin встроенного режима (ffmpeg из imageio-ffmpeg), затем Homebrew."""
    import os
    dirs = (str(app_dir() / "runtime" / "bin"), *_TOOL_DIRS)
    current = os.environ.get("PATH", "").split(":")
    missing = [d for d in dirs if d not in current]
    if missing:
        os.environ["PATH"] = ":".join(missing + current)
```

Комментарий над `_TOOL_DIRS` дополнить строкой: `# Встроенный режим кладёт ffmpeg в app_dir()/runtime/bin — он идёт первым.`

- [ ] **Step 4: Зависимости**

В `pyproject.toml` в `dependencies` добавить две строки после `"anthropic>=1.13",`:

```toml
  "huggingface_hub>=1.0",
  "imageio-ffmpeg>=0.6",
```

Run: `uv lock && uv sync`
Expected: `uv.lock` обновлён, в `.venv` появился `imageio_ffmpeg`.

- [ ] **Step 5: Все тесты зелёные**

Run: `.venv/bin/python -m pytest -q`
Expected: все PASS (было 247, станет 248).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock daemon/app.py tests/test_library_api.py
git commit -m "feat(daemon): ffmpeg рантайма первым в PATH, зависимости imageio-ffmpeg и huggingface_hub"
```

---

### Task 2: `daemon/setup.py` — каркас и `status`

**Files:**
- Create: `daemon/setup.py`
- Create: `tests/test_setup.py`

- [ ] **Step 1: Тесты `status` и протокола**

Создать `tests/test_setup.py`:

```python
"""daemon/setup.py — команды мастера первичной настройки (JSON-строки в stdout)."""
import io
import json

import pytest

# config при первом импорте делает load_dotenv(репо/.env) — импортируем до
# фикстур, иначе он вернёт в os.environ ключи, которые фикстура удалила.
import config  # noqa: F401
from daemon import setup


def events(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


@pytest.fixture
def appdir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path / "app"))
    for key in ("SUMMARY_BACKEND", "ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    import config
    monkeypatch.setattr(config, "DEV_ENV_PATH", tmp_path / "no-dev.env")
    monkeypatch.setattr(setup, "model_cached", lambda repo=setup.MODEL_REPO: True)
    from daemon import claude_auth
    monkeypatch.setattr(claude_auth, "claude_bin", lambda: None)
    return tmp_path / "app"


def write_config(appdir, text):
    appdir.mkdir(parents=True, exist_ok=True)
    (appdir / "config.env").write_text(text)


def test_status_clean_mac(appdir, capsys):
    assert setup.main(["status"]) == 0
    assert events(capsys) == [{"event": "ok", "model": True, "summary": None, "telegram": False}]


def test_status_api_key(appdir, capsys):
    write_config(appdir, "SUMMARY_BACKEND=api\nANTHROPIC_API_KEY=sk-ant-x\n")
    setup.main(["status"])
    assert events(capsys)[-1]["summary"] == "api"


def test_status_api_without_key_is_not_configured(appdir, capsys):
    write_config(appdir, "SUMMARY_BACKEND=api\n")
    setup.main(["status"])
    assert events(capsys)[-1]["summary"] is None


def test_status_claude_cli_default_needs_binary(appdir, capsys, monkeypatch):
    """SUMMARY_BACKEND не задан — дефолт кода claude_cli; годится, только если
    claude_bin() нашёл бинарь (твой Mac), а на чистом Mac — None."""
    from daemon import claude_auth
    monkeypatch.setattr(claude_auth, "claude_bin", lambda: "/x/claude")
    setup.main(["status"])
    assert events(capsys)[-1]["summary"] == "claude_cli"


def test_status_telegram_needs_token_and_chat(appdir, capsys):
    write_config(appdir, "TELEGRAM_BOT_TOKEN=1:abc\n")
    setup.main(["status"])
    assert events(capsys)[-1]["telegram"] is False
    write_config(appdir, "TELEGRAM_BOT_TOKEN=1:abc\nTELEGRAM_CHAT_ID=42\n")
    setup.main(["status"])
    assert events(capsys)[-1]["telegram"] is True


def test_status_model_missing(appdir, capsys, monkeypatch):
    monkeypatch.setattr(setup, "model_cached", lambda repo=setup.MODEL_REPO: False)
    setup.main(["status"])
    assert events(capsys)[-1]["model"] is False


def test_model_cached_checks_config_and_weights(tmp_path, monkeypatch):
    """Офлайн-проверка по кэшу HF: нужны config.json и веса; сеть не трогаем."""
    from huggingface_hub import constants
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path))
    repo = "org/model"
    snap = tmp_path / "models--org--model" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (tmp_path / "models--org--model" / "refs").mkdir()
    (tmp_path / "models--org--model" / "refs" / "main").write_text("abc")
    (snap / "config.json").write_text("{}")
    assert setup.model_cached(repo) is False
    (snap / "weights.safetensors").write_bytes(b"x")
    assert setup.model_cached(repo) is True


def test_unknown_command(capsys):
    assert setup.main(["nope"]) == 2
    assert events(capsys)[-1]["event"] == "error"
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/python -m pytest tests/test_setup.py -q`
Expected: FAIL / ERROR (`cannot import name 'setup' from 'daemon'`)

- [ ] **Step 3: Реализация каркаса и `status`**

Создать `daemon/setup.py`:

```python
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

# Тот же репозиторий, что грузит Transcriber (daemon/transcriber.py).
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


COMMANDS = {
    "status": cmd_status,
}
# Команды, которым нужен секрет из stdin (первая строка).
SECRET_COMMANDS: set[str] = set()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in COMMANDS:
        emit("error", code="internal", message="команды: " + ", ".join(COMMANDS))
        return 2
    name = argv[0]
    secret = sys.stdin.readline().strip() if name in SECRET_COMMANDS else None
    try:
        result = COMMANDS[name](secret) if secret is not None else COMMANDS[name]()
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
```

Примечание: `MODEL_REPO` дублирует `daemon/transcriber.py:31` намеренно — импорт transcriber тянет soundfile. Добавить тест на совпадение (Step 4).

- [ ] **Step 4: Тест — репозиторий модели совпадает с Transcriber**

В конец `tests/test_setup.py`:

```python
def test_model_repo_matches_transcriber():
    from daemon import transcriber
    assert setup.MODEL_REPO == transcriber.MODEL_REPO
```

- [ ] **Step 5: Запустить — проходит**

Run: `.venv/bin/python -m pytest tests/test_setup.py -q`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add daemon/setup.py tests/test_setup.py
git commit -m "feat(setup): CLI мастера настройки — каркас и status"
```

---

### Task 3: `check-key`

**Files:**
- Modify: `daemon/setup.py`
- Test: `tests/test_setup.py`

- [ ] **Step 1: Тесты**

В конец `tests/test_setup.py`:

```python
class FakeModels:
    def __init__(self, exc=None):
        self.exc = exc

    def list(self, limit):
        if self.exc:
            raise self.exc
        return []


def fake_anthropic(monkeypatch, exc=None):
    import anthropic

    class Client:
        def __init__(self, *, api_key, **kwargs):
            self.api_key = api_key
            self.models = FakeModels(exc)

    monkeypatch.setattr(anthropic, "Anthropic", Client)


def run_with_stdin(monkeypatch, argv, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text + "\n"))
    return setup.main(argv)


def test_check_key_ok_writes_backend_and_key(appdir, capsys, monkeypatch):
    import stat
    fake_anthropic(monkeypatch)
    assert run_with_stdin(monkeypatch, ["check-key"], "sk-ant-good") == 0
    assert events(capsys) == [{"event": "ok"}]
    cfg = appdir / "config.env"
    text = cfg.read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-good" in text and "SUMMARY_BACKEND=api" in text
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_check_key_rejected(appdir, capsys, monkeypatch):
    import anthropic
    import httpx
    resp = httpx.Response(401, request=httpx.Request("GET", "https://api.anthropic.com/v1/models"))
    fake_anthropic(monkeypatch, anthropic.AuthenticationError("bad sk-ant-bad", response=resp, body=None))
    assert run_with_stdin(monkeypatch, ["check-key"], "sk-ant-bad") == 1
    ev = events(capsys)[-1]
    assert ev["code"] == "invalid_key"
    assert "sk-ant-bad" not in ev["message"]
    assert not (appdir / "config.env").exists()


def test_check_key_no_network(appdir, capsys, monkeypatch):
    import anthropic
    import httpx
    fake_anthropic(monkeypatch, anthropic.APIConnectionError(
        request=httpx.Request("GET", "https://api.anthropic.com/v1/models")))
    run_with_stdin(monkeypatch, ["check-key"], "sk-ant-x")
    assert events(capsys)[-1]["code"] == "no_network"


def test_check_key_empty(appdir, capsys, monkeypatch):
    assert run_with_stdin(monkeypatch, ["check-key"], "") == 1
    assert events(capsys)[-1]["code"] == "invalid_key"
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/python -m pytest tests/test_setup.py -q -k check_key`
Expected: FAIL (код выхода 2 — команды нет)

- [ ] **Step 3: Реализация**

В `daemon/setup.py` перед `COMMANDS` добавить:

```python
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
```

Обновить таблицы:

```python
COMMANDS = {
    "status": cmd_status,
    "check-key": cmd_check_key,
}
SECRET_COMMANDS = {"check-key"}
```

В `main` поправить вызов: секретные команды получают строку даже пустой:

```python
    secret = sys.stdin.readline().strip() if name in SECRET_COMMANDS else None
    try:
        result = COMMANDS[name](secret) if name in SECRET_COMMANDS else COMMANDS[name]()
```

- [ ] **Step 4: Запустить — проходит**

Run: `.venv/bin/python -m pytest tests/test_setup.py -q`
Expected: все PASS

- [ ] **Step 5: Commit**

```bash
git add daemon/setup.py tests/test_setup.py
git commit -m "feat(setup): check-key — проверка ключа Anthropic и включение API"
```

---

### Task 4: `telegram-check` и `telegram-connect`

**Files:**
- Modify: `daemon/setup.py`
- Test: `tests/test_setup.py`

- [ ] **Step 1: Тесты**

В конец `tests/test_setup.py`:

```python
class FakeTelegram:
    """Подмена requests.post для Bot API: очередь ответов по методу."""

    def __init__(self, responses):
        self.responses = responses  # {method: [(status, json), ...]}
        self.calls = []

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json or {}))
        status, body = self.responses[method].pop(0)

        class R:
            status_code = status

            def json(self_inner):
                return body
        return R()


def fake_tg(monkeypatch, responses):
    import requests
    tg = FakeTelegram(responses)
    monkeypatch.setattr(requests, "post", tg.post)
    return tg


def test_telegram_check_ok(appdir, capsys, monkeypatch):
    fake_tg(monkeypatch, {"getMe": [(200, {"ok": True, "result": {"username": "my_bot"}})]})
    assert run_with_stdin(monkeypatch, ["telegram-check"], "1:abc") == 0
    assert events(capsys)[-1] == {"event": "ok", "bot": "my_bot"}


def test_telegram_check_bad_token(appdir, capsys, monkeypatch):
    fake_tg(monkeypatch, {"getMe": [(401, {"ok": False, "description": "Unauthorized"})]})
    run_with_stdin(monkeypatch, ["telegram-check"], "1:abc")
    assert events(capsys)[-1]["code"] == "invalid_token"


def test_telegram_network_error_hides_token(appdir, capsys, monkeypatch):
    import requests

    def boom(url, json=None, timeout=None):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
    monkeypatch.setattr(requests, "post", boom)
    run_with_stdin(monkeypatch, ["telegram-check"], "1:secret")
    ev = events(capsys)[-1]
    assert ev["code"] == "no_network"
    assert "1:secret" not in ev["message"]


def test_telegram_connect_waits_for_start(appdir, capsys, monkeypatch):
    start = {"update_id": 7, "message": {"text": "/start", "chat": {"id": 555}}}
    other = {"update_id": 6, "message": {"text": "привет", "chat": {"id": 1}}}
    tg = fake_tg(monkeypatch, {
        "getUpdates": [(200, {"ok": True, "result": []}),
                       (200, {"ok": True, "result": [other, start]}),
                       (200, {"ok": True, "result": []})],
        "sendMessage": [(200, {"ok": True, "result": {}})],
    })
    assert run_with_stdin(monkeypatch, ["telegram-connect"], "1:abc") == 0
    assert events(capsys)[-1] == {"event": "ok", "chat_id": 555}
    ack = [p for m, p in tg.calls if m == "getUpdates"][-1]
    assert ack["offset"] == 8
    assert ("sendMessage", {"chat_id": 555, "text": setup.TELEGRAM_HELLO}) in tg.calls
    text = (appdir / "config.env").read_text()
    assert "TELEGRAM_BOT_TOKEN=1:abc" in text and "TELEGRAM_CHAT_ID=555" in text


def test_telegram_connect_timeout(appdir, capsys, monkeypatch):
    import itertools
    # deadline, проверка цикла, расчёт wait — 0; дальше время вышло.
    clock = itertools.chain([0, 0, 0], itertools.repeat(200))
    monkeypatch.setattr(setup.time, "monotonic", lambda: next(clock))
    fake_tg(monkeypatch, {"getUpdates": [(200, {"ok": True, "result": []})]})
    run_with_stdin(monkeypatch, ["telegram-connect"], "1:abc")
    assert events(capsys)[-1]["code"] == "timeout"


def test_telegram_connect_bot_busy(appdir, capsys, monkeypatch):
    fake_tg(monkeypatch, {"getUpdates": [(409, {"ok": False, "description": "Conflict: webhook"})]})
    run_with_stdin(monkeypatch, ["telegram-connect"], "1:abc")
    assert events(capsys)[-1]["code"] == "bot_busy"
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/python -m pytest tests/test_setup.py -q -k telegram`
Expected: FAIL (код 2)

- [ ] **Step 3: Реализация**

В `daemon/setup.py` к импортам добавить `import time`. Перед `COMMANDS`:

```python
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
```

Таблицы:

```python
COMMANDS = {
    "status": cmd_status,
    "check-key": cmd_check_key,
    "telegram-check": cmd_telegram_check,
    "telegram-connect": cmd_telegram_connect,
}
SECRET_COMMANDS = {"check-key", "telegram-check", "telegram-connect"}
```

- [ ] **Step 4: Запустить — проходит**

Run: `.venv/bin/python -m pytest tests/test_setup.py -q`
Expected: все PASS

- [ ] **Step 5: Commit**

```bash
git add daemon/setup.py tests/test_setup.py
git commit -m "feat(setup): telegram-check и telegram-connect — бот без ручного chat id"
```

---

### Task 5: `install-model` с прогрессом

**Files:**
- Modify: `daemon/setup.py`
- Test: `tests/test_setup.py`

- [ ] **Step 1: Тесты**

В конец `tests/test_setup.py`:

```python
def fake_hub(monkeypatch, tmp_path, sizes, cached=()):
    """Подмена huggingface_hub: model_info с размерами, hf_hub_download, который
    двигает переданный tqdm_class шагами по 10 байт."""
    import huggingface_hub
    from huggingface_hub import constants
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path / "hub"))

    class Sibling:
        def __init__(self, name, size):
            self.rfilename, self.size = name, size

    class Info:
        siblings = [Sibling(n, s) for n, s in sizes.items()]

    class Api:
        def model_info(self, repo, files_metadata=False):
            assert files_metadata
            return Info()

    downloaded = []

    def download(repo, filename, tqdm_class=None):
        bar = tqdm_class(total=sizes[filename])
        for _ in range(sizes[filename] // 10):
            bar.update(10)
        bar.close()
        downloaded.append(filename)
        return str(tmp_path / filename)

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    monkeypatch.setattr(setup, "_cached_file", lambda repo, name: name in cached)
    return downloaded


def test_install_model_reports_progress(appdir, capsys, monkeypatch, tmp_path):
    ticks = iter(range(0, 10_000))
    monkeypatch.setattr(setup.time, "monotonic", lambda: next(ticks) * 0.3)
    downloaded = fake_hub(monkeypatch, tmp_path, {"config.json": 10, "weights.safetensors": 100},
                          cached={"config.json"})
    assert setup.main(["install-model"]) == 0
    evs = events(capsys)
    progress = [e for e in evs if e["event"] == "progress"]
    assert downloaded == ["weights.safetensors"]          # закэшированное не качаем
    assert all(e["total"] == 110 and e["stage"] == "model" for e in progress)
    assert progress[0]["done"] >= 10                      # config.json уже учтён
    assert progress[-1]["done"] == 110                    # финал всегда отправлен
    assert len(progress) < 10                             # троттлинг 0,5 с
    assert evs[-1] == {"event": "ok", "model": setup.MODEL_REPO}


def test_install_model_removes_stale_incomplete(appdir, capsys, monkeypatch, tmp_path):
    fake_hub(monkeypatch, tmp_path, {"weights.safetensors": 10})
    blobs = tmp_path / "hub" / "models--mlx-community--whisper-large-v3-turbo" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "abc.123.incomplete").write_bytes(b"x" * 5)
    (blobs / "done-blob").write_bytes(b"y")
    setup.main(["install-model"])
    assert not (blobs / "abc.123.incomplete").exists()
    assert (blobs / "done-blob").exists()


def test_install_model_disk_full_before_start(appdir, capsys, monkeypatch, tmp_path):
    fake_hub(monkeypatch, tmp_path, {"weights.safetensors": 10 * 2**30})
    monkeypatch.setattr(setup, "_free_bytes", lambda path: 2**30)
    assert setup.main(["install-model"]) == 1
    ev = events(capsys)[-1]
    assert ev["code"] == "disk_full" and "ГБ" in ev["message"]


def test_install_model_no_network(appdir, capsys, monkeypatch, tmp_path):
    import huggingface_hub
    import httpx
    from huggingface_hub import constants
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path / "hub"))

    class Api:
        def model_info(self, repo, files_metadata=False):
            raise httpx.ConnectError("nodename nor servname provided")
    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    setup.main(["install-model"])
    assert events(capsys)[-1]["code"] == "no_network"
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/python -m pytest tests/test_setup.py -q -k install_model`
Expected: FAIL (код 2)

- [ ] **Step 3: Реализация**

В `daemon/setup.py` к импортам добавить `import shutil` и `from pathlib import Path`. Заменить `model_cached` и добавить загрузку:

```python
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
```

В `COMMANDS` добавить `"install-model": cmd_install_model,`.

Примечание: `huggingface_hub.HfApi` и `huggingface_hub.hf_hub_download` вызываются через модуль (не `from … import`) — так их подменяют тесты.

- [ ] **Step 4: Запустить — проходит**

Run: `.venv/bin/python -m pytest tests/test_setup.py -q`
Expected: все PASS. Если `test_model_cached_checks_config_and_weights` упал после замены `model_cached` — проверить, что `_cached_file` использует `try_to_load_from_cache` и тест не подменяет `_cached_file`.

- [ ] **Step 5: Ручная проверка на настоящем HF (модель уже в кэше)**

Run: `.venv/bin/python -m daemon.setup install-model`
Expected: две строки `progress` с `done == total` (~1,6 ГБ) и `{"event": "ok", "model": "mlx-community/whisper-large-v3-turbo"}` за секунды — ничего не качается.

- [ ] **Step 6: Commit**

```bash
git add daemon/setup.py tests/test_setup.py
git commit -m "feat(setup): install-model — загрузка Whisper с прогрессом и проверкой места"
```

---

### Task 6: `link-ffmpeg` и проверка ffmpeg из imageio

**Files:**
- Modify: `daemon/setup.py`
- Test: `tests/test_setup.py`

- [ ] **Step 1: Тесты**

В конец `tests/test_setup.py`:

```python
def test_link_ffmpeg(appdir, capsys, monkeypatch, tmp_path):
    import imageio_ffmpeg
    exe = tmp_path / "ffmpeg-macos-aarch64"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: str(exe))
    assert setup.main(["link-ffmpeg"]) == 0
    link = appdir / "runtime" / "bin" / "ffmpeg"
    assert link.is_symlink() and link.resolve() == exe.resolve()
    assert setup.main(["link-ffmpeg"]) == 0  # повтор не падает
    assert events(capsys)[-1] == {"event": "ok", "ffmpeg": str(link)}


@pytest.mark.slow
def test_imageio_ffmpeg_has_what_daemon_uses():
    """stages.py/transcriber.py/reconcile.py: flac, channelsplit, -ignore_length."""
    import subprocess

    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()

    def out(*args):
        return subprocess.run([exe, "-hide_banner", *args], capture_output=True, text=True).stdout

    assert " flac " in out("-encoders")
    assert "channelsplit" in out("-filters")
    assert "ignore_length" in out("-h", "demuxer=wav")
```

- [ ] **Step 2: Запустить — падает**

Run: `.venv/bin/python -m pytest tests/test_setup.py -q -k link_ffmpeg`
Expected: FAIL (код 2)

- [ ] **Step 3: Реализация**

В `daemon/setup.py` перед `COMMANDS`:

```python
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
```

В `COMMANDS` добавить `"link-ffmpeg": cmd_link_ffmpeg,`.

- [ ] **Step 4: Запустить — проходит, включая slow**

Run: `.venv/bin/python -m pytest tests/test_setup.py -q && .venv/bin/python -m pytest tests/test_setup.py -q -m slow`
Expected: все PASS; slow — 1 passed.

- [ ] **Step 5: Полный прогон и commit**

Run: `.venv/bin/python -m pytest -q`
Expected: все PASS.

```bash
git add daemon/setup.py tests/test_setup.py
git commit -m "feat(setup): link-ffmpeg — ffmpeg из imageio-ffmpeg для чистого Mac"
```

---

### Task 7: `RuntimeLogic.swift` — чистая логика + тесты

**Files:**
- Create: `app/Sources/MeetMind/RuntimeLogic.swift`
- Modify: `app/Tests/main.swift`, `app/Tests/run.sh`

- [ ] **Step 1: Тесты**

В `app/Tests/run.sh` в строку `swiftc` добавить `"$DIR/../Sources/MeetMind/RuntimeLogic.swift"` перед `"$DIR/main.swift"`.

В `app/Tests/main.swift` перед финальной сводкой (перед кодом, который печатает итог и выходит по `failures`) добавить:

```swift
// --- RuntimeLogic: события daemon.setup
do {
    if case .progress(let stage, let done, let total)? =
        SetupEvent.parse(#"{"event": "progress", "stage": "model", "done": 5, "total": 10}"#) {
        check(stage == "model" && done == 5 && total == 10, "progress разобран")
    } else { check(false, "progress не разобран") }

    if case .ok(let r)? = SetupEvent.parse(#"{"event": "ok", "model": true, "summary": null, "telegram": false, "chat_id": 555, "bot": "b"}"#) {
        check(r.bool("model") && !r.bool("telegram"), "bool-поля")
        check(r.string("summary") == nil && r.string("bot") == "b", "string/null")
        check(r.int("chat_id") == 555, "int")
    } else { check(false, "ok не разобран") }

    if case .error(let code, let message)? = SetupEvent.parse(#"{"event": "error", "code": "no_network", "message": "x"}"#) {
        check(code == "no_network" && message == "x", "error разобран")
    } else { check(false, "error не разобран") }

    check(SetupEvent.parse("Downloading cpython-3.11") == nil, "не-JSON строка игнорируется")
    check(SetupEvent.parse(#"{"foo": 1}"#) == nil, "JSON без event игнорируется")
}

// --- RuntimeLogic: режим запуска
do {
    let repo = "/r"
    let uv = "/opt/homebrew/bin/uv"
    func mode(saved: String? = nil, env: String? = nil, derived: String = "/nope",
              uvs: [String] = ["/opt/homebrew/bin/uv"], repos: Set<String> = ["/r"],
              override: [String]? = nil) -> RuntimeMode {
        RuntimeLogic.resolveMode(savedRepo: saved, envRepo: env, derivedRepo: derived,
                                 uvCandidates: uvs, commandOverride: override,
                                 isRepo: { repos.contains($0) }, isExecutable: { $0 == uv })
    }
    check(mode(saved: repo) == .dev(repo: repo, uv: uv, command: [uv, "run", "python", "main.py"]), "repoPath + uv → dev")
    check(mode(env: repo) == .dev(repo: repo, uv: uv, command: [uv, "run", "python", "main.py"]), "MEETMIND_REPO → dev")
    check(mode(derived: repo) == .dev(repo: repo, uv: uv, command: [uv, "run", "python", "main.py"]), "рядом со сборкой → dev")
    check(mode(saved: repo, uvs: []) == .bundled, "репо без uv → bundled")
    check(mode() == .bundled, "чистый Mac → bundled")
    check(mode(saved: repo, override: ["/x/python", "main.py"]) == .dev(repo: repo, uv: uv, command: ["/x/python", "main.py"]), "daemonCommand из настроек")
}

// --- RuntimeLogic: транслокация, хэш, обновление рантайма
do {
    check(RuntimeLogic.isTranslocated("/private/var/folders/x/T/AppTranslocation/ABC/d/BeeDott.app"), "транслокация")
    check(!RuntimeLogic.isTranslocated("/Applications/BeeDott.app"), "из Программ — нет")
    check(RuntimeLogic.sha256Hex(Data("abc".utf8)) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "sha256")
    check(RuntimeLogic.needsSync(bundledLockHash: "a", installedLockHash: "a", venvPythonExists: true) == false, "тот же lock — не синхронизируем")
    check(RuntimeLogic.needsSync(bundledLockHash: "b", installedLockHash: "a", venvPythonExists: true), "новый lock — синхронизируем")
    check(RuntimeLogic.needsSync(bundledLockHash: "a", installedLockHash: "a", venvPythonExists: false), "нет python — синхронизируем")
    check(RuntimeLogic.needsSync(bundledLockHash: "a", installedLockHash: nil, venvPythonExists: true), "нет lock.sha — синхронизируем")
    check(RuntimeLogic.setupErrorText(code: "no_network", message: "").contains("интернет"), "текст no_network")
    check(RuntimeLogic.setupErrorText(code: "weird", message: "boom").contains("boom"), "неизвестный код — сообщение как есть")
}
```

- [ ] **Step 2: Запустить — не собирается**

Run: `app/Tests/run.sh`
Expected: ошибка компиляции `cannot find 'SetupEvent' in scope`.

- [ ] **Step 3: Реализация**

Создать `app/Sources/MeetMind/RuntimeLogic.swift`:

```swift
// RuntimeLogic.swift — чистая логика первичной настройки (без AppKit, тесты — app/Tests).
//
// SetupEvent — строка JSON от `python -m daemon.setup` (daemon/setup.py);
// RuntimeMode — откуда запускать демон: dev (репо + uv) или bundled (рантайм в
// Application Support, docs/superpowers/specs/2026-10-13-first-run-setup-design.md).

import CryptoKit
import Foundation

struct SetupResult {
    let fields: [String: Any]

    func string(_ key: String) -> String? { fields[key] as? String }
    func bool(_ key: String) -> Bool { (fields[key] as? Bool) ?? false }
    func int(_ key: String) -> Int? { (fields[key] as? NSNumber)?.intValue }
}

enum SetupEvent {
    case progress(stage: String, done: Int64, total: Int64)
    case info(String)
    case ok(SetupResult)
    case error(code: String, message: String)

    /// nil — строка не событие (вывод библиотек, пустая строка).
    static func parse(_ line: String) -> SetupEvent? {
        guard let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let event = obj["event"] as? String else { return nil }
        switch event {
        case "progress":
            return .progress(stage: obj["stage"] as? String ?? "",
                             done: (obj["done"] as? NSNumber)?.int64Value ?? 0,
                             total: (obj["total"] as? NSNumber)?.int64Value ?? 0)
        case "info":
            return .info(obj["message"] as? String ?? "")
        case "ok":
            var fields = obj
            fields["event"] = nil
            return .ok(SetupResult(fields: fields))
        case "error":
            return .error(code: obj["code"] as? String ?? "internal",
                          message: obj["message"] as? String ?? "")
        default:
            return nil
        }
    }
}

enum RuntimeMode: Equatable {
    /// Репозиторий + uv в системе (машина разработчика): `uv run python main.py`.
    case dev(repo: String, uv: String, command: [String])
    /// Чистый Mac: Python и пакеты в Application Support/MeetMind/runtime.
    case bundled
}

enum RuntimeLogic {
    /// Порядок поиска репо — как раньше в DaemonSupervisor (аудит T2):
    /// UserDefaults repoPath → env MEETMIND_REPO → `<repo>/app/build/BeeDott.app`.
    static func resolveMode(savedRepo: String?, envRepo: String?, derivedRepo: String,
                            uvCandidates: [String], commandOverride: [String]?,
                            isRepo: (String) -> Bool,
                            isExecutable: (String) -> Bool) -> RuntimeMode {
        let repo = [savedRepo, envRepo, derivedRepo].compactMap { $0 }.first(where: isRepo)
        guard let repo, let uv = uvCandidates.first(where: isExecutable) else { return .bundled }
        let command = (commandOverride?.isEmpty == false) ? commandOverride! : [uv, "run", "python", "main.py"]
        return .dev(repo: repo, uv: uv, command: command)
    }

    /// Запуск прямо из Загрузок: macOS исполняет копию из read-only пути.
    static func isTranslocated(_ bundlePath: String) -> Bool {
        bundlePath.contains("/AppTranslocation/")
    }

    static func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    static func needsSync(bundledLockHash: String?, installedLockHash: String?,
                          venvPythonExists: Bool) -> Bool {
        !venvPythonExists || installedLockHash == nil || bundledLockHash != installedLockHash
    }

    /// Текст для человека по коду ошибки daemon/setup.py.
    static func setupErrorText(code: String, message: String) -> String {
        switch code {
        case "no_network": return "Нет интернета — проверь подключение и нажми «Повторить»"
        case "disk_full": return "Не хватает места на диске" + (message.isEmpty ? "" : ": \(message)")
        case "invalid_key": return "Ключ не подходит — проверь, что скопировал его целиком"
        case "invalid_token": return "Токен не подходит — скопируй его из @BotFather целиком"
        case "bot_busy": return "Этот бот уже подключён к другому сервису — создай нового в @BotFather"
        case "timeout": return "Бот не получил /start — открой его в Telegram и нажми «Запустить»"
        default: return "Что-то пошло не так: \(message)"
        }
    }
}
```

- [ ] **Step 4: Запустить — проходит**

Run: `app/Tests/run.sh`
Expected: `OK — все тесты прошли`

- [ ] **Step 5: Commit**

```bash
git add app/Sources/MeetMind/RuntimeLogic.swift app/Tests/main.swift app/Tests/run.sh
git commit -m "feat(app): RuntimeLogic — события setup, режим запуска, хэш lock"
```

---

### Task 8: `RuntimeManager.swift` — процессы

**Files:**
- Create: `app/Sources/MeetMind/RuntimeManager.swift`

Тесты: логика вынесена в RuntimeLogic (Task 7); процессы проверяются `app/Tools/bundled_smoke.sh` (Task 12) и вживую.

- [ ] **Step 1: Реализация**

Создать `app/Sources/MeetMind/RuntimeManager.swift`:

```swift
// RuntimeManager.swift — где живёт Python-рантайм демона и как его ставить
// (docs/superpowers/specs/2026-10-13-first-run-setup-design.md).
//
// dev     — репозиторий + uv (твой Mac): как раньше, `uv run python main.py`.
// bundled — чистый Mac: Contents/Resources/backend (исходники) + bin/uv;
//           `uv sync` ставит Python и пакеты в Application Support/MeetMind/runtime.
// Все методы, запускающие процессы, блокирующие — звать не с main.

import Darwin
import Foundation

final class RuntimeManager: @unchecked Sendable {
    static let shared = RuntimeManager()

    struct Launch {
        let executable: String
        let arguments: [String]
        let cwd: String
        let environment: [String: String]
    }

    private let fm = FileManager.default
    private let lock = NSLock()
    private var children: [Process] = []

    var runtimeDir: URL { ControlClient.appSupportDir.appendingPathComponent("runtime") }
    var binDir: URL { runtimeDir.appendingPathComponent("bin") }
    var venvPython: URL { runtimeDir.appendingPathComponent("venv/bin/python") }
    private var lockShaFile: URL { runtimeDir.appendingPathComponent("lock.sha") }
    var backendDir: URL? { resource("backend") }
    private var bundledUv: URL? { resource("bin/uv") }

    var setupLogURL: URL {
        let dir = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/MeetMind")
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("setup.log")
    }

    private func resource(_ path: String) -> URL? {
        guard let url = Bundle.main.resourceURL?.appendingPathComponent(path),
              fm.fileExists(atPath: url.path) else { return nil }
        return url
    }

    // MARK: - режим

    var mode: RuntimeMode {
        let home = fm.homeDirectoryForCurrentUser.path
        let derived = Bundle.main.bundleURL
            .deletingLastPathComponent()  // build/
            .deletingLastPathComponent()  // app/
            .deletingLastPathComponent()  // <repo>/
            .path
        return RuntimeLogic.resolveMode(
            savedRepo: UserDefaults.standard.string(forKey: "repoPath"),
            envRepo: ProcessInfo.processInfo.environment["MEETMIND_REPO"],
            derivedRepo: derived,
            uvCandidates: ["/opt/homebrew/bin/uv", "/usr/local/bin/uv",
                           "\(home)/.local/bin/uv", "\(home)/.cargo/bin/uv"],
            commandOverride: UserDefaults.standard.array(forKey: "daemonCommand") as? [String],
            isRepo: { [fm] in fm.fileExists(atPath: ($0 as NSString).appendingPathComponent("main.py")) },
            isExecutable: { [fm] in fm.isExecutableFile(atPath: $0) })
    }

    var isDev: Bool { if case .dev = mode { return true } else { return false } }

    /// Рантайм есть: dev — всегда; bundled — venv создан (может требовать sync).
    var hasRuntime: Bool { isDev || fm.isExecutableFile(atPath: venvPython.path) }

    /// bundled: uv.lock в приложении новее установленного (или рантайма нет).
    var needsSync: Bool {
        guard !isDev else { return false }
        let bundled = backendDir.flatMap { try? Data(contentsOf: $0.appendingPathComponent("uv.lock")) }
            .map(RuntimeLogic.sha256Hex)
        let installed = (try? String(contentsOf: lockShaFile, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return RuntimeLogic.needsSync(bundledLockHash: bundled, installedLockHash: installed,
                                      venvPythonExists: fm.isExecutableFile(atPath: venvPython.path))
    }

    func freeDiskBytes() -> Int64 {
        let values = try? ControlClient.appSupportDir.deletingLastPathComponent()
            .resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return values?.volumeAvailableCapacityForImportantUsage ?? .max
    }

    // MARK: - команды запуска

    private func bundledEnvironment(extra: [String: String] = [:]) -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = binDir.path + ":" + (env["PATH"] ?? "/usr/bin:/bin")
        env["PYTHONPYCACHEPREFIX"] = runtimeDir.appendingPathComponent("pycache").path
        env.merge(extra) { _, new in new }
        return env
    }

    /// Команда демона; nil — bundled-рантайм не установлен.
    func daemonLaunch() -> Launch? {
        switch mode {
        case .dev(let repo, _, let command):
            return Launch(executable: command[0], arguments: Array(command.dropFirst()),
                          cwd: repo, environment: ProcessInfo.processInfo.environment)
        case .bundled:
            guard let backendDir, fm.isExecutableFile(atPath: venvPython.path) else { return nil }
            // cwd = runtime/: config.py делает mkdir("sessions") от cwd — не внутри .app.
            return Launch(executable: venvPython.path,
                          arguments: [backendDir.appendingPathComponent("main.py").path],
                          cwd: runtimeDir.path, environment: bundledEnvironment())
        }
    }

    private func setupLaunch(_ command: String) -> Launch? {
        switch mode {
        case .dev(let repo, let uv, _):
            return Launch(executable: uv, arguments: ["run", "python", "-m", "daemon.setup", command],
                          cwd: repo, environment: ProcessInfo.processInfo.environment)
        case .bundled:
            guard let backendDir, fm.isExecutableFile(atPath: venvPython.path) else { return nil }
            return Launch(executable: venvPython.path, arguments: ["-m", "daemon.setup", command],
                          cwd: runtimeDir.path,
                          environment: bundledEnvironment(extra: ["PYTHONPATH": backendDir.path]))
        }
    }

    // MARK: - процессы

    private func makeProcess(_ launch: Launch) -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: launch.executable)
        p.arguments = launch.arguments
        p.currentDirectoryURL = URL(fileURLWithPath: launch.cwd)
        p.environment = launch.environment
        return p
    }

    private func track(_ p: Process) { lock.lock(); children.append(p); lock.unlock() }
    private func untrack(_ p: Process) { lock.lock(); children.removeAll { $0 === p }; lock.unlock() }

    /// macOS не убивает детей вместе с родителем — зовётся из applicationWillTerminate.
    func terminateChildren() {
        lock.lock(); let running = children; lock.unlock()
        for p in running where p.isRunning { p.terminate() }
    }

    private func appendLog(_ text: String) {
        guard let data = text.data(using: .utf8) else { return }
        if !fm.fileExists(atPath: setupLogURL.path) { fm.createFile(atPath: setupLogURL.path, contents: nil) }
        if let h = try? FileHandle(forWritingTo: setupLogURL) {
            h.seekToEndOfFile(); h.write(data); try? h.close()
        }
    }

    /// Построчно читает handle до EOF.
    private func readLines(_ handle: FileHandle, _ onLine: (String) -> Void) {
        var buffer = Data()
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            buffer.append(chunk)
            while let nl = buffer.firstIndex(of: 0x0A) {
                let line = String(decoding: buffer[buffer.startIndex..<nl], as: UTF8.self)
                buffer.removeSubrange(buffer.startIndex...nl)
                onLine(line)
            }
        }
        if !buffer.isEmpty { onLine(String(decoding: buffer, as: UTF8.self)) }
    }

    /// `python -m daemon.setup <command>`; секрет — первой строкой stdin. Возвращает
    /// финальное событие (.ok или .error), промежуточные отдаёт в onEvent.
    func runSetup(_ command: String, stdin secret: String? = nil,
                  onEvent: (SetupEvent) -> Void = { _ in }) -> SetupEvent {
        guard let launch = setupLaunch(command) else {
            return .error(code: "internal", message: "компоненты не установлены")
        }
        let p = makeProcess(launch)
        let out = Pipe(), input = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardInput = input
        p.standardError = err
        do { try p.run() } catch {
            return .error(code: "internal", message: error.localizedDescription)
        }
        track(p)
        defer { untrack(p) }
        if let secret { input.fileHandleForWriting.write(Data((secret + "\n").utf8)) }
        try? input.fileHandleForWriting.close()
        // stderr (бары xet, предупреждения HF) — в лог, не в поток событий.
        let errQueue = DispatchQueue(label: "com.jvmshid.meetmind.setup-stderr")
        errQueue.async { [self] in readLines(err.fileHandleForReading) { appendLog("[\(command)] \($0)\n") } }
        var final: SetupEvent?
        readLines(out.fileHandleForReading) { line in
            guard let event = SetupEvent.parse(line) else { return }
            switch event {
            case .ok, .error: final = event
            case .progress, .info: onEvent(event)
            }
        }
        p.waitUntilExit()
        errQueue.sync {}
        return final ?? .error(code: "internal", message: "\(command): код выхода \(p.terminationStatus)")
    }

    /// bundled: копия uv без карантина → `uv sync` → link-ffmpeg → lock.sha.
    /// onLine — строки вывода uv. nil — успех, иначе текст ошибки (хвост вывода).
    func syncRuntime(onLine: @escaping (String) -> Void) -> String? {
        guard let backendDir, let bundledUv else {
            return "в приложении нет компонентов — сборка без Resources/backend"
        }
        do {
            try fm.createDirectory(at: binDir, withIntermediateDirectories: true)
            let uv = binDir.appendingPathComponent("uv")
            if !fm.contentsEqual(atPath: uv.path, andPath: bundledUv.path) {
                try? fm.removeItem(at: uv)
                try fm.copyItem(at: bundledUv, to: uv)
                try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: uv.path)
            }
            // Скачанный zip помечает всё карантином — снимаем только с копии, бандл не трогаем.
            removexattr(uv.path, "com.apple.quarantine", 0)

            let p = Process()
            p.executableURL = uv
            p.arguments = ["sync", "--frozen", "--no-dev", "--python", "3.11",
                           "--project", backendDir.path]
            var env = ProcessInfo.processInfo.environment
            env["UV_PROJECT_ENVIRONMENT"] = runtimeDir.appendingPathComponent("venv").path
            env["UV_PYTHON_INSTALL_DIR"] = runtimeDir.appendingPathComponent("python").path
            env["UV_CACHE_DIR"] = runtimeDir.appendingPathComponent("uv-cache").path
            env["UV_PYTHON_PREFERENCE"] = "only-managed"
            env["NO_COLOR"] = "1"
            p.environment = env
            p.currentDirectoryURL = runtimeDir
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = pipe
            appendLog("\n== uv sync \(Date())\n")
            try p.run()
            track(p)
            defer { untrack(p) }
            var tail: [String] = []
            readLines(pipe.fileHandleForReading) { line in
                appendLog(line + "\n")
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                guard !trimmed.isEmpty else { return }
                tail = Array((tail + [trimmed]).suffix(6))
                onLine(trimmed)
            }
            p.waitUntilExit()
            guard p.terminationStatus == 0 else { return tail.joined(separator: "\n") }
        } catch {
            return error.localizedDescription
        }
        if case .error(let code, let message) = runSetup("link-ffmpeg") {
            return RuntimeLogic.setupErrorText(code: code, message: message)
        }
        if let data = try? Data(contentsOf: backendDir.appendingPathComponent("uv.lock")) {
            try? RuntimeLogic.sha256Hex(data).write(to: lockShaFile, atomically: true, encoding: .utf8)
        }
        return nil
    }
}
```

- [ ] **Step 2: Компиляция**

Run: `cd app && swiftc -target arm64-apple-macosx14.4 -o "${TMPDIR:-/tmp}/beedott-check" Sources/MeetMind/*.swift`
Expected: без ошибок (предупреждения допустимы).

- [ ] **Step 3: Commit**

```bash
git add app/Sources/MeetMind/RuntimeManager.swift
git commit -m "feat(app): RuntimeManager — uv sync, daemon.setup, команда демона"
```

---

### Task 9: DaemonSupervisor через RuntimeManager

**Files:**
- Modify: `app/Sources/MeetMind/DaemonSupervisor.swift`

- [ ] **Step 1: Состояния**

В `enum RunState` добавить три случая:

```swift
        case notInstalled          // bundled-рантайма нет — ждём мастер (runtimeReady)
        case updating              // uv sync после обновления приложения
        case updateFailed(String)  // uv sync упал — демон не запущен
```

- [ ] **Step 2: Убрать `daemonCommand` и `repoPath`**

Удалить вычисляемые свойства `daemonCommand` и `repoPath` целиком (их логика теперь в `RuntimeLogic.resolveMode` / `RuntimeManager.mode`). Обновить шапочный комментарий файла: «dev-режим: uv run python main.py из репо; bundled: python из Application Support/MeetMind/runtime (RuntimeManager)».

- [ ] **Step 3: bootstrap с установкой/обновлением**

Заменить `bootstrap()`:

```swift
    private func bootstrap() {
        if (try? ControlClient.health()) != nil {
            log("health() ok на старте — демон уже запущен снаружи, супервизор только поллит")
            setState(.healthy)
            startPolling()
            return
        }
        let runtime = RuntimeManager.shared
        guard runtime.hasRuntime else {
            log("рантайм не установлен — жду мастер первичной настройки")
            setState(.notInstalled)
            return
        }
        if runtime.needsSync {
            log("uv.lock приложения изменился — обновляю компоненты")
            setState(.updating)
            if let failure = runtime.syncRuntime(onLine: { _ in }) {
                log("обновление компонентов не удалось: \(failure)")
                setState(.updateFailed(failure))
                return
            }
        }
        spawnProcess()
        startPolling()
    }

    /// Мастер поставил рантайм — запустить демон, если ещё не запущен.
    func runtimeReady() {
        workQueue.async { [weak self] in
            guard let self, self.process?.isRunning != true else { return }
            self.spawnProcess()
            self.startPolling()
        }
    }

    /// Перезапуск, чтобы демон перечитал config.env. false — демон запущен не нами.
    @discardableResult
    func restart() -> Bool {
        guard weSpawned else { return false }
        workQueue.async { [weak self] in
            // Рестарт по backoff уже запланирован — новый процесс и так прочитает config.env.
            guard let self, !self.isRestarting else { return }
            log("перезапуск демона: новые настройки")
            self.terminateProcessGracefully()
            self.consecutiveMisses = 0
            self.spawnProcess()
        }
        return true
    }
```

- [ ] **Step 4: spawnProcess через RuntimeManager**

Заменить начало `spawnProcess()` (от `guard let repoPath` до `p.currentDirectoryURL = …` включительно):

```swift
    private func spawnProcess() {
        guard let launch = RuntimeManager.shared.daemonLaunch() else {
            spawnFailed("рантайм демона не установлен — пройди первичную настройку")
            setState(.notInstalled)
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: launch.executable)
        p.arguments = launch.arguments
        p.currentDirectoryURL = URL(fileURLWithPath: launch.cwd)
        p.environment = launch.environment
```

И строку лога после `p.run()`:

```swift
            log("демон запущен: \(([launch.executable] + launch.arguments).joined(separator: " ")) (cwd=\(launch.cwd), pid=\(p.processIdentifier))")
```

- [ ] **Step 5: Компиляция**

Run: `cd app && swiftc -target arm64-apple-macosx14.4 -o "${TMPDIR:-/tmp}/beedott-check" Sources/MeetMind/*.swift`
Expected: ошибки только в `MeetMindApp.swift` из-за неполного `switch` по `RunState` (чинится в Task 11) — либо без ошибок, если там `default`. Других ошибок нет.

- [ ] **Step 6: Commit** (после Task 11, когда приложение снова компилируется — см. Task 11 Step 6)

---

### Task 10: Мастер — `SetupWizard.swift`

**Files:**
- Create: `app/Sources/MeetMind/SetupWizard.swift`

- [ ] **Step 1: Реализация**

Создать `app/Sources/MeetMind/SetupWizard.swift`:

```swift
// SetupWizard.swift — мастер первичной настройки (4 шага): распознавание речи,
// ключ Anthropic, Telegram (по желанию), готово.
// Спека: docs/superpowers/specs/2026-10-13-first-run-setup-design.md.
// Вся работа — RuntimeManager (uv sync, python -m daemon.setup) на фоновой очереди.

import AppKit
import SwiftUI

struct SetupStatus {
    var model: Bool
    var summary: String?
    var telegram: Bool

    init(_ r: SetupResult) {
        model = r.bool("model"); summary = r.string("summary"); telegram = r.bool("telegram")
    }
}

@MainActor
final class SetupModel: ObservableObject {
    enum Step: Int, CaseIterable { case speech = 1, summary, telegram, done }
    enum Phase: Equatable { case idle, running, done, failed(String) }

    static let minFreeBytes: Int64 = 4 * 1_073_741_824

    @Published var step: Step = .speech
    // шаг 1
    @Published var runtimePhase: Phase = .idle
    @Published var runtimeLine = ""
    @Published var runtimeDetails = ""
    @Published var modelPhase: Phase = .idle
    @Published var modelDone: Int64 = 0
    @Published var modelTotal: Int64 = 0
    @Published var diskWarning: String?
    // шаг 2
    @Published var apiKey = ""
    @Published var keyPhase: Phase = .idle
    @Published var summaryMode: String?
    @Published var replacingKey = false
    // шаг 3
    @Published var botToken = ""
    @Published var botName: String?
    @Published var tokenPhase: Phase = .idle
    @Published var connectPhase: Phase = .idle
    @Published var telegramConfigured = false
    // применение настроек
    @Published var applyNote: String?

    private let runtime = RuntimeManager.shared
    private let queue = DispatchQueue(label: "com.jvmshid.meetmind.setup")
    var onClose: () -> Void = {}

    init(status: SetupStatus?) {
        if let status, runtime.hasRuntime, !runtime.needsSync {
            runtimePhase = .done
            if status.model { modelPhase = .done }
        }
        summaryMode = status?.summary
        telegramConfigured = status?.telegram ?? false
        step = !speechReady ? .speech : (summaryMode == nil ? .summary : .telegram)
    }

    var speechReady: Bool { runtimePhase == .done && modelPhase == .done }
    var summaryReady: Bool { summaryMode != nil && !replacingKey }

    // MARK: шаг 1

    func installSpeech() {
        if runtimePhase == .running || modelPhase == .running { return }
        if AppModel.shared.supervisor.state == .updating {
            // Супервизор уже делает uv sync — второй параллельный sync сломал бы venv.
            diskWarning = "Сейчас обновляются компоненты — подожди минуту и нажми ещё раз"
            return
        }
        let free = runtime.freeDiskBytes()
        if !speechReady && free < Self.minFreeBytes {
            let gb = Double(Self.minFreeBytes - free) / 1_073_741_824
            diskWarning = String(format: "Не хватает места: освободи ещё %.1f ГБ", gb)
            return
        }
        diskWarning = nil
        let needRuntime = runtimePhase != .done
        if needRuntime { runtimePhase = .running; runtimeDetails = "" }
        queue.async { [runtime] in
            if needRuntime {
                if runtime.hasRuntime && !runtime.needsSync {
                    Task { @MainActor in self.runtimePhase = .done }
                } else {
                    let failure = runtime.syncRuntime { line in
                        Task { @MainActor in self.runtimeLine = line }
                    }
                    if let failure {
                        Task { @MainActor in
                            self.runtimeDetails = failure
                            self.runtimePhase = .failed("Не удалось поставить пакеты. Проверь интернет и нажми «Повторить».")
                        }
                        return
                    }
                    Task { @MainActor in
                        self.runtimePhase = .done
                        AppModel.shared.supervisor.runtimeReady()
                    }
                }
            }
            Task { @MainActor in self.modelPhase = .running }
            let result = runtime.runSetup("install-model") { event in
                if case .progress(_, let done, let total) = event {
                    Task { @MainActor in self.modelDone = done; self.modelTotal = total }
                }
            }
            Task { @MainActor in
                switch result {
                case .ok: self.modelPhase = .done
                case .error(let code, let message):
                    self.modelPhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                default: self.modelPhase = .failed("Что-то пошло не так")
                }
            }
        }
    }

    // MARK: шаг 2

    func checkKey() {
        let key = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty, keyPhase != .running else { return }
        keyPhase = .running
        queue.async { [runtime] in
            let result = runtime.runSetup("check-key", stdin: key)
            Task { @MainActor in
                if case .error(let code, let message) = result {
                    self.keyPhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                    return
                }
                self.keyPhase = .done
                self.summaryMode = "api"
                self.replacingKey = false
                self.apiKey = ""
                self.applySettings()
            }
        }
    }

    // MARK: шаг 3

    func checkToken() {
        let token = botToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty, tokenPhase != .running else { return }
        tokenPhase = .running
        botName = nil
        queue.async { [runtime] in
            let result = runtime.runSetup("telegram-check", stdin: token)
            Task { @MainActor in
                switch result {
                case .ok(let r):
                    self.botName = r.string("bot")
                    self.tokenPhase = .done
                case .error(let code, let message):
                    self.tokenPhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                default: break
                }
            }
        }
    }

    func openBot() {
        guard let botName else { return }
        if let tg = URL(string: "tg://resolve?domain=\(botName)"), NSWorkspace.shared.open(tg) { return }
        if let web = URL(string: "https://t.me/\(botName)") { NSWorkspace.shared.open(web) }
    }

    func connectTelegram() {
        let token = botToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard connectPhase != .running else { return }
        connectPhase = .running
        queue.async { [runtime] in
            let result = runtime.runSetup("telegram-connect", stdin: token)
            Task { @MainActor in
                if case .error(let code, let message) = result {
                    self.connectPhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                    return
                }
                self.connectPhase = .done
                self.telegramConfigured = true
                self.botToken = ""
                self.applySettings()
            }
        }
    }

    // MARK: общее

    private func applySettings() {
        let app = AppModel.shared
        if app.recordingSessionId != nil || app.activeJobs > 0 {
            app.restartDaemonWhenIdle()
            applyNote = "Применится после текущей встречи"
        } else if !app.supervisor.restart() {
            applyNote = "Перезапусти демон, чтобы применить"
        } else {
            applyNote = nil
        }
    }

    func next() {
        if let nextStep = Step(rawValue: step.rawValue + 1) { step = nextStep }
    }

    func back() {
        if let prev = Step(rawValue: step.rawValue - 1) { step = prev }
    }

    func finish() {
        UserDefaults.standard.set(true, forKey: AppModel.setupCompletedKey)
        onClose()
    }
}

// MARK: - вью

struct SetupRootView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Шаг \(model.step.rawValue) из 4 · \(stepName)")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Theme.textTertiary(scheme))
            Group {
                switch model.step {
                case .speech: SpeechStepView(model: model)
                case .summary: SummaryStepView(model: model)
                case .telegram: TelegramStepView(model: model)
                case .done: DoneStepView(model: model)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            footer
        }
        .padding(EdgeInsets(top: 18, leading: 28, bottom: 24, trailing: 28))
        .frame(width: 520, height: 460)
    }

    private var stepName: String {
        switch model.step {
        case .speech: return "Распознавание речи"
        case .summary: return "Саммари"
        case .telegram: return "Telegram"
        case .done: return "Готово"
        }
    }

    private var footer: some View {
        HStack(spacing: 10) {
            HStack(spacing: 6) {
                ForEach(SetupModel.Step.allCases, id: \.rawValue) { s in
                    Circle()
                        .fill(s == model.step ? Theme.accent(scheme) : Theme.levelTrack(scheme))
                        .frame(width: 7, height: 7)
                }
            }
            Spacer()
            if model.step != .speech && model.step != .done {
                Button("Назад") { model.back() }.buttonStyle(.plain)
                    .font(.system(size: 13)).foregroundStyle(Theme.textSecondary(scheme))
            }
            switch model.step {
            case .speech:
                Button("Далее") { model.next() }.keyboardShortcut(.defaultAction)
                    .disabled(!model.speechReady)
            case .summary:
                Button("Далее") { model.next() }.keyboardShortcut(.defaultAction)
                    .disabled(!model.summaryReady)
            case .telegram:
                if !model.telegramConfigured {
                    Button("Пропустить") { model.next() }
                }
                Button("Далее") { model.next() }.keyboardShortcut(.defaultAction)
                    .disabled(!model.telegramConfigured)
            case .done:
                Button("Начать") { model.finish() }.keyboardShortcut(.defaultAction)
            }
        }
        .controlSize(.large)
    }
}

private struct StepHeader: View {
    let title: String
    let lead: String
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.system(size: 20, weight: .bold))
            Text(lead).font(.system(size: 13)).foregroundStyle(Theme.textSecondary(scheme))
                .lineSpacing(3).fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct PhaseRow: View {
    let title: String
    let phase: SetupModel.Phase
    var detail: String = ""
    var progress: Double?
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                icon.frame(width: 16)
                Text(title).font(.system(size: 13, weight: .semibold))
                Spacer()
                if !detail.isEmpty {
                    Text(detail).font(.system(size: 11).monospacedDigit())
                        .foregroundStyle(Theme.textTertiary(scheme)).lineLimit(1)
                }
            }
            if let progress, phase == .running {
                ProgressView(value: progress).progressViewStyle(.linear)
            }
            if case .failed(let text) = phase {
                Text(text).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.rowFill(scheme)))
    }

    @ViewBuilder private var icon: some View {
        switch phase {
        case .idle: Image(systemName: "circle").foregroundStyle(Theme.textTertiary(scheme))
        case .running: ProgressView().controlSize(.small)
        case .done: Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green(scheme))
        case .failed: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Theme.orange(scheme))
        }
    }
}

private struct SpeechStepView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            StepHeader(title: "Ставлю распознавание речи",
                       lead: "Скачаю Python-пакеты (~1 ГБ) и модель Whisper (1,5 ГБ). Расшифровка работает на этом Mac — аудио никуда не уходит.")
            PhaseRow(title: "Python и пакеты", phase: model.runtimePhase,
                     detail: model.runtimePhase == .running ? model.runtimeLine : "")
            if !model.runtimeDetails.isEmpty {
                DisclosureGroup("Подробности") {
                    Text(model.runtimeDetails).font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }.font(.system(size: 12))
            }
            PhaseRow(title: "Модель Whisper", phase: model.modelPhase,
                     detail: modelDetail, progress: modelProgress)
            if let warning = model.diskWarning {
                Text(warning).font(.system(size: 12)).foregroundStyle(Theme.orange(scheme))
            }
            if !model.speechReady {
                Button(isFailed ? "Повторить" : "Установить") { model.installSpeech() }
                    .controlSize(.large)
                    .disabled(model.runtimePhase == .running || model.modelPhase == .running)
            }
        }
    }

    private var isFailed: Bool {
        if case .failed = model.runtimePhase { return true }
        if case .failed = model.modelPhase { return true }
        return false
    }

    private var modelProgress: Double? {
        model.modelTotal > 0 ? Double(model.modelDone) / Double(model.modelTotal) : nil
    }

    private var modelDetail: String {
        guard model.modelPhase == .running, model.modelTotal > 0 else { return "" }
        let mb = { (b: Int64) in Int((Double(b) / 1_048_576).rounded()) }
        return "\(mb(model.modelDone)) из \(mb(model.modelTotal)) МБ"
    }
}

private struct SummaryStepView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            StepHeader(title: "Саммари делает Claude",
                       lead: "Нужен ключ Anthropic API. В Anthropic уходит только текст расшифровки, аудио — никогда.")
            if let mode = model.summaryMode, !model.replacingKey {
                HStack {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green(scheme))
                    Text(mode == "api" ? "Anthropic API — ключ сохранён" : "claude CLI (Max)")
                        .font(.system(size: 13))
                    Spacer()
                    Button("Заменить ключ") { model.replacingKey = true; model.keyPhase = .idle }
                }
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 10).fill(Theme.rowFill(scheme)))
            } else {
                SecureField("sk-ant-…", text: $model.apiKey)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { model.checkKey() }
                HStack {
                    Link("Где взять ключ", destination: URL(string: "https://console.anthropic.com/settings/keys")!)
                        .font(.system(size: 12))
                    Spacer()
                    if model.replacingKey {
                        Button("Отмена") { model.replacingKey = false; model.apiKey = "" }
                    }
                    Button("Проверить") { model.checkKey() }
                        .disabled(model.apiKey.isEmpty || model.keyPhase == .running)
                }
                keyStatus
            }
            if let note = model.applyNote {
                Text(note).font(.system(size: 12)).foregroundStyle(Theme.textSecondary(scheme))
            }
        }
    }

    @ViewBuilder private var keyStatus: some View {
        switch model.keyPhase {
        case .running:
            HStack(spacing: 6) { ProgressView().controlSize(.small); Text("Проверяю…").font(.system(size: 12)) }
        case .failed(let text):
            Text(text).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
        default:
            EmptyView()
        }
    }
}

private struct TelegramStepView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            StepHeader(title: "Фоллоу-апы в Telegram",
                       lead: "По желанию. Бот присылает только готовый текст — ни аудио, ни расшифровку целиком.")
            if model.telegramConfigured {
                HStack {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green(scheme))
                    Text("Telegram подключён — бот прислал тестовое сообщение").font(.system(size: 13))
                }
            } else {
                Text("1. В Telegram открой @BotFather → /newbot → скопируй токен.")
                    .font(.system(size: 12)).foregroundStyle(Theme.textSecondary(scheme))
                HStack {
                    SecureField("123456:ABC…", text: $model.botToken)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { model.checkToken() }
                    Button("Проверить") { model.checkToken() }
                        .disabled(model.botToken.isEmpty || model.tokenPhase == .running)
                }
                if case .failed(let text) = model.tokenPhase {
                    Text(text).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
                }
                if let bot = model.botName {
                    Text("2. Открой @\(bot) и нажми «Запустить» (/start).")
                        .font(.system(size: 12)).foregroundStyle(Theme.textSecondary(scheme))
                    HStack {
                        Button("Открыть @\(bot)") { model.openBot() }
                        Button(model.connectPhase == .running ? "Жду /start…" : "Я нажал /start") {
                            model.connectTelegram()
                        }
                        .disabled(model.connectPhase == .running)
                        if model.connectPhase == .running { ProgressView().controlSize(.small) }
                    }
                    if case .failed(let text) = model.connectPhase {
                        Text(text).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
                    }
                }
            }
            if let note = model.applyNote {
                Text(note).font(.system(size: 12)).foregroundStyle(Theme.textSecondary(scheme))
            }
        }
    }
}

private struct DoneStepView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            StepHeader(title: "Готово",
                       lead: "BeeDott живёт в строке меню. Записывай встречу кнопкой или ⌘⇧R — фоллоу-ап придёт сам.")
            VStack(alignment: .leading, spacing: 8) {
                row("Распознавание речи — Whisper на этом Mac", ok: model.speechReady)
                row("Саммари — Claude", ok: model.summaryMode != nil)
                row(model.telegramConfigured ? "Telegram — подключён" : "Telegram — не подключён (можно позже: меню → Настройка…)",
                    ok: model.telegramConfigured)
            }
            Text("Фоллоу-апы: меню → Библиотека, заметки в ~/MeetMind\(model.telegramConfigured ? ", Telegram" : "").")
                .font(.system(size: 12)).foregroundStyle(Theme.textSecondary(scheme))
            Text("При первой записи macOS спросит доступ к микрофону и звуку — нажми «Разрешить».")
                .font(.system(size: 12, weight: .semibold))
        }
    }

    private func row(_ text: String, ok: Bool) -> some View {
        HStack(spacing: 8) {
            Image(systemName: ok ? "checkmark.circle.fill" : "minus.circle")
                .foregroundStyle(ok ? Theme.green(scheme) : Theme.textTertiary(scheme))
            Text(text).font(.system(size: 13))
        }
    }
}

// MARK: - окно

@MainActor
enum SetupWindow {
    private static var controller: NSWindowController?

    static func show(status: SetupStatus?) {
        let model = SetupModel(status: status)
        if controller == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 460),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "Настройка BeeDott"
            window.isReleasedWhenClosed = false
            window.center()
            controller = NSWindowController(window: window)
        }
        model.onClose = { controller?.close() }
        controller?.window?.contentViewController = NSHostingController(rootView: SetupRootView(model: model))
        NSApp.activate(ignoringOtherApps: true)
        controller?.showWindow(nil)
    }

    /// Запуск из Загрузок (App Translocation): рантайм не ставим, просим перенести.
    static func showMoveToApplications() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Перенеси BeeDott в папку «Программы»"
        alert.informativeText = "macOS запустила BeeDott из временной копии, потому что приложение открыто прямо из Загрузок. Перетащи BeeDott в «Программы» и открой оттуда."
        alert.addButton(withTitle: "Открыть Загрузки")
        alert.addButton(withTitle: "Выйти")
        if alert.runModal() == .alertFirstButtonReturn {
            let downloads = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads")
            NSWorkspace.shared.open(downloads)
        }
        NSApp.terminate(nil)
    }
}
```

- [ ] **Step 2: Компиляция** — после Task 11 (нужны `AppModel.setupCompletedKey`, `restartDaemonWhenIdle`).

---

### Task 11: Подключение в AppModel и дропдаун

**Files:**
- Modify: `app/Sources/MeetMind/MeetMindApp.swift`
- Modify: `app/Sources/MeetMind/DropdownView.swift`

- [ ] **Step 1: Ключ, флаг отложенного рестарта, заметка о рантайме**

В `AppModel` рядом с другими `@Published` (около строки 50) добавить:

```swift
    /// Состояние рантайма для футера дропдауна: «Нужна настройка», «Обновляю компоненты…».
    @Published private(set) var runtimeNote: String? = nil
    /// Задачи, которые реально идут или ждут очереди (pending/queued/running).
    /// queueCount считает и failed/failed_retry/skipped — по нему отложенный
    /// рестарт мог не наступить никогда.
    @Published private(set) var activeJobs: Int = 0
    static let setupCompletedKey = "setupCompleted"
    private var pendingDaemonRestart = false
```

- [ ] **Step 2: Состояния супервизора**

Заменить `handleSupervisorState`:

```swift
    private func handleSupervisorState(_ state: DaemonSupervisor.RunState) {
        switch state {
        case .healthy:
            runtimeNote = nil // статус-поллинг (3с) сам подтвердит через daemonHealthy
        case .notInstalled:
            runtimeNote = "Нужна настройка"
            daemonHealthy = false
            updateStatusLine()
            refreshIcon()
        case .updating:
            runtimeNote = "Обновляю компоненты…"
            daemonHealthy = false
            updateStatusLine()
            refreshIcon()
        case .updateFailed:
            runtimeNote = "Компоненты не обновились — см. setup.log"
            daemonHealthy = false
            updateStatusLine()
            refreshIcon()
        case .down, .restarting, .unknown:
            daemonHealthy = false
            refreshIcon()
        }
    }
```

(Если в исходной функции в ветке `.down, .restarting, .unknown` есть ещё строки — сохранить их.)

- [ ] **Step 3: Запуск — транслокация и проверка настройки**

В `start()` первой строкой после `started = true` вставить:

```swift
        // Из Загрузок macOS запускает read-only копию — ставить рантайм и
        // регистрировать автозапуск на неё нельзя.
        if RuntimeLogic.isTranslocated(Bundle.main.bundlePath) {
            SetupWindow.showMoveToApplications()
            return
        }
```

В конец `start()` (после `island = IslandController(model: self)`) добавить `checkSetupOnLaunch()`.

Добавить методы в `AppModel`:

```swift
    // MARK: - первичная настройка (SetupWizard.swift)

    /// Мастер открывается сам, если без него BeeDott не работает: нет рантайма или
    /// модели, либо настройка не пройдена и саммари не настроено. На уже
    /// настроенном Mac — молча отмечаем настройку пройденной.
    private func checkSetupOnLaunch() {
        let runtime = RuntimeManager.shared
        guard !runtime.needsSync || !runtime.hasRuntime else { return } // идёт обновление — супервизор
        DispatchQueue.global(qos: .userInitiated).async {
            var status: SetupStatus?
            if runtime.hasRuntime, case .ok(let r) = runtime.runSetup("status") { status = SetupStatus(r) }
            Task { @MainActor in
                let completed = UserDefaults.standard.bool(forKey: AppModel.setupCompletedKey)
                let needed = status == nil || !(status!.model) || (!completed && status!.summary == nil)
                if needed {
                    SetupWindow.show(status: status)
                } else if !completed {
                    UserDefaults.standard.set(true, forKey: AppModel.setupCompletedKey)
                }
            }
        }
    }

    /// «Настройка…» в дропдауне.
    func openSetup() {
        let runtime = RuntimeManager.shared
        DispatchQueue.global(qos: .userInitiated).async {
            var status: SetupStatus?
            if runtime.hasRuntime, case .ok(let r) = runtime.runSetup("status") { status = SetupStatus(r) }
            Task { @MainActor in SetupWindow.show(status: status) }
        }
    }

    /// Мастер сохранил ключ/Telegram во время записи или обработки — демон
    /// перезапустим, когда очередь опустеет (см. статус-поллинг).
    func restartDaemonWhenIdle() {
        pendingDaemonRestart = true
    }
```

- [ ] **Step 4: Отложенный рестарт в статус-поллинге**

В статус-поллинге после строки `let queueTotal = queueRows.reduce(…)` (около строки 231) добавить:

```swift
                let activeTotal = queueRows
                    .filter { ["pending", "queued", "running"].contains($0["status"] as? String ?? "") }
                    .reduce(0) { $0 + (($1["n"] as? Int) ?? 0) }
```

В блоке `Task { @MainActor in … }` после `self.queueCount = queueTotal` добавить `self.activeJobs = activeTotal`, а после `self.refreshIcon()`:

```swift
                    if self.pendingDaemonRestart && recordingNow == nil && activeTotal == 0 {
                        self.pendingDaemonRestart = false
                        self.supervisor.restart()
                    }
```

- [ ] **Step 5: Завершение дочерних процессов и дропдаун**

В `AppDelegate.applicationWillTerminate` внутри `MainActor.assumeIsolated { … }` последней строкой добавить:

```swift
            RuntimeManager.shared.terminateChildren()
```

В `DropdownView.footer` заменить текст `Text("Демон недоступен")` на `Text(model.runtimeNote ?? "Демон недоступен")`, а перед `Button("Выйти")` (после `Spacer()`) добавить:

```swift
            Button("Настройка…") { model.openSetup() }
                .buttonStyle(.plain)
                .font(.system(size: 13))
                .foregroundStyle(Theme.textSecondary(scheme))
```

- [ ] **Step 6: Компиляция, тесты, commit (Tasks 9–11)**

Run: `cd app && swiftc -target arm64-apple-macosx14.4 -o "${TMPDIR:-/tmp}/beedott-check" Sources/MeetMind/*.swift && Tests/run.sh`
Expected: без ошибок; `OK — все тесты прошли`.

```bash
git add app/Sources/MeetMind/DaemonSupervisor.swift app/Sources/MeetMind/SetupWizard.swift app/Sources/MeetMind/MeetMindApp.swift app/Sources/MeetMind/DropdownView.swift
git commit -m "feat(app): мастер первичной настройки — Whisper, ключ Anthropic, Telegram"
```

---

### Task 12: Сборка, упаковка, проверка bundled-режима

**Files:**
- Modify: `app/build.sh`
- Create: `app/package.sh`, `app/Tools/bundled_smoke.sh`, `docs/INSTALL.md`

- [ ] **Step 1: build.sh — backend и uv**

В `app/build.sh` после блока иконки (перед `echo "-- codesign …"`) вставить:

```bash
# Встроенный режим (чистый Mac): исходники демона + uv. Рантайм ставит мастер
# (RuntimeManager.syncRuntime) в ~/Library/Application Support/MeetMind/runtime.
REPO_DIR="$(cd "$APP_DIR/.." && pwd)"
BACKEND="$CONTENTS/Resources/backend"
echo "-- backend -> $BACKEND"
rm -rf "$BACKEND"
mkdir -p "$BACKEND"
cp "$REPO_DIR/main.py" "$REPO_DIR/config.py" "$REPO_DIR/pyproject.toml" "$REPO_DIR/uv.lock" "$BACKEND/"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$REPO_DIR/daemon/" "$BACKEND/daemon/"

UV_SRC="${UV_BIN:-$HOME/.local/bin/uv}"
echo "-- uv ($UV_SRC) -> $CONTENTS/Resources/bin/uv"
if ! file "$UV_SRC" | grep -q "arm64"; then
    echo "uv не arm64: $UV_SRC (задай UV_BIN)" >&2; exit 1
fi
if otool -L "$UV_SRC" | tail -n +2 | grep -vqE '^[[:space:]]*(/usr/lib/|/System/Library/)'; then
    echo "uv линкуется не только с системными библиотеками: $UV_SRC (нужен standalone, задай UV_BIN)" >&2; exit 1
fi
mkdir -p "$CONTENTS/Resources/bin"
cp "$UV_SRC" "$CONTENTS/Resources/bin/uv"
chmod 755 "$CONTENTS/Resources/bin/uv"
codesign --force -s - "$CONTENTS/Resources/bin/uv"
```

- [ ] **Step 2: package.sh**

Создать `app/package.sh` (и `chmod +x`):

```bash
#!/usr/bin/env bash
# app/package.sh — собрать BeeDott.app и упаковать в zip для коллег (docs/INSTALL.md).
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$APP_DIR/build.sh"
OUT="$APP_DIR/build/BeeDott.zip"
rm -f "$OUT"
ditto -c -k --keepParent "$APP_DIR/build/BeeDott.app" "$OUT"
echo "готово: $OUT ($(du -h "$OUT" | cut -f1))"
```

- [ ] **Step 3: bundled_smoke.sh — bundled-режим без GUI**

Создать `app/Tools/bundled_smoke.sh` (и `chmod +x`):

```bash
#!/usr/bin/env bash
# Проверка встроенного режима без GUI: тот же uv sync, что делает мастер, в
# отдельный app-dir; status, link-ffmpeg, install-model (модель из общего кэша
# HF), запуск демона и health. В бандл ничего не должно записаться.
#   app/Tools/bundled_smoke.sh [путь к BeeDott.app]   (по умолчанию app/build/BeeDott.app)
set -euo pipefail
APP="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/build/BeeDott.app}"
RES="$APP/Contents/Resources"
WORK="$(mktemp -d)"
export MEETMIND_APPDIR="$WORK/app"
R="$MEETMIND_APPDIR/runtime"
# Как у RuntimeManager: байткод — в runtime/, исходники — из бандла.
export PYTHONPATH="$RES/backend" PYTHONPYCACHEPREFIX="$R/pycache"
mkdir -p "$R/bin"
touch "$WORK/marker"
trap '[ -n "${DPID:-}" ] && kill "$DPID" 2>/dev/null; echo "work dir: $WORK"' EXIT

echo "== uv sync (чистый Python и пакеты)"
cp "$RES/bin/uv" "$R/bin/uv"
UV_PROJECT_ENVIRONMENT="$R/venv" UV_PYTHON_INSTALL_DIR="$R/python" UV_CACHE_DIR="$R/uv-cache" \
UV_PYTHON_PREFERENCE=only-managed "$R/bin/uv" sync --frozen --no-dev --python 3.11 --project "$RES/backend"

setup() {
    (cd "$R" && PATH="$R/bin:/usr/bin:/bin" "$R/venv/bin/python" -m daemon.setup "$@")
}
echo "== link-ffmpeg";   setup link-ffmpeg
"$R/bin/ffmpeg" -hide_banner -version | head -1
echo "== status";        setup status
echo "== install-model"; setup install-model | tail -2

echo "== демон"
(cd "$R" && PATH="$R/bin:/usr/bin:/bin" "$R/venv/bin/python" "$RES/backend/main.py" >"$WORK/daemon.log" 2>&1) &
DPID=$!
for _ in $(seq 1 30); do
    if (cd "$R" && "$R/venv/bin/python" -m daemon.cli health 2>/dev/null); then
        break
    fi
    sleep 1
done
(cd "$R" && "$R/venv/bin/python" -m daemon.cli health)

echo "== в бандл ничего не записано"
CHANGED=$(find "$APP" -newer "$WORK/marker" | head -5)
if [ -n "$CHANGED" ]; then echo "ЗАПИСАНО В БАНДЛ:"; echo "$CHANGED"; exit 1; fi
echo "OK — bundled-режим работает"
```

- [ ] **Step 4: Собрать и прогнать smoke**

Run: `app/build.sh && app/Tools/bundled_smoke.sh`
Expected: uv скачивает Python 3.11 и пакеты (несколько минут при пустом кэше), `ffmpeg version 7.1`, `status` → `{"event": "ok", "model": true, "summary": …, "telegram": false}` (на этом Mac `summary` = `"claude_cli"`: `claude_bin()` находит claude из nvm; на чистом Mac — `null`), `install-model` → `ok`, `health` отвечает, `OK — bundled-режим работает`. Если `daemon.cli health` не поддерживает этот вывод — проверить `daemon/cli.py:57` и при необходимости смотреть `$WORK/daemon.log` на строку `daemon v3 запущен`.

- [ ] **Step 5: docs/INSTALL.md**

Создать `docs/INSTALL.md`:

```markdown
# Как поставить BeeDott

1. Распакуй `BeeDott.zip` и перетащи **BeeDott** в папку **Программы**.
   Не открывай прямо из Загрузок — приложение попросит перенести его.
2. Открой BeeDott из Программ. macOS скажет, что не может проверить
   разработчика (приложение пока не нотарифицировано) — нажми «Готово».
3. Открой **Системные настройки → Конфиденциальность и безопасность**, внизу у
   строки про BeeDott нажми **«Всё равно открыть»** и подтверди паролем.
4. Откроется мастер настройки:
   - **Распознавание речи** — «Установить». Скачается около 2,5 ГБ, нужен
     интернет и 4 ГБ свободного места. Занимает 5–15 минут.
   - **Саммари** — вставь ключ Anthropic API
     ([где взять](https://console.anthropic.com/settings/keys)) и нажми
     «Проверить».
   - **Telegram** — по желанию: создай бота у @BotFather, вставь токен, открой
     бота и нажми «Запустить».
5. Готово: BeeDott — значок в строке меню. При первой записи macOS спросит
   доступ к микрофону и звуку — разреши.

Сменить ключ или подключить Telegram позже: значок в строке меню → «Настройка…».
```

- [ ] **Step 6: Commit**

```bash
chmod +x app/package.sh app/Tools/bundled_smoke.sh
git add app/build.sh app/package.sh app/Tools/bundled_smoke.sh docs/INSTALL.md
git commit -m "feat(build): backend и uv в BeeDott.app, zip для коллег, smoke bundled-режима"
```

---

### Task 13: Установка на этот Mac, проверка вживую, push

- [ ] **Step 1: Дождаться простоя и поставить**

Проверить, что не идёт запись/обработка (см. `install.sh` — он сам откажется), затем:

Run: `app/install.sh`
Expected: `/Applications/BeeDott.app` запущен; мастер НЕ открылся (dev-режим, модель есть, саммари настроено); `defaults read com.jvmshid.meetmind setupCompleted` → `1`; демон отвечает (`.venv/bin/python -m daemon.cli health`).

- [ ] **Step 2: Мастер вручную**

Меню → «Настройка…»: открывается на шаге «Telegram», шаги 1–2 отмечены выполненными, на шаге 2 — «Anthropic API — ключ сохранён» и «Заменить ключ». Закрыть окно. Снимок экрана для пользователя.

- [ ] **Step 3: Полные тесты и push**

Run: `.venv/bin/python -m pytest -q && app/Tests/run.sh`
Expected: всё зелёное.

```bash
git push
```

- [ ] **Step 4: Приёмка (делает пользователь)**

Новый пользователь macOS → `app/package.sh` → zip через AirDrop/Загрузки → INSTALL.md по шагам → тестовая запись → фоллоу-ап в Библиотеке.
