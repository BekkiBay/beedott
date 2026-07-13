"""Все пути данных приложения. env MEETMIND_APPDIR — override для тестов."""
import os
from pathlib import Path


def app_dir() -> Path:
    d = Path(os.environ.get("MEETMIND_APPDIR",
        Path.home() / "Library" / "Application Support" / "MeetMind"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path: return app_dir() / "meetmind.db"
def socket_path() -> Path: return app_dir() / "control.sock"


def session_dir(session_id: str) -> Path:
    d = app_dir() / "sessions" / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d
