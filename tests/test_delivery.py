"""Тесты daemon/stages.deliver (PLAN_v3 Task 2.1 — ретраи, идемпотентность доставки).

deliver() итерирует sinks, возвращаемые _build_sinks(): Telegram обязателен
(без него сессия не DELIVERED — исключение), note/obsidian — best-effort (лог
+ failed_retry, не блокируют). Идемпотентность: sink со status='delivered' в
`deliveries` повторно не шлётся.

_build_sinks подменяется monkeypatch-ем на фейковые sink-объекты, чтобы не
трогать реальные Telegram/файловую систему — deliver() работает только с
tuple(name, required, sink) и sink.send(...).
"""
import pytest

from daemon import db, stages
from daemon.paths import session_dir
from daemon.sinks.base import SinkResult
from daemon.states import State


class ScriptedSink:
    """Фейковый sink: send() отдаёт результаты по очереди из заранее заданного
    сценария; после исчерпания сценария — успех по умолчанию. Считает вызовы —
    для проверки идемпотентности (delivered sink не должен слаться повторно)."""

    def __init__(self, name: str, script: list[tuple[bool, str]]):
        self.name = name
        self._script = list(script)
        self.calls = 0

    def send(self, text, *, title, when, session_id) -> SinkResult:
        self.calls += 1
        ok, detail = self._script.pop(0) if self._script else (True, "ok")
        return SinkResult(self.name, ok, detail)


def _make_session(tmp_path, monkeypatch, *, title="Синк") -> tuple:
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual", title=title)
    for st in (State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED,
               State.SUMMARIZING, State.SUMMARIZED):
        db.set_state(con, sid, st)
    sdir = session_dir(sid)
    (sdir / "followup.md").write_text("# Синк\n\nитоги встречи", encoding="utf-8")
    return con, sid


def _delivery_row(con, sid, sink):
    return con.execute(
        "SELECT * FROM deliveries WHERE session_id=? AND sink=?", (sid, sink)).fetchone()


# ────────────────────────── ретраи + идемпотентность (обязательный sink) ──────


def test_telegram_retries_twice_then_succeeds_on_third_deliver(tmp_path, monkeypatch):
    con, sid = _make_session(tmp_path, monkeypatch)
    telegram = ScriptedSink("telegram", [(False, "timeout"), (False, "timeout")])
    monkeypatch.setattr(stages, "_build_sinks", lambda: [("telegram", True, telegram)])

    session = db.get_session(con, sid)

    # 1-й вызов: падает → RuntimeError (обязательный sink), failed_retry-запись
    with pytest.raises(RuntimeError):
        stages.deliver(session)
    row = _delivery_row(con, sid, "telegram")
    assert row["status"] == "failed_retry"
    assert row["attempts"] == 1
    assert row["delivered_at"] is None

    # 2-й вызов: снова падает → вторая failed_retry-запись (тот же ряд, attempts=2)
    with pytest.raises(RuntimeError):
        stages.deliver(session)
    row = _delivery_row(con, sid, "telegram")
    assert row["status"] == "failed_retry"
    assert row["attempts"] == 2
    assert row["delivered_at"] is None

    # 3-й вызов: успех → delivered, delivered_at не NULL, исключения нет
    stages.deliver(session)
    row = _delivery_row(con, sid, "telegram")
    assert row["status"] == "delivered"
    assert row["attempts"] == 3
    assert row["delivered_at"] is not None

    assert telegram.calls == 3


def test_deliver_after_delivered_does_not_resend(tmp_path, monkeypatch):
    con, sid = _make_session(tmp_path, monkeypatch)
    telegram = ScriptedSink("telegram", [])  # всегда успех
    monkeypatch.setattr(stages, "_build_sinks", lambda: [("telegram", True, telegram)])

    session = db.get_session(con, sid)

    stages.deliver(session)
    assert telegram.calls == 1
    row = _delivery_row(con, sid, "telegram")
    assert row["status"] == "delivered"
    first_delivered_at = row["delivered_at"]

    # повторный deliver той же сессии — sink.send НЕ вызывается второй раз
    stages.deliver(session)
    assert telegram.calls == 1
    row = _delivery_row(con, sid, "telegram")
    assert row["status"] == "delivered"
    assert row["delivered_at"] == first_delivered_at


# ────────────────────────── best-effort sink (note) не блокирует telegram ─────


def test_note_failure_does_not_block_delivery_telegram_still_delivered(tmp_path, monkeypatch):
    con, sid = _make_session(tmp_path, monkeypatch)
    note = ScriptedSink("note", [(False, "диск недоступен")])
    telegram = ScriptedSink("telegram", [])  # успех сразу

    monkeypatch.setattr(
        stages, "_build_sinks",
        lambda: [("note", False, note), ("telegram", True, telegram)])

    session = db.get_session(con, sid)

    # note — best-effort, telegram — ok => deliver НЕ бросает исключение
    stages.deliver(session)

    note_row = _delivery_row(con, sid, "note")
    telegram_row = _delivery_row(con, sid, "telegram")
    assert note_row["status"] == "failed_retry"
    assert note_row["delivered_at"] is None
    assert telegram_row["status"] == "delivered"
    assert telegram_row["delivered_at"] is not None
