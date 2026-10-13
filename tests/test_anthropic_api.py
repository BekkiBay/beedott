"""Тесты daemon/anthropic_api.py — саммари через Anthropic API по ключу.

SDK `anthropic` подменяется фейковым модулем в sys.modules: сеть не трогаем,
проверяем форму запроса (модель, structured outputs, fallbacks) и разбор
ответа (refusal, max_tokens, блок fallback, ошибки ключа).
"""
import json
import sys
from types import SimpleNamespace

import pytest

import config
from daemon import anthropic_api
from daemon.orchestrator import PermanentStageError
from daemon.summarizer import FOLLOWUP_SCHEMA, SYSTEM_PROMPT, SummaryAuthError
from tests.conftest import GOOD_FOLLOWUP as GOOD

META = {"title": "Тест", "duration_min": 10}
TRANSCRIPT = "Спикер 1: привет\nСпикер 2: обсудим релиз"


def _text(text):
    return SimpleNamespace(type="text", text=text)


def _message(content, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(content=content, stop_reason=stop_reason,
                           stop_details=stop_details, model="claude-opus-5")


class _FakeStream:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


@pytest.fixture()
def fake_sdk(monkeypatch):
    """Фейковый `anthropic`: .reply — что вернёт стрим, .raises — что кинуть,
    .clients / .requests — что запрашивали."""
    sdk = SimpleNamespace()
    sdk.reply = _message([_text(json.dumps(GOOD, ensure_ascii=False))])
    sdk.raises = None
    sdk.clients, sdk.requests = [], []

    class APIError(Exception):
        pass

    class AuthenticationError(APIError):
        pass

    class PermissionDeniedError(APIError):
        pass

    def stream(**kwargs):
        sdk.requests.append(kwargs)
        if sdk.raises is not None:
            raise sdk.raises
        return _FakeStream(sdk.reply)

    class Anthropic:
        def __init__(self, **kwargs):
            sdk.clients.append(kwargs)
            self.beta = SimpleNamespace(messages=SimpleNamespace(stream=stream))

    sdk.APIError = APIError
    sdk.AuthenticationError = AuthenticationError
    sdk.PermissionDeniedError = PermissionDeniedError
    sdk.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(config, "ANTHROPIC_MODEL", "claude-opus-5")
    return sdk


def test_returns_validated_followup_and_sends_expected_request(fake_sdk):
    assert anthropic_api.summarize(TRANSCRIPT, META) == GOOD

    assert fake_sdk.clients == [{"api_key": "sk-test"}]
    req = fake_sdk.requests[0]
    assert req["model"] == "claude-opus-5"
    assert req["system"] == SYSTEM_PROMPT
    assert TRANSCRIPT in req["messages"][0]["content"]
    assert req["thinking"] == {"type": "adaptive"}
    assert req["output_config"] == {
        "format": {"type": "json_schema", "schema": FOLLOWUP_SCHEMA}}
    assert req["fallbacks"] == "default"
    assert req["betas"] == [anthropic_api.FALLBACK_BETA]


def test_missing_key_raises_auth_error_without_client(fake_sdk, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    with pytest.raises(SummaryAuthError, match="ANTHROPIC_API_KEY"):
        anthropic_api.summarize(TRANSCRIPT, META)
    assert fake_sdk.clients == []


def test_rejected_key_raises_auth_error(fake_sdk):
    fake_sdk.raises = fake_sdk.AuthenticationError("invalid x-api-key")
    with pytest.raises(SummaryAuthError, match="invalid x-api-key"):
        anthropic_api.summarize(TRANSCRIPT, META)


def test_other_api_errors_propagate_for_retry(fake_sdk):
    fake_sdk.raises = fake_sdk.APIError("overloaded")
    with pytest.raises(fake_sdk.APIError):
        anthropic_api.summarize(TRANSCRIPT, META)


def test_refusal_is_permanent(fake_sdk):
    fake_sdk.reply = _message([], stop_reason="refusal",
                              stop_details=SimpleNamespace(category="cyber"))
    with pytest.raises(PermanentStageError, match="cyber"):
        anthropic_api.summarize(TRANSCRIPT, META)


def test_max_tokens_raises(fake_sdk):
    fake_sdk.reply = _message([_text('{"context": "обре')], stop_reason="max_tokens")
    with pytest.raises(RuntimeError, match="max_tokens"):
        anthropic_api.summarize(TRANSCRIPT, META)


def test_text_before_fallback_block_is_dropped(fake_sdk):
    """Отказавшая модель успела что-то написать, затем запрос подхватила
    fallback-модель — в разбор идёт только её ответ."""
    fake_sdk.reply = _message([
        SimpleNamespace(type="thinking", thinking=""),
        _text('{"context": "част'),
        SimpleNamespace(type="fallback"),
        _text(json.dumps(GOOD, ensure_ascii=False)),
    ])
    assert anthropic_api.summarize(TRANSCRIPT, META) == GOOD
