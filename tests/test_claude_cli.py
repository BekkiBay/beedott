"""Тесты daemon/claude_cli.py — саммари через `claude -p` (Max-подписка).

Транскрипт уходит в claude CLI (headless --print), обратно ждём JSON схемы
SPEC_v3 §7. Любой сбой — исключение с причиной (стадия → failed_retry),
проблемы доступа — SummaryAuthError (stages.summarize шлёт уведомление).

subprocess мокается — реальный claude CLI в тестах не зовём.
"""
import json
import subprocess
from unittest.mock import patch

import pytest

from daemon import claude_cli
from daemon.summarizer import SummaryAuthError
from tests.conftest import GOOD_FOLLOWUP as GOOD

META = {"title": "Тест", "duration_min": 10, "attendees": ["Аня"]}
TRANSCRIPT = "Спикер 1: привет\nСпикер 2: обсудим релиз"


def _proc(stdout: str, returncode: int = 0, stderr: bytes = b""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout.encode(), stderr=stderr)


def _logged_in(**run_kwargs):
    """Контекст «claude найден и залогинен» + мок subprocess.run."""
    return (patch("daemon.claude_cli.claude_auth.claude_bin", return_value="/bin/claude"),
            patch("daemon.claude_cli.claude_auth.auth_ok", return_value=True),
            patch("daemon.claude_cli.subprocess.run", **run_kwargs))


def test_returns_validated_followup():
    a, b, c = _logged_in(return_value=_proc(json.dumps(GOOD, ensure_ascii=False)))
    with a, b, c:
        assert claude_cli.summarize(TRANSCRIPT, META) == GOOD


def test_strips_markdown_fence():
    fenced = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    a, b, c = _logged_in(return_value=_proc(fenced))
    with a, b, c:
        assert claude_cli.summarize(TRANSCRIPT, META)["decisions"] == GOOD["decisions"]


def test_prompt_goes_to_stdin_with_print_flags():
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs["input"].decode("utf-8")
        captured["env"] = kwargs["env"]
        return _proc(json.dumps(GOOD, ensure_ascii=False))

    a, b, c = _logged_in(side_effect=fake_run)
    with a, b, c, patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-secret"}):
        claude_cli.summarize(TRANSCRIPT, META, model="opus")

    cmd = captured["cmd"]
    assert cmd[0] == "/bin/claude"
    assert "--print" in cmd
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert "--append-system-prompt" in cmd
    assert "--bare" not in cmd  # --bare не читает CLAUDE_CODE_OAUTH_TOKEN
    # без плагинов/хуков/CLAUDE.md пользователя (иначе, например, caveman-хук
    # перепишет стиль фоллоу-апа) и без мусора в истории сессий его аккаунта
    assert "--safe-mode" in cmd
    assert "--no-session-persistence" in cmd
    assert TRANSCRIPT in captured["input"]
    assert "Аня" in captured["input"]  # участники из календаря
    # API-ключ не должен утечь в CLI — иначе тихий биллинг API вместо подписки
    assert "ANTHROPIC_API_KEY" not in captured["env"]


def test_invalid_json_raises_value_error():
    a, b, c = _logged_in(return_value=_proc("Вот фоллоу-ап: всё хорошо"))
    with a, b, c, pytest.raises(ValueError, match="невалидный JSON"):
        claude_cli.summarize(TRANSCRIPT, META)


def test_nonzero_exit_raises_with_stderr():
    a, b, c = _logged_in(return_value=_proc("", 1, b"rate limited"))
    with a, b, c, pytest.raises(RuntimeError, match="rate limited"):
        claude_cli.summarize(TRANSCRIPT, META)


def test_timeout_raises():
    a, b, c = _logged_in(side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1))
    with a, b, c, pytest.raises(RuntimeError, match="не ответил"):
        claude_cli.summarize(TRANSCRIPT, META)


def test_not_logged_in_raises_auth_error_without_running():
    with patch("daemon.claude_cli.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_cli.claude_auth.auth_ok", return_value=False), \
         patch("daemon.claude_cli.subprocess.run") as run, \
         pytest.raises(SummaryAuthError, match="не залогинен"):
        claude_cli.summarize(TRANSCRIPT, META)
    run.assert_not_called()


def test_missing_binary_raises_auth_error():
    with patch("daemon.claude_cli.claude_auth.claude_bin", return_value=None), \
         pytest.raises(SummaryAuthError, match="MEETMIND_CLAUDE_BIN"):
        claude_cli.summarize(TRANSCRIPT, META)
