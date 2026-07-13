"""Тесты control-сокета: сервер в потоке, синхронный клиент."""
import tempfile

import pytest

from daemon.control import ControlError, ControlServer, request


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
    })
    srv.start()
    return srv


def test_roundtrip(server):
    assert request("health") == {"ok": True}
    assert request("echo", {"x": 1}) == {"got": {"x": 1}}


def test_unknown_method(server):
    with pytest.raises(RuntimeError, match="unknown_method"):
        request("nope")


def test_control_error(server):
    with pytest.raises(RuntimeError, match="busy"):
        request("boom")
