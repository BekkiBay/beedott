# BeeDott в Claude через MCP — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude Desktop и Claude Code читают встречи BeeDott, правят их и управляют записью через локальный MCP-сервер; подключение — шаг «Claude» в мастере настройки.

**Architecture:** `daemon/mcp_server.py` — stdio-сервер на пакете `mcp` 2.x, тонкий клиент control-сокета демона (`daemon.control.request`). Все изменения делает демон (новые методы `update_followup`, `request_start`, `request_stop`, сверка названия в `delete_session`); запись по просьбе Claude запускает и останавливает Swift-приложение по флагам в `status`. Подключение — команды `python -m daemon.setup claude-*` (пишут `mcpServers.beedott` в конфиг Claude Desktop и регистрируют сервер в Claude Code) и шаг мастера в `SetupWizard.swift`.

**Tech Stack:** Python 3.11 (pytest, `mcp>=2.3,<3`), Swift/SwiftUI (swiftc без Xcode: `app/build.sh`, `app/Tests/run.sh`).

**Спек:** `docs/superpowers/specs/2026-10-14-claude-mcp-design.md`.

**Команды проверки:** `.venv/bin/python -m pytest -q` (из корня репо; сейчас 289 passed), `app/Tests/run.sh`, `app/build.sh`.

**Коммит в каждой задаче** (последний шаг) — сообщение по-русски в стиле истории, перед push скан секретов (память проекта: коммитить и пушить готовое):

```bash
git diff --cached | grep -cE "sk-ant-[A-Za-z0-9_-]{20,}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}"   # ожидается 0
git commit -m "<сообщение>"
git push origin master
```

**Живые встречи.** Код править можно всегда. `app/install.sh`, перезапуск приложения или демона — только когда ничего не пишется и не обрабатывается: `install.sh` сам откажется, `FORCE=1` не использовать. Ручные проверки записи — не во время настоящей встречи.

**Отличия от спека (уточнены при планировании):**

- `daemon/mcp_server.py` импортирует ещё `daemon.timeutil` (местное время, как у заметок) — он тоже без `config`.
- `stop_recording` в MCP узнаёт об отмене пустой записи по `sessions_list` (отменённые туда не попадают), а не по `session_detail`: тот создаёт каталог сессии заново (`paths.session_dir` делает `mkdir`), а у отменённой записи каталог уже удалён.
- `claude-code-connect` перед вызовом `claude` делает `import config`: `config.env` попадает в `os.environ`, поэтому `CLAUDE_CONFIG_DIR` оттуда (у автора — `~/.claude-acc2`) учитывается автоматически. Команда для копирования всё равно показывается.

---

## Файлы

| Файл | Что меняется | Этап |
|---|---|---|
| `pyproject.toml`, `uv.lock` | зависимость `mcp>=2.3,<3` | 1 |
| `tests/conftest.py` | autouse: тесты не трогают настоящий конфиг Claude Desktop | 1 |
| `daemon/control.py` | `DaemonUnavailable`, `DaemonError` | 1 |
| `daemon/db.py` | `session_title()` (этап 1), `redeliver()` (этап 2) | 1, 2 |
| `daemon/stages.py` | `_title` → `db.session_title` | 1 |
| `daemon/app.py` | `display_title`, `has_audio`, `recording_display_title` (1); `update_followup`, `delete_session(title)`, общая подготовка Telegram (2); `request_start`, `request_stop`, `start_requested` (3) | 1–3 |
| `daemon/mcp_server.py` (новый) | MCP-сервер: форматтеры и 13 инструментов | 1–3 |
| `daemon/setup.py` | `claude-connect`, `claude-disconnect`, `claude-code-connect`, `claude-code-disconnect`, поле `status.claude` | 1 |
| `daemon/states.py` | переход `DELIVERED → SUMMARIZED` | 2 |
| `daemon/followup_edit.py` (новый) | правка фоллоу-апа, подготовка Telegram-ответа | 2 |
| `tests/test_control.py`, `tests/test_localtime.py`, `tests/test_library_api.py`, `tests/test_setup.py`, `tests/test_states.py` | новые и обновлённые тесты | 1–2 |
| `tests/test_mcp_server.py`, `tests/test_mcp_e2e.py`, `tests/test_followup_edit.py`, `tests/test_record_requests.py` (новые) | тесты | 1–3 |
| `app/Sources/MeetMind/RuntimeLogic.swift` | `ClaudeConnection`, текст `bad_config` (1); `StartRequest` (3) | 1, 3 |
| `app/Tests/main.swift` | тесты чистой логики | 1, 3 |
| `app/Sources/MeetMind/SetupWizard.swift` | шаг `.claude` | 1–3 |
| `app/Sources/MeetMind/MeetMindApp.swift` | ветка `start_requested`, параметр `title` у `performStartRecording`, лог стопа | 3 |
| `app/Tools/bundled_smoke.sh` | импорт MCP-сервера в bundled-рантайме | 1 |
| `docs/INSTALL.md`, `README.md`, `AUDIT.md` | документация и журнал | 1–3 |

---

# Этап 1. Чтение и подключение

### Task 1: зависимость `mcp` и изоляция конфига Claude Desktop в тестах

**Files:** Modify: `pyproject.toml`, `uv.lock`, `requirements.txt`, `tests/conftest.py`

- [ ] **Step 1: добавить зависимость**

```bash
uv add "mcp>=2.3,<3"
.venv/bin/python -c "from mcp.server.mcpserver import MCPServer; from mcp.types import ToolAnnotations; from mcp import ClientSession, StdioServerParameters, stdio_client; import mcp, importlib.metadata as m; print(m.version('mcp'))"
```

Expected: `2.3.0` (или новее в пределах 2.x). В `pyproject.toml` в `dependencies` появилась строка `"mcp>=2.3,<3"`, `uv.lock` обновлён. В `requirements.txt` (его ставит `setup_mac.sh`) после строки `anthropic>=1.13` добавить `mcp>=2.3,<3`.

- [ ] **Step 2: autouse-фикстура** — в конец `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _isolated_claude_desktop_config(tmp_path, monkeypatch):
    """daemon.setup claude-* и status читают конфиг Claude Desktop — в тестах
    только временный файл, настоящий конфиг пользователя не трогаем."""
    monkeypatch.setenv("MEETMIND_CLAUDE_DESKTOP_CONFIG",
                       str(tmp_path / "claude-desktop" / "claude_desktop_config.json"))
```

- [ ] **Step 3: весь набор**

Run: `.venv/bin/python -m pytest -q`
Expected: 289 passed.

- [ ] **Step 4: commit** — `git add pyproject.toml uv.lock requirements.txt tests/conftest.py`, сообщение `build: зависимость mcp для MCP-сервера BeeDott`.

---

### Task 2: типизированные ошибки control-сокета

**Files:** Modify: `daemon/control.py`, `tests/test_control.py`

- [ ] **Step 1: тесты** — в `tests/test_control.py`. Импорт заменить на:

```python
import tempfile
import time

import pytest

from daemon.control import (ControlError, ControlServer, DaemonError,
                            DaemonUnavailable, request)
```

В фикстуре `server` добавить хендлер `"slow": lambda params: time.sleep(0.5) or {},` в словарь хендлеров. В конец файла:

```python
def test_no_socket_is_daemon_unavailable(monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", tempfile.mkdtemp(prefix="mm", dir="/tmp"))
    with pytest.raises(DaemonUnavailable, match="демон не запущен"):
        request("health")


def test_error_reply_is_daemon_error_with_code(server):
    with pytest.raises(DaemonError) as exc:
        request("boom")
    assert exc.value.code == "busy"
    assert exc.value.message == "уже пишем"
    assert isinstance(exc.value, RuntimeError)
    assert str(exc.value) == "busy: уже пишем"  # прежний текст для cli.py


def test_timeout_is_daemon_unavailable(server):
    with pytest.raises(DaemonUnavailable):
        request("slow", timeout=0.1)
```

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_control.py`
Expected: FAIL — `ImportError: cannot import name 'DaemonError'`.

- [ ] **Step 3: реализация** — в `daemon/control.py` после класса `ControlError` добавить:

```python
class DaemonUnavailable(RuntimeError):
    """Демон не отвечает: нет сокета, отказ соединения, тайм-аут, обрыв."""


class DaemonError(RuntimeError):
    """Демон ответил ошибкой. code — код ControlError (busy, not_found, ...);
    текст исключения прежний — «code: message» (daemon/cli.py печатает его)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
```

Функцию `request` заменить целиком:

```python
def request(method: str, params: dict | None = None, timeout: float = 30) -> dict:
    """Синхронный клиент (CLI, MCP-сервер). Возвращает result. Демон ответил
    ошибкой — DaemonError; демон недоступен или оборвал обмен — DaemonUnavailable.
    Оба — RuntimeError с прежними текстами."""
    sp = socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            try:
                s.connect(str(sp))
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                raise DaemonUnavailable(
                    f"демон не запущен (нет сокета {sp}) — запусти: python main.py") from exc
            s.sendall((json.dumps({"id": 1, "method": method, "params": params or {}},
                                  ensure_ascii=False) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                # пропускаем notification-кадры (heartbeat) до нашего ответа
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    msg = json.loads(line)
                    if msg.get("id") == 1:
                        if "error" in msg:
                            raise DaemonError(msg["error"]["code"], msg["error"]["message"])
                        return msg.get("result", {})
                    buf = rest
    except OSError as exc:  # тайм-аут, BrokenPipe на sendall, сброс соединения
        raise DaemonUnavailable(f"демон не отвечает: {exc}") from exc
    raise DaemonUnavailable("демон закрыл соединение без ответа")
```

- [ ] **Step 4: тесты проходят**

Run: `.venv/bin/python -m pytest -q tests/test_control.py && .venv/bin/python -m pytest -q`
Expected: всё зелёное (`test_unknown_method` и `test_control_error` ловят `RuntimeError` по тексту — проходят как раньше).

- [ ] **Step 5: commit** — `daemon/control.py tests/test_control.py`, `feat(control): типизированные ошибки — DaemonUnavailable и DaemonError`.

---

### Task 3: одно отображаемое название встречи и `has_audio`

**Files:** Modify: `daemon/db.py`, `daemon/stages.py`, `daemon/app.py`, `tests/test_localtime.py`, `tests/test_library_api.py`

- [ ] **Step 1: тесты.** В `tests/test_localtime.py` тест `test_default_title_uses_local_time` — последнюю строку заменить на `assert db.session_title(row) == "Встреча 10:10"` (импорт `stages` в файле остаётся — он нужен другим тестам).

В `tests/test_library_api.py` в конец:

```python
# ───── MCP: одно отображаемое название, has_audio ─────────────────────────────


def test_sessions_list_and_detail_have_display_title(app):
    con = app._con
    named = _mk_session(con, title="Синк")
    untitled = _mk_session(con, title=None)
    rows = {r["id"]: r for r in app.sessions_list({})["sessions"]}
    assert rows[named]["display_title"] == "Синк"
    assert rows[untitled]["title"] is None
    assert rows[untitled]["display_title"].startswith("Встреча ")
    session = app.session_detail({"session_id": untitled})["session"]
    assert session["display_title"] == rows[untitled]["display_title"]
    assert session["has_audio"] is False


def test_status_reports_recording_display_title(app):
    app.start_recording({"audio_path_owner": "app"})
    st = app.status({})
    assert st["recording_title"] is None
    assert st["recording_display_title"].startswith("Встреча ")
```

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_localtime.py tests/test_library_api.py`
Expected: FAIL — `AttributeError: module 'daemon.db' has no attribute 'session_title'`, `KeyError: 'display_title'`.

- [ ] **Step 3: `db.session_title`.** В `daemon/db.py` к импортам добавить `from daemon.timeutil import to_local`, после функции `now()`:

```python
def session_title(row) -> str:
    """Отображаемое название встречи: своё или «Встреча ЧЧ:ММ» по местному
    времени начала. Одно имя для Telegram, заметки, поиска и Claude (MCP)."""
    if row["title"]:
        return row["title"]
    hhmm = to_local(row["started_at"]).strftime("%H:%M") if row["started_at"] else "??:??"
    return f"Встреча {hhmm}"
```

- [ ] **Step 4: stages.** В `daemon/stages.py` удалить функцию `_title` (строки 41–45) и заменить все четыре вызова `_title(session)` на `db.session_title(session)` (строки 137, 165, 173, 215 до правки).

- [ ] **Step 5: app.py.** В `sessions_list` в словарь `out.append({...})` после `"title": row["title"],` добавить:

```python
                "display_title": db.session_title(row),
```

В `session_detail` строку `return {"session": dict(row), "followup": followup,` заменить на:

```python
        session = dict(row)
        session["display_title"] = db.session_title(row)
        session["has_audio"] = bool(row["audio_path"]) and Path(row["audio_path"]).exists()
        return {"session": session, "followup": followup,
```

В `status`: строку `rec_title = rec_started = None` заменить на `rec_title = rec_started = rec_display = None`, внутри `if rec is not None:` добавить `rec_display = db.session_title(rec)`, в возвращаемый словарь после `"recording_title": rec_title,` — `"recording_display_title": rec_display,`.

- [ ] **Step 6: тесты проходят**

Run: `.venv/bin/python -m pytest -q`
Expected: всё зелёное.

- [ ] **Step 7: commit** — `daemon/db.py daemon/stages.py daemon/app.py tests/test_localtime.py tests/test_library_api.py`, `feat(daemon): одно отображаемое название встречи и has_audio в карточке`.

---

### Task 4: MCP-сервер — форматтеры ответов

**Files:** Create: `daemon/mcp_server.py`, `tests/test_mcp_server.py`

- [ ] **Step 1: тесты** — создать `tests/test_mcp_server.py`:

```python
"""MCP-сервер BeeDott (daemon/mcp_server.py): форматтеры и инструменты.
Демон подменён фейком control.request — сокета нет."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from daemon import control, mcp_server
from tests.conftest import GOOD_FOLLOWUP as GOOD


@pytest.fixture(autouse=True)
def _tashkent(monkeypatch):
    monkeypatch.setenv("MEETMIND_TZ", "Asia/Tashkent")


SESSIONS = [
    {"id": "aaa111", "title": "Синк", "display_title": "Синк",
     "started_at": "2026-10-14T05:00:00+00:00", "duration_min": 42.4,
     "status": "delivered", "action_items": 2},
    {"id": "bbb222", "title": None, "display_title": "Встреча 15:30",
     "started_at": "2026-10-13T10:30:00+00:00", "duration_min": None,
     "status": "processing", "action_items": None},
]

SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "обсуждаем отчёт", "speaker": "Спикер 1"},
    {"start": 65.0, "end": 70.0, "text": "  ", "speaker": "Спикер 2"},
    {"start": 71.5, "end": 75.0, "text": "беру на себя", "speaker": "Спикер 2"},
]


def detail(**overrides) -> dict:
    session = {"id": "aaa111", "title": "Синк", "display_title": "Синк",
               "started_at": "2026-10-14T05:00:00+00:00",
               "ended_at": "2026-10-14T05:42:00+00:00",
               "attendees": json.dumps(["Аня", "Стас"], ensure_ascii=False),
               "state": "DELIVERED", "has_audio": True, "keep_audio": 1}
    session.update(overrides.pop("session", {}))
    base = {"session": session,
            "followup": dict(GOOD, revision=2, engine="anthropic-api:claude-opus-5-5"),
            "transcript": SEGMENTS, "pipeline": [], "sinks": []}
    base.update(overrides)
    return base


# ───────────────────────────── форматтеры ─────────────────────────────────────


