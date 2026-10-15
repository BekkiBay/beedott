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


@pytest.fixture()
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr("daemon.app.notify", lambda title, msg, *a, **k: sent.append(msg))
    return sent


def test_request_stop_owner_daemon_notifies_user(app, monkeypatch, notes):
    """owner=daemon — Swift не следит за stop_requested (это только для
    owner=app), так что без уведомления здесь пользователь не узнал бы,
    что запись остановил именно Claude."""
    monkeypatch.setattr("daemon.app.Recorder", _FakeRecorder)
    sid = app.start_recording({"audio_path_owner": "daemon"})["session_id"]

    result = app.request_stop({})

    assert result == {"session_id": sid}
    assert any("просьбе Claude" in m for m in notes)


def test_request_stop_owner_daemon_cancelled_recording_skips_mcp_notice(app, monkeypatch, notes):
    """Слишком короткая запись — stop_recording её отменяет и уже уведомляет
    об этом сам; «остановлена по просьбе Claude» здесь было бы неправдой."""
    monkeypatch.setenv("MEETMIND_MIN_RECORDING_SEC", "10")
    monkeypatch.setattr("daemon.app.Recorder", _FakeRecorder)
    sid = app.start_recording({"audio_path_owner": "daemon"})["session_id"]

    result = app.request_stop({})

    assert result == {"session_id": sid, "cancelled": "too_short"}
    assert not any("просьбе Claude" in m for m in notes)
    assert any("короче 10" in m for m in notes)


def test_request_stop_owner_app_does_not_notify_here(app, notes):
    """owner=app — приложение само зовёт stop_recording и само уведомляет
    (MeetMindApp.swift); демон здесь только поднимает флаг."""
    app.start_recording({"audio_path_owner": "app"})
    app.request_stop({})
    assert notes == []
