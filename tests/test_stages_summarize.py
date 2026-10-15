"""stages.summarize: саммари всегда через Claude Opus, способ — SUMMARY_BACKEND.

claude_cli → claude_cli.summarize, подпись «claude-code:<модель>»;
api → anthropic_api.summarize, подпись «anthropic-api:<модель>»;
неизвестное значение → PermanentStageError (ретрай не поможет);
SummaryAuthError → одно уведомление на сессию и failed_retry.
"""
import json

import pytest

import config
from daemon import anthropic_api, claude_cli, db, stages
from daemon.orchestrator import PermanentStageError
from daemon.paths import session_dir
from daemon.states import State
from daemon.summarizer import SummaryAuthError
from tests.conftest import GOOD_FOLLOWUP as GOOD


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    monkeypatch.setattr(stages, "_auth_notified", set())
    monkeypatch.setattr(config, "CLAUDE_CODE_MODEL", "opus")
    monkeypatch.setattr(config, "ANTHROPIC_MODEL", "claude-opus-5-5")
    con = db.connect()
    sid = db.create_session(con, source="manual", title="Синк")
    for st in (State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED):
        db.set_state(con, sid, st)
    (session_dir(sid) / "transcript.txt").write_text("реплика раз\nреплика два",
                                                     encoding="utf-8")
    return db.get_session(con, sid)


def _followup(sid):
    return json.loads((session_dir(sid) / "followup.json").read_text(encoding="utf-8"))


def _fail(*a, **k):
    pytest.fail("этот backend не должен вызываться")


def test_claude_cli_backend(session, monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_BACKEND", "claude_cli")
    seen = {}

    def fake(transcript, meta):
        seen.update(transcript=transcript, meta=meta)
        return dict(GOOD)

    monkeypatch.setattr(claude_cli, "summarize", fake)
    monkeypatch.setattr(anthropic_api, "summarize", _fail)

    stages.summarize(session)

    assert seen["transcript"] == "реплика раз\nреплика два"
    assert seen["meta"]["title"] == "Синк"
    assert seen["meta"]["date"] is not None  # дата встречи — для «завтра», «к пятнице»
    followup = _followup(session["id"])
    assert followup["engine"] == "claude-code:opus"
    assert followup["revision"] == 1
    md = (session_dir(session["id"]) / "followup.md").read_text(encoding="utf-8")
    assert "claude-code:opus" in md


def test_api_backend(session, monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_BACKEND", "api")
    monkeypatch.setattr(anthropic_api, "summarize", lambda t, m: dict(GOOD))
    monkeypatch.setattr(claude_cli, "summarize", _fail)

    stages.summarize(session)

    assert _followup(session["id"])["engine"] == "anthropic-api:claude-opus-5-5"


def test_unknown_backend_is_permanent(session, monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_BACKEND", "ollama")
    with pytest.raises(PermanentStageError, match="SUMMARY_BACKEND"):
        stages.summarize(session)


def test_auth_error_notifies_once_per_session_and_raises(session, monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_BACKEND", "claude_cli")

    def not_logged_in(transcript, meta):
        raise SummaryAuthError("claude CLI не залогинен")

    monkeypatch.setattr(claude_cli, "summarize", not_logged_in)
    sent = []
    monkeypatch.setattr(stages, "notify", lambda title, msg, **kw: sent.append(msg))

    for _ in range(2):  # два ретрая одной сессии
        with pytest.raises(SummaryAuthError):
            stages.summarize(session)

    assert len(sent) == 1 and "не залогинен" in sent[0]
    assert not (session_dir(session["id"]) / "followup.json").exists()


def test_backend_failure_propagates_for_retry(session, monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_BACKEND", "claude_cli")

    def broken(transcript, meta):
        raise ValueError("невалидный JSON от Claude")

    monkeypatch.setattr(claude_cli, "summarize", broken)
    with pytest.raises(ValueError):
        stages.summarize(session)


def test_resummarize_keeps_previous_revision_on_disk(session, monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_BACKEND", "claude_cli")
    monkeypatch.setattr(claude_cli, "summarize", lambda t, m: dict(GOOD))
    sdir = session_dir(session["id"])

    stages.summarize(session)
    first = (sdir / "followup.json").read_text(encoding="utf-8")
    stages.summarize(session)

    assert (sdir / "followup.r1.json").read_text(encoding="utf-8") == first
    assert _followup(session["id"])["revision"] == 2


def test_resummarize_tolerates_non_dict_old_followup(session, monkeypatch):
    monkeypatch.setattr(config, "SUMMARY_BACKEND", "claude_cli")
    monkeypatch.setattr(claude_cli, "summarize", lambda t, m: dict(GOOD))
    (session_dir(session["id"]) / "followup.json").write_text("[]", encoding="utf-8")

    stages.summarize(session)

    assert _followup(session["id"])["revision"] == 2
