from daemon.paths import app_dir, session_dir


def test_app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path / "mm"))
    d = app_dir()
    assert d.exists() and d.name == "mm"


def test_session_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path / "mm"))
    s = session_dir("abc123")
    assert s == app_dir() / "sessions" / "abc123" and s.exists()
