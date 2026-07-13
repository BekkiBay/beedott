# MeetMind v3 «Sentinel» — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Локальный ИИ-секретарь встреч: автодетект Zoom/Meet → локальная запись →
mlx-whisper → Ollama-суммаризация → Telegram, с crash-only надёжностью на SQLite.
Спека: [SPEC_v3.md](./SPEC_v3.md) — читать перед началом.

**Architecture:** Единый подписанный Swift .app (MenuBarExtra + Process Tap захват +
детект + супервизия) поверх Python-демона (пайплайн, SQLite WAL, sinks), связка через
unix-socket JSON-RPC. Milestones M0-M2 — чистый Python (работают и без Swift);
Swift появляется с M3. Постоянный fallback захвата: BlackHole + программный Aggregate Device.

**Tech Stack:** Python 3.11+ (uv, pytest, sounddevice, soundfile, mlx-whisper,
requests/Ollama, python-telegram-bot v22), SQLite (WAL, FTS5), Swift/SwiftUI
(MenuBarExtra, Core Audio Process Taps, NSWorkspace, SMAppService), PyInstaller.

**Правила выполнения:**
- Порядок milestones строгий: M0 → M1 → … Каждый milestone заканчивается ручной
  проверкой из секции «Verify» — не переходить дальше, пока она не пройдена.
- TDD для всей Python-логики (state machine, db, orchestrator, guards). Свисты
  (Swift, спайки) проверяются ручными acceptance-шагами.
- Коммит после каждой задачи. Формат: `feat(m1): db schema and state machine`.
- Все пути данных: `~/Library/Application Support/MeetMind/` (далее `$APPDIR`).

---

## M0 — Гигиена репозитория и спайки (3-5 дней)

Цель: убрать риски (секрет в репо, мёртвый код), поставить тест-инфраструктуру,
де-рискнуть ОБА пути захвата аудио до начала стройки.

### Task 0.1: Git и секреты

**Files:**
- Create: `.gitignore`
- Delete: `credentials.json` (живой Google OAuth secret — Google Calendar API в v3 не используется)

- [ ] **Step 1: git init и .gitignore**

```bash
cd ~/AIApp/meetmind && git init
```

`.gitignore`:
```gitignore
.venv/
__pycache__/
*.pyc
.env
credentials.json
token.json
sessions/
test_output/
*.wav
!tests/fixtures/*.wav
*.m4a
*.log
dist/
build/
app/build/
.pytest_cache/
```

- [ ] **Step 2: убрать секрет с диска**

```bash
mkdir -p ~/.meetmind/legacy && mv credentials.json token.json ~/.meetmind/legacy/ 2>/dev/null
chmod 700 ~/.meetmind/legacy
```

- [ ] **Step 3: первый коммит (фиксирует базовую линию v2)**

```bash
git add -A && git commit -m "chore: baseline v2 before v3 work, secrets excluded"
```

### Task 0.2: Удалить мёртвый код v2

**Files:**
- Delete: `run.py`, `example_server.py`, `daemon/telegram_bot.py`, `web/`,
  `meetmind-firefox/`, `daemon/calendar_client.py`, `daemon/enricher.py`,
  `daemon/scheduler.py`, `daemon/session_manager.py`, `daemon/websocket_server.py`,
  `FILES_CREATED.txt`, `QUICK_START.md` (дубль QUICKSTART.md)

- [ ] **Step 1: удалить файлы** (история остаётся в git)

```bash
git rm -r run.py example_server.py daemon/telegram_bot.py web/ meetmind-firefox/ \
  daemon/calendar_client.py daemon/enricher.py daemon/scheduler.py \
  daemon/session_manager.py daemon/websocket_server.py FILES_CREATED.txt QUICK_START.md
```

- [ ] **Step 2: проверить, что main.py больше не импортирует удалённое**

```bash
python3 -c "import ast,sys; tree=ast.parse(open('main.py').read()); print([n.module or n.names[0].name for n in ast.walk(tree) if isinstance(n,(ast.Import,ast.ImportFrom))])"
```
Если `main.py` импортирует scheduler/session_manager/websocket_server — временно закомментировать
эти импорты и вызовы (main.py целиком заменяется в M1, чинить всерьёз не нужно).

- [ ] **Step 3: Commit** — `chore(m0): drop dead v2 code (web ui, firefox ext, broken ws/scheduler)`

### Task 0.3: Тест-инфраструктура и аудио-фикстура

**Files:**
- Create: `pyproject.toml`, `tests/__init__.py`, `tests/conftest.py`,
  `tests/fixtures/sample_ru.wav`, `tests/test_smoke.py`

- [ ] **Step 1: uv-окружение и pyproject**

```toml
# pyproject.toml
[project]
name = "meetmind"
version = "3.0.0"
requires-python = ">=3.11"
dependencies = [
  "sounddevice>=0.4.6", "soundfile>=0.12", "numpy",
  "mlx-whisper>=0.4; platform_machine=='arm64'",
  "requests>=2.31", "python-telegram-bot>=22", "python-dotenv>=1.0", "psutil>=5.9",
]
[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio", "ruff"]
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-m 'not slow and not hardware'"
markers = [
  "slow: скачивание/инференс модели (запуск: pytest -m slow)",
  "hardware: нужно реальное аудио-устройство и TCC-разрешение (pytest -m hardware)",
]
```

```bash
uv sync
```

- [ ] **Step 2: фикстура — 30 сек русской речи из last_vstrechka.m4a**

```bash
mkdir -p tests/fixtures
ffmpeg -y -i last_vstrechka.m4a -t 30 -ar 16000 -ac 2 tests/fixtures/sample_ru.wav
```

- [ ] **Step 3: smoke-тест**

```python
# tests/test_smoke.py
from pathlib import Path

def test_fixture_exists():
    assert Path("tests/fixtures/sample_ru.wav").stat().st_size > 100_000
```

Run: `uv run pytest -q` → Expected: `1 passed`

- [ ] **Step 4: Commit** — `chore(m0): uv + pytest + ru audio fixture`

### Task 0.4: Спайк A — Core Audio Process Tap (timebox: 2-4 дня, НЕ больше)

**Files:**
- Create: `spikes/ProcessTapSpike/` (Swift package или мини-Xcode проект)
- Create: `spikes/SPIKE_RESULTS.md`

Предусловия: macOS ≥ 14.4. Подпись: **сначала пробовать бесплатный personal team**
(обычный Apple ID в Xcode даёт стабильный Team ID; Developer ID $99/год нужен прежде
всего для нотаризации/распространения, а не для самого TCC-промпта). В спайке проверить:
TCC-разрешение выдаётся? переживает пересборку? Результат — в SPIKE_RESULTS.md.
**Если ни personal team, ни Developer ID не дают стабильного TCC — спайк A зафиксировать
как fail и идти по пути B (Continuum-режим).**

- [ ] **Step 1: минимальный tap по образцу insidegui/AudioCap**

Swift-код: `CATapDescription(stereoMixdownOfProcesses: [pid])` →
`AudioHardwareCreateProcessTap` → `AudioHardwareCreateAggregateDevice` (private aggregate
с `kAudioSubTapUIDKey`) → IOProc пишет буферы в WAV (`AVAudioFile`).
Цель спайка — НЕ архитектура, а ответ «работает/нет» на этом Mac.

