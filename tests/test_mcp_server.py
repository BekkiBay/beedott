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
