# Настройки саммари — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** выбор модели Claude (Opus 5.5 / Sonnet 5.5 / Fable 5.1) и редактируемая инструкция фоллоу-апа в «Настройке…» → шаг «Саммари».

**Architecture:** инструкция — файл `app_dir()/followup_instructions.md`, демон читает его на каждом саммари (`summarizer.load_instructions()`); технический формат JSON (`_FORMAT_RULES`) всегда добавляется кодом. Модель — `ANTHROPIC_MODEL` в `config.env` + перезапуск демона. Мастер говорит с Python через новые команды `daemon.setup` (`summary-settings`, `set-model`, `set-instructions`).

**Tech Stack:** Python 3.11 (pytest), Swift/SwiftUI (swiftc без Xcode, `app/build.sh`, `app/Tests/run.sh`).

**Спек:** `docs/superpowers/specs/2026-10-14-summary-settings-design.md`.

**Команды проверки:** `.venv/bin/python -m pytest -q` (из корня репо), `app/Tests/run.sh`, `app/build.sh`.

---

## Файлы

| Файл | Что меняется |
|---|---|
| `tests/conftest.py` | autouse-изоляция `MEETMIND_APPDIR` |
| `config.py` | константа `DEFAULT_ANTHROPIC_MODEL` |
| `daemon/summarizer.py` | `SYSTEM_PROMPT` (короткая роль), `DEFAULT_INSTRUCTIONS`, `_FORMAT_RULES`, `instructions_path()`, `custom_instructions()`, `load_instructions()`, новый `build_prompt` |
| `daemon/setup.py` | `SUMMARY_MODELS`, разбор stdin, `summary-settings`, `set-model`, `set-instructions`, поля `status` |
| `tests/test_summarizer.py`, `tests/test_anthropic_api.py`, `tests/test_claude_cli.py`, `tests/test_setup.py` | тесты |
| `app/Sources/MeetMind/RuntimeLogic.swift` | `SummaryModelOption`, `summaryModels`, тексты ошибок |
| `app/Tests/main.swift` | тесты текстов ошибок и списка моделей |
| `app/Sources/MeetMind/SetupWizard.swift` | `SetupStatus`, `SetupModel` (модель, инструкция), `SummaryStepView` |
| `app/Sources/MeetMind/InstructionsWindow.swift` (новый) | окно редактора инструкции |
| `AUDIT.md` | запись в журнал |

---

### Task 1: изоляция тестов от настоящей папки BeeDott

**Files:** Modify: `tests/conftest.py`

- [ ] **Step 1: добавить autouse-фикстуру** в конец `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _isolated_appdir(tmp_path, monkeypatch):
    """Каждый тест — со своей папкой приложения: иначе load_instructions()
    прочитает настоящий followup_instructions.md пользователя. Тесты со своей
    фикстурой appdir ставят MEETMIND_APPDIR позже и переопределяют этот."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path / "isolated-app"))
```

- [ ] **Step 2: прогнать весь набор**

Run: `.venv/bin/python -m pytest -q`
Expected: все тесты проходят (столько же, сколько до изменения). Если какой-то тест падает — он неявно зависел от настоящей папки; разобраться и починить тест, а не фикстуру.

- [ ] **Step 3: commit**

```bash
git add tests/conftest.py
git commit -m "test: каждый тест со своей папкой приложения (MEETMIND_APPDIR)"
```

---

### Task 2: промпт — стандартная инструкция, формат и чтение файла

**Files:** Modify: `daemon/summarizer.py`, `tests/test_summarizer.py`, `tests/test_anthropic_api.py`, `tests/test_claude_cli.py`

- [ ] **Step 1: падающие тесты** — дописать в `tests/test_summarizer.py`:

```python
from daemon import summarizer


def test_default_instructions_cover_all_sections():
    for key in ("context", "discussed", "decisions", "action_items", "open_questions"):
        assert key in summarizer.DEFAULT_INSTRUCTIONS


def test_load_instructions_default_without_file():
    assert summarizer.load_instructions() == summarizer.DEFAULT_INSTRUCTIONS
    assert summarizer.custom_instructions() is None


def test_load_instructions_blank_file_means_default():
    summarizer.instructions_path().write_text("  \n\n", encoding="utf-8")
    assert summarizer.load_instructions() == summarizer.DEFAULT_INSTRUCTIONS
    assert summarizer.custom_instructions() is None


def test_load_instructions_custom_file():
    summarizer.instructions_path().write_text("Пиши капслоком.\n", encoding="utf-8")
    assert summarizer.load_instructions() == "Пиши капслоком."
    assert summarizer.custom_instructions() == "Пиши капслоком."


def test_load_instructions_unreadable_file_means_default():
    summarizer.instructions_path().write_bytes(b"\xff\xfe\x00bad")
    assert summarizer.load_instructions() == summarizer.DEFAULT_INSTRUCTIONS


def test_build_prompt_order_transcript_instructions_format():
    summarizer.instructions_path().write_text("Пиши капслоком.", encoding="utf-8")
    prompt = build_prompt("Спикер 1: катим в пятницу", {"title": "Синк"})
    t = prompt.index("Спикер 1: катим в пятницу")
    i = prompt.index("Пиши капслоком.")
    f = prompt.index(summarizer._FORMAT_RULES)
    assert t < i < f
    assert summarizer.DEFAULT_INSTRUCTIONS not in prompt
```

