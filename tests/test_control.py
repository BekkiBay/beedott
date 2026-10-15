"""Тесты control-сокета: сервер в потоке, синхронный клиент."""
import shutil
import tempfile
import time

import pytest

from daemon.control import (ControlError, ControlServer, DaemonError,
                            DaemonUnavailable, request)


@pytest.fixture()
def server(monkeypatch):
    # НЕ pytest tmp_path: unix socket path ограничен ~104 байтами на macOS
    short_dir = tempfile.mkdtemp(prefix="mm", dir="/tmp")
    monkeypatch.setenv("MEETMIND_APPDIR", short_dir)

    def boom(params):
        raise ControlError("busy", "уже пишем")

    srv = ControlServer({
        "health": lambda params: {"ok": True},
        "echo": lambda params: {"got": params},
        "boom": boom,
        "slow": lambda params: time.sleep(0.5) or {},
    })
    srv.start()
    yield srv
    shutil.rmtree(short_dir, ignore_errors=True)


def test_roundtrip(server):
    assert request("health") == {"ok": True}
    assert request("echo", {"x": 1}) == {"got": {"x": 1}}


def test_unknown_method(server):
    with pytest.raises(RuntimeError, match="unknown_method"):
        request("nope")


def test_control_error(server):
    with pytest.raises(RuntimeError, match="busy"):
        request("boom")


def test_no_socket_is_daemon_unavailable(monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", tempfile.mkdtemp(prefix="mm", dir="/tmp"))
    with pytest.raises(DaemonUnavailable, match="демон не запущен"):
        request("health")


def test_error_reply_is_daemon_error_with_code(server):
    with pytest.raises(DaemonError) as exc:
        request("boom")
    assert exc.value.code == "busy"
    assert exc.value.message == "уже пишем"
    assert isinstance(exc.value, RuntimeError)
    assert str(exc.value) == "busy: уже пишем"  # прежний текст для cli.py


def test_timeout_is_daemon_unavailable(server):
    with pytest.raises(DaemonUnavailable):
        request("slow", timeout=0.1)


def test_large_payload_roundtrips(server):
    # ~100 KB — update_followup с длинной расшифровкой в контексте; дефолтный
    # лимит StreamReader (64 KiB) рвал бы такой запрос на limit exceeded.
    big = "x" * 100_000
    assert request("echo", {"text": big}) == {"got": {"text": big}}
