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
        _write_wav(self.out_path, seconds=2)

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


def _write_wav(path, *, seconds: float, rate: int = 48000):
    import numpy as np
    import soundfile as sf
    frames = int(rate * seconds)
    samples = (np.random.default_rng(0).standard_normal((frames, 2)) * 3000).astype("int16")
    sf.write(str(path), samples, rate, subtype="PCM_16")


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
    # для теста кладём настоящий WAV по audio_path, чтобы имитировать это
    # (пустой/битый файл stop теперь отменяет, см. тесты T3 ниже).
    _write_wav(started["audio_path"], seconds=1)

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


# ───────── аудит T3: владелец записи переживает рестарт демона ───────────────


@pytest.fixture()
def notes(monkeypatch):
    sent = []
    monkeypatch.setattr("daemon.app.notify", lambda title, msg, *a, **k: sent.append(msg))
    return sent


def test_start_owner_app_persists_owner_in_db(app, monkeypatch):
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    sid = app.start_recording({"audio_path_owner": "app"})["session_id"]
    assert _session_row(app, sid)["audio_owner"] == "app"


def test_restarted_daemon_adopts_live_app_recording(app, monkeypatch, notes):
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    started = app.start_recording({"audio_path_owner": "app"})
    sid = started["session_id"]
    _write_wav(started["audio_path"], seconds=1)

    restarted = DaemonApp()  # демон упал и поднялся, Swift продолжает писать
    restarted.startup(db.connect())

    st = restarted.status({})
    assert st["recording"] == sid
    assert st["recording_owner"] == "app"
    assert restarted.stop_recording({}) == {"session_id": sid}
    assert db.get_session(restarted._con, sid)["state"] == State.RECORDED


def test_stop_owner_app_with_empty_wav_cancels_session(app, monkeypatch, notes):
    import numpy as np
    import soundfile as sf
    from daemon.paths import session_dir
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    started = app.start_recording({"audio_path_owner": "app"})
    sid = started["session_id"]
    sf.write(started["audio_path"], np.zeros((0, 2), dtype="int16"), 48000)
    sdir = session_dir(sid)  # session_dir() создаёт каталог — берём путь до стопа

    result = app.stop_recording({})

    assert result == {"session_id": sid, "cancelled": "empty"}
    assert _session_row(app, sid)["state"] == State.CANCELLED
    assert not sdir.exists()
    assert app._active_sid is None
    assert any("пуст" in m for m in notes)


def test_stop_owner_app_repairs_unfinalized_wav_header(app, monkeypatch, notes):
    """Swift упал, не закрыв AVAudioFile: в заголовке нули вместо размеров.
    stop (или финализация брошенной записи) чинит заголовок, а не отменяет."""
    import struct
    import soundfile as sf
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    started = app.start_recording({"audio_path_owner": "app"})
    sid = started["session_id"]
    wav = started["audio_path"]
    _write_wav(wav, seconds=1)
    from pathlib import Path
    raw = bytearray(Path(wav).read_bytes())
    struct.pack_into("<I", raw, 4, 0)
    struct.pack_into("<I", raw, 40, 0)
    Path(wav).write_bytes(bytes(raw))

    assert app.stop_recording({}) == {"session_id": sid}
    assert _session_row(app, sid)["state"] == State.RECORDED
    assert sf.info(wav).frames == 48000


# ───────── аудит T7: предохранители для записи Swift (owner=app) ─────────────


def _quiet_guard(**kw):
    from daemon.app_guard import AppRecordingGuard
    defaults = dict(max_sec=6 * 3600, min_disk_stop_mb=500, silence_min=30,
                    disk_free=lambda p: 10**12, tail_rms=lambda p, s: None)
    defaults.update(kw)
    return AppRecordingGuard(**defaults)


def test_guard_tick_requests_stop_once_on_duration_limit(app, monkeypatch, notes):
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    started = app.start_recording({"audio_path_owner": "app"})
    _write_wav(started["audio_path"], seconds=1)
    app._guard = _quiet_guard(max_sec=0)

    app.guard_tick()
    app.guard_tick()

    assert app.status({})["stop_requested"] == "duration_limit"
    assert len([m for m in notes if "длительност" in m]) == 1
    assert app._active_sid == started["session_id"]  # останавливает Swift, не демон


def test_guard_tick_finalizes_recording_when_app_is_gone(app, monkeypatch, notes):
    import os
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    started = app.start_recording({"audio_path_owner": "app"})
    sid = started["session_id"]
    _write_wav(started["audio_path"], seconds=1)
    os.utime(started["audio_path"], (0, 0))  # файл давно не меняется — Swift мёртв
    app._guard = _quiet_guard(stale_sec=60)

    app.guard_tick()

    row = _session_row(app, sid)
    assert row["state"] == State.RECORDED
    assert row["gap_note"]
    assert app._active_sid is None
    assert app.status({})["stop_requested"] is None


# ───────── продукт P6: случайная короткая запись отменяется сама ─────────────


def test_stop_owner_app_shorter_than_min_cancels(app, monkeypatch, notes):
    monkeypatch.setenv("MEETMIND_MIN_RECORDING_SEC", "10")
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    started = app.start_recording({"audio_path_owner": "app"})
    sid = started["session_id"]
    _write_wav(started["audio_path"], seconds=3)

    assert app.stop_recording({}) == {"session_id": sid, "cancelled": "too_short"}
    assert _session_row(app, sid)["state"] == State.CANCELLED
    assert any("короче 10" in m for m in notes)


def test_stop_owner_daemon_shorter_than_min_cancels(app, monkeypatch, notes):
    monkeypatch.setenv("MEETMIND_MIN_RECORDING_SEC", "10")
    monkeypatch.setattr("daemon.app.Recorder", _FakeRecorder)
    sid = app.start_recording({"audio_path_owner": "daemon"})["session_id"]

    assert app.stop_recording({}) == {"session_id": sid, "cancelled": "too_short"}
    assert _session_row(app, sid)["state"] == State.CANCELLED


def test_stop_long_enough_recording_is_processed(app, monkeypatch, notes):
    monkeypatch.setenv("MEETMIND_MIN_RECORDING_SEC", "10")
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    started = app.start_recording({"audio_path_owner": "app"})
    _write_wav(started["audio_path"], seconds=11, rate=8000)
    assert app.stop_recording({}) == {"session_id": started["session_id"]}


def test_notifications_pop_handler_and_session_link(app, monkeypatch):
    """Уведомление предохранителя несёт session_id — клик в приложении
    открывает карточку этой встречи (продукт P16)."""
    from daemon import notifier
    monkeypatch.setattr("daemon.app.Recorder", _BoomRecorder)
    started = app.start_recording({"audio_path_owner": "app"})
    _write_wav(started["audio_path"], seconds=1)
    app._guard = _quiet_guard(max_sec=0)
    assert app.notifications_pop({}) == {"notifications": []}

    app.guard_tick()

    items = app.notifications_pop({})["notifications"]
    assert items and items[0]["session_id"] == started["session_id"]
    assert notifier.pop_for_app() == []


def test_logging_hides_telegram_token_in_http_logs():
    """httpx пишет на INFO полный URL, а в URL Telegram Bot API — токен бота
    (2026-10-12: токен лежал открытым текстом в daemon.log)."""
    import logging
    from daemon.app import _configure_logging
    _configure_logging()
    for name in ("httpx", "httpx2"):
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)
