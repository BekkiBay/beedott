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


def test_title_falls_back_to_title_then_placeholder():
    """Старый демон мог не писать display_title (совместимость)."""
    assert mcp_server._title({"display_title": "Синк", "title": "x"}) == "Синк"
    assert mcp_server._title({"title": "Старый формат"}) == "Старый формат"
    assert mcp_server._title({}) == "Без названия"


def test_meeting_list_without_display_title_uses_title():
    sessions = [{"id": "old1", "title": "Старая сессия",
                 "started_at": "2026-10-14T05:00:00+00:00", "duration_min": 10,
                 "status": "delivered", "action_items": None}]
    assert "**Старая сессия**" in mcp_server.format_meeting_list(sessions)


def test_meeting_list_shows_recording_instead_of_mapped_status():
    sessions = [{"id": "r1", "title": None, "display_title": "Встреча 09:00",
                 "started_at": "2026-10-14T05:00:00+00:00", "duration_min": None,
                 "status": "processing", "action_items": None, "state": "RECORDING"}]
    out = mcp_server.format_meeting_list(sessions)
    assert "идёт запись" in out
    assert "обрабатывается" not in out


def test_meeting_list_error_status_appends_trimmed_error():
    long_line = "о" * 250
    sessions = [{"id": "e1", "title": None, "display_title": "Сбой",
                 "started_at": "2026-10-14T05:00:00+00:00", "duration_min": 3,
                 "status": "error", "action_items": None,
                 "error": f"{long_line}\nвторая строка traceback"}]
    out = mcp_server.format_meeting_list(sessions)
    assert f"ошибка: {long_line[:200]}" in out
    assert "вторая строка" not in out


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


def test_search_snippet_newlines_become_spaces():
    results = [{"session_id": "aaa111", "title": None, "source": "summary",
                "snippet": "первая строка\nвторая строка"}]
    out = mcp_server.format_search("x", results, SESSIONS, limit=10)
    assert "- саммари: первая строка вторая строка" in out


def test_search_uses_title_when_session_lacks_display_title():
    """Старый демон мог не писать display_title в sessions_list (совместимость)."""
    sessions = [{"id": "old1", "title": "Старая встреча",
                 "started_at": "2026-10-14T05:00:00+00:00"}]
    results = [{"session_id": "old1", "title": "Старая встреча",
                "source": "summary", "snippet": "x"}]
    out = mcp_server.format_search("x", results, sessions, limit=10)
    assert "### Старая встреча" in out


def test_search_limit_and_nothing_found():
    results = [{"session_id": s["id"], "title": None, "source": "summary", "snippet": "x"}
               for s in SESSIONS]
    assert "bbb222" not in mcp_server.format_search("x", results, SESSIONS, limit=1)
    assert mcp_server.format_search("пусто", [], SESSIONS, limit=5) == \
        "По запросу «пусто» ничего не нашлось."


def test_search_orders_by_recency_and_shows_truncation_hint():
    """Хиты демона приходят старое-первым (FTS5 без ORDER BY ≈ rowid); формат
    должен показывать самую новую встречу, а не первую по порядку хитов."""
    results = [
        {"session_id": SESSIONS[1]["id"], "title": None, "source": "summary", "snippet": "x"},
        {"session_id": SESSIONS[0]["id"], "title": None, "source": "summary", "snippet": "x"},
    ]
    out = mcp_server.format_search("x", results, SESSIONS, limit=1)
    assert "id `aaa111`" in out
    assert "bbb222" not in out
    assert "(показаны 1 из 2 встреч — уточни запрос или увеличь limit)" in out


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


def test_meeting_card_cancelled_state():
    d = detail(followup=None, session={"state": "CANCELLED"})
    out = mcp_server.format_meeting(d)
    assert "отменена" in out


def test_meeting_card_without_display_title_uses_title():
    d = detail(session={"display_title": None, "title": "Старая карточка"})
    out = mcp_server.format_meeting(d)
    assert out.startswith("# Старая карточка")