Run: `.venv/bin/python -m pytest -q tests/test_summarizer.py`
Expected: FAIL (`AttributeError: ... DEFAULT_INSTRUCTIONS` / `instructions_path`).

- [ ] **Step 2: реализация в `daemon/summarizer.py`.**

В импорты (после `import json`):

```python
import logging
from pathlib import Path

from daemon.paths import app_dir

logger = logging.getLogger(__name__)
```

Заменить блок `SYSTEM_PROMPT = (...)` на:

```python
# Роль — фиксированная. Всё, что определяет фоллоу-ап (стиль, разделы, правила),
# — в инструкции после транскрипта: на длинном контексте указания после
# документа соблюдаются лучше. Инструкцию пользователь правит в «Настройке…».
SYSTEM_PROMPT = "Ты пишешь фоллоу-ап по рабочей встрече по инструкции из сообщения пользователя."

DEFAULT_INSTRUCTIONS = """Ты пишешь фоллоу-ап по рабочей встрече для участника, который хочет за минуту понять главное: о чём договорились, что делать ему и другим, что осталось открытым. Фоллоу-ап приходит в Telegram — его должно хотеться дочитать.

Пиши по-русски простым живым языком, как толковый коллега пишет в рабочий чат: короткие законченные предложения, без канцелярита и телеграфного стиля. Не пересказывай встречу подряд — отбирай важное и ставь его первым. Одну мысль пиши один раз: если пункт уже есть в решениях или задачах, не повторяй его в обсуждении. Термины и сленг команды оставляй как есть — их все знают, но сам слова не сокращай и не перегружай текст цифрами: число нужно, только если без него непонятно решение.

Разделы фоллоу-апа:
- "context": 2–3 предложения — о чём встреча и главный итог. Человек, который её пропустил, должен понять суть только из этого абзаца.
- "discussed": самые важные темы — не больше 6 пунктов, каждый в одно-два коротких предложения: что обсуждали и к чему пришли или что выяснилось. Мелочи и то, что уже есть в решениях и задачах, не включай.
- "decisions": принятые решения, каждое — одной фразой.
- "action_items": конкретные поручения, у которых есть исполнитель. task начинается с глагола и понятна без контекста встречи. Близкие поручения одному человеку объединяй в одно; мелочи вроде «ответить в чате» пропускай. deadline — дата, если срок назвали («завтра», «к пятнице» считай от даты встречи), "asap" — если сказали «срочно», иначе null. owner — имя человека, который взял задачу (по обращениям и «я сделаю»); если имя не понять — его роль («ведущий встречи»). «Спикер 2» исполнителем не пиши — это голоса всех собеседников сразу.
- "open_questions": что действительно осталось нерешённым и важно — обычно не больше 3–4 пунктов; не повторяй в них задачи.

Правила:
- Не выдумывай факты, которых нет в транскрипте.
- В транскрипте «Спикер 1» — микрофон автора записи, «Спикер 2» — остальные участники звонка. Имена бери из разговора или списка участников; если неразличимы — так и пиши «Спикер 1», «Спикер 2».
- Даты в тексте пиши по-человечески: «ср, 14.10», а не 2026-10-14."""

INSTRUCTIONS_FILE = "followup_instructions.md"


def instructions_path() -> Path:
    return app_dir() / INSTRUCTIONS_FILE


def custom_instructions() -> str | None:
    """Своя инструкция пользователя или None: файла нет, он пустой или не
    читается (тогда — предупреждение в лог, саммари идёт по стандартной)."""
    try:
        text = instructions_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("инструкция фоллоу-апа не читается (%s) — беру стандартную", exc)
        return None
    return text or None


def load_instructions() -> str:
    return custom_instructions() or DEFAULT_INSTRUCTIONS
```

