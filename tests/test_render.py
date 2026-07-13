from datetime import datetime

from daemon.render import render_followup
from tests.conftest import GOOD_FOLLOWUP as GOOD


def test_render_contains_sections():
    md = render_followup(GOOD, engine="vikhr-8b", title="Синк", duration_min=30)
    for anchor in ("**Контекст**", "**Договорённости**", "**Action items**", "vikhr-8b"):
        assert anchor in md


def test_render_all_section_headers_present():
    md = render_followup(GOOD, engine="vikhr-8b", title="Синк", duration_min=30)
    for anchor in (
        "**Контекст**",
        "**Что обсуждалось**",
        "**Договорённости**",
        "**Action items**",
        "**Открытые вопросы**",
    ):
        assert anchor in md


def test_render_action_item_format():
    followup = dict(GOOD, action_items=[
        {"task": "сделать X", "owner": "Bekzod", "deadline": "2026-07-20"}
    ])
    md = render_followup(followup, engine="vikhr-8b", title="Синк", duration_min=30)
    assert "📅 2026-07-20 — сделать X — Bekzod" in md


def test_render_deadline_none_shows_asap():
    md = render_followup(GOOD, engine="vikhr-8b", title="Синк", duration_min=30)
    assert "📅 asap — сделать — Bekzod" in md


def test_render_empty_lists_show_net():
    followup = {
        "context": "",
        "discussed": [],
        "decisions": [],
        "action_items": [],
        "open_questions": [],
    }
    md = render_followup(followup, engine="vikhr-8b", title="Синк", duration_min=10)
    assert md.count("нет") == 5  # context + discussed + decisions + action_items + open_questions


def test_render_engine_icon_local_vs_cloud():
    md_local = render_followup(GOOD, engine="vikhr-8b", title="t", duration_min=5)
    md_cloud = render_followup(GOOD, engine="claude-sonnet", title="t", duration_min=5)
    assert "🖥" in md_local and "vikhr-8b" in md_local
    assert "☁️" in md_cloud and "claude-sonnet" in md_cloud


def test_render_with_when_includes_date():
    md = render_followup(
        GOOD, engine="vikhr-8b", title="Синк", duration_min=30,
        when=datetime(2026, 7, 13, 14, 30),
    )
    assert "13.07.2026" in md and "14:30" in md


def test_render_without_when_has_no_date_separator_crash():
    # when=None — просто не должно падать, шапка без даты
    md = render_followup(GOOD, engine="vikhr-8b", title="Синк", duration_min=30)
    assert md.startswith("**Синк**")


def test_render_disclaimer_present():
    md = render_followup(GOOD, engine="vikhr-8b", title="t", duration_min=5)
    assert (
        "Запись хранится локально. Запись разговора без согласия собеседников "
        "может нарушать местные законы — ответственность на пользователе."
    ) in md


def test_render_degraded_warning():
    followup = dict(GOOD, degraded=True)
    md = render_followup(followup, engine="vikhr-8b", title="t", duration_min=5)
    assert "⚠️" in md


def test_render_not_degraded_no_warning():
    md = render_followup(GOOD, engine="vikhr-8b", title="t", duration_min=5)
    assert "⚠️" not in md