def test_meeting_card_failed_state_trims_long_pipeline_error():
    long_text = "summarize: " + "б" * 250
    d = detail(session={"state": "FAILED"},
               pipeline=[{"step": "summarized", "ok": False,
                          "error": f"{long_text}\nTraceback (most recent call last)"}])
    out = mcp_server.format_meeting(d)
    assert f"ошибка: {long_text[:200]}" in out
    assert "Traceback" not in out


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


def test_transcript_total_minutes_uses_max_end_not_last_segment():
    """Сегменты сортированы по start (как у транскрайбера со стерео-дорожками
    двух спикеров) — у последнего по порядку сегмента end может быть не самым
    большим."""
    segments = [
        {"start": 0.0, "end": 600.0, "text": "длинная реплика", "speaker": "Спикер 1"},
        {"start": 5.0, "end": 20.0, "text": "короткая реплика", "speaker": "Спикер 2"},
    ]
    out = mcp_server.format_transcript(segments, from_minute=20)
    assert out == "С 20-й минуты расшифровки нет — всего 10 мин."


def test_transcript_continuation_hint_has_real_meeting_id(monkeypatch):
    monkeypatch.setattr(mcp_server, "TRANSCRIPT_LIMIT", 40)
    out = mcp_server.format_transcript(SEGMENTS, meeting_id="aaa111")
    assert 'get_meeting("aaa111", include_transcript=true, from_minute=1).)' in out


def test_meeting_card_transcript_hint_has_real_meeting_id(monkeypatch):
    monkeypatch.setattr(mcp_server, "TRANSCRIPT_LIMIT", 40)
    out = mcp_server.format_meeting(detail(), include_transcript=True)
    assert 'get_meeting("aaa111", include_transcript=true, from_minute=1).)' in out


def test_transcript_paging_covers_every_line_with_overlapping_speakers(monkeypatch):
    """Два спикера перекрываются по времени внутри каждой минуты, сегменты
    отсортированы по start (как у транскрайбера). Пройти по подсказкам
    from_minute до конца и убедиться, что ни одна непустая реплика не
    потерялась и страниц конечное число."""
    monkeypatch.setattr(mcp_server, "TRANSCRIPT_LIMIT", 116)
    raw = []
    for m in range(10):
        raw.append({"start": m * 60 + 5.0, "end": m * 60 + 10.0,
                    "text": f"А линия {m:02d}", "speaker": "Спикер 1"})
        raw.append({"start": m * 60 + 7.0, "end": m * 60 + 15.0,
                    "text": f"Б линия {m:02d}", "speaker": "Спикер 2"})
    segments = sorted(raw, key=lambda s: s["start"])

    def expected_line(seg):
        mm, ss = divmod(int(seg["start"]), 60)
        return f"[{mm:02d}:{ss:02d}] {seg['speaker']}: {seg['text']}"

    expected = {expected_line(seg) for seg in segments if seg["text"].strip()}

    seen: set[str] = set()
    from_minute = 0
    pages = 0
    while True:
        pages += 1
        assert pages <= 50, "страницы не заканчиваются"
        page = mcp_server.format_transcript(segments, from_minute=from_minute,
                                             meeting_id="mmm111")
        lines = page.splitlines()
        hint = lines[-1] if lines and lines[-1].startswith("(Показано") else None
        body = lines[:-1] if hint else lines
        seen.update(body)
        if hint is None:
            break
        from_minute = int(hint.split("from_minute=")[1].rstrip(").)"))

    assert expected <= seen
    assert pages < 50


def test_action_items_without_display_title_uses_title():
    old_session = {"id": "old1", "title": "Старые задачи",
                   "started_at": "2026-10-14T05:00:00+00:00"}
    rows = [(old_session, dict(GOOD, action_items=[
        {"task": "Сделать", "owner": "Bekzod", "deadline": None}]))]
    out = mcp_server.format_action_items(rows, days=7)
    assert "### Старые задачи" in out


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


