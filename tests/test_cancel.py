"""Тест 1 из PLAN_v3 Task 2.2 (cancel-флоу): cancel во время RECORDING.

Чистая логика на уровне db + states + файлов, без запуска DaemonApp/сокета
(control-слой правится параллельно в daemon/app.py). Проверяем инвариант
SPEC_v3 states.py: may_delete_files() истинно только для DELIVERED/CANCELLED —
удаление аудио допустимо ТОЛЬКО после перехода в CANCELLED, не раньше.
"""
import shutil

from daemon import db
from daemon.paths import session_dir
from daemon.states import State, may_delete_files


def test_cancel_during_recording_discards_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    con = db.connect()

    sid = db.create_session(con, source="manual", title="Встреча под отмену")
    db.set_state(con, sid, State.RECORDING)

    sdir = session_dir(sid)
    audio = sdir / "audio.wav"
    audio.write_bytes(b"fake-audio-bytes")
    assert audio.exists()

    # пока RECORDING — файлы удалять нельзя (инвариант: удаление только
    # в DELIVERED/CANCELLED)
    state_before = State(db.get_session(con, sid)["state"])
    assert state_before == State.RECORDING
    assert not may_delete_files(state_before)

    # cancel: RECORDING -> CANCELLED (разрешённый переход из любого
    # нетерминального состояния, см. states.ALLOWED)
    db.set_state(con, sid, State.CANCELLED)

    state_after = State(db.get_session(con, sid)["state"])
    assert state_after == State.CANCELLED
    assert may_delete_files(state_after)

    # теперь удаление разрешено инвариантом — сносим session_dir целиком
    shutil.rmtree(sdir, ignore_errors=True)

    assert not sdir.exists()
    assert not audio.exists()

    # никаких доставок не произошло — сессию отменили до deliver-стадии
    deliveries = con.execute(
        "SELECT * FROM deliveries WHERE session_id=?", (sid,)).fetchall()
    assert deliveries == []

    session = db.get_session(con, sid)
    assert session["state"] == State.CANCELLED
