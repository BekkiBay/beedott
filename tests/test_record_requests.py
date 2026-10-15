"""Запись по просьбе Claude (MCP): демон поднимает флаг в status, пишет
приложение (Process Tap). Стоп owner=app — через stop_requested, как у
предохранителей; owner=daemon — сразу."""
import pytest

from daemon.app import DaemonApp
from daemon.control import ControlError
from tests.test_app_owner import _FakeRecorder


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def app(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    a = DaemonApp()
    a._clock = clock
    return a


def test_request_start_sets_flag_in_status(app):
    assert app.status({})["start_requested"] is None
    assert app.request_start({"title": "  Синк  "}) == {"requested": True}
    assert app.status({})["start_requested"] == {"title": "Синк"}


def test_request_start_without_title(app):
    app.request_start({})
    assert app.status({})["start_requested"] == {"title": None}


def test_start_request_expires(app, clock):
    app.request_start({})
    clock.now += 8.1
    assert app.status({})["start_requested"] is None


def test_start_recording_clears_request(app):
    app.request_start({"title": "Синк"})
    app.start_recording({"audio_path_owner": "app", "title": "Синк", "source": "mcp"})
    assert app.status({})["start_requested"] is None


def test_failed_start_also_clears_request(app):
    app.request_start({})
    with pytest.raises(ControlError):
        app.start_recording({"audio_path_owner": "cloud"})
    assert app.status({})["start_requested"] is None


def test_request_start_while_recording_is_busy(app):
    app.start_recording({"audio_path_owner": "app"})
    with pytest.raises(ControlError) as exc:
        app.request_start({})
    assert exc.value.code == "busy"


def test_request_stop_owner_app_sets_flag(app):
    sid = app.start_recording({"audio_path_owner": "app"})["session_id"]
    assert app.request_stop({}) == {"session_id": sid, "requested": True}
    assert app.status({})["stop_requested"] == "mcp"


def test_request_stop_keeps_guard_reason(app):
    app.start_recording({"audio_path_owner": "app"})
    app._stop_request = "duration_limit"
    app.request_stop({})
    assert app.status({})["stop_requested"] == "duration_limit"


def test_request_stop_owner_daemon_stops_now(app, monkeypatch):
    _FakeRecorder.instances = []
    monkeypatch.setattr("daemon.app.Recorder", _FakeRecorder)
    sid = app.start_recording({"title": "x"})["session_id"]
    assert app.request_stop({})["session_id"] == sid
    assert app.status({})["recording"] is None


def test_request_stop_without_recording(app):
    with pytest.raises(ControlError) as exc:
        app.request_stop({})
    assert exc.value.code == "no_active_session"