def test_status_recording_clock_drift_clamped_to_zero():
    """recording_started_at чуть в будущем относительно now (расхождение
    часов) — минуты не должны уходить в минус."""
    now = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)
    st = {"recording": "ccc333", "recording_display_title": "Встреча 10:30",
          "recording_started_at": "2026-10-15T06:05:00+00:00",
          "queue": [], "sessions": {}}
    out = mcp_server.format_status(st, now=now)
    assert "(0 мин)" in out


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


def test_action_items_skips_meeting_deleted_between_list_and_detail(daemon):
    now = datetime.now(timezone.utc)
    deleted = dict(SESSIONS[0], id="del000", started_at=(now - timedelta(days=1)).isoformat())
    kept = dict(SESSIONS[0], id="aaa111", started_at=(now - timedelta(days=2)).isoformat())
    daemon.handlers["sessions_list"] = {"sessions": [deleted, kept]}

    def session_detail(params):
        if params["session_id"] == "del000":
            raise control.DaemonError("not_found", "нет сессии del000")
        return detail()

    daemon.handlers["session_detail"] = session_detail
    out = mcp_server.action_items(days=7)
    assert "- сделать — Bekzod" in out


def test_recording_status_tool(daemon):
    daemon.handlers["status"] = {"recording": None, "queue": [], "sessions": {}}
    assert mcp_server.recording_status().startswith("Запись не идёт.")


@pytest.mark.parametrize("error, text", [
    (control.DaemonUnavailable("нет сокета"), "BeeDott не запущен или не отвечает"),
    (control.DaemonError("not_found", "нет сессии x"), "Встреча не найдена"),
    (control.DaemonError("busy", "встреча сейчас обрабатывается"),
     "BeeDott: встреча сейчас обрабатывается"),
    (control.DaemonError("unknown_method", "session_detail"),
     "BeeDott нужно перезапустить"),
    (ValueError("boom"), "BeeDott: ошибка — ValueError: boom"),
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
                                      "resend": p["resend"], "telegram": "pending"},
        "session_detail": lambda p: detail(),  # has_audio=True, title="Синк"
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
        ("session_detail", {"session_id": "aaa111"}),  # set_keep_audio: has_audio?
        ("set_keep_audio", {"session_id": "aaa111", "keep": True}),
        ("session_detail", {"session_id": "aaa111"}),  # delete_meeting: сверка названия
        ("delete_session", {"session_id": "aaa111", "title": "Синк"}),
    ]


def test_set_keep_audio_refuses_when_audio_already_gone(daemon):
    daemon.handlers["session_detail"] = lambda p: detail(session={"has_audio": False})
    out = mcp_server.set_keep_audio("aaa111", True)
    assert "аудио уже нет" in out
    assert not any(c[0] == "set_keep_audio" for c in daemon.calls)


def test_delete_with_wrong_title_is_readable(daemon):
    daemon.handlers["session_detail"] = lambda p: detail()  # title="Синк"
    with pytest.raises(ToolError, match="называется «Синк»"):
        mcp_server.delete_meeting("aaa111", "Ретро")
    assert not any(c[0] == "delete_session" for c in daemon.calls)


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


def test_stop_recording_warns_about_new_recording_started_right_after(daemon, fast):
    # Детектор звонков (браузер ещё говорил) тут же начал новую запись того
    # же звонка — recording_status становится другим непустым id.
    daemon.handlers["status"] = statuses({"recording": "ccc333"}, {"recording": "ccc333"},
                                         {"recording": "ddd444"})
    daemon.handlers["request_stop"] = {"session_id": "ccc333", "requested": True}
    daemon.handlers["sessions_list"] = {"sessions": [dict(SESSIONS[0], id="ccc333")]}
    out = mcp_server.stop_recording()
    assert "ушла в обработку" in out
    assert "новая запись (id `ddd444`)" in out
    assert "recording_status" in out


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