- [ ] **Step 2: acceptance-прогон**

Запустить Zoom-звонок (можно тестовый meeting сам с собой), сказать фразу, включить
музыку в браузере. Запустить спайк с PID zoom.us → 20 сек → открыть WAV.
Expected: слышен звук Zoom; музыка браузера НЕ слышна (изоляция по процессу).

- [ ] **Step 3: записать результат в `spikes/SPIKE_RESULTS.md`**

Формат: macOS-версия, что запросил TCC, подводные камни, вывод go/no-go, ссылки на код.

### Task 0.5: Спайк B — программный Aggregate Device (BlackHole, fallback-путь)

**Files:**
- Create: `spikes/aggregate_device_spike.py`

- [ ] **Step 1: установить BlackHole и написать спайк**

```bash
brew install blackhole-2ch
uv add pyobjc-framework-CoreAudio --group dev
```

Скрипт делает ОБЕ стороны программно, без Audio MIDI Setup:
1. **Input:** `AudioHardwareCreateAggregateDevice` (PyObjC CoreAudio HAL) — Aggregate
   Device «MeetMind In» = UID BlackHole + UID встроенного микрофона; `sounddevice`
   открывает его как input.
2. **Output:** тем же API создать Multi-Output Device «MeetMind Out» = BlackHole +
   текущие колонки/наушники (иначе системный звук не попадает в BlackHole ИЛИ
   пользователь не слышит собеседника); на время записи переключить
   `kAudioHardwarePropertyDefaultOutputDevice` на «MeetMind Out», по окончании —
   **восстановить прежний default output**.

Затем запись 10 сек в WAV.

- [ ] **Step 2: acceptance-прогон**

Проиграть звук в браузере + говорить в мик. Expected: в WAV слышны оба источника;
оба устройства созданы/удалены скриптом без GUI; default output восстановлен;
пользователь слышал звук во время записи.

- [ ] **Step 3: результат в `spikes/SPIKE_RESULTS.md`**, Commit — `spike(m0): process tap + programmatic aggregate device results`

### Task 0.6: Гейт-решение по захвату

- [ ] Заполнить таблицу в `spikes/SPIKE_RESULTS.md`: путь A ok? путь B ok? →
  - A ok → план по умолчанию (M3-M4 Swift), B = постоянный fallback.
  - A fail/нет Developer ID → **Continuum-режим**: M3-M4 заменяются задачами M3'/M4'
    (rumps-menubar остаётся, захват только путь B). Отметить выбор здесь в PLAN_v3.md.

**M0 Verify:** git-репо чистый (`git status`), `uv run pytest` зелёный, оба спайка
имеют письменный вердикт, секретов в репо нет (`git grep -l client_secret` пусто).

---

## M1 — Ручной happy path на новом состоянии (1-1.5 недели)

Цель: хоткей/CLI старт → стоп → транскрипт → Ollama-JSON → Telegram + заметка,
всё состояние в SQLite с первого дня. Пока без Swift: интерфейс — `python -m daemon.cli`.

### Task 1.1: Пути приложения и конфиг

**Files:**
- Create: `daemon/paths.py`
- Modify: `config.py` (пути, чистка WEB_*)
- Test: `tests/test_paths.py`

- [ ] **Step 1: тест**

```python
# tests/test_paths.py
from daemon.paths import app_dir, session_dir

def test_app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path / "mm"))
    d = app_dir()
    assert d.exists() and d.name == "mm"

def test_session_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path / "mm"))
    s = session_dir("abc123")
    assert s == app_dir() / "sessions" / "abc123" and s.exists()
```

- [ ] **Step 2: реализация**

```python
# daemon/paths.py
"""Все пути данных приложения. env MEETMIND_APPDIR — override для тестов."""
import os
from pathlib import Path

def app_dir() -> Path:
    d = Path(os.environ.get("MEETMIND_APPDIR",
        Path.home() / "Library" / "Application Support" / "MeetMind"))
    d.mkdir(parents=True, exist_ok=True)
    return d

def db_path() -> Path: return app_dir() / "meetmind.db"
def socket_path() -> Path: return app_dir() / "control.sock"

def session_dir(session_id: str) -> Path:
    d = app_dir() / "sessions" / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d
```

В `config.py`: удалить WEB_PORT/WEB_*; `.env` читать из `app_dir()/config.env`,
с fallback на `./.env` для разработки.

- [ ] **Step 3:** `uv run pytest tests/test_paths.py -q` → PASS. Commit — `feat(m1): app paths + config cleanup`

### Task 1.2: SQLite-слой и state machine (ядро, строго TDD)

**Files:**
- Create: `daemon/db.py`, `daemon/states.py`
- Test: `tests/test_db.py`, `tests/test_states.py`

- [ ] **Step 1: тесты state machine**

```python
# tests/test_states.py
import pytest
from daemon.states import State, assert_transition, TERMINAL, may_delete_files

def test_happy_chain():
    chain = [State.DETECTED, State.RECORDING, State.RECORDED, State.TRANSCRIBING,
             State.TRANSCRIBED, State.SUMMARIZING, State.SUMMARIZED,
             State.DELIVERING, State.DELIVERED]
    for a, b in zip(chain, chain[1:]):
        assert_transition(a, b)  # не бросает

def test_illegal_jump():
    with pytest.raises(ValueError):
        assert_transition(State.RECORDED, State.DELIVERED)

def test_cancel_from_anywhere_nonterminal():
    assert_transition(State.RECORDING, State.CANCELLED)
    with pytest.raises(ValueError):
        assert_transition(State.DELIVERED, State.CANCELLED)

def test_file_deletion_invariant():
    assert may_delete_files(State.DELIVERED)
    assert may_delete_files(State.CANCELLED)
    for s in State:
        if s not in TERMINAL:
            assert not may_delete_files(s)

def test_reprocess_from_failed_allowed():
    # /reprocess: откат из FAILED к состоянию перед упавшей стадией
    for target in (State.RECORDED, State.TRANSCRIBED, State.SUMMARIZED):
        assert_transition(State.FAILED, target)
    with pytest.raises(ValueError):
        assert_transition(State.FAILED, State.DELIVERED)  # напрямую в конец нельзя
```

- [ ] **Step 2: реализация states.py**