Заменить `_SCHEMA_RULES = """..."""` целиком на:

```python
# Технический формат ответа — не редактируется: от него зависят валидация,
# рендер и доставка. Через API схему дополнительно держит structured outputs.
_FORMAT_RULES = """Формат ответа — один JSON-объект (никакого текста вне JSON) ровно с ключами:
- "context": строка;
- "discussed", "decisions", "open_questions": списки строк;
- "action_items": список объектов {"task": "что сделать", "owner": "кто", "deadline": "2026-07-20" | "asap" | null}, deadline — дата в формате YYYY-MM-DD.
Если по разделу сказать нечего — пустой список [] (или пустая строка "" для context). Набор ключей и типы строго по схеме, без лишних ключей. Ответ — только валидный JSON: без markdown-обёртки ```json, без преамбулы."""
```

Заменить тело `build_prompt`:

```python
def build_prompt(transcript: str, meta: dict) -> str:
    """Пользовательский промпт: мета встречи + транскрипт + инструкция
    (своя или стандартная) + технический формат ответа."""
    title = meta.get("title") or "Без названия"
    duration = meta.get("duration_min")
    duration_str = f"{duration} минут" if duration is not None else "неизвестна"
    return f"""{attendees_hint(meta)}{_date_hint(meta)}Ниже — транскрипт встречи «{title}» (длительность {duration_str}). Составь по нему фоллоу-ап по инструкции после транскрипта.

Транскрипт:
---
{transcript}
---

Инструкция:
{load_instructions()}

