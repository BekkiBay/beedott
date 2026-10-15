"""Тесты daemon/claude_auth.py — auth-обвязка headless-вызовов claude CLI.

PLAN_v3 Task 7.4: env очищен от ANTHROPIC_*-ключей (иначе -p режим молча
биллит API-кредиты вместо Max-подписки); токен инъектится; auth_ok маппит
exit code subprocess-а.
"""
import subprocess
from unittest.mock import patch

from daemon import claude_auth


# ─────────────────────────────── claude_bin ──────────────────────────────────


def test_claude_bin_env_override(monkeypatch):
    monkeypatch.setenv("MEETMIND_CLAUDE_BIN", "/opt/custom/claude")
    assert claude_auth.claude_bin() == "/opt/custom/claude"


def test_claude_bin_which_fallback(monkeypatch):
    monkeypatch.delenv("MEETMIND_CLAUDE_BIN", raising=False)
    with patch("daemon.claude_auth.shutil.which", return_value="/usr/local/bin/claude"):
        assert claude_auth.claude_bin() == "/usr/local/bin/claude"


def test_claude_bin_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("MEETMIND_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # пустой дом: типовых установок нет
    with patch("daemon.claude_auth.shutil.which", return_value=None):
        assert claude_auth.claude_bin() is None


def _fake_bin(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_claude_bin_finds_newest_nvm_install_outside_path(monkeypatch, tmp_path):
    """Демон из .app получает урезанный PATH без nvm (2026-10-12: саммари трёх
    встреч встали — claude стоял в ~/.nvm/versions/node/v26.1.0/bin)."""
    monkeypatch.delenv("MEETMIND_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    nvm = tmp_path / ".nvm" / "versions" / "node"
    _fake_bin(nvm / "v9.11.2" / "bin" / "claude")
    newest = _fake_bin(nvm / "v26.1.0" / "bin" / "claude")
    with patch("daemon.claude_auth.shutil.which", return_value=None):
        assert claude_auth.claude_bin() == str(newest)


def test_claude_bin_finds_native_installer_location(monkeypatch, tmp_path):
    monkeypatch.delenv("MEETMIND_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    native = _fake_bin(tmp_path / ".local" / "bin" / "claude")
    with patch("daemon.claude_auth.shutil.which", return_value=None):
        assert claude_auth.claude_bin() == str(native)


def test_claude_bin_finds_homebrew_install(monkeypatch, tmp_path):
    """Homebrew ставит claude в /opt/homebrew/bin — мимо PATH демона и мимо
    nvm/~/.local; проверяем раньше них (подставляем путь через monkeypatch,
    не трогая реальный /opt/homebrew)."""
    monkeypatch.delenv("MEETMIND_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    brew = _fake_bin(tmp_path / "homebrew-bin" / "claude")
    monkeypatch.setattr(claude_auth, "_HOMEBREW_BIN", brew)
    with patch("daemon.claude_auth.shutil.which", return_value=None):
        assert claude_auth.claude_bin() == str(brew)


def test_claude_bin_finds_usr_local_install(monkeypatch, tmp_path):
    monkeypatch.delenv("MEETMIND_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    usr_local = _fake_bin(tmp_path / "usr-local-bin" / "claude")
    monkeypatch.setattr(claude_auth, "_HOMEBREW_BIN", tmp_path / "no-homebrew" / "claude")
    monkeypatch.setattr(claude_auth, "_USR_LOCAL_BIN", usr_local)
    with patch("daemon.claude_auth.shutil.which", return_value=None):
        assert claude_auth.claude_bin() == str(usr_local)


# ─────────────────────────────── claude_env ──────────────────────────────────


def test_claude_env_strips_anthropic_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = claude_auth.claude_env(None)
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"


def test_claude_env_injects_oauth_token(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    env = claude_auth.claude_env("oauth-123")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-123"


def test_claude_env_no_token_no_key(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    env = claude_auth.claude_env(None)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


# ─────────────────────────────── auth_ok ─────────────────────────────────────


def _run_result(code: int):
    return subprocess.CompletedProcess(args=[], returncode=code)


def test_auth_ok_true():
    with patch("daemon.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_auth.subprocess.run", return_value=_run_result(0)):
        assert claude_auth.auth_ok(None) is True


def test_auth_ok_false_on_nonzero_exit():
    with patch("daemon.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_auth.subprocess.run", return_value=_run_result(1)):
        assert claude_auth.auth_ok(None) is False


def test_auth_ok_false_without_binary():
    with patch("daemon.claude_auth.claude_bin", return_value=None):
        assert claude_auth.auth_ok(None) is False


def test_auth_ok_false_on_timeout():
    with patch("daemon.claude_auth.claude_bin", return_value="/bin/claude"), \
         patch("daemon.claude_auth.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=30)):
        assert claude_auth.auth_ok(None) is False
