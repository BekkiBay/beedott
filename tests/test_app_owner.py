"""Тесты PLAN_v3 Task 4.1-4.2 (audio_path_owner): DaemonApp-хендлеры без сокета.

audio_path_owner="app" — Swift CaptureEngine (Process Tap) пишет WAV сам; демон
только заводит sessions-строку/каталог и отдаёт audio_path, Python Recorder
НИКОГДА не создаётся в этом режиме. Проверяем это жёстко: Recorder monkeypatch-ится
на класс, который падает при инстанцировании — если бы app.py всё-таки попытался
создать Recorder для owner=app, тест уронил бы AssertionError.

DaemonApp вызывается напрямую (без ControlServer/unix-сокета — путь сокета на
macOS ограничен ~104 байтами, а он тут и не нужен: методы-хендлеры — обычные
синхронные функции).
"""
import pytest

from daemon import db
from daemon.app import DaemonApp
from daemon.control import ControlError
from daemon.states import State


class _BoomRecorder:
    """Замена daemon.app.Recorder — падает при любой попытке создать инстанс.
    Используется в owner=app тестах: если бы Recorder всё-таки создавался,
    тест немедленно провалился бы с AssertionError."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "Recorder не должен создаваться при audio_path_owner=app")


class _FakeRecorder:
    """Замена daemon.app.Recorder для owner=daemon тестов — без реального аудио-
    устройства/TCC (сравни с tests/test_recorder.py::test_streams_to_disk,
    помечен @pytest.mark.hardware и не входит в дефолтный прогон)."""

    instances: list["_FakeRecorder"] = []

    def __init__(self, *, out_path, on_autostop=None, on_silence=None):
        self.out_path = out_path
        self.on_autostop = on_autostop
        self.on_silence = on_silence
        self.started = False
        self.stopped = False
        _FakeRecorder.instances.append(self)

    def start(self):
        self.started = True
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.write_bytes(b"RIFF....WAVEfake")

    def stop(self):
        self.stopped = True
        return str(self.out_path)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    _FakeRecorder.instances = []
    return DaemonApp()


def _session_row(app, sid):
    return db.get_session(app._con, sid)


# --------------------------------------------------------------- owner=app ---

def test_start_recording_owner_app_creates_no_recorder(app, monkeypatch):
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)

    result = app.start_recording({"audio_path_owner": "app", "title": "Standup"})

    assert "session_id" in result
    assert "audio_path" in result
    sid = result["session_id"]

    assert app._recorder is None
    assert app._active_sid == sid
    assert app._owner == "app"

    row = _session_row(app, sid)
    assert row["state"] == State.RECORDING
    assert row["audio_path"] == result["audio_path"]
    assert row["audio_path"].endswith("audio.wav")


def test_start_recording_owner_app_default_param_is_daemon(app, monkeypatch):
    # audio_path_owner отсутствует/None -> дефолт "daemon" (SPEC_v3 §6)
    monkeypatch.setattr("daemon.app.Recorder", _FakeRecorder)
    result = app.start_recording({"title": "no owner param"})
    assert app._owner == "daemon"
    assert _FakeRecorder.instances  # Recorder создан для дефолтного пути
    assert "audio_path" not in result  # только owner=app возвращает audio_path


def test_start_recording_bad_owner_value_rejected(app, monkeypatch):
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    with pytest.raises(ControlError) as exc:
        app.start_recording({"audio_path_owner": "cloud"})
    assert exc.value.code == "bad_params"
    # ничего не должно было создаться
    assert app._active_sid is None


def test_stop_recording_owner_app_finalizes_without_recorder(app, monkeypatch):
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    started = app.start_recording({"audio_path_owner": "app"})
    sid = started["session_id"]

    # Swift CaptureEngine уже дописал WAV и сам его закрыл до stop_recording —
    # для теста кладём непустой файл по audio_path, чтобы имитировать это.
    audio_path = started["audio_path"]
    from pathlib import Path
    Path(audio_path).write_bytes(b"RIFF....WAVEfake-app-audio")

    result = app.stop_recording({})
    assert result == {"session_id": sid}

    assert app._active_sid is None
    assert app._recorder is None
    assert app._owner is None

    row = _session_row(app, sid)
    assert row["state"] == State.RECORDED
    assert row["ended_at"] is not None

    jobs = app._con.execute(
        "SELECT * FROM pipeline_jobs WHERE session_id=?", (sid,)).fetchall()
    stages = {j["stage"]: j["status"] for j in jobs}
    assert stages.get("transcribe") == "queued"


def test_cancel_recording_owner_app_no_recorder_touched(app, monkeypatch):
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    started = app.start_recording({"audio_path_owner": "app", "title": "cancel me"})
    sid = started["session_id"]
    from daemon.paths import session_dir
    sdir = session_dir(sid)
    assert sdir.exists()

    result = app.cancel_recording({})
    assert result == {"session_id": sid}

    assert app._active_sid is None
    assert app._owner is None
    assert not sdir.exists()

    row = _session_row(app, sid)
    assert row["state"] == State.CANCELLED


def test_stop_recording_owner_app_wrong_session_id_errors(app, monkeypatch):
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    app.start_recording({"audio_path_owner": "app"})
    with pytest.raises(ControlError) as exc:
        app.stop_recording({"session_id": "not-the-active-one"})
    assert exc.value.code == "no_active_session"


def test_second_start_while_owner_app_active_is_busy(app, monkeypatch):
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    app.start_recording({"audio_path_owner": "app"})
    with pytest.raises(ControlError) as exc:
        app.start_recording({"audio_path_owner": "app"})
    assert exc.value.code == "busy"


# ------------------------------------------------------------ owner=daemon ---

def test_stop_recording_owner_daemon_calls_recorder_stop(app, monkeypatch):
    monkeypatch.setattr("daemon.app.Recorder", _FakeRecorder)
    started = app.start_recording({"audio_path_owner": "daemon", "title": "normal"})
    sid = started["session_id"]
    assert len(_FakeRecorder.instances) == 1
    fake = _FakeRecorder.instances[0]
    assert fake.started is True

    result = app.stop_recording({})
    assert result == {"session_id": sid}
    assert fake.stopped is True

    row = _session_row(app, sid)
    assert row["state"] == State.RECORDED


def test_cancel_recording_owner_daemon_calls_recorder_stop(app, monkeypatch):
    monkeypatch.setattr("daemon.app.Recorder", _FakeRecorder)
    started = app.start_recording({"audio_path_owner": "daemon"})
    sid = started["session_id"]
    fake = _FakeRecorder.instances[0]

    from daemon.paths import session_dir
    sdir = session_dir(sid)

    result = app.cancel_recording({})
    assert result == {"session_id": sid}
    assert fake.stopped is True
    assert not sdir.exists()

    row = _session_row(app, sid)
    assert row["state"] == State.CANCELLED
