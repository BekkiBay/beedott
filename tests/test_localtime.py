"""Продукт P7: всё, что читает человек (название встречи, папка и имя заметки),
— в местном времени (UTC+5 для Ташкента), а не в UTC. В БД время остаётся UTC."""
from datetime import datetime

from daemon import db, stages
from daemon.paths import session_dir
from daemon.sinks.base import SinkResult
from daemon.states import State
from daemon.timeutil import to_local


def test_to_local_converts_utc_iso_to_configured_zone(monkeypatch):
    monkeypatch.setenv("MEETMIND_TZ", "Asia/Tashkent")
    local = to_local("2026-07-15T05:10:51+00:00")
    assert (local.hour, local.minute) == (10, 10)
    assert local.utcoffset().total_seconds() == 5 * 3600


def test_default_title_uses_local_time(monkeypatch):
    monkeypatch.setenv("MEETMIND_TZ", "Asia/Tashkent")
    row = {"title": None, "started_at": "2026-07-15T05:10:51+00:00"}
    assert db.session_title(row) == "Встреча 10:10"


def test_deliver_passes_local_time_to_sinks(tmp_path, monkeypatch):
    """Встреча в 01:30 по Ташкенту (20:30 UTC прошлого дня) должна попасть в
    папку своего дня, а не вчерашнего."""
    monkeypatch.setenv("MEETMIND_TZ", "Asia/Tashkent")
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual", title="Ночной созвон")
    con.execute("UPDATE sessions SET started_at='2026-07-14T20:30:00+00:00' WHERE id=?", (sid,))
    con.commit()
    for st in (State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED,
               State.SUMMARIZING, State.SUMMARIZED):
        db.set_state(con, sid, st)
    (session_dir(sid) / "followup.md").write_text("итоги", encoding="utf-8")

    seen: list[datetime] = []

    class Sink:
        def send(self, text, *, title, when, session_id):
            seen.append(when)
            return SinkResult("note", True, "ok")

    monkeypatch.setattr(stages, "_build_sinks", lambda: [("note", False, Sink())])
    stages.deliver(db.get_session(con, sid))

    assert seen[0].strftime("%Y-%m-%d %H:%M") == "2026-07-15 01:30"
