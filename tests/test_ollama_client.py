"""Тесты daemon/ollama_client.py (PLAN_v3 Task 2.5).

SPEC_v3 §10, кейс «Ollama не запущена»: демон пытается поднять сам
(`ollama serve`), не смог за ≤20 сек → OllamaDown (штатный failed_retry у
оркестратора, БЕЗ автопереключения на Claude). Модель не скачана → отдельная
ошибка OllamaModelMissing с подсказкой `ollama pull <model>`.

Все тесты мокают requests.get/post и subprocess.Popen — никаких реальных
сетевых вызовов или процессов. Таймауты autostart-поллинга подменяются на
крошечные значения, чтобы тесты не ждали реальные 20 секунд.
"""
import requests

from daemon import ollama_client
from daemon.ollama_client import (
    OllamaDown,
    OllamaModelMissing,
    chat_json,
    ensure_model,
    ensure_running,
)

import pytest


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


def _tags_payload(*names: str) -> dict:
    return {"models": [{"name": n} for n in names]}


@pytest.fixture(autouse=True)
def _fast_autostart(monkeypatch):
    """Не ждать реальные 20 секунд — крошечные таймаут/интервал поллинга."""
    monkeypatch.setattr(ollama_client, "OLLAMA_AUTOSTART_TIMEOUT_S", 0.05)
    monkeypatch.setattr(ollama_client, "OLLAMA_AUTOSTART_POLL_INTERVAL_S", 0.01)


# ─────────────────────────────── ensure_running ───────────────────────────────


def test_ensure_running_already_up_no_autostart(monkeypatch):
    """Ollama уже отвечает на /api/tags → ensure_running просто возвращается,
    ollama serve НЕ запускается."""
    calls = {"get": 0, "popen": 0}

    def fake_get(url, timeout=None):
        calls["get"] += 1
        assert url == "http://fake:11434/api/tags"
        return FakeResponse(_tags_payload("test-model"))

    def fake_popen(*args, **kwargs):
        calls["popen"] += 1
        raise AssertionError("ollama serve не должен запускаться, если Ollama уже жива")

    monkeypatch.setattr(ollama_client.requests, "get", fake_get)
    monkeypatch.setattr(ollama_client.subprocess, "Popen", fake_popen)

    ensure_running("http://fake:11434")

    assert calls["get"] == 1
    assert calls["popen"] == 0


def test_ensure_running_autostart_recovers(monkeypatch):
    """Первый health-check падает (connection refused) → autostart поднимает
    `ollama serve` → следующий поллинг-запрос уже успешен, исключения нет."""
    calls = {"get": 0}

    def fake_get(url, timeout=None):
        calls["get"] += 1
        if calls["get"] == 1:
            raise requests.ConnectionError("connection refused")
        return FakeResponse(_tags_payload("test-model"))

    popen_calls = []

    def fake_popen(cmd, stdout=None, stderr=None):
        popen_calls.append(cmd)

    monkeypatch.setattr(ollama_client.requests, "get", fake_get)
    monkeypatch.setattr(ollama_client.subprocess, "Popen", fake_popen)

    ensure_running("http://fake:11434")

    assert popen_calls == [["ollama", "serve"]]
    assert calls["get"] >= 2


def test_ensure_running_never_comes_up_raises_ollama_down(monkeypatch):
    """Autostart запускает ollama serve, но /api/tags так и не отвечает в
    пределах таймаута → OllamaDown."""
    def fake_get(url, timeout=None):
        raise requests.ConnectionError("connection refused")

    popen_calls = []

    def fake_popen(cmd, stdout=None, stderr=None):
        popen_calls.append(cmd)

    monkeypatch.setattr(ollama_client.requests, "get", fake_get)
    monkeypatch.setattr(ollama_client.subprocess, "Popen", fake_popen)

    with pytest.raises(OllamaDown):
        ensure_running("http://fake:11434")

    assert popen_calls == [["ollama", "serve"]]


def test_ensure_running_autostart_disabled_raises_immediately(monkeypatch):
    """autostart=False: Ollama не отвечает → сразу OllamaDown, ollama serve не трогаем."""
    def fake_get(url, timeout=None):
        raise requests.ConnectionError("connection refused")

    def fake_popen(*a, **k):
        raise AssertionError("autostart=False не должен запускать ollama serve")

    monkeypatch.setattr(ollama_client.requests, "get", fake_get)
    monkeypatch.setattr(ollama_client.subprocess, "Popen", fake_popen)

    with pytest.raises(OllamaDown):
        ensure_running("http://fake:11434", autostart=False)


def test_ensure_running_popen_missing_binary_raises_ollama_down(monkeypatch):
    """`ollama` бинарник вообще не найден (FileNotFoundError из Popen) → OllamaDown,
    а не необработанное исключение."""
    def fake_get(url, timeout=None):
        raise requests.ConnectionError("connection refused")

    def fake_popen(*a, **k):
        raise FileNotFoundError("ollama: command not found")

    monkeypatch.setattr(ollama_client.requests, "get", fake_get)
    monkeypatch.setattr(ollama_client.subprocess, "Popen", fake_popen)

    with pytest.raises(OllamaDown):
        ensure_running("http://fake:11434")


# ─────────────────────────────── ensure_model ─────────────────────────────────


def test_ensure_model_present_ok(monkeypatch):
    def fake_get(url, timeout=None):
        return FakeResponse(_tags_payload("vikhr-nemo:12b-instruct-q4", "llama3:8b"))

    monkeypatch.setattr(ollama_client.requests, "get", fake_get)

    ensure_model("http://fake:11434", "vikhr-nemo:12b-instruct-q4")  # не бросает


def test_ensure_model_missing_raises_with_pull_hint(monkeypatch):
    def fake_get(url, timeout=None):
        return FakeResponse(_tags_payload("llama3:8b"))

    monkeypatch.setattr(ollama_client.requests, "get", fake_get)

    with pytest.raises(OllamaModelMissing) as exc_info:
        ensure_model("http://fake:11434", "vikhr-nemo:12b-instruct-q4")

    assert "ollama pull vikhr-nemo:12b-instruct-q4" in str(exc_info.value)


def test_ensure_model_ollama_unreachable_raises_ollama_down(monkeypatch):
    """/api/tags недоступен во время ensure_model → OllamaDown (не ModelMissing)."""
    def fake_get(url, timeout=None):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(ollama_client.requests, "get", fake_get)

    with pytest.raises(OllamaDown):
        ensure_model("http://fake:11434", "vikhr-nemo:12b-instruct-q4")


# ─────────────────────────────── chat_json ────────────────────────────────────


def test_chat_json_posts_payload_and_returns_content(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        return FakeResponse({"message": {"role": "assistant", "content": '{"ok": true}'}})

    monkeypatch.setattr(ollama_client.requests, "post", fake_post)

    content = chat_json(
        [{"role": "user", "content": "hi"}],
        model="test-model", url="http://fake:11434", num_ctx=16384, timeout=600)

    assert content == '{"ok": true}'
    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "http://fake:11434/api/chat"
    assert payload["model"] == "test-model"
    assert payload["format"] == "json"
    assert payload["options"]["num_ctx"] == 16384
    assert timeout == 600


def test_chat_json_raises_on_http_error(monkeypatch):
    def fake_post(url, json=None, timeout=None):
        return FakeResponse({}, status_code=500)

    monkeypatch.setattr(ollama_client.requests, "post", fake_post)

    with pytest.raises(requests.RequestException):
        chat_json(
            [{"role": "user", "content": "hi"}],
            model="test-model", url="http://fake:11434", num_ctx=16384, timeout=600)