```python
# daemon/states.py
"""Конечный автомат сессии. Инвариант: файлы удаляются только в DELIVERED/CANCELLED."""
from enum import StrEnum

class State(StrEnum):
    DETECTED = "DETECTED"; RECORDING = "RECORDING"; RECORDED = "RECORDED"
    TRANSCRIBING = "TRANSCRIBING"; TRANSCRIBED = "TRANSCRIBED"
    SUMMARIZING = "SUMMARIZING"; SUMMARIZED = "SUMMARIZED"
    DELIVERING = "DELIVERING"; DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"; FAILED = "FAILED"

_CHAIN = [State.DETECTED, State.RECORDING, State.RECORDED, State.TRANSCRIBING,
          State.TRANSCRIBED, State.SUMMARIZING, State.SUMMARIZED,
          State.DELIVERING, State.DELIVERED]
ALLOWED: dict[State, set[State]] = {a: {b} for a, b in zip(_CHAIN, _CHAIN[1:])}
TERMINAL = {State.DELIVERED, State.CANCELLED, State.FAILED}
for s in State:
    if s not in TERMINAL:
        ALLOWED.setdefault(s, set()).update({State.CANCELLED, State.FAILED})
        ALLOWED[s].add(s)  # ретрай этапа = повторный вход в то же состояние

# /reprocess: единственный выход из FAILED — откат к состоянию перед
# самой ранней непройденной стадией (transcribe→RECORDED, summarize→TRANSCRIBED,
# deliver→SUMMARIZED). Джобы этой стадии сбрасываются в queued (см. db.reprocess).
ALLOWED[State.FAILED] = {State.RECORDED, State.TRANSCRIBED, State.SUMMARIZED}

def assert_transition(a: State, b: State) -> None:
    if b not in ALLOWED.get(a, set()):
        raise ValueError(f"illegal transition {a} -> {b}")

def may_delete_files(s: State) -> bool:
    return s in (State.DELIVERED, State.CANCELLED)
```

- [ ] **Step 3: тесты db.py**

```python
# tests/test_db.py
from daemon import db
from daemon.states import State

def test_schema_and_session_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual", title="тест")
    s = db.get_session(con, sid)
    assert s["state"] == State.DETECTED and s["title"] == "тест"
    db.set_state(con, sid, State.RECORDING)
    assert db.get_session(con, sid)["state"] == State.RECORDING

def test_illegal_transition_rejected(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    with pytest.raises(ValueError):
        db.set_state(con, sid, State.DELIVERED)

def test_jobs_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    db.enqueue_job(con, sid, "transcribe")
    job = db.next_job(con)
    assert job["stage"] == "transcribe" and job["status"] == "queued"
```

- [ ] **Step 4: реализация db.py**

```python
# daemon/db.py
"""SQLite-слой. WAL. Дисциплина: один writer на таблицу (см. SPEC_v3 §3).
Все датавремена — ISO8601 UTC строки."""
import sqlite3, uuid
from datetime import datetime, timezone
from daemon.paths import db_path
from daemon.states import State, assert_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, meeting_key TEXT, title TEXT,
  source TEXT NOT NULL, state TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, audio_path TEXT, gap_note TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pipeline_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  stage TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
  chain INTEGER NOT NULL DEFAULT 1,  -- 0 = standalone постпроцесс (diarize), sessions.state не трогает
  attempts INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT, last_error TEXT,
  updated_at TEXT NOT NULL, UNIQUE(session_id, stage));
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL, sink TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
  delivered_at TEXT, detail TEXT, UNIQUE(session_id, sink));
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  session_id UNINDEXED, title, transcript, summary);
"""

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path(), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    return con

def create_session(con, *, source: str, title: str | None = None,
                   meeting_key: str | None = None) -> str:
    sid = uuid.uuid4().hex[:12]
    con.execute(
        "INSERT INTO sessions(id, meeting_key, title, source, state, started_at,"
        " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (sid, meeting_key, title, source, State.DETECTED, now(), now(), now()))
    con.commit()
    return sid

def get_session(con, sid: str) -> sqlite3.Row:
    return con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()

def set_state(con, sid: str, new: State, **fields) -> None:
    cur = State(get_session(con, sid)["state"])
    assert_transition(cur, new)
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(
        f"UPDATE sessions SET state=?, updated_at=?{', ' + sets if sets else ''} WHERE id=?",
        (new, now(), *fields.values(), sid))
    con.commit()

def try_set_state(con, sid: str, new: State, **fields) -> bool:
    """Как set_state, но при недопустимом переходе возвращает False, не бросает.
    Используется оркестратором после завершения стадии: если сессию тем временем
    отменили (CANCELLED) — результат стадии тихо выбрасывается, демон не падает."""
    try:
        set_state(con, sid, new, **fields)
        return True
    except ValueError:
        return False

def enqueue_job(con, sid: str, stage: str, *, chain: int = 1) -> None:
    con.execute(
        "INSERT INTO pipeline_jobs(session_id, stage, chain, updated_at) VALUES(?,?,?,?)"
        " ON CONFLICT(session_id, stage) DO UPDATE SET status='queued',"
        " next_retry_at=NULL, updated_at=excluded.updated_at",
        (sid, stage, chain, now()))
    con.commit()

_REPROCESS_TARGET = {"transcribe": State.RECORDED, "summarize": State.TRANSCRIBED,
                     "deliver": State.SUMMARIZED}

def reprocess(con, sid: str) -> None:
    """Выход из FAILED: найти самую раннюю непройденную chain-стадию, откатить
    сессию к состоянию перед ней, сбросить её джобы в queued."""
    row = con.execute(
        "SELECT stage FROM pipeline_jobs WHERE session_id=? AND chain=1"
        " AND status != 'done' ORDER BY id LIMIT 1", (sid,)).fetchone()
    if row is None:
        return
    set_state(con, sid, _REPROCESS_TARGET[row["stage"]])
    con.execute(
        "UPDATE pipeline_jobs SET status='queued', attempts=0, next_retry_at=NULL,"
        " updated_at=? WHERE session_id=? AND status IN ('failed','failed_retry')",
        (now(), sid))
    con.commit()

def next_job(con) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM pipeline_jobs WHERE status IN ('queued','failed_retry')"
        " AND (next_retry_at IS NULL OR next_retry_at <= ?)"
        " ORDER BY id LIMIT 1", (now(),)).fetchone()
```

- [ ] **Step 5:** `uv run pytest tests/test_db.py tests/test_states.py -q` → PASS.
  Commit — `feat(m1): sqlite state store + session state machine (tdd)`

### Task 1.3: Потоковый Recorder (fallback-путь, адаптация v2)

**Files:**
- Modify: `daemon/recorder.py`
- Test: `tests/test_recorder.py`

Что меняется относительно v2: (1) писать чанки сразу на диск через `soundfile.SoundFile`
в callback, НЕ копить `audio_data` в памяти; (2) сохранять стерео (ch0=система, ch1=мик
при aggregate; не делать `.mean(axis=1)`) — нужно диаризации; (3) отдавать путь к
файлу сразу при старте.

- [ ] **Step 1: тест (запись с default-устройства, без BlackHole в CI)**

```python
# tests/test_recorder.py
import time
import pytest
from daemon.recorder import Recorder

@pytest.mark.hardware  # реальное аудио-устройство + TCC; не входит в дефолтный прогон
def test_streams_to_disk(tmp_path):
    wav = tmp_path / "a.wav"
    r = Recorder(out_path=wav)
    r.start()
    time.sleep(2)
    size_1s = wav.stat().st_size
    time.sleep(2)
    r.stop()
    assert wav.stat().st_size > size_1s > 44  # файл растёт ВО ВРЕМЯ записи
```

- [ ] **Step 2: реализация** — ядро callback:

**Realtime-safety (важно):** в аудио-callback НЕЛЬЗЯ делать дисковый I/O — задержка
диска (Spotlight, Time Machine) даст overrun и потерю сэмплов. Паттерн — очередь +
отдельный поток-писатель (как `rec_unlimited.py` из примеров python-sounddevice):

```python
# daemon/recorder.py (ключевой фрагмент)
import queue, threading
import sounddevice as sd, soundfile as sf

class Recorder:
    def __init__(self, out_path, device=None, samplerate=16000, channels=2):
        self.out_path, self.device = out_path, device
        self.samplerate, self.channels = samplerate, channels
        self._q: queue.Queue = queue.Queue()
        self._stream = self._writer = None
        self._stop = threading.Event()

    def _callback(self, indata, frames, t, status):
        self._q.put_nowait(indata.copy())        # только память, без I/O

    def _write_loop(self):
        with sf.SoundFile(self.out_path, "w", samplerate=self.samplerate,
                          channels=self.channels, subtype="PCM_16") as f:
            while not (self._stop.is_set() and self._q.empty()):
                try:
                    f.write(self._q.get(timeout=0.5))
                except queue.Empty:
                    continue

    def start(self):
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()
        self._stream = sd.InputStream(
            device=self.device, samplerate=self.samplerate,
            channels=self.channels, callback=self._callback)
        self._stream.start()

    def stop(self):
        self._stream.stop(); self._stream.close()
        self._stop.set(); self._writer.join(timeout=10)
```

Сохранить из v2 цепочку выбора устройства: Aggregate «MeetMind In» → BlackHole → default mic
(функция `pick_device()`), плюс лог о том, какой путь выбран.

- [ ] **Step 3:** pytest PASS (локально, не в headless CI). Commit — `feat(m1): streaming stereo recorder`

### Task 1.4: Transcribe stage

**Files:**
- Modify: `daemon/transcriber.py`
- Create: `daemon/stages.py` (обёртки стадий для оркестратора)
- Test: `tests/test_transcriber.py` (медленный, помечен `@pytest.mark.slow`)

- [ ] **Step 1: вшить анти-галлюцинационные дефолты** (перенос из `test_one_meeting.sh:89-101`):

```python
DEFAULT_KWARGS = dict(
    language="ru",
    temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),   # fallback-лестница
    compression_ratio_threshold=2.4,
    logprob_threshold=-1.0,
    no_speech_threshold=0.6,
    condition_on_previous_text=False,               # v2 было True — источник лупов
)
MODEL_REPO = "mlx-community/whisper-large-v3-turbo"
```

- [ ] **Step 2: дедуп последовательных одинаковых строк** (перенос python-сниппета из
  `test_one_meeting.sh:107-126` в функцию `dedupe_lines(text) -> text`), юнит-тест на неё:

```python
def test_dedupe():
    from daemon.transcriber import dedupe_lines
    assert dedupe_lines("а\nа\nа\nб") == "а\nа\nб"   # одно повторение оставляем
```

- [ ] **Step 3: чанкинг длинного аудио** — если WAV > 60 мин: резать ffmpeg-ом на куски
  по 30 мин с перекрытием 5 сек, транскрибировать последовательно, склеивать с
  корректировкой таймкодов. Юнит-тест на функцию склейки таймкодов (без ffmpeg).

- [ ] **Step 4: slow-тест на фикстуре**

```python
@pytest.mark.slow
def test_transcribe_fixture():
    from daemon.transcriber import Transcriber
    segs = Transcriber().transcribe("tests/fixtures/sample_ru.wav")
    assert len(segs) > 0 and any(s["text"].strip() for s in segs)
```

Run: `uv run pytest -m slow -q` → PASS. Commit — `feat(m1): transcriber with anti-hallucination defaults + chunking`

### Task 1.5: Summarize stage — Ollama, строгий JSON

**Files:**
- Modify: `daemon/summarizer.py`
- Create: `daemon/render.py`
- Test: `tests/test_summarizer.py`, `tests/test_render.py`

- [ ] **Step 1: схема и валидация (тест первым)**

```python
# tests/conftest.py — общая фикстура для test_summarizer и test_render
GOOD_FOLLOWUP = {"context": "x", "discussed": [], "decisions": ["решили"],
                 "action_items": [{"task": "сделать", "owner": "Bekzod", "deadline": None}],
                 "open_questions": []}
```

```python
# tests/test_summarizer.py
from daemon.summarizer import validate_followup
from tests.conftest import GOOD_FOLLOWUP as GOOD

def test_validate_ok():
    assert validate_followup(GOOD) == GOOD

def test_validate_bad():
    import pytest
    with pytest.raises(ValueError):
        validate_followup({"context": 1})
```

- [ ] **Step 2: реализация.** Ollama `POST /api/chat` c `"format": "json"`, системный
  промпт и правила — реюз из `daemon/summarizer.py` v2 (SYSTEM_PROMPT там уже хороший),
  но user-промпт просит СТРОГО JSON по схеме из SPEC_v3 §7. `validate_followup` —
  ручная проверка типов (без jsonschema-зависимости). **Память/контекст:** v2 ставил
  `num_ctx=32768` — для 8B Q4 это ещё ~4-4.5GB KV-cache сверх ~5GB весов. В v3:
  `num_ctx=16384`, map-reduce чанк ≤ 48k символов (~12k токенов), рекомендация
  `OLLAMA_KV_CACHE_TYPE=q8_0` в конфиге. Map-reduce: транскрипт больше чанка →
  резать на части, суммировать каждую в тот же JSON, финальный проход сливает.
  Невалидный JSON после 2 ретраев → `{"context": <сырой текст>, ...пустые списки}` +
  флаг `degraded=True`.

- [ ] **Step 3: рендер markdown (тест первым)**

```python
# tests/test_render.py
from daemon.render import render_followup
from tests.conftest import GOOD_FOLLOWUP as GOOD

def test_render_contains_sections():
    md = render_followup(GOOD, engine="vikhr-8b", title="Синк", duration_min=30)
    for anchor in ("**Контекст**", "**Договорённости**", "**Action items**", "vikhr-8b"):
        assert anchor in md
```

Формат рендера — как в v2 `USER_PROMPT_TEMPLATE` (Контекст / Что обсуждалось /
Договорённости / Action items `📅 дедлайн — что — кто` / Открытые вопросы) + подпись
движка + дисклеймер из `sinks/base.py:62-63`.

- [ ] **Step 4:** pytest PASS. Commit — `feat(m1): ollama json summarizer + markdown renderer`

### Task 1.6: Sinks — NoteSink (слияние markdown+obsidian) + Telegram reuse

**Files:**
- Create: `daemon/sinks/note.py`
- Modify: `daemon/sinks/telegram.py` (принимает готовый markdown-текст)
- Delete: `daemon/sinks/obsidian.py`, `daemon/sinks/markdown.py` (после слияния)
- Test: `tests/test_note_sink.py`

- [ ] **Step 1: единый Protocol.** v2-sinks имеют `send(followup, session)` — в v3
  контракт меняется (вход — готовый markdown), обновить `daemon/sinks/base.py`:

```python
# daemon/sinks/base.py (новый Protocol; SinkResult без изменений)
class BaseSink(Protocol):
    name: str
    def send(self, text: str, *, title: str, when: datetime,
             session_id: str) -> SinkResult: ...
```

Регистрация: `note` и `obsidian` — ДВА экземпляра одного класса NoteSink
(раздельные строки в `deliveries`, раздельные ретраи — SPEC §5):
`NoteSink(name="note", base_dir=~/MeetMind)` и
`NoteSink(name="obsidian", base_dir=<vault>/Meetings, frontmatter=True)`.

- [ ] **Step 2: тест NoteSink**

```python
# tests/test_note_sink.py
from datetime import datetime
from daemon.sinks.note import NoteSink

def test_note_sink_writes_md(tmp_path):
    sink = NoteSink(name="note", base_dir=tmp_path / "md")
    res = sink.send("# hi", title="встреча", when=datetime(2026, 7, 13, 14, 30),
                    session_id="abc")
    assert res.success and len(list((tmp_path / "md").rglob("*.md"))) == 1

def test_obsidian_variant_adds_frontmatter(tmp_path):
    sink = NoteSink(name="obsidian", base_dir=tmp_path / "v", frontmatter=True)
    sink.send("# hi", title="встреча", when=datetime(2026, 7, 13, 14, 30),
              session_id="abc")
    md = next((tmp_path / "v").rglob("*.md")).read_text()
    assert md.startswith("---")
```

- [ ] **Step 3: реализация** — перенос логики из v2 `markdown.py` (уже quality 5) +
  frontmatter из `obsidian.py`. TelegramSink: оставить `asyncio.run`-паттерн v2,
  привести сигнатуру к новому Protocol; идемпотентность обеспечивает deliver-стадия
  (Task 2.1) по `deliveries.status='delivered'`.

- [ ] **Step 3:** pytest PASS. Commit — `feat(m1): unified NoteSink, telegram sink reuse`

### Task 1.7: Orchestrator — poll-loop, стадии, memory-guard

**Files:**
- Create: `daemon/orchestrator.py`, `daemon/memguard.py`
- Test: `tests/test_orchestrator.py`

- [ ] **Step 1: тест с фейковыми стадиями**

```python
# tests/test_orchestrator.py
from daemon import db
from daemon.states import State
from daemon.orchestrator import Orchestrator

def test_pipeline_runs_all_stages(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    for st in (State.RECORDING, State.RECORDED):
        db.set_state(con, sid, st)
    db.enqueue_job(con, sid, "transcribe")
    calls = []
    orch = Orchestrator(con, stages={
        "transcribe": lambda s: calls.append("t"),
        "summarize":  lambda s: calls.append("s"),
        "deliver":    lambda s: calls.append("d")})
    orch.run_once_until_idle()
    assert calls == ["t", "s", "d"]
    assert db.get_session(con, sid)["state"] == State.DELIVERED

def test_stage_failure_sets_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual")
    for st in (State.RECORDING, State.RECORDED):
        db.set_state(con, sid, st)
    db.enqueue_job(con, sid, "transcribe")
    orch = Orchestrator(con, stages={"transcribe": lambda s: 1/0})
    orch.run_once_until_idle()
    job = con.execute("SELECT * FROM pipeline_jobs WHERE session_id=?", (sid,)).fetchone()
    assert job["status"] == "failed_retry" and job["attempts"] == 1
    assert job["next_retry_at"] is not None
```

- [ ] **Step 2: реализация.** Ключевые правила:
  - строго один job одновременно (24GB бюджет);
  - карта stage→(state_running, state_done, next_stage) — только для `chain=1`:
    `transcribe→(TRANSCRIBING, TRANSCRIBED, summarize)`,
    `summarize→(SUMMARIZING, SUMMARIZED, deliver)`,
    `deliver→(DELIVERING, DELIVERED, None)`;
  - **non-chain джобы** (`chain=0`, напр. diarize из M7): выполняются тем же циклом,
    но `sessions.state` НЕ трогают — только свой `pipeline_jobs.status`
    (queued→running→done/failed_retry). Сессия может быть уже DELIVERED — это норма;
  - **гонка с cancel:** переходы состояний после завершения стадии — через
    `db.try_set_state(...)`; вернул False (сессию отменили во время работы стадии) →
    результат стадии выбрасывается, файлы удаляет cancel-флоу, демон НЕ падает;
  - исключение стадии → `attempts+=1`, `next_retry_at = now + min(60*2**attempts, 3600)`,
    статус `failed_retry`; после 8 попыток — `failed` + сессия `FAILED` + уведомление;
  - `run_forever()`: poll каждые 3 сек; `run_once_until_idle()` — для тестов;
  - перед transcribe/summarize — `memguard.require(stage)`:

```python
# daemon/memguard.py
"""Guard: не грузить тяжёлую модель, если доступной памяти мало (иначе swap/OOM)."""
import psutil

# summarize: ~5GB весов Vikhr-8B Q4 + KV-cache при num_ctx=16384 + запас.
# Цифры — стартовые: в M2 Verify замерить реальный пик RSS ollama и скорректировать.
REQUIRED = {"transcribe": 4 * 2**30, "summarize": 10 * 2**30, "diarize": 2 * 2**30}

def available() -> int: return psutil.virtual_memory().available

class NotEnoughMemory(RuntimeError): ...

def require(stage: str) -> None:
    need = REQUIRED.get(stage, 0)
    if available() < need:
        raise NotEnoughMemory(f"{stage}: need {need>>30}GB, have {available()>>30}GB")
```
  `NotEnoughMemory` обрабатывается как обычный failed_retry (backoff подождёт, пока память освободится).

- [ ] **Step 3:** pytest PASS. Commit — `feat(m1): orchestrator poll-loop + memory guard (tdd)`

### Task 1.8: CLI и связка (замена main.py)

**Files:**
- Rewrite: `main.py` (запуск демона: orchestrator + control-интерфейс)
- Create: `daemon/cli.py`
- Test: ручная e2e

- [ ] **Step 1: CLI-команды** (argparse): `record start [--title]`, `record stop`,
  `record cancel`, `status`, `reprocess <sid>`, `daemon` (foreground run).
  В M1 команды работают напрямую с БД и Recorder внутри одного процесса `daemon`;
  разделение на socket-клиент/сервер придёт в M3 (Task 3.2).

- [ ] **Step 2: e2e руками (Verify M1):**

```bash
export APPDIR="$HOME/Library/Application Support/MeetMind"
uv run python -m daemon.cli daemon          # терминал 1
uv run python -m daemon.cli record start --title "тест v3"   # терминал 2
# сказать пару фраз по-русски (можно включить видео на русском)
uv run python -m daemon.cli record stop
```
Expected: ≤3 мин спустя — сообщение в Telegram со структурированным фоллоу-апом;
`sqlite3 "$APPDIR/meetmind.db" 'SELECT state FROM sessions'` → `DELIVERED`;
заметка в `~/MeetMind/<дата>/`.

- [ ] **Step 3: Commit** — `feat(m1): cli + daemon entrypoint, manual happy path works`

---

