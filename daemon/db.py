"""SQLite-слой. WAL. Дисциплина: один writer на таблицу (см. SPEC_v3 §3).
Все датавремена — ISO8601 UTC строки."""
import sqlite3
import uuid
from datetime import datetime, timezone

from daemon.paths import db_path, session_dir
from daemon.states import State, assert_transition
from daemon.timeutil import to_local

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, meeting_key TEXT, title TEXT,
  source TEXT NOT NULL, state TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, audio_path TEXT, gap_note TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  audio_owner TEXT,  -- кто пишет audio.wav: app (Swift) | daemon (Recorder)
  keep_audio INTEGER NOT NULL DEFAULT 0,  -- 1 = retention аудио не удаляет
  attendees TEXT);  -- JSON-список участников из календаря
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


def session_title(row) -> str:
    """Отображаемое название встречи: своё или «Встреча ЧЧ:ММ» по местному
    времени начала. Одно имя для Telegram, заметки, поиска и Claude (MCP).
    started_at не парсится (битая/старая запись) — «??:??» вместо падения."""
    if row["title"]:
        return row["title"]
    try:
        hhmm = to_local(row["started_at"]).strftime("%H:%M") if row["started_at"] else "??:??"
    except (ValueError, TypeError):
        hhmm = "??:??"
    return f"Встреча {hhmm}"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path(), timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


# Колонки sessions, появившиеся после первой версии схемы: имя → объявление.
# audio_owner — аудит T3 (кто пишет WAV, переживает рестарт демона);
# keep_audio — продукт P10; attendees — продукт P8.
_SESSION_COLUMNS_ADDED = {
    "audio_owner": "TEXT",
    "keep_audio": "INTEGER NOT NULL DEFAULT 0",
    "attendees": "TEXT",
}


def _migrate(con) -> None:
    """Догнать схему старых БД (CREATE TABLE IF NOT EXISTS новых колонок не
    добавляет)."""
    existing = _session_columns(con)
    for name, decl in _SESSION_COLUMNS_ADDED.items():
        if name in existing:
            continue
        try:
            con.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")
            con.commit()
        except sqlite3.OperationalError as exc:
            # другой процесс (демон / CLI) успел добавить колонку между проверкой и ALTER
            if "duplicate column" not in str(exc):
                raise