def test_meeting_list_local_time_status_and_id():
    out = mcp_server.format_meeting_list(SESSIONS)
    assert "- **Синк** — 14.10.2026 10:00, 42 мин, готово, задач: 2 · id `aaa111`" in out
    assert "**Встреча 15:30** — 13.10.2026 15:30, длительность неизвестна, обрабатывается" in out


def test_meeting_list_empty():
    assert mcp_server.format_meeting_list([]) == "Встреч пока нет."


def test_search_groups_hits_by_meeting():
    results = [
        {"session_id": "aaa111", "title": "Синк", "source": "transcript", "snippet": "про [[отчёт]]"},
        {"session_id": "zzz999", "title": None, "source": "summary", "snippet": "[[отчёт]] готов"},
        {"session_id": "aaa111", "title": "Синк", "source": "summary", "snippet": "сдать [[отчёт]]"},
    ]
    out = mcp_server.format_search("отчёт", results, SESSIONS, limit=10)
    assert out.index("### Синк — 14.10.2026 10:00 · id `aaa111`") < out.index("zzz999")
    assert "- расшифровка: про **отчёт**" in out
    assert "- саммари: сдать **отчёт**" in out
    assert "### Без названия — дата неизвестна · id `zzz999`" in out


def test_search_limit_and_nothing_found():
    results = [{"session_id": s["id"], "title": None, "source": "summary", "snippet": "x"}
               for s in SESSIONS]
    assert "bbb222" not in mcp_server.format_search("x", results, SESSIONS, limit=1)
    assert mcp_server.format_search("пусто", [], SESSIONS, limit=5) == \
        "По запросу «пусто» ничего не нашлось."


def test_meeting_card_with_followup():
    out = mcp_server.format_meeting(detail())
    assert out.startswith("# Синк\n14.10.2026 10:00 · 42 мин · готово · id `aaa111`")
    assert "Участники: Аня, Стас" in out
    assert "Аудио: есть, закреплено" in out
    assert "## Фоллоу-ап (ревизия 2)" in out
    assert "- сделать — Bekzod" in out
    assert "### Открытые вопросы\nнет" in out
    assert "## Расшифровка" not in out


def test_meeting_card_without_followup_and_failed_state():
    d = detail(followup=None, session={"state": "FAILED", "has_audio": False},
               pipeline=[{"step": "summarized", "ok": False, "error": "summarize: 529"}])
    out = mcp_server.format_meeting(d)
    assert "ошибка: summarize: 529" in out
    assert "Аудио: нет" in out
    assert "Фоллоу-апа пока нет." in out


def test_meeting_card_with_transcript():
    out = mcp_server.format_meeting(detail(), include_transcript=True)
    assert "## Расшифровка\n[00:00] Спикер 1: обсуждаем отчёт\n[01:11] Спикер 2: беру на себя" in out


def test_transcript_mono_and_from_minute():
    mono = [{"start": 61.0, "end": 64.0, "text": "без спикера"}]
    assert mcp_server.format_transcript(mono) == "[01:01] без спикера"
    assert mcp_server.format_transcript(SEGMENTS, from_minute=1) == \
        "[01:11] Спикер 2: беру на себя"
    assert mcp_server.format_transcript(SEGMENTS, from_minute=9) == \
        "С 9-й минуты расшифровки нет — всего 2 мин."
    assert mcp_server.format_transcript([]) == "Расшифровки нет."


def test_long_transcript_comes_in_pages(monkeypatch):
    monkeypatch.setattr(mcp_server, "TRANSCRIPT_LIMIT", 300)
    segments = [{"start": float(i * 10), "end": float(i * 10 + 9),
                 "text": f"реплика номер {i}", "speaker": "Спикер 1"} for i in range(60)]
    first = mcp_server.format_transcript(segments)
    assert first.startswith("[00:00] Спикер 1: реплика номер 0")
    tail = first.splitlines()[-1]
    assert tail.startswith("(Показано с 0 по ") and "из 10;" in tail
    next_minute = int(tail.split("from_minute=")[1].rstrip(").)"))
    second = mcp_server.format_transcript(segments, from_minute=next_minute)
    assert second.startswith(f"[{next_minute:02d}:")


def test_action_items_and_status_formatting():
    rows = [(SESSIONS[0], dict(GOOD, action_items=[
        {"task": "Собрать отчёт", "owner": "Стас", "deadline": "2026-10-16"},
        {"task": "Позвать Аню", "owner": "Bekzod", "deadline": None}]))]
    out = mcp_server.format_action_items(rows, days=7)
    assert "### Синк — 14.10.2026 10:00 · id `aaa111`" in out
    assert "- Собрать отчёт — Стас, срок: 2026-10-16" in out
    assert "- Позвать Аню — Bekzod" in out
    assert mcp_server.format_action_items([], days=3) == "За последние 3 дн. задач нет."

    now = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)
    st = {"recording": "ccc333", "recording_display_title": "Встреча 10:30",
          "recording_started_at": "2026-10-15T05:30:00+00:00",
          "queue": [{"stage": "transcribe", "status": "queued", "n": 1},
                    {"stage": "deliver", "status": "failed", "n": 4}],
          "sessions": {"DELIVERED": 20, "FAILED": 2}}
    out = mcp_server.format_status(st, now=now)
    assert "Идёт запись **Встреча 10:30** с 10:30 (30 мин) · id `ccc333`" in out
    assert "В обработке стадий: 1." in out
    assert "Встреч с ошибкой: 2" in out
    assert mcp_server.format_status({"recording": None, "queue": [], "sessions": {}}) == \
        "Запись не идёт.\nОчередь обработки пуста."
```

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_mcp_server.py`
Expected: FAIL — `ImportError: cannot import name 'mcp_server'`.

- [ ] **Step 3: реализация** — создать `daemon/mcp_server.py`:

```python
"""MCP-сервер BeeDott: Claude Desktop и Claude Code спрашивают встречи.

Спек: docs/superpowers/specs/2026-10-14-claude-mcp-design.md. Запуск:
`python -m daemon.mcp_server` (stdio; Claude запускает процесс сам по записи в
своём конфиге — её пишет `python -m daemon.setup claude-connect`).

Всё — через control-сокет демона (daemon.control.request): в БД и файлы сессий
сервер не лезет. Импортирует только daemon.control, daemon.paths и
daemon.timeutil — не config и не daemon.db: Claude запускает процесс с
произвольным cwd, а `import config` делает mkdir("sessions") от cwd.
stdout — канал протокола MCP: print() здесь ломает протокол.
"""
import json
import math
from datetime import datetime, timezone

from daemon.timeutil import to_local

# Транскрипт за один ответ: лимит вывода MCP в Claude Code ~25 тыс. токенов,
# кириллица плотнее латиницы; часовая встреча — около 54 000 символов.
TRANSCRIPT_LIMIT = 60_000

_STATUS = {"delivered": "готово", "processing": "обрабатывается", "error": "ошибка"}
_SOURCE = {"summary": "саммари", "transcript": "расшифровка"}
_ACTIVE_JOBS = ("pending", "queued", "running")  # как RuntimeLogic.activeJobs


def _when(iso: str | None) -> str:
    return to_local(iso).strftime("%d.%m.%Y %H:%M") if iso else "дата неизвестна"


def _minutes(value) -> str:
    return f"{round(value)} мин" if value is not None else "длительность неизвестна"


def format_meeting_list(sessions: list[dict]) -> str:
    if not sessions:
        return "Встреч пока нет."
    lines = []
    for s in sessions:
        tasks = f", задач: {s['action_items']}" if s.get("action_items") else ""
        lines.append(f"- **{s['display_title']}** — {_when(s['started_at'])}, "
                     f"{_minutes(s.get('duration_min'))}, "
                     f"{_STATUS.get(s['status'], s['status'])}{tasks} · id `{s['id']}`")
    return "\n".join(lines)


def format_search(query: str, results: list[dict], sessions: list[dict], limit: int) -> str:
    """Попадания демона (по строке на источник) → группы по встречам. Даты —
    из sessions_list; встречи нет в списке — «дата неизвестна»."""
    by_id = {s["id"]: s for s in sessions}
    groups: dict[str, list[dict]] = {}
    for hit in results:
        groups.setdefault(hit["session_id"], []).append(hit)
    if not groups:
        return f"По запросу «{query}» ничего не нашлось."
    lines = []
    for sid, hits in list(groups.items())[:limit]:
        s = by_id.get(sid)
        title = s["display_title"] if s else (hits[0].get("title") or "Без названия")
        lines.append(f"### {title} — {_when(s['started_at'] if s else None)} · id `{sid}`")
        for hit in hits:
            snippet = hit["snippet"].replace("[[", "**").replace("]]", "**")
            lines.append(f"- {_SOURCE.get(hit['source'], hit['source'])}: {snippet}")
    return "\n".join(lines)


def _session_minutes(s: dict) -> int | None:
    if not s.get("ended_at"):
        return None
    try:
        delta = datetime.fromisoformat(s["ended_at"]) - datetime.fromisoformat(s["started_at"])
    except (TypeError, ValueError):
        return None
    return round(delta.total_seconds() / 60)


def _attendees(s: dict) -> list[str]:
    try:
        names = json.loads(s.get("attendees") or "[]")
    except ValueError:
        return []
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def _state_text(detail: dict) -> str:
    state = detail["session"]["state"]
    if state == "DELIVERED":
        return "готово"
    if state == "FAILED":
        errors = [p["error"] for p in detail.get("pipeline") or [] if p.get("ok") is False]
        return "ошибка" + (f": {errors[0]}" if errors else "")
    if state in ("DETECTED", "RECORDING"):
        return "идёт запись"
    return "обрабатывается"


def _bullets(items: list[str]) -> list[str]:
    return [f"- {x}" for x in items] or ["нет"]


def format_followup(followup: dict) -> list[str]:
    """Все пять разделов всегда, пустые — «нет»: Claude правит фоллоу-ап
    целиком (update_followup) и должен видеть и пустые разделы."""
    lines = [f"## Фоллоу-ап (ревизия {followup.get('revision', 1)})",
             "### Коротко", followup.get("context") or "нет",
             "### Что обсуждали", *_bullets(followup.get("discussed") or []),
             "### Решения", *_bullets(followup.get("decisions") or []),
             "### Задачи"]
    tasks = followup.get("action_items") or []
    for t in tasks:
        deadline = f", срок: {t['deadline']}" if t.get("deadline") else ""
        lines.append(f"- {t['task']} — {t['owner']}{deadline}")
    if not tasks:
        lines.append("нет")
    lines += ["### Открытые вопросы", *_bullets(followup.get("open_questions") or [])]
    return lines


def format_transcript(segments: list[dict], from_minute: int = 0) -> str:
    """Строки `[мм:сс] Спикер N: текст` с минуты from_minute. Длиннее
    TRANSCRIPT_LIMIT — обрезка по строке и подсказка, с какой минуты дальше."""
    if not segments:
        return "Расшифровки нет."
    total = math.ceil(segments[-1]["end"] / 60)
    start = from_minute * 60
    lines: list[str] = []
    size = 0
    next_minute = None
    for seg in segments:
        text = seg["text"].strip()
        if seg["end"] <= start or not text:
            continue
        mm, ss = divmod(int(seg["start"]), 60)
        speaker = seg.get("speaker")
        line = f"[{mm:02d}:{ss:02d}] {speaker}: {text}" if speaker else f"[{mm:02d}:{ss:02d}] {text}"
        if lines and size + len(line) + 1 > TRANSCRIPT_LIMIT:
            # Следующая страница начнётся с минуты этой строки (повтор пары
            # строк лучше потери); не раньше from_minute + 1 — без зацикливания.
            next_minute = max(int(seg["start"] // 60), from_minute + 1)
            break
        lines.append(line)
        size += len(line) + 1
    if not lines:
        return f"С {from_minute}-й минуты расшифровки нет — всего {total} мин."
    if next_minute is not None:
        lines.append(f"(Показано с {from_minute} по {next_minute} минуту из {total}; "
                     f"продолжение — get_meeting(meeting_id, include_transcript=true, "
                     f"from_minute={next_minute}).)")
    return "\n".join(lines)


def format_meeting(detail: dict, *, include_transcript: bool = False,
                   from_minute: int = 0) -> str:
    s = detail["session"]
    minutes = _session_minutes(s)
    duration = f"{minutes} мин" if minutes is not None else "длительность неизвестна"
    lines = [f"# {s['display_title']}",
             f"{_when(s['started_at'])} · {duration} · {_state_text(detail)} · id `{s['id']}`"]
    names = _attendees(s)
    if names:
        lines.append("Участники: " + ", ".join(names))
    if s.get("has_audio"):
        lines.append("Аудио: есть, закреплено" if s.get("keep_audio") else "Аудио: есть")
    else:
        lines.append("Аудио: нет")
    lines.append("")
    followup = detail.get("followup")
    lines += format_followup(followup) if followup else ["Фоллоу-апа пока нет."]
    if include_transcript:
        lines += ["", "## Расшифровка",
                  format_transcript(detail.get("transcript") or [], from_minute)]
    return "\n".join(lines)


def format_action_items(rows: list[tuple[dict, dict]], days: int) -> str:
    out = []
    for s, followup in rows:
        tasks = followup.get("action_items") or []
        if not tasks:
            continue
        out.append(f"### {s['display_title']} — {_when(s['started_at'])} · id `{s['id']}`")
        for t in tasks:
            deadline = f", срок: {t['deadline']}" if t.get("deadline") else ""
            out.append(f"- {t['task']} — {t['owner']}{deadline}")
    return "\n".join(out) if out else f"За последние {days} дн. задач нет."


def format_status(st: dict, now: datetime | None = None) -> str:
    """status демона → текст. last_error не показываем: он не сбрасывается
    после успеха и показывал бы старые ошибки."""
    now = now or datetime.now(timezone.utc)
    lines = []
    if st.get("recording"):
        started = st.get("recording_started_at")
        since = ""
        if started:
            minutes = round((now - datetime.fromisoformat(started)).total_seconds() / 60)
            since = f" с {to_local(started).strftime('%H:%M')} ({minutes} мин)"
        title = st.get("recording_display_title") or "Без названия"
        lines.append(f"Идёт запись **{title}**{since} · id `{st['recording']}`")
    else:
        lines.append("Запись не идёт.")
    active = sum(r.get("n", 0) for r in st.get("queue") or [] if r.get("status") in _ACTIVE_JOBS)
    lines.append(f"В обработке стадий: {active}." if active else "Очередь обработки пуста.")
    failed = (st.get("sessions") or {}).get("FAILED", 0)
    if failed:
        lines.append(f"Встреч с ошибкой: {failed} — подробности в list_meetings.")
    return "\n".join(lines)
```

- [ ] **Step 4: тесты проходят**

Run: `.venv/bin/python -m pytest -q tests/test_mcp_server.py`
Expected: PASS. Если `test_long_transcript_comes_in_pages` не сходится в подсчёте минут — поправить тест под фактическую страницу (логика: первая строка, не влезшая в лимит, даёт `from_minute`), не ослабляя проверку, что вторая страница начинается с этой минуты.

- [ ] **Step 5: commit** — `daemon/mcp_server.py tests/test_mcp_server.py`, `feat(mcp): форматтеры ответов MCP-сервера`.

---

### Task 5: MCP-сервер — пять инструментов чтения и ошибки

**Files:** Modify: `daemon/mcp_server.py`, `tests/test_mcp_server.py`

- [ ] **Step 1: тесты** — в конец `tests/test_mcp_server.py`:

```python
# ───────────────────────────── инструменты ────────────────────────────────────

import asyncio  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
READ_TOOLS = {"list_meetings", "search_meetings", "get_meeting", "action_items",
              "recording_status"}


class FakeDaemon:
    """Подмена control.request: ответы по методу, журнал вызовов."""

    def __init__(self):
        self.handlers: dict = {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, params=None, timeout=30):
        self.calls.append((method, params or {}))
        handler = self.handlers[method]
        if isinstance(handler, Exception):
            raise handler
        return handler(params or {}) if callable(handler) else handler


@pytest.fixture()
def daemon(monkeypatch):
    fake = FakeDaemon()
    monkeypatch.setattr(control, "request", fake)
    return fake


def tools() -> dict:
    return {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}


def test_read_tools_are_read_only():
    found = tools()
    assert READ_TOOLS <= set(found)
    for name in READ_TOOLS:
        assert found[name].annotations.read_only_hint is True, name
        assert found[name].description, name


def test_list_meetings_clamps_limit(daemon):
    daemon.handlers["sessions_list"] = {"sessions": SESSIONS}
    assert "**Синк**" in mcp_server.list_meetings(limit=500)
    mcp_server.list_meetings(limit=0)
    assert [c[1]["limit"] for c in daemon.calls] == [100, 1]


def test_search_meetings_takes_dates_from_full_list(daemon):
    daemon.handlers["search"] = {"results": [
        {"session_id": "aaa111", "title": "Синк", "source": "summary", "snippet": "[[x]]"}]}
    daemon.handlers["sessions_list"] = {"sessions": SESSIONS}
    out = mcp_server.search_meetings("x")
    assert "14.10.2026 10:00" in out
    assert daemon.calls == [("search", {"query": "x"}),
                            ("sessions_list", {"limit": mcp_server.ALL_SESSIONS})]


def test_get_meeting_passes_transcript_flags(daemon):
    daemon.handlers["session_detail"] = lambda p: detail()
    out = mcp_server.get_meeting("aaa111", include_transcript=True, from_minute=1)
    assert daemon.calls == [("session_detail", {"session_id": "aaa111"})]
    assert "## Расшифровка\n[01:11] Спикер 2: беру на себя" in out


def test_action_items_window(daemon):
    now = datetime.now(timezone.utc)
    recent = dict(SESSIONS[0], started_at=(now - timedelta(days=2)).isoformat())
    old = dict(SESSIONS[0], id="old000", started_at=(now - timedelta(days=30)).isoformat())
    no_tasks = dict(SESSIONS[1], started_at=(now - timedelta(days=1)).isoformat())
    daemon.handlers["sessions_list"] = {"sessions": [no_tasks, recent, old]}
    daemon.handlers["session_detail"] = lambda p: detail()
    out = mcp_server.action_items(days=7)
    assert "- сделать — Bekzod" in out
    assert [c for c in daemon.calls if c[0] == "session_detail"] == \
        [("session_detail", {"session_id": "aaa111"})]
    assert daemon.calls[0] == ("sessions_list", {"limit": mcp_server.ALL_SESSIONS})


def test_recording_status_tool(daemon):
    daemon.handlers["status"] = {"recording": None, "queue": [], "sessions": {}}
    assert mcp_server.recording_status().startswith("Запись не идёт.")


@pytest.mark.parametrize("error, text", [
    (control.DaemonUnavailable("нет сокета"), "BeeDott не запущен или не отвечает"),
    (control.DaemonError("not_found", "нет сессии x"), "Встреча не найдена"),
    (control.DaemonError("busy", "встреча сейчас обрабатывается"),
     "BeeDott: встреча сейчас обрабатывается"),
    (ValueError("boom"), "BeeDott: ошибка — boom"),
])
def test_errors_become_readable_tool_errors(daemon, error, text):
    daemon.handlers["session_detail"] = error
    with pytest.raises(ToolError, match=text):
        mcp_server.get_meeting("x")


def test_errors_reach_client_through_server(daemon):
    daemon.handlers["session_detail"] = control.DaemonError("not_found", "нет сессии x")
    with pytest.raises(ToolError, match="Встреча не найдена"):
        asyncio.run(mcp_server.server.call_tool("get_meeting", {"meeting_id": "x"}))


def test_import_does_not_pull_config():
    code = ("import sys, daemon.mcp_server; "
            "bad = [m for m in ('config', 'daemon.db', 'daemon.stages') if m in sys.modules]; "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd="/", capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(REPO)})
    assert r.returncode == 0, r.stdout + r.stderr
```

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_mcp_server.py`
Expected: FAIL — `AttributeError: module 'daemon.mcp_server' has no attribute 'server'`.

- [ ] **Step 3: реализация.** В `daemon/mcp_server.py` импорты заменить на:

```python
import functools
import json
import math
from datetime import datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from daemon import control
from daemon.timeutil import to_local
```

После `TRANSCRIPT_LIMIT` добавить:

```python
# «Все встречи» для поиска и задач: дефолт демона (200) обрезал бы окно.
ALL_SESSIONS = 10_000

READ = ToolAnnotations(read_only_hint=True)

INSTRUCTIONS = (
    "Это встречи пользователя, записанные BeeDott: BeeDott записывает звонки, "
    "расшифровывает их и пишет фоллоу-ап. meeting_id бери из list_meetings или "
    "search_meetings. Длинную расшифровку читай страницами: get_meeting с "
    "include_transcript=true и from_minute из подсказки в конце ответа."
)

server = MCPServer(name="beedott", instructions=INSTRUCTIONS)
```

В конец файла:

```python
# ─────────────────────────────── инструменты ─────────────────────────────────


