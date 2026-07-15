"""Врезка Claude-бустера в stages.summarize (PLAN_v3 Task 7.4).

Правила: USE_CLAUDE_CODE=True → после успешного Ollama-саммари зовём
claude_booster.boost(); boost вернул dict → в followup.json/md уходит
улучшенная версия с подписью движка claude; boost вернул None (любой сбой)
→ остаётся Ollama-версия с подписью Ollama-модели, стадия НЕ падает.
USE_CLAUDE_CODE=False → boost не вызывается вообще.
"""
import json

import pytest

import config
from daemon import claude_booster, db, memguard, stages
from daemon.paths import session_dir
from daemon.states import State
from tests.conftest import GOOD_FOLLOWUP as GOOD


@pytest.fixture(autouse=True)
def _no_memory_limit(monkeypatch):
    """stages.summarize сам зовёт memguard.require — реальная свободная RAM
    машины не должна решать, каким путём (Ollama/Claude) пойдёт тест."""
    monkeypatch.setattr(memguard, "available", lambda: 64 * 2**30)


BOOSTED = dict(GOOD, context="улучшено клодом", degraded=False)


def _make_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual", title="Бустер")
    for st in (State.RECORDING, State.RECORDED, State.TRANSCRIBING, State.TRANSCRIBED):
        db.set_state(con, sid, st)
    (session_dir(sid) / "transcript.txt").write_text("реплика раз\nреплика два",
                                                     encoding="utf-8")
    return con, sid


def _run_summarize(tmp_path, monkeypatch, *, use_claude: bool, boost_result):
    con, sid = _make_session(tmp_path, monkeypatch)
    monkeypatch.setattr(stages, "summarize_ollama",
                        lambda transcript, meta: dict(GOOD, degraded=False))
    monkeypatch.setattr(config, "USE_CLAUDE_CODE", use_claude)
    calls = []

    def fake_boost(transcript, ollama_followup, meta, **kw):
        calls.append((transcript, ollama_followup))
        return boost_result

    monkeypatch.setattr(claude_booster, "boost", fake_boost)
    stages.summarize(db.get_session(con, sid))
    sdir = session_dir(sid)
    followup = json.loads((sdir / "followup.json").read_text(encoding="utf-8"))
    md = (sdir / "followup.md").read_text(encoding="utf-8")
    return calls, followup, md


def test_boosted_followup_written_with_claude_engine(tmp_path, monkeypatch):
    calls, followup, md = _run_summarize(
        tmp_path, monkeypatch, use_claude=True, boost_result=dict(BOOSTED))
    assert len(calls) == 1
    assert calls[0][0] == "реплика раз\nреплика два"  # транскрипт ушёл в бустер
    assert followup["context"] == "улучшено клодом"
    assert "claude" in md.lower()
    assert config.OLLAMA_MODEL not in md


def test_boost_none_keeps_ollama_version(tmp_path, monkeypatch):
    calls, followup, md = _run_summarize(
        tmp_path, monkeypatch, use_claude=True, boost_result=None)
    assert len(calls) == 1
    assert followup["context"] == GOOD["context"]
    assert config.OLLAMA_MODEL in md


def test_boost_not_called_when_disabled_in_config(tmp_path, monkeypatch):
    calls, followup, md = _run_summarize(
        tmp_path, monkeypatch, use_claude=False, boost_result=dict(BOOSTED))
    assert calls == []
    assert followup["context"] == GOOD["context"]
    assert config.OLLAMA_MODEL in md


# ───────── fallback: Ollama недоступна → саммари напрямую через Claude ────────


def _transcribed_session(tmp_path, monkeypatch):
    from daemon.paths import session_dir
    from daemon.states import State
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()
    sid = db.create_session(con, source="manual", title="Фоллбек")
    for st in (State.RECORDING, State.RECORDED, State.TRANSCRIBING,
               State.TRANSCRIBED):
        db.set_state(con, sid, st)
    (session_dir(sid) / "transcript.txt").write_text("текст", encoding="utf-8")
    return con, sid


def test_ollama_down_falls_back_to_claude_direct(tmp_path, monkeypatch):
    from daemon import ollama_client
    from daemon.paths import session_dir
    con, sid = _transcribed_session(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "USE_CLAUDE_CODE", True)

    def ollama_boom(transcript, meta):
        raise ollama_client.OllamaModelMissing("нет модели")

    monkeypatch.setattr(stages, "summarize_ollama", ollama_boom)
    monkeypatch.setattr(claude_booster, "summarize_direct",
                        lambda transcript, meta: dict(GOOD, degraded=False))
    monkeypatch.setattr(claude_booster, "boost",
                        lambda *a, **k: pytest.fail("boost не для fallback-пути"))

    stages.summarize(db.get_session(con, sid))

    followup = json.loads(
        (session_dir(sid) / "followup.json").read_text(encoding="utf-8"))
    assert followup["engine"].startswith("claude-code:")
    assert followup["context"] == GOOD["context"]


def test_low_ram_falls_back_to_claude_direct(tmp_path, monkeypatch):
    from daemon import memguard
    from daemon.paths import session_dir
    con, sid = _transcribed_session(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "USE_CLAUDE_CODE", True)
    monkeypatch.setattr(memguard, "available", lambda: 1 * 2**30)  # 1GB — мало
    monkeypatch.setattr(
        stages, "summarize_ollama",
        lambda *a, **k: pytest.fail("Ollama не должна вызываться при 1GB RAM"))
    monkeypatch.setattr(claude_booster, "summarize_direct",
                        lambda transcript, meta: dict(GOOD, degraded=False))

    stages.summarize(db.get_session(con, sid))

    followup = json.loads(
        (session_dir(sid) / "followup.json").read_text(encoding="utf-8"))
    assert followup["engine"].startswith("claude-code:")


def test_ollama_down_without_claude_reraises(tmp_path, monkeypatch):
    from daemon import ollama_client
    con, sid = _transcribed_session(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "USE_CLAUDE_CODE", False)

    def ollama_boom(transcript, meta):
        raise ollama_client.OllamaDown("лежит")

    monkeypatch.setattr(stages, "summarize_ollama", ollama_boom)
    with pytest.raises(ollama_client.OllamaDown):
        stages.summarize(db.get_session(con, sid))


def test_claude_direct_failure_reraises_original(tmp_path, monkeypatch):
    from daemon import ollama_client
    con, sid = _transcribed_session(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "USE_CLAUDE_CODE", True)

    def ollama_boom(transcript, meta):
        raise ollama_client.OllamaDown("лежит")

    monkeypatch.setattr(stages, "summarize_ollama", ollama_boom)
    monkeypatch.setattr(claude_booster, "summarize_direct", lambda *a, **k: None)
    with pytest.raises(ollama_client.OllamaDown):
        stages.summarize(db.get_session(con, sid))