{_FORMAT_RULES}"""
```

Проверить, что `_SCHEMA_RULES` больше нигде не упоминается: `grep -rn _SCHEMA_RULES daemon tests` — пусто.

- [ ] **Step 3: тесты API и CLI на свою инструкцию.** В `tests/test_anthropic_api.py` дописать:

```python
def test_custom_instructions_go_to_user_message(fake_sdk):
    from daemon import summarizer
    summarizer.instructions_path().write_text("Пиши капслоком.", encoding="utf-8")
    anthropic_api.summarize(TRANSCRIPT, META)
    req = fake_sdk.requests[0]
    assert req["system"] == SYSTEM_PROMPT
    assert "Пиши капслоком." in req["messages"][0]["content"]
```

В `tests/test_claude_cli.py` в `test_prompt_goes_to_stdin_with_print_flags` после `assert "--append-system-prompt" in cmd` добавить:

```python
    from daemon.summarizer import DEFAULT_INSTRUCTIONS, SYSTEM_PROMPT
    assert cmd[cmd.index("--append-system-prompt") + 1] == SYSTEM_PROMPT
```

и после `assert TRANSCRIPT in captured["input"]`:

```python
    assert DEFAULT_INSTRUCTIONS in captured["input"]
```

- [ ] **Step 4: прогнать**

Run: `.venv/bin/python -m pytest -q`
Expected: всё зелёное, включая старые `test_build_prompt_contains_transcript_meta_and_schema` (`'"action_items"'` есть в `_FORMAT_RULES`) и `test_returns_validated_followup_and_sends_expected_request` (`system == SYSTEM_PROMPT`).

- [ ] **Step 5: commit**

```bash
git add daemon/summarizer.py tests/test_summarizer.py tests/test_anthropic_api.py tests/test_claude_cli.py
git commit -m "feat(summary): инструкция фоллоу-апа из файла, формат JSON отдельно"
```

---

### Task 3: проверка качества на прошлых встречах (вручную, без доставки)

**Files:** только scratchpad (вне репо — репозиторий публичный, транскрипты не коммитим).

- [ ] **Step 1: скрипт в scratchpad** `quality_check.py`:

```python
"""Сравнить новый промпт со старыми фоллоу-апами на 3 встречах (без демона и Telegram)."""
import json, sys
from pathlib import Path
sys.path.insert(0, "/Users/b.botirov/AI/meetmind")
import config  # noqa: F401  (load_dotenv: ключ API)
from daemon import anthropic_api, db
from daemon.stages import _attendees, _duration_min, _title
from daemon.timeutil import to_local

APP = Path.home() / "Library/Application Support/MeetMind"
con = db.connect()
cands = []
for d in (APP / "sessions").iterdir():
    t, f = d / "transcript.txt", d / "followup.json"
    if t.exists() and f.exists():
        cands.append((t.stat().st_size, d.stat().st_mtime, d.name))
longest = max(cands)[2]
recent = [c[2] for c in sorted(cands, key=lambda c: c[1], reverse=True) if c[2] != longest][:2]
out = {}
for sid in [longest, *recent]:
    s = db.get_session(con, sid)
    sdir = APP / "sessions" / sid
    meta = {"title": _title(s), "duration_min": _duration_min(s), "attendees": _attendees(s),
            "date": to_local(s["started_at"]).date() if s["started_at"] else None}
    new = anthropic_api.summarize((sdir / "transcript.txt").read_text(encoding="utf-8"), meta)
    old = json.loads((sdir / "followup.json").read_text(encoding="utf-8"))
    out[sid] = {"old": old, "new": new}
    for name, fu in (("OLD", old), ("NEW", new)):
        print(f"--- {sid} {name}: discussed={len(fu['discussed'])} decisions={len(fu['decisions'])} "
              f"tasks={len(fu['action_items'])} open={len(fu['open_questions'])}")
        print(fu["context"])
        for a in fu["action_items"]:
            print("  •", a["owner"], "—", a["task"], "|", a["deadline"])
Path(__file__).with_name("quality_out.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
```

Перед запуском убедиться, что `daemon.stages` экспортирует `_attendees`, `_duration_min`, `_title`, а `daemon.timeutil` — `to_local` (`grep -n "def _attendees\|def _duration_min\|def _title" daemon/stages.py`, `grep -n "def to_local" daemon/timeutil.py`); если имена другие — поправить импорт.

- [ ] **Step 2: запустить** (≈3 запроса к Opus 5.5, порядка $1)

Run: `cd /Users/b.botirov/AI/meetmind && .venv/bin/python <scratchpad>/quality_check.py`

- [ ] **Step 3: сравнить.** Критерии: в NEW все разделы заполнены по смыслу как в OLD, задачи не потерялись (те же исполнители и суть), стиль — короткие живые предложения, нет канцелярита и повторов. Если NEW заметно хуже — вернуться к Task 2 (например, перенести стиль обратно в `SYSTEM_PROMPT`) и повторить. Результат кратко записать в отчёт пользователю; файлы из scratchpad не коммитить.

---

### Task 4: `config.DEFAULT_ANTHROPIC_MODEL` и команды `daemon.setup`

**Files:** Modify: `config.py:84`, `daemon/setup.py`, `tests/test_setup.py`

- [ ] **Step 1: падающие тесты** — в `tests/test_setup.py` в фикстуре `appdir` добавить `"ANTHROPIC_MODEL"` в кортеж `delenv`; ожидание `test_status_clean_mac` заменить на:

```python
    assert events(capsys) == [{"event": "ok", "model": True, "summary": None, "telegram": False,
                               "summary_model": "claude-opus-5-5", "summary_custom": False}]
```

и дописать в конец файла:

```python
import re
from pathlib import Path

from daemon import summarizer


def run(name, stdin_text, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    code = setup.main([name])
    return code, events(capsys)[-1]


def test_set_model_writes_config(appdir, capsys, monkeypatch):
    code, ev = run("set-model", "claude-sonnet-5-5\n", capsys, monkeypatch)
    assert code == 0 and ev == {"event": "ok", "model": "claude-sonnet-5-5"}
    assert "ANTHROPIC_MODEL=claude-sonnet-5-5" in (appdir / "config.env").read_text()
    setup.main(["status"])
    assert events(capsys)[-1]["summary_model"] == "claude-sonnet-5-5"


def test_set_model_rejects_unknown(appdir, capsys, monkeypatch):
    code, ev = run("set-model", "gpt-5\n", capsys, monkeypatch)
    assert code == 1 and ev["code"] == "bad_model"
    assert not (appdir / "config.env").exists()


def test_set_instructions_keeps_multiline_text(appdir, capsys, monkeypatch):
    text = "Первая строка.\n\nВторая строка."
    code, ev = run("set-instructions", text + "\n", capsys, monkeypatch)
    assert code == 0 and ev == {"event": "ok", "custom": True}
    assert summarizer.custom_instructions() == text
    setup.main(["status"])
    assert events(capsys)[-1]["summary_custom"] is True


def test_set_instructions_default_or_blank_removes_file(appdir, capsys, monkeypatch):
    run("set-instructions", "Своя.\n", capsys, monkeypatch)
    code, ev = run("set-instructions", summarizer.DEFAULT_INSTRUCTIONS + "\n", capsys, monkeypatch)
    assert ev == {"event": "ok", "custom": False}
    assert not summarizer.instructions_path().exists()
    run("set-instructions", "Своя.\n", capsys, monkeypatch)
    code, ev = run("set-instructions", "  \n", capsys, monkeypatch)
    assert ev == {"event": "ok", "custom": False}
    assert not summarizer.instructions_path().exists()


def test_set_instructions_too_long(appdir, capsys, monkeypatch):
    code, ev = run("set-instructions", "я" * (setup.MAX_INSTRUCTIONS + 1), capsys, monkeypatch)
    assert code == 1 and ev["code"] == "too_long"
    assert not summarizer.instructions_path().exists()


def test_summary_settings(appdir, capsys, monkeypatch):
    run("set-instructions", "Своя.\n", capsys, monkeypatch)
    setup.main(["summary-settings"])
    ev = events(capsys)[-1]
    assert ev["model"] == "claude-opus-5-5"
    assert ev["instructions"] == "Своя." and ev["custom"] is True
    assert ev["default_instructions"] == summarizer.DEFAULT_INSTRUCTIONS


def test_summary_models_match_swift():
    swift = Path(__file__).parent.parent / "app/Sources/MeetMind/RuntimeLogic.swift"
    ids = set(re.findall(r'id: "(claude-[a-z0-9-]+)"', swift.read_text(encoding="utf-8")))
    assert ids == set(setup.SUMMARY_MODELS)
    assert config.DEFAULT_ANTHROPIC_MODEL in setup.SUMMARY_MODELS
```

Run: `.venv/bin/python -m pytest -q tests/test_setup.py`
Expected: FAIL (нет команд, нет полей; `test_summary_models_match_swift` падает, пока не сделан Task 5 — это ожидаемо, он позеленеет там).

- [ ] **Step 2: `config.py`.** Строку 84 заменить на:

```python
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
```

- [ ] **Step 3: `daemon/setup.py`.** Рядом с прочими константами модуля:

```python
# Модели саммари в мастере. Тот же список с подписями — RuntimeLogic.summaryModels
# (Swift), совпадение проверяет tests/test_setup.py.
SUMMARY_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1")
MAX_INSTRUCTIONS = 20_000
```

Функции (рядом с `cmd_status`):

```python
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
```

В `cmd_status` заменить `return` на:

```python
    from daemon import summarizer
    return {"model": model_cached(), "summary": summary, "telegram": telegram,
            "summary_model": _summary_model(),
            "summary_custom": summarizer.custom_instructions() is not None}
```

В `COMMANDS` добавить `"summary-settings": cmd_summary_settings, "set-model": cmd_set_model, "set-instructions": cmd_set_instructions`. Блок констант stdin и начало `main` заменить:

```python
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
```

(остаток `main` — без изменений: маскирование `secret`, `emit`).

- [ ] **Step 4: прогнать**

Run: `.venv/bin/python -m pytest -q tests/test_setup.py`
Expected: всё зелёное, кроме `test_summary_models_match_swift` (ждёт Task 5).

- [ ] **Step 5: commit**

```bash
git add config.py daemon/setup.py tests/test_setup.py
git commit -m "feat(setup): summary-settings, set-model, set-instructions"
```

---

### Task 5: Swift — список моделей и тексты ошибок

**Files:** Modify: `app/Sources/MeetMind/RuntimeLogic.swift`, `app/Tests/main.swift`

- [ ] **Step 1: падающие тесты** — в `app/Tests/main.swift` перед финальной строкой `if failures == 0 …`:

```swift
// 24. Настройки саммари: модели в мастере и тексты ошибок команд.
do {
    check(RuntimeLogic.summaryModels.map(\.id) == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"], "три модели по порядку")
    check(RuntimeLogic.summaryModels.contains { $0.id == RuntimeLogic.defaultSummaryModel }, "дефолт в списке")
    check(RuntimeLogic.setupErrorText(code: "bad_model", message: "").contains("модели"), "текст bad_model")
    check(RuntimeLogic.setupErrorText(code: "too_long", message: "").contains("20 000"), "текст too_long")
}
```

Run: `app/Tests/run.sh` → Expected: ошибка компиляции (`summaryModels` нет).

- [ ] **Step 2: реализация** — в `RuntimeLogic.swift` перед `enum RuntimeLogic` (или внутри, по стилю файла — рядом с `SetupResult`):

```swift
/// Модель саммари в мастере. Тот же список id — daemon/setup.py SUMMARY_MODELS.
struct SummaryModelOption: Equatable {
    let id: String
    let title: String
    let caption: String
}
```

внутри `enum RuntimeLogic`:

```swift
    static let defaultSummaryModel = "claude-opus-5-5"
    static let summaryModels: [SummaryModelOption] = [
        SummaryModelOption(id: "claude-opus-5-5", title: "Opus 5.5",
                           caption: "По умолчанию — лучшее качество за свои деньги"),
        SummaryModelOption(id: "claude-sonnet-5-5", title: "Sonnet 5.5",
                           caption: "Быстрее и в 2 раза дешевле"),
        SummaryModelOption(id: "claude-fable-5-1", title: "Fable 5.1",
                           caption: "Самая сильная, в 2,5 раза дороже Opus"),
    ]
```

в `setupErrorText` перед `default:`:

```swift
        case "bad_model": return "Такой модели нет в списке"
        case "too_long": return "Инструкция длиннее 20 000 символов — сократи"
```

- [ ] **Step 3: прогнать**

Run: `app/Tests/run.sh && .venv/bin/python -m pytest -q`
Expected: `OK — все тесты прошли`; pytest полностью зелёный (включая `test_summary_models_match_swift`).

- [ ] **Step 4: commit**

```bash
git add app/Sources/MeetMind/RuntimeLogic.swift app/Tests/main.swift
git commit -m "feat(app): список моделей саммари и тексты ошибок настроек"
```

---

### Task 6: окно инструкции

**Files:** Create: `app/Sources/MeetMind/InstructionsWindow.swift`

- [ ] **Step 1: файл целиком**

```swift
// InstructionsWindow.swift — редактор инструкции фоллоу-апа («Настройка…» →
// Саммари → «Инструкция для фоллоу-апов…»). Текст — daemon.setup
// summary-settings / set-instructions; формат JSON демон добавляет сам.
// Спека: docs/superpowers/specs/2026-10-14-summary-settings-design.md.

import AppKit
import SwiftUI

@MainActor
final class InstructionsModel: ObservableObject {
    enum Load: Equatable { case loading, ready, failed(String) }

    @Published var load: Load = .loading
    @Published var text = ""
    @Published var saving = false
    @Published var error: String?
    private var savedText = ""
    private var defaultText = ""
    var onSaved: (Bool) -> Void = { _ in }
    var onClose: () -> Void = {}
    private let queue = DispatchQueue(label: "com.jvmshid.meetmind.instructions")

    var dirty: Bool { load == .ready && text != savedText }

    func reload() {
        load = .loading
        error = nil
        queue.async {
            let result = RuntimeManager.shared.runSetup("summary-settings")
            Task { @MainActor in
                switch result {
                case .ok(let r):
                    self.savedText = r.string("instructions") ?? ""
                    self.defaultText = r.string("default_instructions") ?? ""
                    self.text = self.savedText
                    self.load = .ready
                case .error(let code, let message):
                    self.load = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                default:
                    self.load = .failed("Что-то пошло не так")
                }
            }
        }
    }

    func resetToDefault() { text = defaultText }

    func save(then done: @escaping () -> Void = {}) {
        guard !saving, load == .ready else { return }
        saving = true
        error = nil
        let body = text
        queue.async {
            let result = RuntimeManager.shared.runSetup("set-instructions", stdin: body)
            Task { @MainActor in
                self.saving = false
                switch result {
                case .ok(let r):
                    self.savedText = body
                    self.onSaved(r.bool("custom"))
                    done()
                case .error(let code, let message):
                    self.error = RuntimeLogic.setupErrorText(code: code, message: message)
                default:
                    self.error = "Что-то пошло не так"
                }
            }
        }
    }
}

struct InstructionsView: View {
    @ObservedObject var model: InstructionsModel
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Инструкция для фоллоу-апов").font(.system(size: 17, weight: .bold))
            Group {
                switch model.load {
                case .loading:
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("Загружаю…").font(.system(size: 12))
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                case .failed(let text):
                    VStack(spacing: 8) {
                        Text(text).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
                        Button("Повторить") { model.reload() }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                case .ready:
                    TextEditor(text: $model.text)
                        .font(.system(size: 13))
                        .scrollContentBackground(.hidden)
                        .padding(6)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.rowFill(scheme)))
                }
            }
            Text("Формат ответа (разделы и JSON) BeeDott добавляет сам. Новая инструкция применится к следующим встречам; пересобрать старую — Библиотека → «Пересобрать саммари».")
                .font(.system(size: 12))
                .foregroundStyle(Theme.textSecondary(scheme))
                .fixedSize(horizontal: false, vertical: true)
            if let error = model.error {
                Text(error).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
            }
            HStack {
                Button("Сбросить к стандартной") { model.resetToDefault() }
                    .disabled(model.load != .ready)
                Spacer()
                if model.saving { ProgressView().controlSize(.small) }
                Button("Отмена") { model.onClose() }
                // ⌘S, а не Return: Return в редакторе — перевод строки.
                Button("Сохранить") { model.save { model.onClose() } }
                    .keyboardShortcut("s", modifiers: .command)
                    .buttonStyle(.borderedProminent)
                    .disabled(model.load != .ready || model.saving)
            }
        }
        .padding(20)
        .frame(minWidth: 520, idealWidth: 640, minHeight: 420, idealHeight: 560)
    }
}

@MainActor
enum InstructionsWindow {
    private static var controller: NSWindowController?
    private static var model: InstructionsModel?
    private static let guardDelegate = CloseGuard()

    static func show(onSaved: @escaping (Bool) -> Void) {
        if let controller, controller.window?.isVisible == true {
            model?.onSaved = onSaved
            NSApp.activate(ignoringOtherApps: true)
            controller.showWindow(nil)
            return
        }
        let model = InstructionsModel()
        model.onSaved = onSaved
        model.onClose = { controller?.close() }  // close() не спрашивает windowShouldClose
        self.model = model
        if controller == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 560),
                                  styleMask: [.titled, .closable, .resizable],
                                  backing: .buffered, defer: false)
            window.title = "Инструкция для фоллоу-апов"
            window.isReleasedWhenClosed = false
            window.delegate = guardDelegate
            window.center()
            controller = NSWindowController(window: window)
        }
        let hosting = NSHostingController(rootView: InstructionsView(model: model))
        hosting.sizingOptions = [.minSize]
        controller?.window?.contentViewController = hosting
        NSApp.activate(ignoringOtherApps: true)
        controller?.showWindow(nil)
        model.reload()
    }

    /// Крестик окна при несохранённых правках — спросить.
    fileprivate static func shouldClose() -> Bool {
        guard let model, model.dirty else { return true }
        let alert = NSAlert()
        alert.messageText = "Сохранить изменения инструкции?"
        alert.addButton(withTitle: "Сохранить")
        alert.addButton(withTitle: "Не сохранять")
        alert.addButton(withTitle: "Отмена")
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            model.save { controller?.close() }
            return false
        case .alertSecondButtonReturn:
            return true
        default:
            return false
        }
    }
}

private final class CloseGuard: NSObject, NSWindowDelegate {
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        MainActor.assumeIsolated { InstructionsWindow.shouldClose() }
    }
}
```

- [ ] **Step 2: сборка**

Run: `app/build.sh 2>&1 | grep -E "error|готово"`
Expected: `-- готово: …/BeeDott.app`, без `error`. Если `scrollContentBackground` или `sizingOptions` недоступны для `-target arm64-apple-macosx14.4` — компилятор скажет; оба есть с macOS 13.

- [ ] **Step 3: commit**

```bash
git add app/Sources/MeetMind/InstructionsWindow.swift
git commit -m "feat(app): окно редактора инструкции фоллоу-апа"
```

---

### Task 7: шаг «Саммари» — модель и кнопка инструкции

**Files:** Modify: `app/Sources/MeetMind/SetupWizard.swift`

- [ ] **Step 1: `SetupStatus`** — поля и разбор:

```swift
struct SetupStatus {
    var model: Bool
    var summary: String?
    var telegram: Bool
    var summaryModel: String?
    var summaryCustom: Bool

    init(_ r: SetupResult) {
        model = r.bool("model")
        summary = r.string("summary")
        telegram = r.bool("telegram")
        summaryModel = r.string("summary_model")
        summaryCustom = r.bool("summary_custom")
    }
}
```

- [ ] **Step 2: `SetupModel`** — в блок `// шаг 2` добавить:

```swift
    /// Нет status (чистый Mac, рантайма ещё нет) — дефолт: ANTHROPIC_MODEL и
    /// файла инструкции там тоже нет.
    @Published var summaryModel = RuntimeLogic.defaultSummaryModel
    @Published var summaryModelPhase: Phase = .idle
    @Published var instructionsCustom = false
```

в `init` после `summaryMode = status?.summary`:

```swift
        summaryModel = status?.summaryModel ?? RuntimeLogic.defaultSummaryModel
        instructionsCustom = status?.summaryCustom ?? false
```

в `isBusy` — добавить `summaryModelPhase` в массив. После `checkKey()`:

```swift
    func setSummaryModel(_ id: String) {
        guard id != summaryModel, summaryModelPhase != .running else { return }
        let previous = summaryModel
        summaryModel = id
        summaryModelPhase = .running
        queue.async { [runtime] in
            let result = runtime.runSetup("set-model", stdin: id)
            Task { @MainActor in
                if case .error(let code, let message) = result {
                    self.summaryModel = previous
                    self.summaryModelPhase = .failed(RuntimeLogic.setupErrorText(code: code, message: message))
                    return
                }
                self.summaryModelPhase = .done
                self.applySettings()
            }
        }
    }

    func openInstructions() {
        InstructionsWindow.show { custom in self.instructionsCustom = custom }
    }
```

- [ ] **Step 3: `SummaryStepView`** — сразу после закрывающей скобки `.background(...)` у строки «ключ сохранён» (внутри ветки `if let mode = model.summaryMode, !model.replacingKey {`) добавить:

```swift
                if mode == "api" { modelPicker }
                HStack(spacing: 8) {
                    Button("Инструкция для фоллоу-апов…") { model.openInstructions() }
                    Text(model.instructionsCustom ? "своя" : "стандартная")
                        .font(.system(size: 12))
                        .foregroundStyle(Theme.textTertiary(scheme))
                }
```

и в структуру `SummaryStepView` (рядом с `keyStatus`):

```swift
    private var modelPicker: some View {
        let known = RuntimeLogic.summaryModels.first { $0.id == model.summaryModel }
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Модель").font(.system(size: 13, weight: .semibold))
                Spacer()
                if model.summaryModelPhase == .running { ProgressView().controlSize(.small) }
                Picker("", selection: Binding(get: { model.summaryModel },
                                              set: { model.setSummaryModel($0) })) {
                    ForEach(RuntimeLogic.summaryModels, id: \.id) { Text($0.title).tag($0.id) }
                    if known == nil {
                        Text("\(model.summaryModel) (вручную)").tag(model.summaryModel)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .fixedSize()
                .disabled(model.summaryModelPhase == .running)
            }
            Text(known?.caption ?? "Модель задана вручную в config.env")
                .font(.system(size: 12))
                .foregroundStyle(Theme.textSecondary(scheme))
            if case .failed(let text) = model.summaryModelPhase {
                Text(text).font(.system(size: 12)).foregroundStyle(Theme.recordingRed(scheme))
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.rowFill(scheme)))
    }
```

- [ ] **Step 4: сборка и тесты**

Run: `app/build.sh 2>&1 | grep -E "error|готово" && app/Tests/run.sh && .venv/bin/python -m pytest -q`
Expected: сборка готова, Swift-тесты OK, pytest зелёный.

- [ ] **Step 5: commit**

```bash
git add app/Sources/MeetMind/SetupWizard.swift
git commit -m "feat(app): выбор модели и инструкция на шаге «Саммари»"
```

---

### Task 8: установка, ручная проверка, журнал

**Files:** Modify: `AUDIT.md`

- [ ] **Step 1: установка — только без записи и обработки.** `app/install.sh` сам откажется, если идёт запись/обработка; `FORCE=1` не использовать. Если занят — ждать в фоне (цикл по `sessions.state` в БД, как в прошлый раз), потом ставить.

- [ ] **Step 2: ручная проверка** (dev-режим автора): меню → «Настройка…» → шаг «Саммари»: список моделей показывает текущую (Opus 5.5); смена на Sonnet 5.5 → «Применится…»/перезапуск демона, в `~/Library/Application Support/MeetMind/config.env` строка `ANTHROPIC_MODEL=claude-sonnet-5-5`; вернуть Opus 5.5. «Инструкция для фоллоу-апов…» → текст стандартной; правка + «Сохранить» → метка «своя», файл `followup_instructions.md` есть; «Сбросить к стандартной» + «Сохранить» → «стандартная», файла нет; крестик с правками → вопрос «Сохранить изменения?». Модель и инструкцию после проверки оставить как были (Opus 5.5, стандартная).

- [ ] **Step 3: `AUDIT.md`** — в конец журнала:

```markdown
- 2026-10-14 — настройки саммари: в «Настройке…» → «Саммари» выбор модели
  (Opus 5.5 / Sonnet 5.5 / Fable 5.1, пишется в config.env, демон
  перезапускается) и редактор инструкции фоллоу-апа (файл
  followup_instructions.md в папке приложения, читается на каждом саммари;
  формат JSON добавляется кодом). Стандартная инструкция теперь идёт после
  транскрипта целиком; качество сверено на 3 прошлых встречах.
```

- [ ] **Step 4: финальная проверка, скан секретов, commit + push**

```bash
.venv/bin/python -m pytest -q && app/Tests/run.sh
git diff origin/master..HEAD | grep -cE "sk-ant-[A-Za-z0-9_-]{20,}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}"   # ожидается 0
git add AUDIT.md && git commit -m "docs(audit): настройки саммари в журнале"
git push origin master
```