def _session_columns(con) -> set[str]:
    return {r[1] for r in con.execute("PRAGMA table_info(sessions)")}


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
    row = get_session(con, sid)
    if row is None:
        # сессию удалили (продукт P14) — для оркестратора это «недопустимый
        # переход»: try_set_state вернёт False, джоба уйдёт в skipped
        raise ValueError(f"нет сессии {sid}")
    cur = State(row["state"])
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
    сессию к состоянию перед ней, сбросить её джобы в queued.

    Если сессия ещё в in-progress состоянии стадии (например TRANSCRIBING при
    failed_retry — оркестратор ретраит сам), откат состояния невозможен и не
    нужен: try_set_state тихо пропустит его, а сброс джоба в queued снимет
    next_retry_at — ретрай пойдёт немедленно (кнопка «Повторить», экран 06)."""
    row = con.execute(
        "SELECT stage FROM pipeline_jobs WHERE session_id=? AND chain=1"
        " AND status != 'done' ORDER BY id LIMIT 1", (sid,)).fetchone()
    if row is None:
        return
    try_set_state(con, sid, _REPROCESS_TARGET[row["stage"]])
    con.execute(
        "UPDATE pipeline_jobs SET status='queued', attempts=0, next_retry_at=NULL,"
        " updated_at=? WHERE session_id=? AND status IN ('failed','failed_retry')",
        (now(), sid))
    con.commit()


def resummarize(con, sid: str) -> None:
    """«Пересобрать саммари» (экран 06): из DELIVERED/FAILED назад в TRANSCRIBED,
    summarize+deliver в queued, доставки сброшены в pending (идемпотентность
    deliver иначе пропустит уже отправленные sinks). Транскрипция не трогается.

    Если сессия ещё SUMMARIZING/DELIVERING (failed_retry) — откат состояния
    невозможен и не нужен (try_set_state пропустит), requeue джобов достаточно."""
    try_set_state(con, sid, State.TRANSCRIBED)
    con.execute(
        "UPDATE pipeline_jobs SET status='queued', attempts=0, next_retry_at=NULL,"
        " updated_at=? WHERE session_id=? AND stage IN ('summarize','deliver')",
        (now(), sid))
    con.execute(
        "UPDATE deliveries SET status='pending', delivered_at=NULL"
        " WHERE session_id=?", (sid,))
    con.commit()


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


# ─── notes_fts: полнотекстовый поиск по встречам (экран 05) ──────────────────
# Writer-дисциплина (SPEC_v3 §3): notes_fts пишет только стадия summarize
# (index_note) — один writer, как у остальных таблиц.


def index_note(con, sid: str, *, title: str, transcript: str, summary: str) -> None:
    """Переиндексировать сессию (delete+insert — реген не плодит дубликатов)."""
    con.execute("DELETE FROM notes_fts WHERE session_id=?", (sid,))
    con.execute(
        "INSERT INTO notes_fts(session_id, title, transcript, summary)"
        " VALUES(?,?,?,?)", (sid, title or "", transcript, summary))
    con.commit()


def backfill_fts(con) -> int:
    """Доиндексировать сессии, обработанные до появления FTS (файлы транскрипта
    на диске есть, строки в notes_fts нет). Вызывается при старте демона,
    идемпотентен. Возвращает число доиндексированных сессий."""
    rows = con.execute(
        "SELECT id, title FROM sessions WHERE state = ? AND id NOT IN"
        " (SELECT session_id FROM notes_fts)", (State.DELIVERED,)).fetchall()
    n = 0
    for row in rows:
        sdir = session_dir(row["id"])
        tpath = sdir / "transcript.txt"
        if not tpath.exists():
            continue
        mpath = sdir / "followup.md"
        summary = mpath.read_text(encoding="utf-8") if mpath.exists() else ""
        index_note(con, row["id"], title=row["title"] or "",
                   transcript=tpath.read_text(encoding="utf-8"), summary=summary)
        n += 1
    return n


def _fts_match(query: str) -> str | None:
    """Пользовательский запрос → безопасное FTS5 MATCH-выражение: каждое слово
    в кавычках с '*' (prefix), операторы/кавычки пользователя не интерпретируются."""
    import re
    terms = re.findall(r"\w+", query, re.UNICODE)
    if not terms:
        return None
    return " ".join(f'"{t}"*' for t in terms)


def search_notes(con, query: str, *, limit: int = 50) -> list[dict]:
    """FTS5-поиск по транскриптам и саммари. Сниппеты с маркерами [[слово]]
    (UI подсвечивает). Колонки notes_fts: 0=session_id, 1=title, 2=transcript,
    3=summary."""
    match = _fts_match(query)
    if match is None:
        return []
    results = []
    for col_idx, source in ((2, "transcript"), (3, "summary")):
        rows = con.execute(
            f"SELECT session_id, title,"
            f" snippet(notes_fts, {col_idx}, '[[', ']]', '…', 12) AS snip"
            f" FROM notes_fts WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (f"{'transcript' if source == 'transcript' else 'summary'}: {match}",
             limit)).fetchall()
        results.extend(
            {"session_id": r["session_id"], "title": r["title"],
             "source": source, "snippet": r["snip"]}
            for r in rows)
    return results


def next_job(con) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM pipeline_jobs WHERE status IN ('queued','failed_retry')"
        " AND (next_retry_at IS NULL OR next_retry_at <= ?)"
        " ORDER BY id LIMIT 1", (now(),)).fetchone()