## M2 — Crash-only надёжность (1-1.5 недели)

### Task 2.1: Ретраи, идемпотентность доставки

**Files:**
- Modify: `daemon/orchestrator.py` (deliver-стадия пишет в `deliveries`)
- Test: `tests/test_delivery.py`

- [ ] Тест: sink падает 2 раза → доставляется с 3-й; `delivered_at` ставится только
  после success; повторный deliver той же сессии НЕ шлёт второе сообщение (проверка
  `deliveries.status`).
- [ ] Реализация: deliver-стадия итерирует sinks; Telegram обязателен (без него сессия
  не DELIVERED), note/notion — best-effort (лог + `failed`, не блокируют).
- [ ] Commit — `feat(m2): idempotent delivery with per-sink tracking`

### Task 2.2: Cancel-флоу

**Files:**
- Modify: `daemon/cli.py`, `daemon/orchestrator.py`
- Test: `tests/test_cancel.py`

- [ ] Тест 1: cancel во время RECORDING → состояние CANCELLED, `session_dir` пуст
  (файлы удалены), джобы удалены/skipped, sinks не вызваны.
- [ ] Тест 2 (`test_cancel_during_active_stage_no_crash`): cancel сессии, у которой
  стадия УЖЕ выполняется (TRANSCRIBING/SUMMARIZING) → сессия CANCELLED сразу;
  оркестратор по завершении стадии получает False от `try_set_state`, выбрасывает
  результат, НЕ падает; файлы удалены, sinks не вызваны.
- [ ] Реализация: `cancel` разрешён из любого нетерминального состояния; останавливает
  Recorder (если пишет), пишет CANCELLED, удаляет файлы (это единственное место кроме
  retention, где `may_delete_files` возвращает True). Для running-джобы файлы удаляются
  после того, как оркестратор подтвердил отказ от результата (флаг/поллинг статуса).
- [ ] Commit — `feat(m2): cancel discards audio and never delivers, race-safe`

### Task 2.3: Предохранители записи

**Files:**
- Modify: `daemon/recorder.py`
- Test: `tests/test_guards.py`

- [ ] Хард-лимит: `MAX_RECORDING_SEC = int(os.environ.get("MEETMIND_MAX_RECORDING_SEC", 6*3600))`
  (env-override нужен для проверки в M2 Verify) — авто-стоп + уведомление
  (notifier.py, с исправленным экранированием: строки в AppleScript через
  `s.replace('\\', '\\\\').replace('"', '\\"')` — юнит-тест на кавычки/эмодзи в title).
- [ ] Disk-guard: перед стартом `shutil.disk_usage(...).free < 2GB` → предупреждение;
  во время записи (в callback раз в ~30 сек) < 500MB → аккуратный стоп с сохранением.
- [ ] Silence-эвристика: RMS ниже порога 30 мин подряд → уведомление с предложением
  остановиться (сам не останавливает). Тест на RMS-детектор с синтетическим массивом.
- [ ] Commit — `feat(m2): duration/disk/silence guards + applescript escaping fix`

### Task 2.4: Retention

**Files:**
- Create: `daemon/retention.py`
- Test: `tests/test_retention.py`

- [ ] Тест: сессия DELIVERED старше N дней → wav удалён, транскрипт/заметка остаются;
  сессия FAILED любого возраста → НЕ тронута.

```python
# daemon/retention.py (ядро)
def sweep(con, keep_days: int = 14) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    rows = con.execute(
        "SELECT id, audio_path FROM sessions WHERE state='DELIVERED'"
        " AND ended_at < ? AND audio_path IS NOT NULL", (cutoff,)).fetchall()
    ...
```
- [ ] Commit — `feat(m2): retention sweep (delivered-only invariant)`

### Task 2.5: Ollama supervision

**Files:**
- Create: `daemon/ollama_client.py`
- Modify: `daemon/summarizer.py` (перенести `_ping_ollama`/`_summarize_ollama` в клиент;
  summarizer использует клиент — иначе health-check не относится к боевому вызову)
- Test: `tests/test_ollama_client.py` (мок HTTP)

- [ ] health-check `GET /api/tags`; если connection refused → `subprocess.Popen(["ollama", "serve"])`
  + ожидание ≤20 сек; не поднялась → исключение → штатный failed_retry.
  Модель не скачана (404 по имени) → ошибка с текстом `ollama pull <model>` в уведомлении.
- [ ] **Быстрое уведомление (не ждать 8 ретраев):** если autostart не поднял Ollama за
  ≤20 сек — СРАЗУ notifier + Telegram «Ollama не запущена, встреча X в очереди»,
  один раз на сессию, независимо от backoff-счётчика (SPEC §10 требует немедленный сигнал).
- [ ] Commit — `feat(m2): ollama health-check, autostart, fast-path notification`

### Task 2.6: Startup reconciliation + уведомление о долгой обработке

**Files:**
- Create: `daemon/reconcile.py`
- Modify: `main.py` (вызов при старте), `daemon/orchestrator.py` (таймер обработки)
- Test: `tests/test_reconcile.py`

- [ ] Тест reconciliation: в БД сессия в RECORDING, демон стартует «после ребута»
  (рекордер не жив): WAV на диске непустой → сессия RECORDED + `gap_note` +
  транскрайб-джоба в очереди; WAV нет/пустой → CANCELLED. Сессия с running-джобой
  без живого процесса → джоба обратно в queued.
- [ ] Тест таймера: время в TRANSCRIBING+SUMMARIZING > 15 мин (мок часов) → одно
  промежуточное уведомление «обработка длинной встречи продолжается» (SPEC §9/§10).
- [ ] Commit — `feat(m2): startup reconciliation + long-processing notification`

### M2 Verify (ручной чеклист, все пункты обязательны)

- [ ] `kill -9` демона посреди TRANSCRIBING → рестарт → фоллоу-ап доехал, дублей в TG нет.
- [ ] `pkill ollama` перед summarize → БЫСТРОЕ уведомление (≤1 мин), job в failed_retry,
  аудио на месте → `ollama serve` → доехал сам.
- [ ] Выключить Wi-Fi перед deliver → заметка на диске появилась, TG в failed_retry → включить → дослано.
- [ ] Cancel во время записи → файлов нет, TG молчит. Cancel во время транскрипции →
  то же + демон жив.
- [ ] `record start` и не останавливать → авто-стоп (проверять с `MEETMIND_MAX_RECORDING_SEC=60`).
- [ ] Диск: старт при <2GB свободного → предупреждение; симулировать <500MB во время
  записи (уменьшить порог env-переменной) → аккуратный стоп, записанное обработано.
- [ ] Reprocess: довести сессию до FAILED (выключить Ollama на все ретраи с укороченным
  backoff) → `cli reprocess <sid>` → доехала до DELIVERED.
- [ ] Замерить пик RSS `ollama` при summarize реальной встречи → скорректировать
  `memguard.REQUIRED` при расхождении.

Commit — `test(m2): crash-only checklist passed`

---

## M3 — Swift MenuBar Shell + ControlSocket (1.5-2 недели)

> Гейт: если в Task 0.6 выбран Continuum-режим — вместо M3/M4 выполняются M3'/M4'
> (в конце файла), Swift-задачи пропускаются.