def _call(method: str, params: dict | None = None) -> dict:
    return control.request(method, params)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def tool_errors(fn):
    """Любая беда → ToolError с понятным текстом: непойманное исключение
    MCPServer заменяет на безликое «Error executing tool» и теряет текст."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except control.DaemonUnavailable as exc:
            raise ToolError("BeeDott не запущен или не отвечает — открой приложение "
                            "BeeDott и повтори") from exc
        except control.DaemonError as exc:
            if exc.code == "not_found":
                raise ToolError("Встреча не найдена — возьми id из list_meetings "
                                "или search_meetings") from exc
            raise ToolError(f"BeeDott: {exc.message}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"BeeDott: ошибка — {exc}") from exc
    return wrapper


@server.tool(annotations=READ, structured_output=False)
@tool_errors
def list_meetings(limit: int = 20) -> str:
    """Последние встречи пользователя, новые сверху: название, дата, длительность,
    статус, число задач и id. limit — от 1 до 100."""
    sessions = _call("sessions_list", {"limit": _clamp(limit, 1, 100)})["sessions"]
    return format_meeting_list(sessions)


@server.tool(annotations=READ, structured_output=False)
@tool_errors
def search_meetings(query: str, limit: int = 10) -> str:
    """Полнотекстовый поиск по саммари и расшифровкам встреч. Результат
    сгруппирован по встречам: название, дата, id и фрагменты с совпадениями.
    limit — число встреч, от 1 до 50."""
    results = _call("search", {"query": query})["results"]
    sessions = _call("sessions_list", {"limit": ALL_SESSIONS})["sessions"]
    return format_search(query, results, sessions, _clamp(limit, 1, 50))


@server.tool(annotations=READ, structured_output=False)
@tool_errors
def get_meeting(meeting_id: str, include_transcript: bool = False,
                from_minute: int = 0) -> str:
    """Карточка встречи: название, дата, участники, статус и фоллоу-ап (коротко,
    что обсуждали, решения, задачи, открытые вопросы). include_transcript=true
    добавляет расшифровку с таймкодами с минуты from_minute; длинная приходит
    страницами — в конце подсказка, с какой минуты продолжить."""
    detail = _call("session_detail", {"session_id": meeting_id})
    return format_meeting(detail, include_transcript=include_transcript,
                          from_minute=_clamp(from_minute, 0, 100_000))


@server.tool(annotations=READ, structured_output=False)
@tool_errors
def action_items(days: int = 7) -> str:
    """Задачи из фоллоу-апов встреч за последние days дней (от 1 до 90):
    задача, ответственный, срок и встреча."""
    days = _clamp(days, 1, 90)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    for s in _call("sessions_list", {"limit": ALL_SESSIONS})["sessions"]:
        # session_detail тащит весь транскрипт — только встречи в окне и с задачами
        if not s.get("action_items") or datetime.fromisoformat(s["started_at"]) < since:
            continue
        followup = _call("session_detail", {"session_id": s["id"]}).get("followup")
        if followup:
            rows.append((s, followup))
    return format_action_items(rows, days)


@server.tool(annotations=READ, structured_output=False)
@tool_errors
def recording_status() -> str:
    """Идёт ли сейчас запись (название, с какого времени), сколько стадий
    обработки в очереди и сколько встреч с ошибкой."""
    return format_status(_call("status"))


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: тесты проходят**

Run: `.venv/bin/python -m pytest -q tests/test_mcp_server.py && .venv/bin/python -m pytest -q`
Expected: всё зелёное.

- [ ] **Step 5: commit** — `daemon/mcp_server.py tests/test_mcp_server.py`, `feat(mcp): инструменты чтения — встречи, поиск, карточка, задачи, статус`.

---

### Task 6: e2e — настоящий MCP-клиент через stdio

**Files:** Create: `tests/test_mcp_e2e.py`

- [ ] **Step 1: тест**

```python
"""MCP по-настоящему: `python -m daemon.mcp_server` подпроцессом с cwd /,
клиент из пакета mcp (stdio), демон — ControlServer с фейковыми хендлерами на
сокете во временной папке приложения."""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client

from daemon.control import ControlServer

REPO = Path(__file__).resolve().parent.parent
SESSION = {"id": "aaa111", "title": "Синк", "display_title": "Синк",
           "started_at": "2026-10-14T05:00:00+00:00", "duration_min": 30.0,
           "status": "delivered", "action_items": 1}


@pytest.fixture()
def appdir(monkeypatch):
    # НЕ pytest tmp_path: путь unix-сокета ограничен ~104 байтами на macOS
    d = tempfile.mkdtemp(prefix="mm", dir="/tmp")
    monkeypatch.setenv("MEETMIND_APPDIR", d)
    ControlServer({
        "sessions_list": lambda p: {"sessions": [SESSION]},
        "search": lambda p: {"results": [{"session_id": "aaa111", "title": "Синк",
                                          "source": "summary", "snippet": "про [[отчёт]]"}]},
    }).start()
    return d


def run(appdir: str, scenario):
    # stdio_client передаёт ребёнку только HOME, LOGNAME, PATH, SHELL, TERM, USER —
    # PYTHONPATH и MEETMIND_APPDIR явно.
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "daemon.mcp_server"], cwd="/",
        env={"PYTHONPATH": str(REPO), "MEETMIND_APPDIR": appdir, "MEETMIND_TZ": "Asia/Tashkent"})

    async def main():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await scenario(session)

    return asyncio.run(main())


def test_client_lists_tools_and_searches(appdir):
    async def scenario(session):
        listed = await session.list_tools()
        found = await session.call_tool("search_meetings", {"query": "отчёт"})
        missing = await session.call_tool("get_meeting", {"meeting_id": "x"})
        return listed, found, missing

    listed, found, missing = run(appdir, scenario)
    assert {t.name for t in listed.tools} == {
        "list_meetings", "search_meetings", "get_meeting", "action_items", "recording_status"}
    assert not found.is_error
    assert "### Синк — 14.10.2026 10:00" in found.content[0].text
    assert "**отчёт**" in found.content[0].text
    # у фейкового демона нет session_detail → unknown_method → понятная ошибка
    assert missing.is_error
    assert "BeeDott: " in missing.content[0].text
```

- [ ] **Step 2: прогнать**

Run: `.venv/bin/python -m pytest -q tests/test_mcp_e2e.py`
Expected: PASS. Если `ClientSession`/`stdio_client` в установленной версии ведут себя иначе (сигнатура, атрибуты результата) — сверить по исходникам `.venv/lib/python3.11/site-packages/mcp/client/` и поправить тест, не сервер.

- [ ] **Step 3: commit** — `tests/test_mcp_e2e.py`, `test(mcp): e2e — клиент mcp через stdio против фейкового демона`.

---

### Task 7: `daemon.setup` — подключение к Claude Desktop

**Files:** Modify: `daemon/setup.py`, `tests/test_setup.py`

- [ ] **Step 1: тесты.** В `tests/test_setup.py`: к импортам добавить `import os`, `import shlex`, `import sys`. В `test_status_clean_mac` в ожидаемый словарь добавить `"claude": None`. В конец файла:

```python
# ───── MCP: подключение к Claude Desktop ──────────────────────────────────────


def desktop_config() -> Path:
    return Path(os.environ["MEETMIND_CLAUDE_DESKTOP_CONFIG"])


def write_desktop(data) -> Path:
    path = desktop_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_claude_connect_creates_config(appdir, capsys):
    assert setup.main(["claude-connect"]) == 0
    path = desktop_config()
    assert events(capsys)[-1] == {"event": "ok", "config": str(path)}
    entry = json.loads(path.read_text())["mcpServers"]["beedott"]
    assert entry == setup.mcp_launch()
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "daemon.mcp_server"]
    assert (Path(entry["env"]["PYTHONPATH"]) / "daemon" / "mcp_server.py").exists()


def test_claude_connect_keeps_other_settings_and_backs_up(appdir, capsys):
    path = write_desktop({"preferences": {"x": 1}, "mcpServers": {"trino": {"command": "t"}}})
    setup.main(["claude-connect"])
    data = json.loads(path.read_text())
    assert data["preferences"] == {"x": 1}
    assert data["mcpServers"]["trino"] == {"command": "t"}
    assert "beedott" in data["mcpServers"]
    backup = json.loads(path.with_name(path.name + ".beedott.bak").read_text())
    assert backup["mcpServers"] == {"trino": {"command": "t"}}


def test_claude_connect_is_idempotent(appdir, capsys):
    setup.main(["claude-connect"])
    first = desktop_config().read_text()
    setup.main(["claude-connect"])
    assert desktop_config().read_text() == first


def test_claude_connect_empty_file_is_empty_config(appdir, capsys):
    path = write_desktop("")
    assert setup.main(["claude-connect"]) == 0
    assert "beedott" in json.loads(path.read_text())["mcpServers"]


def test_claude_connect_keeps_symlink(appdir, capsys, tmp_path):
    real = tmp_path / "real-config.json"
    real.write_text("{}")
    path = desktop_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(real)
    setup.main(["claude-connect"])
    assert path.is_symlink()
    assert "beedott" in json.loads(real.read_text())["mcpServers"]


@pytest.mark.parametrize("raw", ["{oops", "[1, 2]", '{"mcpServers": []}'])
def test_claude_connect_bad_config_untouched(appdir, capsys, raw):
    path = write_desktop(raw)
    assert setup.main(["claude-connect"]) == 1
    assert events(capsys)[-1]["code"] == "bad_config"
    assert path.read_text() == raw


def test_claude_disconnect_removes_only_beedott(appdir, capsys):
    path = write_desktop({"mcpServers": {"trino": {"command": "t"}}})
    setup.main(["claude-connect"])
    assert setup.main(["claude-disconnect"]) == 0
    assert json.loads(path.read_text())["mcpServers"] == {"trino": {"command": "t"}}


def test_claude_disconnect_without_file_is_ok(appdir, capsys):
    assert setup.main(["claude-disconnect"]) == 0
    assert not desktop_config().exists()


def test_status_claude_states(appdir, capsys):
    def claude():
        setup.main(["status"])
        return events(capsys)[-1]["claude"]

    assert claude() is None
    setup.main(["claude-connect"])
    assert claude() == "connected"
    path = desktop_config()
    data = json.loads(path.read_text())
    data["mcpServers"]["beedott"]["command"] = "/old/python"
    path.write_text(json.dumps(data))
    assert claude() == "stale"
    path.write_text("{oops")
    assert claude() == "bad_config"


def test_launch_env_carries_pycache_prefix(appdir, monkeypatch):
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", "/tmp/pyc")
    assert setup.mcp_launch()["env"]["PYTHONPYCACHEPREFIX"] == "/tmp/pyc"
```

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_setup.py`
Expected: FAIL — `test_status_clean_mac` (нет `claude`), `AttributeError: ... 'mcp_launch'`, неизвестная команда `claude-connect`.

- [ ] **Step 3: реализация.** В `daemon/setup.py` в модульный docstring после абзаца про код выхода добавить строку: `Команды claude-* подключают MCP-сервер BeeDott к Claude (спек 2026-10-14-claude-mcp-design.md).` После констант (`WEIGHT_FILES`) добавить:

```python
# MCP-сервер BeeDott в конфигах Claude (спек 2026-10-14-claude-mcp-design.md).
CLAUDE_SERVER_NAME = "beedott"
DESKTOP_CONFIG = (Path.home() / "Library" / "Application Support" / "Claude"
                  / "claude_desktop_config.json")
```

После `cmd_set_instructions` добавить:

```python
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
    """Копия в .beedott.bak, затем временный файл + os.replace. Симлинк
    остаётся симлинком — пишем в файл, на который он указывает."""
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.copy2(target, target.with_name(target.name + ".beedott.bak"))
    tmp = target.with_name(target.name + ".beedott.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def cmd_claude_connect() -> dict:
    """Записать mcpServers.beedott; preferences, чужие серверы и прочее — как
    есть. Пишется только при закрытом Claude Desktop (мастер закрывает его сам):
    открытый Claude при выходе перезаписал бы файл своей копией."""
    path = desktop_config_path()
    data = _read_desktop_config(path)
    servers = dict(data.get("mcpServers") or {})
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
    except (OSError, ValueError, SetupError):
        return "bad_config"
    entry = (data.get("mcpServers") or {}).get(CLAUDE_SERVER_NAME)
    if entry is None:
        return None
    want = mcp_launch()
    if isinstance(entry, dict) and all(entry.get(k) == want[k] for k in ("command", "args", "env")):
        return "connected"
    return "stale"
```

В `cmd_status` в возвращаемый словарь добавить `"claude": claude_desktop_state(),` (после `"summary_custom"`). В `COMMANDS` добавить:

```python
    "claude-connect": cmd_claude_connect,
    "claude-disconnect": cmd_claude_disconnect,
```

- [ ] **Step 4: тесты проходят**

Run: `.venv/bin/python -m pytest -q tests/test_setup.py && .venv/bin/python -m pytest -q`
Expected: всё зелёное.

- [ ] **Step 5: commit** — `daemon/setup.py tests/test_setup.py`, `feat(setup): claude-connect / claude-disconnect и status.claude`.

---

### Task 8: `daemon.setup` — регистрация в Claude Code

**Files:** Modify: `daemon/setup.py`, `tests/test_setup.py`

- [ ] **Step 1: тесты** — в конец `tests/test_setup.py`:

```python
# ───── MCP: Claude Code ───────────────────────────────────────────────────────


def fake_claude(tmp_path, monkeypatch, exit_code=0) -> Path:
    """Фейковый claude CLI: пишет argv в файл, выходит с exit_code."""
    log = tmp_path / "claude-argv.jsonl"
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.exit({exit_code})\n")
    script.chmod(0o755)
    from daemon import claude_auth
    monkeypatch.setattr(claude_auth, "claude_bin", lambda: str(script))
    return log


def claude_calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_claude_code_connect_registers_same_launch(appdir, capsys, monkeypatch, tmp_path):
    log = fake_claude(tmp_path, monkeypatch)
    assert setup.main(["claude-code-connect"]) == 0
    result = events(capsys)[-1]
    assert result["claude_code"] == "added"
    remove, add = claude_calls(log)
    assert remove == ["mcp", "remove", "--scope", "user", "beedott"]
    assert add[:5] == ["mcp", "add-json", "--scope", "user", "beedott"]
    assert json.loads(add[5]) == {"type": "stdio", **setup.mcp_launch()}
    assert shlex.split(result["command"]) == ["claude", *add]


def test_claude_code_connect_without_cli(appdir, capsys):
    assert setup.main(["claude-code-connect"]) == 0
    result = events(capsys)[-1]
    assert result["claude_code"] == "absent"
    assert result["command"].startswith("claude mcp add-json --scope user beedott ")


def test_claude_code_connect_cli_fails(appdir, capsys, monkeypatch, tmp_path):
    fake_claude(tmp_path, monkeypatch, exit_code=1)
    assert setup.main(["claude-code-connect"]) == 0
    assert events(capsys)[-1]["claude_code"] == "failed"


def test_claude_code_disconnect(appdir, capsys, monkeypatch, tmp_path):
    log = fake_claude(tmp_path, monkeypatch)
    setup.main(["claude-code-disconnect"])
    assert events(capsys)[-1] == {"event": "ok", "claude_code": "removed"}
    assert claude_calls(log) == [["mcp", "remove", "--scope", "user", "beedott"]]


def test_claude_code_disconnect_without_cli(appdir, capsys):
    setup.main(["claude-code-disconnect"])
    assert events(capsys)[-1] == {"event": "ok", "claude_code": "absent"}
```

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_setup.py -k claude_code`
Expected: FAIL — неизвестная команда (`main` вернёт 2).

- [ ] **Step 3: реализация.** В `daemon/setup.py` к импортам добавить `import shlex` и `import subprocess`; после `WEIGHT_FILES`-блока констант — `CLAUDE_CLI_TIMEOUT = 30`. После `claude_desktop_state` добавить:

```python
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
    binp = _claude_cli()
    if not binp:
        return {"claude_code": "absent"}
    ok = _claude_run(binp, "mcp", "remove", "--scope", "user", CLAUDE_SERVER_NAME)
    return {"claude_code": "removed" if ok else "failed"}
```

В `COMMANDS`:

```python
    "claude-code-connect": cmd_claude_code_connect,
    "claude-code-disconnect": cmd_claude_code_disconnect,
```

- [ ] **Step 4: тесты проходят**

Run: `.venv/bin/python -m pytest -q`
Expected: всё зелёное.

- [ ] **Step 5: commit** — `daemon/setup.py tests/test_setup.py`, `feat(setup): регистрация MCP-сервера в Claude Code`.

---

### Task 9: Swift — состояние подключения и текст ошибки конфига

**Files:** Modify: `app/Sources/MeetMind/RuntimeLogic.swift`, `app/Tests/main.swift`

- [ ] **Step 1: тест** — в `app/Tests/main.swift` перед строкой `if failures == 0 {`:

```swift
// 25. Шаг «Claude»: состояние подключения из status и текст ошибки конфига.
do {
    check(RuntimeLogic.claudeConnection("connected") == .connected, "connected")
    check(RuntimeLogic.claudeConnection("stale") == .stale, "stale")
    check(RuntimeLogic.claudeConnection("bad_config") == .badConfig, "bad_config")
    check(RuntimeLogic.claudeConnection(nil) == .disconnected, "нет ключа")
    check(RuntimeLogic.setupErrorText(code: "bad_config", message: "/x.json").contains("/x.json"),
          "путь в тексте bad_config")
}
```

- [ ] **Step 2: убедиться, что не собирается**

Run: `app/Tests/run.sh`
Expected: ошибка компиляции — нет `claudeConnection`.

- [ ] **Step 3: реализация.** В `RuntimeLogic.swift` перед `enum RuntimeLogic` добавить:

```swift
/// Подключение BeeDott к Claude Desktop — поле claude из `daemon.setup status`.
enum ClaudeConnection: Equatable {
    case disconnected, connected, stale, badConfig
}
```

Внутри `enum RuntimeLogic` (перед `setupErrorText`):

```swift
    /// Поле claude из status: connected | stale | bad_config | null.
    static func claudeConnection(_ raw: String?) -> ClaudeConnection {
        switch raw {
        case "connected": return .connected
        case "stale": return .stale
        case "bad_config": return .badConfig
        default: return .disconnected
        }
    }
```

В `setupErrorText` перед `default:`:

```swift
        case "bad_config":
            return "Конфиг Claude повреждён — открой Claude → Настройки → Developer и исправь файл"
                + (message.isEmpty ? "" : ": \(message)")
```

- [ ] **Step 4: тесты проходят**

Run: `app/Tests/run.sh`
Expected: `OK — все тесты прошли`.

- [ ] **Step 5: commit** — `app/Sources/MeetMind/RuntimeLogic.swift app/Tests/main.swift`, `feat(app): состояние подключения Claude и текст ошибки конфига`.

---

### Task 10: Swift — шаг мастера «Claude»

**Files:** Modify: `app/Sources/MeetMind/SetupWizard.swift`

- [ ] **Step 1: шапка файла.** Первые две строки комментария заменить на:

```swift
// SetupWizard.swift — мастер первичной настройки (5 шагов): распознавание речи,
// ключ Anthropic + модель и инструкция фоллоу-апа, Telegram и Claude (по желанию), готово.
// Спеки: docs/superpowers/specs/2026-10-13-first-run-setup-design.md,
// 2026-10-14-claude-mcp-design.md (шаг «Claude»).
```

(старую строку `// Спека: ...` удалить).

- [ ] **Step 2: `SetupStatus`** — поле `var claude: String?` и в `init` строку `claude = r.string("claude")`.

- [ ] **Step 3: `SetupModel` — состояние.** `enum Step` заменить на `enum Step: Int, CaseIterable { case speech = 1, summary, telegram, claude, done }`. После блока `// шаг 3` (перед `// применение настроек`):

```swift
    // шаг 4 — Claude (MCP)
    static let claudeBundleId = "com.anthropic.claudefordesktop"
    static let claudeConfigHint = "~/Library/Application Support/Claude/claude_desktop_config.json"
    @Published var claudeState: String?
    @Published var claudeDesktopInstalled = false
    @Published var claudeChecking = false
    @Published var claudePhase: Phase = .idle
    /// Ждёт подтверждения перезапуска Claude: true — подключить, false — отключить.
    @Published var claudeConfirm: Bool?
    /// Итог регистрации в Claude Code (added | failed | absent) и команда для терминала.
    @Published var claudeCodeResult: String?
    @Published var claudeCodeCommand: String?
```

В `init` после `telegramConfigured = ...` строку выбора шага заменить на:

```swift
        claudeState = status?.claude
        if !speechReady {
            step = .speech
        } else if summaryMode == nil {
            step = .summary
        } else if telegramConfigured && !claudeConnected {
            step = .claude
        } else {
            step = .telegram
        }
```

После `var summaryReady` добавить:

```swift
    var claudeConnected: Bool { RuntimeLogic.claudeConnection(claudeState) == .connected }
```

В `isBusy` в массив добавить `claudePhase`.

- [ ] **Step 4: `SetupModel` — действия.** Перед `// MARK: общее`:

```swift
    // MARK: шаг 4

    /// Заново спросить status (на чистом Mac мастер открылся до рантайма) и
    /// проверить, установлен ли Claude Desktop.
    func refreshClaude() {
        claudeDesktopInstalled = NSWorkspace.shared.urlForApplication(
            withBundleIdentifier: Self.claudeBundleId) != nil
        guard !claudeChecking, claudePhase != .running else { return }
        claudeChecking = true
        queue.async { [runtime] in
            let result = runtime.runSetup("status")
            Task { @MainActor in
                if case .ok(let r) = result { self.claudeState = r.string("claude") }
                self.claudeChecking = false
            }
        }
    }

    /// Подключить или отключить. Открытый Claude держит конфиг в памяти и при
    /// выходе перезаписал бы его — поэтому сначала спрашиваем про перезапуск.
    func claudeAction(connect: Bool) {
        guard claudePhase != .running else { return }
        let running = !NSRunningApplication.runningApplications(
            withBundleIdentifier: Self.claudeBundleId).isEmpty
        if running { claudeConfirm = connect } else { performClaude(connect: connect) }
    }

    func performClaude(connect: Bool) {
        claudeConfirm = nil
        claudePhase = .running
        claudeCodeResult = nil
        claudeCodeCommand = nil
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: Self.claudeBundleId)
        let wasRunning = !apps.isEmpty
        apps.forEach { $0.terminate() }
        queue.async { [runtime] in
            if wasRunning {
                let deadline = Date().addingTimeInterval(10)
                while Date() < deadline && apps.contains(where: { !$0.isTerminated }) {
                    Thread.sleep(forTimeInterval: 0.2)
                }
                if apps.contains(where: { !$0.isTerminated }) {
                    Task { @MainActor in
                        self.claudePhase = .failed("Claude не закрылся — закрой его вручную и нажми ещё раз")
                    }
                    return
                }
            }
            let result = runtime.runSetup(connect ? "claude-connect" : "claude-disconnect")
            if wasRunning { Task { @MainActor in self.openClaude() } }
            if case .error(let code, let message) = result {
                Task { @MainActor in
                    self.claudePhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                }
                return
            }
            // Claude Code — уже при открытом Claude: его CLI медленный.
            let code = runtime.runSetup(connect ? "claude-code-connect" : "claude-code-disconnect")
            let status = runtime.runSetup("status")
            Task { @MainActor in
                if connect, case .ok(let r) = code {
                    self.claudeCodeResult = r.string("claude_code")
                    self.claudeCodeCommand = r.string("command")
                }
                if case .ok(let r) = status { self.claudeState = r.string("claude") }
                self.claudePhase = .done
            }
        }
    }

    /// Claude Desktop нет — только Claude Code.
    func connectClaudeCodeOnly() {
        guard claudePhase != .running else { return }
        claudePhase = .running
        queue.async { [runtime] in
            let result = runtime.runSetup("claude-code-connect")
            Task { @MainActor in
                switch result {
                case .ok(let r):
                    self.claudeCodeResult = r.string("claude_code")
                    self.claudeCodeCommand = r.string("command")
                    self.claudePhase = .done
                case .error(let code, let message):
                    self.claudePhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                default:
                    self.claudePhase = .failed("Что-то пошло не так")
                }
            }
        }
    }

    func openClaude() {
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: Self.claudeBundleId)
        else { return }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration())
    }
```

- [ ] **Step 5: корневая вью.** В `SetupRootView`: `"Шаг \(model.step.rawValue) из 4 · \(stepName)"` → `из 5`; в `switch model.step` добавить `case .claude: ClaudeStepView(model: model)`; в `stepName` — `case .claude: return "Claude"`; в `footer` перед `case .done:`:

```swift
            case .claude:
                if !model.claudeConnected && model.claudeCodeResult != "added" {
                    Button("Пропустить") { model.next() }
                        .disabled(model.claudePhase == .running)
                }
                Button("Далее") { model.next() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(model.claudePhase == .running
                              || (!model.claudeConnected && model.claudeCodeResult != "added"))
```

Кнопку «Назад» в `footer` дополнить: `.disabled(model.step == .claude && model.claudePhase == .running)` — пока пишется конфиг, шаги не переключаются (спек).

- [ ] **Step 6: `ClaudeStepView`** — после `TelegramStepView`:

```swift
private struct ClaudeStepView: View {
    @ObservedObject var model: SetupModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            StepHeader(title: "Встречи в Claude",
                       lead: "По желанию. Подключи BeeDott к Claude Desktop — и спрашивай Claude про свои встречи.")
            if model.claudeChecking {
                busyRow("Проверяю Claude…")
            } else if let connect = model.claudeConfirm {
                Text("Claude перезапустится, открытые чаты сохранятся.").font(.system(size: 13))
                HStack {
                    Button(connect ? "Перезапустить и подключить" : "Перезапустить и отключить") {
                        model.performClaude(connect: connect)
                    }
                    .buttonStyle(.borderedProminent)
                    Button("Отмена") { model.claudeConfirm = nil }
                }
            } else if model.claudePhase == .running {
                busyRow("Подключаю…")
            } else {
                content
            }
            if case .failed(let text) = model.claudePhase {
                Text(text)
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.recordingRed(scheme))
                    .fixedSize(horizontal: false, vertical: true)
            }
            codeResult
        }
        .onAppear { model.refreshClaude() }
    }

    @ViewBuilder private var content: some View {
        switch RuntimeLogic.claudeConnection(model.claudeState) {
        case .connected:
            HStack {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(Theme.green(scheme))
                Text("Claude подключён").font(.system(size: 13))
                Spacer()
                if model.claudeDesktopInstalled { Button("Открыть Claude") { model.openClaude() } }
                Button("Отключить") { model.claudeAction(connect: false) }
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 10).fill(Theme.rowFill(scheme)))
            Text("Открой Claude и спроси: «что решили на последней встрече?»")
                .font(.system(size: 12))
                .foregroundStyle(Theme.textSecondary(scheme))
        case .badConfig:
            Text(RuntimeLogic.setupErrorText(code: "bad_config", message: SetupModel.claudeConfigHint))
                .font(.system(size: 12))
                .foregroundStyle(Theme.orange(scheme))
                .fixedSize(horizontal: false, vertical: true)
            Button("Проверить снова") { model.refreshClaude() }
        case .disconnected, .stale:
            if model.claudeDesktopInstalled {
                examples
                Button(RuntimeLogic.claudeConnection(model.claudeState) == .stale
                       ? "Переподключить" : "Подключить") { model.claudeAction(connect: true) }
                    .buttonStyle(.borderedProminent)
            } else {
                Text("Claude Desktop не установлен.")
                    .font(.system(size: 12))
                    .foregroundStyle(Theme.textSecondary(scheme))
                HStack {
                    Link("Скачать Claude", destination: URL(string: "https://claude.ai/download")!)
                    Button("Проверить снова") { model.refreshClaude() }
                    Spacer()
                    Button("У меня только Claude Code") { model.connectClaudeCodeOnly() }
                        .buttonStyle(.link)
                }
                .font(.system(size: 12))
            }
        }
    }

    private var examples: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Self.exampleRequests, id: \.self) { request in
                Text("«\(request)»").font(.system(size: 12))
            }
            Text(Self.dataNote)
                .font(.system(size: 12))
                .foregroundStyle(Theme.textSecondary(scheme))
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 4)
        }
    }

    // Этап 1 — только чтение; этапы 2 и 3 дописывают правку и запись.
    static let exampleRequests = [
        "Что решили на последней встрече?",
        "Какие задачи на мне за неделю?",
        "Где обсуждали минимальную корзину?",
    ]
    static let dataNote = "Claude видит встречу, только когда сам её запрашивает; ответы идут в твой аккаунт Claude."

    @ViewBuilder private var codeResult: some View {
        switch model.claudeCodeResult {
        case "added":
            Label("Подключено и к Claude Code", systemImage: "checkmark.circle.fill")
                .font(.system(size: 12))
        case "failed":
            Text("Claude Code не подключился сам — выполни команду в терминале.")
                .font(.system(size: 12)).foregroundStyle(Theme.textSecondary(scheme))
        case "absent":
            Text("Claude Code не найден. Если он у тебя есть — выполни команду в терминале.")
                .font(.system(size: 12)).foregroundStyle(Theme.textSecondary(scheme))
        default:
            EmptyView()
        }
        if let command = model.claudeCodeCommand {
            DisclosureGroup("Команда для Claude Code") {
                VStack(alignment: .leading, spacing: 6) {
                    Text(command)
                        .font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled)
                        .lineLimit(5)
                    Button("Скопировать") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(command, forType: .string)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .font(.system(size: 12))
        }
    }

    private func busyRow(_ text: String) -> some View {
        HStack(spacing: 6) {
            ProgressView().controlSize(.small)
            Text(text).font(.system(size: 12))
        }
    }
}
```

- [ ] **Step 7: шаг «Готово».** В `DoneStepView` в `VStack` строк после Telegram:

```swift
                row(model.claudeConnected ? "Claude — подключён"
                                          : "Claude — не подключён (можно позже: меню → Настройка…)",
                    ok: model.claudeConnected)
```

- [ ] **Step 8: сборка и тесты**

Run: `app/build.sh && app/Tests/run.sh`
Expected: сборка без ошибок, `OK — все тесты прошли`. Предупреждения Swift-конкурентности допустимы, ошибки — нет. Визуально: `app/Tools/setup_preview.sh` (если поддерживает шаги — открыть шаг «Claude»; если нет — смотреть на ручной проверке Task 11).

- [ ] **Step 9: commit** — `app/Sources/MeetMind/SetupWizard.swift`, `feat(app): шаг мастера «Claude» — подключение встреч к Claude Desktop и Code`.

---

### Task 11: smoke, документация, установка, ручная проверка этапа 1

**Files:** Modify: `app/Tools/bundled_smoke.sh`, `docs/INSTALL.md`, `README.md`, `AUDIT.md`

- [ ] **Step 1: smoke.** В `app/Tools/bundled_smoke.sh` после строки `echo "== status";        setup status`:

```bash
echo "== MCP-сервер импортируется с cwd /"
(cd / && "$R/venv/bin/python" -c "import daemon.mcp_server")
```

- [ ] **Step 2: `docs/INSTALL.md`** — в пункт 4 после строки про Telegram:

```markdown
   - **Claude** — по желанию: «Подключить» — и в Claude Desktop можно
     спрашивать «что решили на последней встрече?». Claude Desktop
     перезапустится. Если подключён Claude Code — BeeDott появится и там.
```

- [ ] **Step 3: `README.md`** — после раздела «Два способа вызвать Claude» новый раздел:

```markdown
## Встречи в Claude (MCP)

BeeDott — MCP-сервер для Claude Desktop и Claude Code: `python -m
daemon.mcp_server` (stdio), ходит в демон через `control.sock`, данные не
покидают Mac, пока Claude сам их не запросит. Подключение — «Настройка…» → шаг
«Claude» (пишет `mcpServers.beedott` в конфиг Claude Desktop и выполняет
`claude mcp add-json --scope user beedott …`). Вручную для Claude Code — та же
команда из шага мастера («Команда для Claude Code»). Инструменты:
`list_meetings`, `search_meetings`, `get_meeting`, `action_items`,
`recording_status`. Спек: `docs/superpowers/specs/2026-10-14-claude-mcp-design.md`.
```

- [ ] **Step 4: весь набор, commit** — `.venv/bin/python -m pytest -q && app/Tests/run.sh`; `git add app/Tools/bundled_smoke.sh docs/INSTALL.md README.md`, `docs: MCP-сервер BeeDott — установка и README`.

- [ ] **Step 5: установка — только без записи и обработки**

```bash
sqlite3 ~/Library/Application\ Support/MeetMind/meetmind.db \
  "SELECT state, COUNT(*) FROM sessions WHERE state IN ('RECORDING','RECORDED','TRANSCRIBING','SUMMARIZING','DELIVERING') GROUP BY state; SELECT COUNT(*) FROM pipeline_jobs WHERE status='running';"
```

Пусто и `0` → `app/install.sh`. Иначе — ждать в фоне (цикл по тому же запросу), потом ставить. `FORCE=1` не использовать.

- [ ] **Step 6: проверка живого MCP-сервера против настоящего демона (только чтение)** — скрипт в scratchpad:

```python
import asyncio, sys
from mcp import ClientSession, StdioServerParameters, stdio_client

params = StdioServerParameters(command="/Users/b.botirov/AI/meetmind/.venv/bin/python",
                               args=["-m", "daemon.mcp_server"], cwd="/",
                               env={"PYTHONPATH": "/Users/b.botirov/AI/meetmind"})

async def main():
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for name, args in [("list_meetings", {"limit": 3}), ("recording_status", {}),
                               ("action_items", {"days": 7})]:
                res = await s.call_tool(name, args)
                print(f"== {name} error={res.is_error}\n{res.content[0].text[:800]}\n")

asyncio.run(main())
```

Run: `.venv/bin/python <scratchpad>/mcp_live.py`
Expected: три ответа без `error=True`, реальные встречи с местным временем. Если `get_meeting` на самой длинной встрече (`list_meetings` → id) с `include_transcript=true` даёт ответ больше ~60 000 символов — уменьшить `TRANSCRIPT_LIMIT` и записать в спек фактическое значение.

- [ ] **Step 7: ручная проверка с пользователем** (dev-режим автора):
  1. Меню → «Настройка…» → открывается шаг «Claude» (Telegram уже подключён). Видны примеры просьб и «Подключить».
  2. «Подключить» при открытом Claude Desktop → подтверждение «Claude перезапустится…» → Claude закрывается и открывается снова, шаг показывает «Claude подключён».
  3. `~/Library/Application Support/Claude/claude_desktop_config.json`: есть `mcpServers.beedott`, серверы confluence/clickhouse/trino и `preferences` на месте; рядом `claude_desktop_config.json.beedott.bak`.
  4. Claude Code: `claude mcp get beedott` (в терминале с `CLAUDE_CONFIG_DIR=~/.claude-acc2`) показывает сервер. Если нет — выполнить команду из «Команда для Claude Code».
  5. В Claude Desktop: «что решили на последней встрече?» → ответ с данными BeeDott.
  6. Если `terminate()` вызвал запрос разрешения на автоматизацию — записать в спек; запасной путь «Закрой Claude вручную» должен сработать.

- [ ] **Step 8: `AUDIT.md`** — в конец журнала:

```markdown
- 2026-10-15 — MCP, этап 1: BeeDott — MCP-сервер для Claude Desktop и Claude
  Code (`python -m daemon.mcp_server`, через control.sock): список встреч,
  поиск, карточка с расшифровкой страницами, задачи за N дней, статус записи.
  Подключение — шаг мастера «Claude» (конфиг Desktop с бэкапом, перезапуск
  Claude с подтверждения; `claude mcp add-json` для Claude Code).
```

Commit `AUDIT.md`, `docs(audit): MCP, этап 1 — чтение встреч из Claude`.

---

# Этап 2. Правка встреч

### Task 12: переход `DELIVERED → SUMMARIZED` и `db.redeliver`

**Files:** Modify: `daemon/states.py`, `daemon/db.py`, `tests/test_states.py`, `tests/test_db.py`

- [ ] **Step 1: тесты.** В конец `tests/test_states.py`:

```python
def test_followup_edit_redelivers_from_delivered():
    """Правка фоллоу-апа из Claude (daemon/followup_edit.py) заново прогоняет
    только deliver."""
    assert_transition(State.DELIVERED, State.SUMMARIZED)
```

В конец `tests/test_db.py` (`db` и `State` там уже импортированы):

```python
def test_redeliver_resets_deliver_job_in_one_go(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual", title="Синк")
    for st in (State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED,
               State.SUMMARIZING, State.FAILED):
        db.set_state(con, sid, st)
    for stage in ("transcribe", "summarize", "deliver"):
        db.enqueue_job(con, sid, stage)
    con.execute("UPDATE pipeline_jobs SET status='done' WHERE session_id=? AND stage='transcribe'", (sid,))
    con.execute("UPDATE pipeline_jobs SET status='failed', attempts=8, last_error='x'"
                " WHERE session_id=? AND stage IN ('summarize', 'deliver')", (sid,))
    con.commit()

    db.redeliver(con, sid)

    assert db.get_session(con, sid)["state"] == State.SUMMARIZED
    jobs = {r["stage"]: r for r in con.execute(
        "SELECT * FROM pipeline_jobs WHERE session_id=?", (sid,))}
    assert (jobs["deliver"]["status"], jobs["deliver"]["attempts"],
            jobs["deliver"]["last_error"], jobs["deliver"]["next_retry_at"]) == ("queued", 0, None, None)
    assert (jobs["summarize"]["status"], jobs["summarize"]["last_error"]) == ("done", None)


def test_redeliver_inserts_missing_deliver_job(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    for st in (State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED,
               State.SUMMARIZING, State.SUMMARIZED, State.DELIVERING, State.DELIVERED):
        db.set_state(con, sid, st)
    db.redeliver(con, sid)
    row = con.execute("SELECT status FROM pipeline_jobs WHERE session_id=? AND stage='deliver'",
                      (sid,)).fetchone()
    assert row["status"] == "queued"
```

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_states.py tests/test_db.py`
Expected: FAIL — `ValueError: illegal transition DELIVERED -> SUMMARIZED`, нет `db.redeliver`.

- [ ] **Step 3: states.** В `daemon/states.py` строку `ALLOWED[State.DELIVERED] = {State.TRANSCRIBED}` заменить на:

```python
ALLOWED[State.DELIVERED] = {State.TRANSCRIBED}
# Правка фоллоу-апа из Claude (daemon/followup_edit.py, db.redeliver): новая
# ревизия уже записана — заново прогоняется только deliver.
ALLOWED[State.DELIVERED].add(State.SUMMARIZED)
```

- [ ] **Step 4: db.** В `daemon/db.py` после `resummarize`:

```python
def redeliver(con, sid: str) -> None:
    """Правка фоллоу-апа (daemon/followup_edit.py): заново прогнать только
    deliver. Одна транзакция (вызывающий может добавить свои UPDATE до неё —
    они уйдут тем же commit): состояние → SUMMARIZED; job deliver → queued со
    сброшенными попытками — enqueue_job их не сбрасывает, и после FAILED на
    deliver (attempts=8) первая же неудача стала бы окончательной. Упавшая
    пересборка (job summarize failed, а прежний followup.json есть) → done:
    фоллоу-ап теперь есть, карточка не должна показывать шаг упавшим."""
    row = get_session(con, sid)
    assert_transition(State(row["state"]), State.SUMMARIZED)
    ts = now()
    con.execute("UPDATE sessions SET state=?, updated_at=? WHERE id=?",
                (State.SUMMARIZED, ts, sid))
    con.execute(
        "INSERT INTO pipeline_jobs(session_id, stage, chain, updated_at) VALUES(?, 'deliver', 1, ?)"
        " ON CONFLICT(session_id, stage) DO UPDATE SET status='queued', attempts=0,"
        " next_retry_at=NULL, last_error=NULL, updated_at=excluded.updated_at",
        (sid, ts))
    con.execute(
        "UPDATE pipeline_jobs SET status='done', last_error=NULL, updated_at=?"
        " WHERE session_id=? AND stage='summarize' AND status IN ('failed', 'failed_retry')",
        (ts, sid))
    con.commit()
```

- [ ] **Step 5: тесты проходят**

Run: `.venv/bin/python -m pytest -q`
Expected: всё зелёное.

- [ ] **Step 6: commit** — `daemon/states.py daemon/db.py tests/test_states.py tests/test_db.py`, `feat(daemon): db.redeliver — повторная доставка после правки фоллоу-апа`.

---

### Task 13: `daemon/followup_edit.py` — правка фоллоу-апа

**Files:** Create: `daemon/followup_edit.py`, `tests/test_followup_edit.py`; Modify: `daemon/app.py` (`resummarize`)

- [ ] **Step 1: тесты** — создать `tests/test_followup_edit.py`:

```python
"""Правка фоллоу-апа из Claude (MCP update_followup): daemon/followup_edit.py."""
import json

import pytest

from daemon import db, followup_edit
from daemon.followup_edit import EditError
from daemon.paths import session_dir
from daemon.states import State
from tests.conftest import GOOD_FOLLOWUP as GOOD

CHAIN = [State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED,
         State.SUMMARIZING, State.SUMMARIZED, State.DELIVERING, State.DELIVERED]
EDITED = dict(GOOD, action_items=[{"task": "Собрать отчёт", "owner": "Стас", "deadline": None}])


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    return db.connect()


def _session(con, *, state=State.DELIVERED, followup=True, transcript=True):
    sid = db.create_session(con, source="manual", title="Синк")
    for st in CHAIN:
        db.set_state(con, sid, st)
        if st == state:
            break
    sdir = session_dir(sid)
    if transcript:
        (sdir / "transcript.txt").write_text("Спикер 1: обсуждаем отчёт", encoding="utf-8")
    if followup:
        (sdir / "followup.json").write_text(json.dumps(
            dict(GOOD, engine="anthropic-api:claude-opus-5-5", revision=1),
            ensure_ascii=False), encoding="utf-8")
    for stage in ("transcribe", "summarize", "deliver"):
        db.enqueue_job(con, sid, stage)
    con.execute("UPDATE pipeline_jobs SET status='done' WHERE session_id=?", (sid,))
    for sink in ("telegram", "note"):
        con.execute("INSERT INTO deliveries(session_id, sink, status, delivered_at)"
                    " VALUES(?,?,?,?)", (sid, sink, "delivered", db.now()))
    con.commit()
    return sid


def _jobs(con, sid):
    return {r["stage"]: dict(r) for r in con.execute(
        "SELECT * FROM pipeline_jobs WHERE session_id=?", (sid,))}


def _deliveries(con, sid):
    return {r["sink"]: r["status"] for r in con.execute(
        "SELECT sink, status FROM deliveries WHERE session_id=?", (sid,))}


def test_edit_writes_new_revision_and_requeues_delivery(con):
    sid = _session(con)
    assert followup_edit.apply(con, sid, EDITED, resend=False) == {
        "session_id": sid, "revision": 2, "resend": False}
    saved = json.loads((session_dir(sid) / "followup.json").read_text(encoding="utf-8"))
    assert saved["action_items"] == EDITED["action_items"]
    assert (saved["engine"], saved["revision"]) == ("anthropic-api:claude-opus-5-5", 2)
    md = (session_dir(sid) / "followup.md").read_text(encoding="utf-8")
    assert "Собрать отчёт — Стас" in md and "№2" in md
    assert db.get_session(con, sid)["state"] == State.SUMMARIZED
    deliver = _jobs(con, sid)["deliver"]
    assert (deliver["status"], deliver["attempts"]) == ("queued", 0)
    assert _deliveries(con, sid) == {"telegram": "delivered", "note": "pending"}
    hit = con.execute("SELECT session_id FROM notes_fts WHERE notes_fts MATCH 'Стас'").fetchone()
    assert hit["session_id"] == sid


def test_resend_resets_telegram_and_replies_to_first_message(con):
    sid = _session(con)
    (session_dir(sid) / "telegram_progress.json").write_text('{"hash": "x", "sent": ["555", "556"]}')
    followup_edit.apply(con, sid, EDITED, resend=True)
    assert _deliveries(con, sid)["telegram"] == "pending"
    assert (session_dir(sid) / "telegram_reply_to").read_text() == "555"
    assert not (session_dir(sid) / "telegram_progress.json").exists()


def test_failed_delivery_gets_fresh_retries(con):
    sid = _session(con, state=State.DELIVERING)
    db.set_state(con, sid, State.FAILED)
    con.execute("UPDATE pipeline_jobs SET status='failed', attempts=8, last_error='telegram: 502'"
                " WHERE session_id=? AND stage='deliver'", (sid,))
    con.commit()
    followup_edit.apply(con, sid, EDITED, resend=False)
    deliver = _jobs(con, sid)["deliver"]
    assert (deliver["status"], deliver["attempts"], deliver["last_error"]) == ("queued", 0, None)
    assert db.get_session(con, sid)["state"] == State.SUMMARIZED


def test_failed_resummarize_marks_summarize_done(con):
    sid = _session(con, state=State.SUMMARIZING)
    db.set_state(con, sid, State.FAILED)
    con.execute("UPDATE pipeline_jobs SET status='failed', last_error='529'"
                " WHERE session_id=? AND stage='summarize'", (sid,))
    con.commit()
    followup_edit.apply(con, sid, EDITED, resend=False)
    assert _jobs(con, sid)["summarize"]["status"] == "done"


def test_invalid_followup_is_bad_params_and_files_untouched(con):
    sid = _session(con)
    before = (session_dir(sid) / "followup.json").read_text(encoding="utf-8")
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, {"context": "x"}, resend=False)
    assert exc.value.code == "bad_params"
    assert (session_dir(sid) / "followup.json").read_text(encoding="utf-8") == before
    assert db.get_session(con, sid)["state"] == State.DELIVERED


@pytest.mark.parametrize("state, message", [
    (State.RECORDING, "идёт запись"),
    (State.TRANSCRIBING, "обрабатывается"),
])
def test_busy_states(con, state, message):
    sid = _session(con, state=state, followup=False)
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, EDITED, resend=False)
    assert exc.value.code == "busy" and message in str(exc.value)


def test_running_job_is_busy(con):
    sid = _session(con)
    con.execute("UPDATE pipeline_jobs SET status='running' WHERE session_id=? AND stage='deliver'",
                (sid,))
    con.commit()
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, EDITED, resend=False)
    assert exc.value.code == "busy"


def test_failed_without_followup(con):
    sid = _session(con, state=State.TRANSCRIBING, followup=False)
    db.set_state(con, sid, State.FAILED)
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, EDITED, resend=False)
    assert exc.value.code == "no_followup" and "пересобери саммари" in str(exc.value)
    (session_dir(sid) / "transcript.txt").unlink()
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, EDITED, resend=False)
    assert "повтори обработку" in str(exc.value)


def test_unknown_or_cancelled_is_not_found(con):
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, "nope", EDITED, resend=False)
    assert exc.value.code == "not_found"
    sid = _session(con, state=State.RECORDING, followup=False)
    db.set_state(con, sid, State.CANCELLED)
    with pytest.raises(EditError) as exc:
        followup_edit.apply(con, sid, EDITED, resend=False)
    assert exc.value.code == "not_found"
```

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_followup_edit.py`
Expected: FAIL — `ImportError: cannot import name 'followup_edit'`.

- [ ] **Step 3: реализация** — создать `daemon/followup_edit.py`:

```python
"""Правка фоллоу-апа из Claude (MCP-инструмент update_followup).

