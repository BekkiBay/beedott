"""Общие фикстуры для тестов MeetMind."""

# Эталонный валидный followup-объект (схема SPEC_v3 §7).
# Используется в test_summarizer.py и test_render.py.
GOOD_FOLLOWUP = {
    "context": "x",
    "discussed": [],
    "decisions": ["решили"],
    "action_items": [{"task": "сделать", "owner": "Bekzod", "deadline": None}],
    "open_questions": [],
}


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _short_min_recording(monkeypatch):
    """Продукт P6: записи короче MEETMIND_MIN_RECORDING_SEC (по умолчанию 10 с)
    отменяются. В тестах порог 1 с, чтобы фикстуры WAV были маленькими;
    тесты самого порога выставляют его явно."""
    monkeypatch.setenv("MEETMIND_MIN_RECORDING_SEC", "1")


@pytest.fixture(autouse=True)
def _reset_notification_hub():
    """Продукт P16: очередь уведомлений для приложения — состояние модуля;
    сбрасываем, чтобы «приложение на связи» не протекало между тестами."""
    from daemon import notifier
    notifier._reset_hub()
    yield
    notifier._reset_hub()


@pytest.fixture(autouse=True)
def _isolated_appdir(tmp_path, monkeypatch):
    """Каждый тест — со своей папкой приложения: иначе load_instructions()
    прочитает настоящий followup_instructions.md пользователя. Тесты со своей
    фикстурой appdir ставят MEETMIND_APPDIR позже и переопределяют этот."""
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path / "isolated-app"))


@pytest.fixture(autouse=True)
def _isolated_claude_desktop_config(tmp_path, monkeypatch):
    """daemon.setup claude-* и status читают конфиг Claude Desktop — в тестах
    только временный файл, настоящий конфиг пользователя не трогаем."""
    monkeypatch.setenv("MEETMIND_CLAUDE_DESKTOP_CONFIG",
                       str(tmp_path / "claude-desktop" / "claude_desktop_config.json"))