### Task 3.1: ControlSocket-сервер в Python

**Files:**
- Create: `daemon/control.py`
- Modify: `main.py` (поднимает socket-сервер рядом с оркестратором),
  `daemon/cli.py` (команды становятся socket-клиентом)
- Test: `tests/test_control.py`

- [ ] Тест: asyncio unix-сервер на `paths.socket_path()`; клиент шлёт
  `{"id":1,"method":"status"}` → получает `{"id":1,"result":{...}}`; неизвестный метод →
  `{"error":{"code":"unknown_method"}}`. Методы — по таблице SPEC_v3 §6, маршрутизируются
  в те же функции, что CLI в M1.
- [ ] Heartbeat: демон раз в 5 сек пишет notification-кадр `{"method":"heartbeat"}`
  всем подключённым клиентам.
- [ ] Commit — `feat(m3): control socket json-rpc server, cli becomes client`

### Task 3.2: Xcode-проект с фиксированной идентичностью

**Files:**
- Create: `app/MeetMind.xcodeproj`, `app/MeetMind/MeetMindApp.swift`

- [ ] Bundle ID `com.<owner>.meetmind` и Team ID зафиксировать в первом же коммите;
  Signing: Developer ID Application; capability: Audio Input. Минимальный
  MenuBarExtra («🧠» иконка, пункт Quit) собирается и подписан.
- [ ] Проверка: `codesign -dv app/build/MeetMind.app` показывает стабильный TeamIdentifier.
- [ ] Commit — `feat(m3): signed menubar skeleton with pinned bundle/team id`

### Task 3.3: UI + hotkey + socket-клиент

**Files:**
- Create: `app/MeetMind/ControlClient.swift`, `app/MeetMind/MenuView.swift`,
  `app/MeetMind/HotKey.swift`

- [ ] ControlClient: `NWConnection` (unix socket) + line-delimited JSON; методы
  start/stop/cancel/status; подписка на heartbeat/state_changed.
- [ ] Menu: иконка idle `🧠` / recording `🔴` (по status/state_changed);
  пункты Start Recording / Stop / Cancel / Open Notes Folder / Status / Quit.
- [ ] Глобальный хоткей ⌘⇧R (Carbon `RegisterEventHotKey` или `NSEvent.addGlobalMonitor` —
  toggle start/stop; ⌘⇧X — cancel). pynput/tray.py после этого удаляются.
- [ ] Acceptance: старт из меню → REC-иконка в течение 1 сек; e2e из M1 работает из меню.
- [ ] Commit — `feat(m3): menubar ui + global hotkeys over control socket`

### Task 3.4: Supervisor — Swift владеет жизнью демона

**Files:**
- Create: `app/MeetMind/DaemonSupervisor.swift`

- [ ] Swift запускает демон как child `Process` (dev-режим: `uv run python main.py`;
  путь к бинарю — из UserDefaults, чтобы M6 переключил на frozen). 3 пропущенных
  heartbeat (15 сек) → SIGTERM, 5 сек, SIGKILL → рестарт с backoff 1/2/4/8…60 сек.
  Иконка меню «⚠️ daemon down» пока не восстановился.
- [ ] Acceptance: `kill -9 <python pid>` → демон перезапущен ≤20 сек, очередь продолжилась.
- [ ] Commit — `feat(m3): heartbeat supervision with auto-restart`

**M3 Verify:** полный e2e (запись → TG) без терминала, только menubar; убийство демона
самовосстанавливается; REC-индикатор honest (горит ⇔ пишем).

---

## M4 — Нативный захват аудио (1-2 недели)

### Task 4.1: CaptureEngine (Process Tap) в Swift

**Files:**
- Create: `app/MeetMind/CaptureEngine.swift` (перенос кода из спайка 0.4)

- [ ] По `start_recording` c meeting-контекстом Swift сам пишет WAV:
  Process Tap (PID звонилки) → ch0; микрофон (AVAudioEngine) → ch1; единый стерео WAV
  16kHz в `session_dir`. Путь передаётся демону в `start_recording.params.audio_path_owner="app"`
  — Python-Recorder в этом режиме не стартует.
- [ ] TCC: первый запуск запрашивает Microphone + System Audio Recording; онбординг-текст — в M6.
- [ ] Acceptance: реальный Zoom-звонок → в WAV оба направления разговора, музыка из
  другого приложения отсутствует.

### Task 4.2: Выбор пути захвата + вечный fallback

**Files:**
- Modify: `app/MeetMind/CaptureEngine.swift`, `daemon/recorder.py`, `daemon/control.py`

- [ ] Логика выбора: macOS ≥ 14.4 и tap создался → нативный путь; иначе ответ демону
  `capture=python` → Python Recorder (BlackHole/aggregate из Task 0.5 интегрируется:
  `daemon/audio_device.py` создаёт aggregate программно при старте, удаляет при выходе).
  Выбранный путь пишется в лог и в `sessions.gap_note` при деградации.
- [ ] Acceptance: принудительно выключить tap (feature-флаг) → пайплайн работает через BlackHole.
- [ ] Commit — `feat(m4): native process tap capture with permanent blackhole fallback`

### Task 4.3: Устойчивость к смене устройств

- [ ] Тест руками: начать запись → подключить AirPods посреди звонка → запись НЕ рвётся
  (tap не привязан к output-устройству); зафиксировать поведение в `docs/verify-m4.md`.
  На fallback-пути задокументировать известное ограничение.

---

## M5 — Автодетект встреч (1-1.5 недели)

### Task 5.1: DetectionEngine (Swift) — confirmed signals

**Files:**
- Create: `app/MeetMind/DetectionEngine.swift`

- [ ] Сигнал A: `NSWorkspace.didLaunchApplicationNotification` + поллинг
  `runningApplications` — `us.zoom.xos`, браузеры (Chrome/Safari/Arc).
- [ ] Сигнал B: занятость входного аудио-устройства —
  `kAudioDevicePropertyDeviceIsRunningSomewhere` (CoreAudio listener). Известно, что на
  части конфигураций может врать → он ПОДТВЕРЖДАЮЩИЙ, не главный; при недоступности —
  таймер-эвристика (Zoom on top >60 сек).
- [ ] Сигнал C (meeting_id): Accessibility — заголовок фронтового окна:
  Zoom: «Zoom Meeting» окно; Meet: тайтл вкладки `meet.google.com/xxx-yyyy-zzz`.
- [ ] Правило старта: A && B → start (meeting_key из C, если есть, иначе `app:ts`).
  Правило конца: пропажа A (процесс/окно) с debounce 60 сек ИЛИ смена meeting_key
  (мгновенно закрыть старую, открыть новую).

### Task 5.2: Back-to-back и сон

- [ ] Тест руками: две подряд тестовые Meet-комнаты без паузы → две сессии, два фоллоу-апа.
- [ ] `NSWorkspace.willSleepNotification` → флаг; `didWake` → звонок жив? продолжить
  (+`gap_note`) : закрыть по последней метке и отправить в обработку.