Claude присылает исправленный фоллоу-ап целиком; демон проверяет его той же
схемой, что ответ модели (summarizer.validate_followup), пишет новую ревизию,
обновляет поиск и заново доставляет заметку — и Telegram, если просили resend.
Спек: docs/superpowers/specs/2026-10-14-claude-mcp-design.md, «Правка фоллоу-апа».
"""
import json
import os
from pathlib import Path

from daemon import db, stages, summarizer
from daemon.paths import session_dir
from daemon.render import render_followup
from daemon.states import State


class EditError(Exception):
    """Отказ с кодом для ControlError: not_found, busy, no_followup, bad_params."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def prepare_telegram_resend(sdir: Path) -> None:
    """Новая версия уйдёт в Telegram ответом на первое отправленное сообщение
    (продукт P12); прогресс отправки забывается — иначе побайтно тот же текст
    «уже отправлен» и сообщение не придёт. Общая для resummarize и правки."""
    progress = sdir / "telegram_progress.json"
    try:
        sent = json.loads(progress.read_text(encoding="utf-8")).get("sent") or []
    except (OSError, json.JSONDecodeError):
        sent = []
    if sent:
        (sdir / "telegram_reply_to").write_text(str(sent[0]), encoding="utf-8")
    progress.unlink(missing_ok=True)


