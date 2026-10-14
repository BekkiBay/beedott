import json

import pytest

from daemon import summarizer
from daemon.summarizer import (
    FOLLOWUP_KEYS,
    FOLLOWUP_SCHEMA,
    build_prompt,
    normalize_followup,
    parse_followup,
    validate_followup,
)
from tests.conftest import GOOD_FOLLOWUP as GOOD


# ─────────────────────────────── validate_followup ───────────────────────────


def test_validate_ok():
    assert validate_followup(GOOD) == GOOD


def test_validate_bad():
    with pytest.raises(ValueError):
        validate_followup({"context": 1})


def test_validate_missing_key():
    bad = {k: v for k, v in GOOD.items() if k != "open_questions"}
    with pytest.raises(ValueError):
        validate_followup(bad)


def test_validate_extra_key():
    bad = dict(GOOD, extra="nope")
    with pytest.raises(ValueError):
        validate_followup(bad)


def test_validate_wrong_type_discussed():
    bad = dict(GOOD, discussed="не список")
    with pytest.raises(ValueError):
        validate_followup(bad)


def test_validate_action_item_wrong_deadline_type():
    bad = dict(GOOD, action_items=[{"task": "x", "owner": "y", "deadline": 123}])
    with pytest.raises(ValueError):
        validate_followup(bad)


def test_validate_action_item_extra_key():
    bad = dict(GOOD, action_items=[
        {"task": "x", "owner": "y", "deadline": None, "priority": "high"}
    ])
    with pytest.raises(ValueError):
        validate_followup(bad)


def test_validate_deadline_none_is_ok():
    assert validate_followup(GOOD)["action_items"][0]["deadline"] is None


def test_validate_not_a_dict():
    with pytest.raises(ValueError):
        validate_followup(["not", "a", "dict"])


# ───────── продукт P9: null вместо строки — нормализуем, а не выбрасываем ──────


def test_normalize_fills_null_owner_and_coerces_types():
    raw = {
        "context": None,
        "discussed": ["a", 5],
        "decisions": None,
        "action_items": [
            {"task": "сделать отчёт", "owner": None, "deadline": None},
            {"task": "созвон", "owner": "Аня", "deadline": 20260720, "extra": 1},
            {"owner": "Боря"},
            "мусор",
        ],
        "open_questions": [],
    }
    out = validate_followup(normalize_followup(raw))
    assert out["context"] == ""
    assert out["discussed"] == ["a", "5"]
    assert out["decisions"] == []
    assert out["action_items"] == [
        {"task": "сделать отчёт", "owner": "не назначен", "deadline": None},
        {"task": "созвон", "owner": "Аня", "deadline": "20260720"},
    ]


def test_normalize_leaves_wrong_shape_for_validation_to_reject():
    with pytest.raises(ValueError):
        validate_followup(normalize_followup(["не", "объект"]))


# ─────────────────────────────── parse_followup ──────────────────────────────


def test_parse_followup_accepts_plain_and_fenced_json():
    raw = json.dumps(GOOD, ensure_ascii=False)
    assert parse_followup(raw) == GOOD
    assert parse_followup("```json\n" + raw + "\n```") == GOOD


def test_parse_followup_normalizes_null_owner():
    raw = json.dumps(dict(GOOD, action_items=[
        {"task": "сделать", "owner": None, "deadline": None}]), ensure_ascii=False)
    assert parse_followup(raw)["action_items"][0]["owner"] == "не назначен"


def test_parse_followup_rejects_garbage_with_reason():
    with pytest.raises(ValueError, match="невалидный JSON"):
        parse_followup("Вот фоллоу-ап: …")


def test_parse_followup_rejects_wrong_schema():
    with pytest.raises(ValueError):
        parse_followup(json.dumps({"context": "x"}))


# ─────────────────────────────── схема и промпт ──────────────────────────────


def test_followup_schema_matches_manual_validation():
    """JSON Schema для API и ручная validate_followup описывают одно и то же."""
    assert set(FOLLOWUP_SCHEMA["properties"]) == FOLLOWUP_KEYS
    assert set(FOLLOWUP_SCHEMA["required"]) == FOLLOWUP_KEYS
    item = FOLLOWUP_SCHEMA["properties"]["action_items"]["items"]
    assert set(item["required"]) == {"task", "owner", "deadline"}


def test_build_prompt_contains_transcript_meta_and_schema():
    prompt = build_prompt("Спикер 1: катим в пятницу",
                          {"title": "Синк", "duration_min": 30})
    assert "Спикер 1: катим в пятницу" in prompt
    assert "«Синк»" in prompt and "30 минут" in prompt
    assert '"action_items"' in prompt


def test_build_prompt_gives_meeting_date_for_relative_deadlines():
    """«Завтра», «к пятнице» модель переводит в даты только зная дату встречи
    (через API текущей даты у неё нет)."""
    from datetime import date
    prompt = build_prompt("т", {"title": "Синк", "date": date(2026, 10, 12)})
    assert "пн, 12.10.2026" in prompt
    assert "Дата встречи" not in build_prompt("т", {"title": "Синк"})


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
