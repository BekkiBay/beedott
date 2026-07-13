import json

import pytest
import requests

from daemon.summarizer import (
    MAP_REDUCE_CHUNK_CHARS,
    summarize_ollama,
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


# ─────────────────────────────── summarize_ollama (mocked) ───────────────────


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


def _ollama_payload(content_obj: dict) -> dict:
    return {"message": {"role": "assistant", "content": json.dumps(content_obj, ensure_ascii=False)}}


def test_summarize_ollama_success(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json))
        return FakeResponse(_ollama_payload(GOOD))

    monkeypatch.setattr("daemon.summarizer.requests.post", fake_post)

    result = summarize_ollama(
        "Спикер 1: привет\nСпикер 2: привет",
        {"title": "Синк", "duration_min": 15},
        model="test-model",
        ollama_url="http://fake:11434",
    )

    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "http://fake:11434/api/chat"
    assert payload["format"] == "json"
    assert payload["model"] == "test-model"
    assert payload["options"]["num_ctx"] == 16384

    assert result["degraded"] is False
    for key in ("context", "discussed", "decisions", "action_items", "open_questions"):
        assert result[key] == GOOD[key]


def test_summarize_ollama_invalid_json_degrades_after_retries(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(1)
        return FakeResponse({"message": {"role": "assistant", "content": "это не json вообще"}})

    monkeypatch.setattr("daemon.summarizer.requests.post", fake_post)

    result = summarize_ollama(
        "какой-то транскрипт",
        {"title": "Синк", "duration_min": 15},
        model="test-model",
        ollama_url="http://fake:11434",
    )

    assert len(calls) == 3  # 1 попытка + MAX_JSON_RETRIES(2) ретрая
    assert result["degraded"] is True
    assert result["context"] == "это не json вообще"
    assert result["discussed"] == []
    assert result["decisions"] == []
    assert result["action_items"] == []
    assert result["open_questions"] == []


def test_summarize_ollama_network_error_degrades(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        raise requests.ConnectionError("ollama не поднята")

    monkeypatch.setattr("daemon.summarizer.requests.post", fake_post)

    result = summarize_ollama(
        "транскрипт встречи",
        {"title": "Синк", "duration_min": 15},
        model="test-model",
        ollama_url="http://fake:11434",
    )

    assert result["degraded"] is True
    # сеть недоступна — сырого текста от модели нет, fallback на сам транскрипт
    assert result["context"] == "транскрипт встречи"


def test_summarize_ollama_retries_then_succeeds(monkeypatch):
    responses = [
        FakeResponse({"message": {"role": "assistant", "content": "мусор"}}),
        FakeResponse(_ollama_payload(GOOD)),
    ]

    def fake_post(url, json=None, timeout=None):
        return responses.pop(0)

    monkeypatch.setattr("daemon.summarizer.requests.post", fake_post)

    result = summarize_ollama(
        "транскрипт",
        {"title": "Синк", "duration_min": 15},
        model="test-model",
        ollama_url="http://fake:11434",
    )

    assert result["degraded"] is False
    assert result["context"] == GOOD["context"]


def test_summarize_ollama_map_reduce_triggers_on_long_transcript(monkeypatch):
    long_transcript = "слово " * (MAP_REDUCE_CHUNK_CHARS // 6 + 1000)
    assert len(long_transcript) > MAP_REDUCE_CHUNK_CHARS

    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        return FakeResponse(_ollama_payload(GOOD))

    monkeypatch.setattr("daemon.summarizer.requests.post", fake_post)

    result = summarize_ollama(
        long_transcript,
        {"title": "Долгая встреча", "duration_min": 180},
        model="test-model",
        ollama_url="http://fake:11434",
    )

    # минимум 2 map-чанка + 1 reduce-проход
    assert len(calls) >= 3
    assert result["degraded"] is False
    for key in ("context", "discussed", "decisions", "action_items", "open_questions"):
        assert result[key] == GOOD[key]


def test_summarize_ollama_map_reduce_degraded_partial_propagates(monkeypatch):
    long_transcript = "слово " * (MAP_REDUCE_CHUNK_CHARS // 6 + 1000)

    call_count = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        call_count["n"] += 1
        # первый map-чанк всегда невалиден (после ретраев деградирует),
        # остальные вызовы (второй map-чанк + reduce) — валидны
        if call_count["n"] <= 3:
            return FakeResponse({"message": {"role": "assistant", "content": "плохой json"}})
        return FakeResponse(_ollama_payload(GOOD))

    monkeypatch.setattr("daemon.summarizer.requests.post", fake_post)

    result = summarize_ollama(
        long_transcript,
        {"title": "Долгая встреча", "duration_min": 180},
        model="test-model",
        ollama_url="http://fake:11434",
    )

    # один из map-шагов деградировал → итог помечен как частично деградированный,
    # даже если reduce-проход дал валидный JSON
    assert result["degraded"] is True