def _write_atomic(path: Path, text: str) -> None:
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


def _check_editable(con, row, sid: str) -> Path:
    if row is None or row["state"] == State.CANCELLED:
        raise EditError("not_found", f"нет сессии {sid}")
    state = State(row["state"])
    if state in (State.DETECTED, State.RECORDING):
        raise EditError("busy", "идёт запись этой встречи")
    running = con.execute("SELECT 1 FROM pipeline_jobs WHERE session_id=? AND status='running'",
                          (sid,)).fetchone()
    if state not in (State.DELIVERED, State.FAILED) or running:
        raise EditError("busy", "встреча сейчас обрабатывается — попробуй, когда закончится")
    sdir = session_dir(sid)
    if not (sdir / "followup.json").exists():
        if (sdir / "transcript.txt").exists():
            raise EditError("no_followup", "фоллоу-апа ещё нет — сначала пересобери саммари")
        raise EditError("no_followup", "встреча не обработалась — повтори обработку в BeeDott")
    return sdir


def apply(con, sid: str, followup, *, resend: bool) -> dict:
    """Записать исправленный фоллоу-ап новой ревизией и поставить повторную
    доставку. Вызывать под локом демона (DaemonApp.update_followup)."""
    row = db.get_session(con, sid)
    sdir = _check_editable(con, row, sid)
    try:
        clean = summarizer.validate_followup(summarizer.normalize_followup(followup))
    except ValueError as exc:
        raise EditError("bad_params", f"фоллоу-ап не по схеме: {exc}") from exc

    path = sdir / "followup.json"
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    try:
        revision = int(prev.get("revision", 1)) + 1
    except (TypeError, ValueError):
        revision = 2
    engine = prev.get("engine") or "claude"
    new = dict(clean, engine=engine, revision=revision)
    title = db.session_title(row)
    md = render_followup(new, engine=engine, title=title,
                         duration_min=stages._duration_min(row), revision=revision)
    _write_atomic(path, json.dumps(new, ensure_ascii=False, indent=1))
    _write_atomic(sdir / "followup.md", md)

    tpath = sdir / "transcript.txt"
    transcript = tpath.read_text(encoding="utf-8") if tpath.exists() else ""
    db.index_note(con, sid, title=title, transcript=transcript, summary=md)

    sinks = ["note", "obsidian"]
    if resend:
        prepare_telegram_resend(sdir)
        sinks.append("telegram")
    # без commit — уйдёт одной транзакцией с redeliver
    con.execute(
        f"UPDATE deliveries SET status='pending', delivered_at=NULL"
        f" WHERE session_id=? AND sink IN ({', '.join('?' * len(sinks))})", (sid, *sinks))
    db.redeliver(con, sid)
    return {"session_id": sid, "revision": revision, "resend": resend}
```

- [ ] **Step 4: `resummarize` зовёт общую функцию.** В `daemon/app.py` к импорту `from daemon import db, notifier, reconcile, retention` добавить `followup_edit` (в алфавитном порядке: `db, followup_edit, notifier, ...`). В `resummarize` блок от `sdir = session_dir(sid)` до `progress.unlink(missing_ok=True)` включительно заменить на:

```python
        followup_edit.prepare_telegram_resend(session_dir(sid))
```

Комментарий над блоком («Доставки сбрасываются — прогресс Telegram тоже…») оставить.

- [ ] **Step 5: тесты проходят**

Run: `.venv/bin/python -m pytest -q tests/test_followup_edit.py tests/test_library_api.py && .venv/bin/python -m pytest -q`
Expected: всё зелёное, в том числе `test_resummarize_forgets_telegram_progress` и `test_resummarize_remembers_previous_telegram_message`.

- [ ] **Step 6: commit** — `daemon/followup_edit.py daemon/app.py tests/test_followup_edit.py`, `feat(daemon): правка фоллоу-апа новой ревизией с повторной доставкой`.

---

### Task 14: демон — `update_followup` и сверка названия при удалении

**Files:** Modify: `daemon/app.py`, `tests/test_library_api.py`

- [ ] **Step 1: тесты** — в конец `tests/test_library_api.py`:

```python
# ───── MCP: правка фоллоу-апа и удаление со сверкой названия ──────────────────


def test_update_followup_handler(app):
    from daemon.control import ControlError
    sid = _mk_session(app._con)
    with pytest.raises(ControlError) as exc:
        app.update_followup({"session_id": sid, "followup": {"context": "x"}})
    assert exc.value.code == "bad_params"
    r = app.update_followup({"session_id": sid, "followup": dict(GOOD), "resend": False})
    assert r == {"session_id": sid, "revision": 2, "resend": False}


def test_update_followup_refuses_active_recording(app):
    from daemon.control import ControlError
    sid = app.start_recording({"audio_path_owner": "app"})["session_id"]
    with pytest.raises(ControlError) as exc:
        app.update_followup({"session_id": sid, "followup": dict(GOOD)})
    assert exc.value.code == "busy"


def test_delete_session_checks_title(app):
    from daemon.control import ControlError
    con = app._con
    sid = _mk_session(con, title="Ретро")
    with pytest.raises(ControlError) as exc:
        app.delete_session({"session_id": sid, "title": "Синк"})
    assert exc.value.code == "bad_params" and "«Ретро»" in str(exc.value)
    assert db.get_session(con, sid) is not None
    assert app.delete_session({"session_id": sid, "title": " Ретро "})["deleted"] is True


def test_delete_untitled_session_by_display_title(app):
    sid = _mk_session(app._con, title=None)
    shown = app.sessions_list({})["sessions"][0]["display_title"]
    assert app.delete_session({"session_id": sid, "title": shown})["deleted"] is True
```

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_library_api.py -k "update_followup or title"`
Expected: FAIL — нет `update_followup`, удаление не сверяет название.

- [ ] **Step 3: `update_followup`.** В `daemon/app.py` после `rename_session`:

```python
    def update_followup(self, params: dict) -> dict:
        """Правка фоллоу-апа из Claude (MCP update_followup): новая ревизия,
        поиск, повторная доставка заметки (и Telegram при resend) —
        daemon/followup_edit.py."""
        sid = params.get("session_id")
        if not sid:
            raise ControlError("bad_params", "нужен session_id")
        with self._lock:
            if sid == self._active_sid:
                raise ControlError("busy", "идёт запись этой встречи")
            try:
                return followup_edit.apply(self._con, sid, params.get("followup"),
                                           resend=bool(params.get("resend")))
            except followup_edit.EditError as exc:
                raise ControlError(exc.code, str(exc)) from exc
```

В `handlers()` после `"rename_session": self.rename_session,` — `"update_followup": self.update_followup,`.

- [ ] **Step 4: сверка названия.** Тот же фрагмент есть ещё в `rename_session` и `set_keep_audio` — править только в `delete_session` (для точной замены захватить и строку `def delete_session(self, params: dict) -> dict:` с докстрингом). В `delete_session` строки

```python
        sid = params.get("session_id")
        if not sid or db.get_session(self._con, sid) is None:
            raise ControlError("not_found", f"нет сессии {sid}")
```

заменить на:

```python
        sid = params.get("session_id")
        row = db.get_session(self._con, sid) if sid else None
        if row is None:
            raise ControlError("not_found", f"нет сессии {sid}")
        # Claude (MCP) передаёт название, которое видел: страховка от удаления
        # не той встречи по перепутанному id. Приложение удаляет без title.
        expected = params.get("title")
        if expected is not None and expected.strip() != db.session_title(row).strip():
            raise ControlError("bad_params", f"название не совпадает: встреча {sid} "
                                             f"называется «{db.session_title(row)}»")
```

