"""Тесты daemon/claude_booster.py (PLAN_v3 Task 7.4).

Бустер — opt-in надстройка над Ollama-фоллоу-апом: транскрипт + Ollama-JSON
уходят в claude CLI (headless --print, Max-подписка), обратно ждём JSON той же
схемы SPEC_v3 §7. Любой сбой (auth/exit code/невалидный JSON/схема/таймаут) →
None, остаётся Ollama-версия. Auth-сбой дополнительно: одно уведомление и
отключение бустера до рестарта демона.

subprocess мокается — реальный claude CLI в тестах не зовём.
"""
import json
import subprocess
from unittest.mock import patch

import pytest

from daemon import claude_booster
from tests.conftest import GOOD_FOLLOWUP as GOOD

META = {"title": "Тест", "duration_min": 10}
TRANSCRIPT = "Спикер 1: привет\nСпикер 2: обсудим релиз"


@pytest.fixture(autouse=True)
def _reset_booster():
    """Бустер хранит module-level флаг «отключён до рестарта» — сбрасываем."""
    claude_booster.reset()
    yield
    claude_booster.reset()


def _proc(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout.encode(), stderr=b"")


def _boost(**over):
    kwargs = dict(transcript=TRANSCRIPT, ollama_followup=GOOD, meta=META)
    kwargs.update(over)
    return claude_booster.boost(**kwargs)


# ─────────────────────────────── happy path ──────────────────────────────────


def test_boost_returns_validated_followup():
    with patch("daemon.claude_booster.claude_auth.auth_ok", return_value=True), \
         patch("daemon.claude_booster.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_booster.subprocess.run",
               return_value=_proc(json.dumps(GOOD, ensure_ascii=False))):
        result = _boost()
    assert result is not None
    assert result["decisions"] == GOOD["decisions"]
    assert result["degraded"] is False


def test_boost_strips_markdown_fence():
    fenced = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    with patch("daemon.claude_booster.claude_auth.auth_ok", return_value=True), \
         patch("daemon.claude_booster.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_booster.subprocess.run", return_value=_proc(fenced)):
        result = _boost()
    assert result is not None
    assert result["context"] == GOOD["context"]


def test_boost_cli_invocation_and_env(monkeypatch):
    """Команда: --print --model … --append-system-prompt … --output-format text;
    транскрипт и Ollama-JSON идут в stdin; env без ANTHROPIC_API_KEY."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _proc(json.dumps(GOOD, ensure_ascii=False))

    with patch("daemon.claude_booster.claude_auth.auth_ok", return_value=True), \
         patch("daemon.claude_booster.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_booster.subprocess.run", side_effect=fake_run):
        assert _boost(model="sonnet") is not None

    cmd = captured["cmd"]
    assert cmd[0] == "/bin/claude"
    assert "--print" in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert "--append-system-prompt" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "text"
    assert "--bare" not in cmd

    stdin = captured["kwargs"]["input"].decode()
    assert TRANSCRIPT in stdin
    assert GOOD["action_items"][0]["task"] in stdin
    assert "ANTHROPIC_API_KEY" not in captured["kwargs"]["env"]


# ─────────────────────────────── сбои → None ─────────────────────────────────


@pytest.mark.parametrize("bad_stdout", [
    "не json вообще",
    json.dumps({"context": "x"}),  # валидный JSON, невалидная схема §7
    "",
])
def test_boost_invalid_output_returns_none(bad_stdout):
    with patch("daemon.claude_booster.claude_auth.auth_ok", return_value=True), \
         patch("daemon.claude_booster.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_booster.subprocess.run", return_value=_proc(bad_stdout)):
        assert _boost() is None
    assert not claude_booster.is_disabled()  # плохой JSON ≠ auth-сбой


def test_boost_nonzero_exit_returns_none():
    with patch("daemon.claude_booster.claude_auth.auth_ok", return_value=True), \
         patch("daemon.claude_booster.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_booster.subprocess.run", return_value=_proc("boom", 1)):
        assert _boost() is None


def test_boost_timeout_returns_none():
    with patch("daemon.claude_booster.claude_auth.auth_ok", return_value=True), \
         patch("daemon.claude_booster.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_booster.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)):
        assert _boost() is None


# ────────────────── auth-сбой: notify один раз + off до рестарта ──────────────


def test_boost_auth_failure_notifies_once_and_disables():
    with patch("daemon.claude_booster.claude_auth.auth_ok", return_value=False), \
         patch("daemon.claude_booster.notify") as mock_notify, \
         patch("daemon.claude_booster.subprocess.run") as mock_run:
        assert _boost() is None
        assert claude_booster.is_disabled()
        # второй вызов: без повторного notify и без subprocess
        assert _boost() is None
    assert mock_notify.call_count == 1
    assert "claude" in mock_notify.call_args.args[1].lower()
    mock_run.assert_not_called()


def test_boost_missing_binary_disables_as_auth_failure():
    with patch("daemon.claude_booster.claude_auth.auth_ok", return_value=False), \
         patch("daemon.claude_booster.claude_auth.claude_bin", return_value=None), \
         patch("daemon.claude_booster.notify"):
        assert _boost() is None
    assert claude_booster.is_disabled()


# ─────────────── summarize_direct: Claude вместо Ollama (fallback) ────────────


def test_summarize_direct_returns_validated_followup():
    with patch("daemon.claude_booster.claude_auth.auth_ok", return_value=True), \
         patch("daemon.claude_booster.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_booster.subprocess.run",
               return_value=_proc(json.dumps(GOOD, ensure_ascii=False))) as mock_run:
        result = claude_booster.summarize_direct(TRANSCRIPT, META)
    assert result is not None
    assert result["decisions"] == GOOD["decisions"]
    assert result["degraded"] is False
    stdin = mock_run.call_args.kwargs["input"].decode()
    assert TRANSCRIPT in stdin
    assert "Черновик" not in stdin  # прямой промпт, без Ollama-черновика


def test_summarize_direct_invalid_json_returns_none():
    with patch("daemon.claude_booster.claude_auth.auth_ok", return_value=True), \
         patch("daemon.claude_booster.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_booster.subprocess.run", return_value=_proc("мусор")):
        assert claude_booster.summarize_direct(TRANSCRIPT, META) is None