- [ ] Commit — `feat(m5): confirmed-signal detection, meeting_key sessions, sleep handling`

**M5 Verify (полевые, минимум 3 дня реального использования):**
- [ ] Реальный Zoom-звонок и реальный Meet-звонок задетектились и доставились без рук.
- [ ] Zoom, висящий в фоне без звонка, запись НЕ стартовал; ложных срабатываний в логе = 0.
- [ ] Две встречи подряд без паузы → две отдельные сессии и два фоллоу-апа.
- [ ] Усыпить Mac во время активного звонка → разбудить → запись продолжилась, в заметке gap_note.
- [ ] Усыпить Mac, завершив звонок во сне → сессия закрыта по последней метке и обработана.

---

## M6 — Онбординг и упаковка (1-2 недели)

### Task 6.1: Заморозка демона

- [ ] PyInstaller onedir → `MeetMind.app/Contents/Resources/daemon/`; mlx-whisper модели
  качаются в `$APPDIR/models` при онбординге (не в бандл). Supervisor переключается на
  frozen-путь.
- [ ] **Известные грабли MLX (делать ПЕРВЫМ шагом M6, не в конце):** собирать с
  `--collect-all mlx --collect-all mlx_whisper` (иначе `.metallib` не попадёт в бандл и
  Metal-инференс молча не заработает); `mlx-whisper` декларирует зависимость `torch`
  (модуль `torch_whisper.py`) — проверить граф импортов и исключить
  `--exclude-module torch`, если боевой путь `mlx_whisper.transcribe` его не тянет.
  Сразу после первой сборки прогнать РЕАЛЬНУЮ транскрипцию фикстуры на frozen-бинаре.
- [ ] Acceptance: приложение работает на Mac без установленного Python/uv.

### Task 6.2: Автозапуск

- [ ] `SMAppService.mainApp.register()` + галка «Launch at login» в меню.
  Acceptance: ребут → иконка на месте, демон жив.

### Task 6.3: Onboarding wizard (SwiftUI, поверх логики setup_mac.sh)

- [ ] Шаги: (1) welcome + consent-предупреждение о законности записи;
  (2) разрешения по очереди с объяснением: Microphone → System Audio Recording →
  Accessibility → Notifications; (3) Homebrew/Ollama установка + `ollama pull
  hf.co/Vikhrmodels/Vikhr-YandexGPT-5-Lite-8B-it_GGUF` с прогрессом (или fallback-модель
  из конфига); (4) Telegram: инструкция BotFather, поле token/chat_id, тест-пинг;
  (5) опционально Obsidian vault picker; (6) **если выбран fallback-захват** (нет
  Process Tap): установка BlackHole + программное создание Aggregate/Multi-Output
  устройств из Task 0.5 — без единого захода в Audio MIDI Setup; (7) финал —
  тестовая 15-сек запись → мини-фоллоу-ап. Конфиг пишется в `$APPDIR/config.env`
  с `chmod 600` (SPEC D14) + тест на права файла.
- [ ] Acceptance (M6 Verify): чистый macOS-аккаунт → до рабочего состояния ≤15 мин,
  ни одной команды в терминале.

---

## M7 — Phase 2: диаризация, календарь, поиск, Claude (2-3 недели, по желанию)

### Task 7.1: Диаризация (неблокирующая)

- [ ] `daemon/stages_diarize.py`: senko (первый кандидат — скорость) или
  pyannote-segmentation-MLX + WeSpeaker; вход — стерео WAV (ch1=мик → «Я»),
  выход — сегменты Speaker 1/2. Джоба ставится как **non-chain**
  (`db.enqueue_job(con, sid, "diarize", chain=0)`) после доставки: выполняется общим
  циклом оркестратора, но `sessions.state` НЕ трогает (сессия остаётся DELIVERED —
  поддержка non-chain джобов заложена в Task 1.7). Обновляет заметку на диске и
  notes_fts; в TG НЕ дошлёт. Memory-guard стадии. Сбой → status `skipped`.
- [ ] Тест: diarize-джоба на DELIVERED-сессии выполняется без исключений и без
  изменения `sessions.state`.

### Task 7.2: EventKit-контекст

- [ ] `app/MeetMind/CalendarContext.swift`: EKEventStore, событие ±15 мин от старта →
  title/attendees в `sessions`. Разрешение Calendar — в онбординг. Пайплайн без
  события работает как раньше (title = «Встреча HH:MM»).

### Task 7.3: FTS5 + Telegram-команды

- [ ] После summarize писать в `notes_fts`. Telegram-бот (расширение sink до
  polling-бота): `/status`, `/search <слово>` (top-5 сниппетов), `/reprocess <id>`,
  `/claude on|off` (глобальный toggle бустера). Команды идут через ControlSocket.

### Task 7.4: Claude-бустер (opt-in)

- [ ] `daemon/claude_booster.py`: вызов `claude --print --model sonnet
  --append-system-prompt <SYSTEM_PROMPT> --output-format text` (паттерн из
  `test_one_meeting.sh:184-189`), вход — транскрипт + Ollama-JSON, выход — JSON той же
  схемы; `validate_followup` обязателен; любой сбой/невалид → остаётся Ollama-версия,
  в заметке пометка движка. Self-check версии CLI при старте демона.
  Включается: глобально в config, per-meeting через `/claude on` до конца встречи.
- [ ] Commit-серия по задаче.

---

## M3'/M4' — Continuum-режим (только если гейт 0.6 отверг Swift)

- M3': оставить `tray.py` (rumps): фикс — работа через ControlSocket-клиент вместо
  приватных методов; явная проверка Accessibility-разрешения с подсказкой; cancel-пункт.
  Упаковка py2app со стабильным Bundle ID. Supervisor-роль играет launchd
  (`KeepAlive=true` на демон-процесс).
- M4': захват навсегда BlackHole + программный aggregate (`daemon/audio_device.py`);
  детект — Python: `process_watcher.py` (reuse) + CoreAudio busy-listener через PyObjC +
  AppleScript/Accessibility для тайтла окна (meeting_id). Остальные milestones без изменений.

---

## Сроки (part-time, соло)

| Milestone | Оценка | Кумулятив |
|---|---|---|
| M0 гигиена+спайки | 0.5-1 нед | 1 |
| M1 ручной happy path | 1-1.5 нед | 2.5 |
| M2 надёжность | 1-1.5 нед | 4 |
| M3 Swift shell | 1.5-2 нед | 6 |
| M4 нативный захват | 1-2 нед | 8 |
| M5 автодетект | 1-1.5 нед | 9.5 |
| M6 онбординг+упаковка | 1-2 нед | ~11 |
| M7 Phase 2 | 2-3 нед | ~14 (опц.) |

Продукт полезен уже после M2 (ручная запись хоткеем из CLI), ежедневно полезен после M5.

## Definition of Done (v3 core = M0-M6)

- Все must-пункты SPEC_v3 §2 закрыты и проверены чеклистами Verify.
- `uv run pytest` зелёный; slow-тесты зелёные локально.
- Метрики §13 собираются из SQLite одним запросом (`scripts/metrics.sql`).
- Неделя реального использования: ≥95% встреч доставлены без рук.