В докстринг `delete_session` дописать: `title (необязательно) — сверка названия перед удалением (MCP).`

- [ ] **Step 5: тесты проходят**

Run: `.venv/bin/python -m pytest -q`
Expected: всё зелёное.

- [ ] **Step 6: commit** — `daemon/app.py tests/test_library_api.py`, `feat(daemon): update_followup и сверка названия при удалении встречи`.

---

### Task 15: MCP — инструменты правки

**Files:** Modify: `daemon/mcp_server.py`, `tests/test_mcp_server.py`, `tests/test_mcp_e2e.py`, `app/Sources/MeetMind/SetupWizard.swift`, `README.md`

- [ ] **Step 1: тесты** — в конец `tests/test_mcp_server.py`:

```python
EDIT_TOOLS = {"rename_meeting", "resummarize_meeting", "update_followup", "set_keep_audio"}


def test_edit_tool_annotations():
    found = tools()
    for name in EDIT_TOOLS:
        a = found[name].annotations
        assert (a.read_only_hint, a.destructive_hint) == (False, False), name
    a = found["delete_meeting"].annotations
    assert (a.read_only_hint, a.destructive_hint) == (False, True)
    schema = found["update_followup"].input_schema
    assert {"meeting_id", "followup", "resend"} <= set(schema["properties"])


def test_edit_tools_pass_params(daemon):
    daemon.handlers.update({
        "rename_session": lambda p: {"session_id": p["session_id"], "title": p["title"]},
        "resummarize": {"session_id": "aaa111", "state": "TRANSCRIBED"},
        "update_followup": lambda p: {"session_id": p["session_id"], "revision": 3,
                                      "resend": p["resend"]},
        "set_keep_audio": lambda p: {"session_id": p["session_id"], "keep_audio": p["keep"]},
        "delete_session": {"session_id": "aaa111", "deleted": True},
    })
    assert "«Ретро»" in mcp_server.rename_meeting("aaa111", "Ретро")
    assert "Пересобираю" in mcp_server.resummarize_meeting("aaa111")
    out = mcp_server.update_followup("aaa111", dict(GOOD), resend=True)
    assert "ревизия 3" in out and "Telegram" in out
    assert "закреплено" in mcp_server.set_keep_audio("aaa111", True)
    assert "удалена" in mcp_server.delete_meeting("aaa111", "Синк")
    assert daemon.calls == [
        ("rename_session", {"session_id": "aaa111", "title": "Ретро"}),
        ("resummarize", {"session_id": "aaa111"}),
        ("update_followup", {"session_id": "aaa111", "followup": dict(GOOD), "resend": True}),
        ("set_keep_audio", {"session_id": "aaa111", "keep": True}),
        ("delete_session", {"session_id": "aaa111", "title": "Синк"}),
    ]


def test_delete_with_wrong_title_is_readable(daemon):
    daemon.handlers["delete_session"] = control.DaemonError(
        "bad_params", "название не совпадает: встреча aaa111 называется «Синк»")
    with pytest.raises(ToolError, match="называется «Синк»"):
        mcp_server.delete_meeting("aaa111", "Ретро")
```

В `tests/test_mcp_e2e.py`: в ожидаемое множество инструментов добавить `"rename_meeting", "resummarize_meeting", "update_followup", "set_keep_audio", "delete_meeting"` и в сценарий — вызов `delete_meeting` с проверкой ошибки:

```python
        wrong = await session.call_tool("delete_meeting", {"meeting_id": "aaa111", "title": "Ретро"})
```

(вернуть его четвёртым из `scenario`; проверка: `assert wrong.is_error and "называется «Синк»" in wrong.content[0].text`). Импорт: `from daemon.control import ControlError, ControlServer`. В фикстуру `appdir` перед `ControlServer({...})`:

```python
    def wrong_title(params):
        raise ControlError("bad_params", "название не совпадает: встреча aaa111 называется «Синк»")
```

и в словарь хендлеров — `"delete_session": wrong_title,`.

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_mcp_server.py tests/test_mcp_e2e.py`
Expected: FAIL — нет инструментов правки.

- [ ] **Step 3: реализация.** В `daemon/mcp_server.py` к импортам добавить `from typing_extensions import TypedDict` (на Python 3.11 pydantic требует TypedDict из typing_extensions). После `READ = ...`:

```python
EDIT = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DELETE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
```

`INSTRUCTIONS` заменить на:

```python
INSTRUCTIONS = (
    "Это встречи пользователя, записанные BeeDott: BeeDott записывает звонки, "
    "расшифровывает их и пишет фоллоу-ап. meeting_id бери из list_meetings или "
    "search_meetings. Длинную расшифровку читай страницами: get_meeting с "
    "include_transcript=true и from_minute из подсказки в конце ответа. "
    "Править встречи можно только по просьбе пользователя. Перед update_followup "
    "прочитай встречу через get_meeting и пришли фоллоу-ап целиком, изменив только "
    "то, о чём просили; resend=true — только если пользователь просил отправить в "
    "Telegram. Удаляй встречу только по явной просьбе."
)
```

Перед `def main()`:

```python
class ActionItem(TypedDict):
    task: str
    owner: str
    deadline: str | None


class Followup(TypedDict):
    """Фоллоу-ап по схеме SPEC_v3 §7; окончательная проверка — в демоне
    (summarizer.validate_followup)."""
    context: str
    discussed: list[str]
    decisions: list[str]
    action_items: list[ActionItem]
    open_questions: list[str]


@server.tool(annotations=EDIT, structured_output=False)
@tool_errors
def rename_meeting(meeting_id: str, title: str) -> str:
    """Переименовать встречу. Библиотека и поиск увидят новое название; уже
    отправленные сообщения Telegram и заметки не меняются."""
    r = _call("rename_session", {"session_id": meeting_id, "title": title})
    return f"Встреча переименована: «{r['title']}»."


@server.tool(annotations=EDIT, structured_output=False)
@tool_errors
def resummarize_meeting(meeting_id: str) -> str:
    """Заново собрать фоллоу-ап встречи через Claude из той же расшифровки —
    как кнопка «Пересобрать саммари» в карточке."""
    _call("resummarize", {"session_id": meeting_id})
    return ("Пересобираю фоллоу-ап — новая версия появится в Библиотеке через пару "
            "минут и, если подключён Telegram, придёт туда ответом на прежнюю.")


@server.tool(annotations=EDIT, structured_output=False)
@tool_errors
def update_followup(meeting_id: str, followup: Followup, resend: bool = False) -> str:
    """Заменить фоллоу-ап встречи исправленной версией. Сначала прочитай встречу
    через get_meeting и пришли фоллоу-ап ЦЕЛИКОМ (все пять разделов), изменив
    только то, о чём просил пользователь. deadline — дата, "asap" или null.
    resend=true — ещё и отправить новую версию в Telegram ответом на прежнюю
    (только если пользователь просил)."""
    r = _call("update_followup", {"session_id": meeting_id, "followup": dict(followup),
                                  "resend": resend})
    sent = ("Новая версия уйдёт в Telegram ответом на прежнюю." if resend
            else "В Telegram не отправляю; заметка в ~/MeetMind обновится.")
    return f"Фоллоу-ап обновлён (ревизия {r['revision']}). {sent}"


@server.tool(annotations=EDIT, structured_output=False)
@tool_errors
def set_keep_audio(meeting_id: str, keep: bool) -> str:
    """Хранить аудио встречи (keep=true): автоочистка старых записей его не
    удалит. keep=false — снова по общим правилам."""
    _call("set_keep_audio", {"session_id": meeting_id, "keep": keep})
    return ("Аудио закреплено — автоочистка его не удалит." if keep
            else "Аудио снова удаляется по общим правилам.")


@server.tool(annotations=DELETE, structured_output=False)
@tool_errors
def delete_meeting(meeting_id: str, title: str) -> str:
    """Удалить встречу навсегда: аудио, расшифровку, фоллоу-ап и поиск. Заметки
    в ~/MeetMind и сообщения в Telegram останутся. Только по явной просьбе
    пользователя. title — точное название встречи из list_meetings или
    get_meeting: если не совпадёт, ничего не удалится."""
    _call("delete_session", {"session_id": meeting_id, "title": title})
    return f"Встреча «{title}» удалена. Заметки в ~/MeetMind и сообщения в Telegram остались."
```

- [ ] **Step 4: тексты мастера.** В `ClaudeStepView` (`SetupWizard.swift`) `exampleRequests` и `dataNote` заменить на:

```swift
    static let exampleRequests = [
        "Что решили на последней встрече?",
        "Какие задачи на мне за неделю?",
        "В задаче про отчёт исполнитель — Стас, поправь",
    ]
    static let dataNote = "Claude видит встречу, только когда сам её запрашивает; ответы идут в твой аккаунт Claude. Менять встречи Claude будет с твоего разрешения в Claude."
```

В `README.md` в разделе «Встречи в Claude (MCP)» список инструментов дополнить: `rename_meeting`, `resummarize_meeting`, `update_followup` (с `resend` в Telegram), `set_keep_audio`, `delete_meeting` (сверяет название).

- [ ] **Step 5: тесты и сборка**

Run: `.venv/bin/python -m pytest -q && app/build.sh && app/Tests/run.sh`
Expected: всё зелёное.

- [ ] **Step 6: commit** — `daemon/mcp_server.py tests/test_mcp_server.py tests/test_mcp_e2e.py app/Sources/MeetMind/SetupWizard.swift README.md`, `feat(mcp): инструменты правки — переименование, пересборка, правка фоллоу-апа, аудио, удаление`.

---

### Task 16: установка и ручная проверка этапа 2

**Files:** Modify: `AUDIT.md`

- [ ] **Step 1: установка** — как Task 11 Step 5 (проверка простоя, `app/install.sh`).

- [ ] **Step 2: ручная проверка с пользователем** (Claude Desktop или Claude Code, на прошлой встрече, которую не жалко):
  1. «Переименуй встречу X в …» → в Библиотеке новое имя.
  2. «В задаче … исполнитель — …, поправь» без отправки → Claude зовёт `get_meeting`, затем `update_followup` (`resend=false`); в карточке ревизия +1, в `~/MeetMind/<дата>/` заметка обновилась, Telegram молчит.
  3. То же «и перешли в Telegram» → новая версия пришла ответом на старое сообщение. **Сообщение уходит в Telegram — делать только с согласия пользователя.**
  4. «Удали встречу …» на тестовой встрече → Claude спрашивает разрешение (`destructive_hint`); с неверным названием — отказ с правильным названием.

- [ ] **Step 3: `AUDIT.md`** — в журнал:

```markdown
- 2026-10-15 — MCP, этап 2: из Claude можно переименовать встречу, пересобрать
  саммари, исправить фоллоу-ап (новая ревизия, заметка перезаписывается,
  Telegram — только с resend), закрепить аудио и удалить встречу (со сверкой
  названия). Повторная доставка — db.redeliver со сбросом попыток.
```

Commit `AUDIT.md`, `docs(audit): MCP, этап 2 — правка встреч из Claude`.

---

# Этап 3. Управление записью

### Task 17: демон — запросы старта и стопа записи

**Files:** Modify: `daemon/app.py`; Create: `tests/test_record_requests.py`

- [ ] **Step 1: тесты** — создать `tests/test_record_requests.py`:

```python
"""Запись по просьбе Claude (MCP): демон поднимает флаг в status, пишет
приложение (Process Tap). Стоп owner=app — через stop_requested, как у
предохранителей; owner=daemon — сразу."""
import pytest

from daemon.app import DaemonApp
from daemon.control import ControlError
from tests.test_app_owner import _FakeRecorder


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def app(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    a = DaemonApp()
    a._clock = clock
    return a


def test_request_start_sets_flag_in_status(app):
    assert app.status({})["start_requested"] is None
    assert app.request_start({"title": "  Синк  "}) == {"requested": True}
    assert app.status({})["start_requested"] == {"title": "Синк"}


def test_request_start_without_title(app):
    app.request_start({})
    assert app.status({})["start_requested"] == {"title": None}


def test_start_request_expires(app, clock):
    app.request_start({})
    clock.now += 8.1
    assert app.status({})["start_requested"] is None


def test_start_recording_clears_request(app):
    app.request_start({"title": "Синк"})
    app.start_recording({"audio_path_owner": "app", "title": "Синк", "source": "mcp"})
    assert app.status({})["start_requested"] is None


def test_failed_start_also_clears_request(app):
    app.request_start({})
    with pytest.raises(ControlError):
        app.start_recording({"audio_path_owner": "cloud"})
    assert app.status({})["start_requested"] is None


def test_request_start_while_recording_is_busy(app):
    app.start_recording({"audio_path_owner": "app"})
    with pytest.raises(ControlError) as exc:
        app.request_start({})
    assert exc.value.code == "busy"


def test_request_stop_owner_app_sets_flag(app):
    sid = app.start_recording({"audio_path_owner": "app"})["session_id"]
    assert app.request_stop({}) == {"session_id": sid, "requested": True}
    assert app.status({})["stop_requested"] == "mcp"


def test_request_stop_keeps_guard_reason(app):
    app.start_recording({"audio_path_owner": "app"})
    app._stop_request = "duration_limit"
    app.request_stop({})
    assert app.status({})["stop_requested"] == "duration_limit"


def test_request_stop_owner_daemon_stops_now(app, monkeypatch):
    _FakeRecorder.instances = []
    monkeypatch.setattr("daemon.app.Recorder", _FakeRecorder)
    sid = app.start_recording({"title": "x"})["session_id"]
    assert app.request_stop({})["session_id"] == sid
    assert app.status({})["recording"] is None


def test_request_stop_without_recording(app):
    with pytest.raises(ControlError) as exc:
        app.request_stop({})
    assert exc.value.code == "no_active_session"
```

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_record_requests.py`
Expected: FAIL — нет `start_requested` в status, нет `request_start`.

- [ ] **Step 3: реализация.** В `daemon/app.py`:

После `GAP_NOTE_APP_GONE = ...`:

```python
# Запрос записи от Claude (MCP) живёт столько, сколько приложению нужно, чтобы
# его увидеть (опрос status раз в 3 с) и начать; позже не сработает. MCP ждёт
# дольше (12 с) — если приложение начало в последний момент, ответ не соврёт.
START_REQUEST_TTL_SEC = 8
```

В `DaemonApp.__init__` после `self._stop_request = ...`:

```python
        self._start_request: dict | None = None  # запрос записи от Claude (MCP)
        self._clock = time.monotonic  # тесты подменяют
```

В `start_recording` первой строкой внутри `with self._lock:`:

```python
            # Запрос Claude отработан при любом исходе — неудачный старт не
            # повторяется на следующем опросе.
            self._start_request = None
```

В `status`, перед `return {`:

```python
        with self._lock:
            req = self._start_request
            if req is not None and req["expires"] <= self._clock():
                self._start_request = req = None
        start_requested = {"title": req["title"]} if req is not None else None
```

и в возвращаемый словарь после `"stop_requested": self._stop_request,` — `"start_requested": start_requested,`.

После `stop_recording` добавить:

```python
    def request_start(self, params: dict) -> dict:
        """Claude (MCP) просит начать запись. Пишет приложение (Process Tap и
        микрофон, права TCC), не демон: флаг уходит в status, приложение при
        опросе запускает запись как по ⌘⇧R. Флаг живёт START_REQUEST_TTL_SEC."""
        title = (params.get("title") or "").strip()[:200] or None
        with self._lock:
            if self._active_sid is not None:
                raise ControlError("busy", "уже идёт запись")
            self._start_request = {"title": title,
                                   "expires": self._clock() + START_REQUEST_TTL_SEC}
        return {"requested": True}

    def request_stop(self, params: dict) -> dict:
        """Claude (MCP) просит остановить запись. Пишет приложение — флаг
        stop_requested (как у предохранителей), приложение останавливает движок
        и само зовёт stop_recording. Пишет демон — останавливаем сразу."""
        with self._lock:
            if self._active_sid is None:
                raise ControlError("no_active_session", "нет активной записи")
            if self._owner == "app":
                if self._stop_request is None:
                    self._stop_request = "mcp"
                return {"session_id": self._active_sid, "requested": True}
        return self.stop_recording({})
```

В `handlers()` после `"cancel_recording": ...`:

```python
            "request_start": self.request_start,
            "request_stop": self.request_stop,
```

- [ ] **Step 4: тесты проходят**

Run: `.venv/bin/python -m pytest -q`
Expected: всё зелёное (в том числе `tests/test_app_owner.py` — предохранители не изменились).

- [ ] **Step 5: commit** — `daemon/app.py tests/test_record_requests.py`, `feat(daemon): запросы старта и стопа записи от Claude через status`.

---

### Task 18: MCP — старт, стоп и импорт

**Files:** Modify: `daemon/mcp_server.py`, `tests/test_mcp_server.py`, `tests/test_mcp_e2e.py`

- [ ] **Step 1: тесты** — в конец `tests/test_mcp_server.py`:

```python
CONTROL_TOOLS = {"start_recording", "stop_recording", "import_audio"}


@pytest.fixture()
def fast(monkeypatch):
    monkeypatch.setattr(mcp_server, "POLL_SEC", 0)
    monkeypatch.setattr(mcp_server, "WAIT_SEC", 0.05)


def statuses(*items):
    it = iter(items)
    last = {}

    def handler(params):
        nonlocal last
        last = next(it, last)
        return last
    return handler


def test_control_tool_annotations():
    found = tools()
    for name in CONTROL_TOOLS:
        a = found[name].annotations
        assert (a.read_only_hint, a.destructive_hint) == (False, False), name


def test_start_recording_waits_for_app(daemon, fast):
    daemon.handlers["request_start"] = {"requested": True}
    daemon.handlers["status"] = statuses(
        {"recording": None},
        {"recording": "ccc333", "recording_display_title": "Синк"})
    out = mcp_server.start_recording("Синк")
    assert out == "Запись началась: **Синк** · id `ccc333`."
    assert daemon.calls[0] == ("request_start", {"title": "Синк"})


def test_start_recording_timeout(daemon, fast):
    daemon.handlers["request_start"] = {"requested": True}
    daemon.handlers["status"] = {"recording": None}
    with pytest.raises(ToolError, match="не начал запись"):
        mcp_server.start_recording()
    assert daemon.calls[0] == ("request_start", {})


def test_start_recording_busy(daemon, fast):
    daemon.handlers["request_start"] = control.DaemonError("busy", "уже идёт запись")
    with pytest.raises(ToolError, match="уже идёт запись"):
        mcp_server.start_recording()


def test_stop_recording_processed(daemon, fast):
    daemon.handlers["status"] = statuses({"recording": "ccc333"}, {"recording": "ccc333"},
                                         {"recording": None})
    daemon.handlers["request_stop"] = {"session_id": "ccc333", "requested": True}
    daemon.handlers["sessions_list"] = {"sessions": [dict(SESSIONS[0], id="ccc333")]}
    assert "ушла в обработку" in mcp_server.stop_recording()


def test_stop_recording_cancelled_empty(daemon, fast):
    daemon.handlers["status"] = statuses({"recording": "ccc333"}, {"recording": None})
    daemon.handlers["request_stop"] = {"session_id": "ccc333", "requested": True}
    daemon.handlers["sessions_list"] = {"sessions": SESSIONS}
    assert "отменил" in mcp_server.stop_recording()


def test_stop_recording_nothing_to_stop(daemon, fast):
    daemon.handlers["status"] = {"recording": None}
    with pytest.raises(ToolError, match="Запись не идёт"):
        mcp_server.stop_recording()


def test_stop_recording_timeout(daemon, fast):
    daemon.handlers["status"] = {"recording": "ccc333"}
    daemon.handlers["request_stop"] = {"session_id": "ccc333", "requested": True}
    with pytest.raises(ToolError, match="ещё останавливается"):
        mcp_server.stop_recording()


def test_import_audio(daemon):
    daemon.handlers["import_audio"] = {"session_id": "ddd444"}
    assert "ddd444" in mcp_server.import_audio("/tmp/call.m4a", "Звонок")
    assert daemon.calls == [("import_audio", {"path": "/tmp/call.m4a", "title": "Звонок"})]
```

В `tests/test_mcp_e2e.py` в ожидаемое множество инструментов добавить `"start_recording", "stop_recording", "import_audio"` (итого 13).

- [ ] **Step 2: убедиться, что падают**

Run: `.venv/bin/python -m pytest -q tests/test_mcp_server.py tests/test_mcp_e2e.py`
Expected: FAIL — нет инструментов управления.

- [ ] **Step 3: реализация.** В `daemon/mcp_server.py` к импортам добавить `import time`; после `ALL_SESSIONS`:

```python
# Старт и стоп записи: приложение видит флаг демона при опросе status (раз в
# 3 с); ждём дольше срока флага (8 с) — ответ не соврёт.
WAIT_SEC = 12
POLL_SEC = 0.5
```

В `INSTRUCTIONS` в конец строки добавить: `" Запись: start_recording и stop_recording — только по просьбе пользователя; import_audio импортирует аудиофайл с этого Mac."`

Перед `def main()`:

```python
@server.tool(annotations=EDIT, structured_output=False)
@tool_errors
def start_recording(title: str | None = None) -> str:
    """Начать запись звонка в BeeDott (пишет приложение: звук компьютера и
    микрофон). title — название встречи; без него BeeDott возьмёт его из
    Календаря. Ответ — когда запись реально началась."""
    _call("request_start", {"title": title} if title else {})
    deadline = time.monotonic() + WAIT_SEC
    while True:
        st = _call("status")
        if st.get("recording"):
            title_now = st.get("recording_display_title") or "Без названия"
            return f"Запись началась: **{title_now}** · id `{st['recording']}`."
        if time.monotonic() >= deadline:
            raise ToolError("BeeDott не начал запись за 12 с — приложение не запущено "
                            "или занято; проверь recording_status")
        time.sleep(POLL_SEC)


@server.tool(annotations=EDIT, structured_output=False)
@tool_errors
def stop_recording() -> str:
    """Остановить текущую запись; встреча уйдёт в обработку, фоллоу-ап будет
    через несколько минут."""
    sid = _call("status").get("recording")
    if not sid:
        raise ToolError("Запись не идёт.")
    _call("request_stop")
    deadline = time.monotonic() + WAIT_SEC
    while _call("status").get("recording") == sid:
        if time.monotonic() >= deadline:
            raise ToolError("Запись ещё останавливается — проверь recording_status")
        time.sleep(POLL_SEC)
    # Пустую или случайную короткую запись демон отменяет (CANCELLED), а
    # отменённых нет в sessions_list. session_detail не годится: он создаёт
    # каталог сессии заново (paths.session_dir).
    listed = _call("sessions_list", {"limit": 20})["sessions"]
    if not any(s["id"] == sid for s in listed):
        return "Запись была пустой или слишком короткой — BeeDott её отменил."
    return (f"Запись остановлена, встреча ушла в обработку · id `{sid}`. "
            "Фоллоу-ап будет через несколько минут.")


@server.tool(annotations=EDIT, structured_output=False)
@tool_errors
def import_audio(path: str, title: str | None = None) -> str:
    """Импортировать аудиофайл с этого Mac (m4a, mp3, wav…) как встречу: он
    пройдёт расшифровку и фоллоу-ап. path — полный путь к файлу."""
    params = {"path": path}
    if title:
        params["title"] = title
    r = _call("import_audio", params)
    return f"Импортирую — встреча появится в Библиотеке и пройдёт обработку · id `{r['session_id']}`."
```

- [ ] **Step 4: тесты проходят**

Run: `.venv/bin/python -m pytest -q`
Expected: всё зелёное.

- [ ] **Step 5: commit** — `daemon/mcp_server.py tests/test_mcp_server.py tests/test_mcp_e2e.py`, `feat(mcp): старт и стоп записи и импорт аудио из Claude`.

---

### Task 19: Swift — запись по просьбе Claude

**Files:** Modify: `app/Sources/MeetMind/RuntimeLogic.swift`, `app/Tests/main.swift`, `app/Sources/MeetMind/MeetMindApp.swift`

- [ ] **Step 1: тест** — в `app/Tests/main.swift` перед `if failures == 0 {`:

```swift
// 26. Запрос записи от Claude (MCP) в status демона.
do {
    check(RuntimeLogic.startRequest(["start_requested": ["title": "Синк"]]) == StartRequest(title: "Синк"), "с названием")
    check(RuntimeLogic.startRequest(["start_requested": ["title": NSNull()]]) == StartRequest(title: nil), "без названия")
    check(RuntimeLogic.startRequest(["start_requested": ["title": "  "]]) == StartRequest(title: nil), "пустое название")
    check(RuntimeLogic.startRequest(["start_requested": NSNull()]) == nil, "null — запроса нет")
    check(RuntimeLogic.startRequest([:]) == nil, "старый демон без поля")
}
```

- [ ] **Step 2: убедиться, что не собирается**

Run: `app/Tests/run.sh`
Expected: ошибка компиляции — нет `StartRequest`.

- [ ] **Step 3: `RuntimeLogic.swift`.** Перед `enum RuntimeLogic`:

```swift
/// Claude (MCP) просит начать запись: поле start_requested в status демона.
struct StartRequest: Equatable {
    let title: String?
}
```

Внутри `enum RuntimeLogic` (после `activeJobs`):

```swift
    /// start_requested из status: {"title": ...} — запрос есть; null или поля
    /// нет (старый демон) — нет. Пустое название — nil (возьмём из Календаря).
    static func startRequest(_ status: [String: Any]) -> StartRequest? {
        guard let request = status["start_requested"] as? [String: Any] else { return nil }
        let title = (request["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        return StartRequest(title: (title?.isEmpty ?? true) ? nil : title)
    }
```

- [ ] **Step 4: `MeetMindApp.swift`.**

(а) Сигнатура `performStartRecording`: `nonisolated private func performStartRecording(source: String = "manual") throws -> String? {` → `nonisolated private func performStartRecording(source: String = "manual", title: String? = nil) throws -> String? {`. Сразу после `let meeting = CalendarContext.currentMeeting()`:

```swift
        // Название от Claude (MCP) главнее Календаря; участники — из Календаря.
        let recordingTitle = title ?? meeting?.title
```

В обоих вызовах `ControlClient.startRecording(title: meeting?.title, ...)` заменить `title: meeting?.title` на `title: recordingTitle`.

(б) В `refreshStatus` в ветке предохранителя `NSLog("MeetMind: предохранитель (%@) — останавливаю запись", reason)` → `NSLog("MeetMind: демон просит остановить запись (%@)", reason)` и комментарий над ней дополнить: `// Предохранитель демона (лимит длительности, диск, размер WAV) — аудит T7 — или Claude (MCP, причина "mcp").`

(в) В `refreshStatus` сразу после закрывающей скобки цепочки `if let sid = recording, ... { ... } else if ownerKnown, ... { ... }` и перед комментарием `// P16: пока мы поллим...`:

```swift
                // Claude (MCP) попросил начать запись — как ⌘⇧R, но с его
                // названием. Флаг снимает демон в start_recording.
                if recording == nil, self.capture.engine == nil,
                   let request = RuntimeLogic.startRequest(result) {
                    do {
                        recording = try self.performStartRecording(source: "mcp", title: request.title)
                        NSLog("MeetMind: запись по просьбе Claude началась")
                    } catch {
                        NSLog("MeetMind: запись по просьбе Claude не началась (%@)",
                              error.localizedDescription)
                    }
                }
```

- [ ] **Step 5: тесты и сборка**

Run: `app/Tests/run.sh && app/build.sh`
Expected: `OK — все тесты прошли`, сборка без ошибок.

- [ ] **Step 6: commit** — `app/Sources/MeetMind/RuntimeLogic.swift app/Tests/main.swift app/Sources/MeetMind/MeetMindApp.swift`, `feat(app): запись по просьбе Claude — флаг start_requested из status`.

---

### Task 20: тексты, документация, установка, ручная проверка этапа 3

**Files:** Modify: `app/Sources/MeetMind/SetupWizard.swift`, `README.md`, `docs/INSTALL.md`, `AUDIT.md`

- [ ] **Step 1: тексты мастера.** В `ClaudeStepView` (`SetupWizard.swift`): строку `// Этап 1 — только чтение; этапы 2 и 3 дописывают правку и запись.` удалить; `exampleRequests` и `dataNote`:

```swift
    static let exampleRequests = [
        "Что решили на последней встрече?",
        "В задаче про отчёт исполнитель — Стас, поправь",
        "Начни запись «Синк по реакциям»",
    ]
    static let dataNote = "Claude видит встречу, только когда сам её запрашивает; ответы идут в твой аккаунт Claude. Менять встречи и запускать запись Claude будет с твоего разрешения в Claude."
```

- [ ] **Step 2: документация.** `README.md` → раздел «Встречи в Claude (MCP)»: добавить `start_recording` / `stop_recording` (пишет приложение по флагу в `status`) и `import_audio`. `docs/INSTALL.md` → строка про шаг «Claude»: «…спрашивать про встречи, править фоллоу-апы и запускать запись».

- [ ] **Step 3: тесты и сборка, commit**

Run: `.venv/bin/python -m pytest -q && app/Tests/run.sh && app/build.sh`
Commit `app/Sources/MeetMind/SetupWizard.swift README.md docs/INSTALL.md`, `docs(mcp): тексты мастера и README — правка и запись из Claude`.

- [ ] **Step 4: установка** — как Task 11 Step 5.

- [ ] **Step 5: ручная проверка с пользователем — не во время настоящей встречи:**
  1. В Claude: «начни запись „Тест MCP“» → в меню-баре идёт запись, `sqlite3 … "SELECT audio_owner, source, title FROM sessions ORDER BY created_at DESC LIMIT 1"` → `app|mcp|Тест MCP` (нативный движок, не Python-рекордер).
  2. Через ~15 с: «останови запись» → Claude отвечает «ушла в обработку» (тишина даст ошибку транскрипции «в записи нет речи» — это нормально для теста).
  3. Запись на 3 с → «останови» → «Запись была пустой или слишком короткой — BeeDott её отменил».
  4. При закрытом BeeDott «начни запись» → «BeeDott не запущен…».
  5. Тестовые встречи удалить через Claude (`delete_meeting`) — заодно проверка удаления.

- [ ] **Step 6: `AUDIT.md`** — в журнал:

```markdown
- 2026-10-15 — MCP, этап 3: из Claude можно начать и остановить запись (демон
  поднимает start_requested / stop_requested="mcp" в status, пишет приложение
  нативным движком) и импортировать аудиофайл. Отмена записи из Claude не
  отдаётся. Спек: docs/superpowers/specs/2026-10-14-claude-mcp-design.md.
```

Commit `AUDIT.md`, `docs(audit): MCP, этап 3 — запись из Claude`.

- [ ] **Step 7: финальная проверка**

```bash
.venv/bin/python -m pytest -q && app/Tests/run.sh
git status --short   # пусто
git log origin/master..HEAD --oneline   # пусто — всё запушено
```
