"""Notifier / notify(): экранирование AppleScript-строк (SPEC_v3 §10, PLAN_v3
Task 2.3). Баг v2: title/message подставлялись в f-строку без экранирования —
кавычка внутри названия встречи ломала osascript-скрипт. Здесь subprocess
мокается (реальный osascript не запускается), проверяем ИМЕННО итоговую
строку скрипта, переданную в subprocess.run.
"""
import subprocess
from datetime import datetime

from daemon import notifier
from daemon.notifier import Notifier, _escape_applescript, notify
from daemon.session import Session


def _fake_run_factory(calls, returncode=0, stdout=""):
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode, stdout=stdout)

    return fake_run


# ------------------------------------------------- _escape_applescript ---

def test_escape_applescript_quotes():
    assert _escape_applescript('he said "hi"') == 'he said \\"hi\\"'


def test_escape_applescript_backslash():
    assert _escape_applescript("a\\b") == "a\\\\b"


def test_escape_applescript_order_backslash_before_quotes():
    # Если бы порядок был обратным, экранированная кавычка сама попала бы
    # под второй replace и получила бы лишний бэкслеш.
    assert _escape_applescript('\\"') == '\\\\\\"'


def test_escape_applescript_leaves_emoji_untouched():
    assert _escape_applescript("готово 🚀") == "готово 🚀"


# ------------------------------------------------------------- notify() ---

def test_notify_escapes_quotes_backslash_and_emoji(monkeypatch):
    calls = []
    monkeypatch.setattr(notifier.subprocess, "run", _fake_run_factory(calls))

    title = 'Meeting "Q3" \\ review 🚀'
    message = 'Заметка "готова" \\ emoji 😀'

    ok = notify(title, message)

    assert ok is True
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "osascript"
    assert args[1] == "-e"
    script = args[2]

    expected_title = _escape_applescript(title)
    expected_message = _escape_applescript(message)
    assert f'with title "{expected_title}"' in script
    assert f'display notification "{expected_message}"' in script
    # необработанная (неэкранированная) кавычка из исходного title/message
    # не должна встретиться "голой" в скрипте
    assert '"Q3"' not in script
    assert '"готова"' not in script
    # эмодзи проходит как есть — экранированию не подлежит
    assert "🚀" in script and "😀" in script
    assert kwargs.get("check") is True


def test_notify_includes_subtitle_when_given(monkeypatch):
    calls = []
    monkeypatch.setattr(notifier.subprocess, "run", _fake_run_factory(calls))

    notify("T", "M", subtitle='sub "quoted"')

    script = calls[0][0][2]
    assert 'subtitle "sub \\"quoted\\""' in script


def test_notify_omits_subtitle_when_not_given(monkeypatch):
    calls = []
    monkeypatch.setattr(notifier.subprocess, "run", _fake_run_factory(calls))

    notify("T", "M")

    script = calls[0][0][2]
    assert "subtitle" not in script


def test_notify_returns_false_on_called_process_error(monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(notifier.subprocess, "run", fake_run)
    assert notify("T", "M") is False


def test_notify_returns_false_on_timeout(monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    monkeypatch.setattr(notifier.subprocess, "run", fake_run)
    assert notify("T", "M") is False


# -------------------------------------------------- Notifier (Session) ---

def _session(title: str) -> Session:
    return Session(title=title, start_time=datetime(2026, 7, 13, 15, 0))


def test_notify_recording_started_escapes_title(monkeypatch):
    calls = []
    monkeypatch.setattr(notifier.subprocess, "run", _fake_run_factory(calls))

    Notifier.notify_recording_started(_session('1:1 "Bekzod" \\ sync'))

    script = calls[0][0][2]
    assert '\\"Bekzod\\"' in script
    assert '\\\\ sync' in script
    assert '"1:1 "Bekzod"' not in script  # неэкранированная кавычка не просочилась


def test_notify_done_includes_escaped_title_and_subtitle(monkeypatch):
    calls = []
    monkeypatch.setattr(notifier.subprocess, "run", _fake_run_factory(calls))

    Notifier.notify_done(_session('Демо "релиза"'))

    script = calls[0][0][2]
    assert '\\"релиза\\"' in script
    assert 'subtitle "Отправлено в Telegram"' in script


def test_notify_upcoming_escapes_dialog_title_and_accepts_go(monkeypatch):
    calls = []
    monkeypatch.setattr(
        notifier.subprocess, "run",
        _fake_run_factory(calls, returncode=0, stdout="button returned:ГО"),
    )
    monkeypatch.setattr(Notifier, "_open_meet_url", staticmethod(lambda url: None))

    session = _session('Sync "weekly" review')
    result = Notifier.notify_upcoming(session)

    assert result is True
    script = calls[0][0][2]
    assert '\\"weekly\\"' in script
    assert '"Sync "weekly" review' not in script


def test_notify_upcoming_returns_false_when_cancelled(monkeypatch):
    calls = []
    monkeypatch.setattr(
        notifier.subprocess, "run",
        _fake_run_factory(calls, returncode=0, stdout="button returned:ОТМЕН"),
    )

    session = _session("Sync")
    result = Notifier.notify_upcoming(session)

    assert result is False
    assert session.status == "cancelled"


# ───── продукт P16: пока приложение опрашивает демон, уведомления — через него ─


def test_notify_goes_to_app_queue_while_app_polls(monkeypatch):
    from daemon import notifier
    calls = []
    monkeypatch.setattr(notifier.subprocess, "run", lambda *a, **k: calls.append(a))

    assert notifier.pop_for_app() == []  # приложение вышло на связь
    assert notifier.notify("MeetMind", "Встреча обработана", session_id="abc") is True

    assert calls == []  # osascript не звали
    items = notifier.pop_for_app()
    assert [(i["title"], i["message"], i["session_id"]) for i in items] == [
        ("MeetMind", "Встреча обработана", "abc")]
    assert notifier.pop_for_app() == []  # каждое — ровно один раз


def test_notify_falls_back_to_osascript_when_app_is_silent(monkeypatch):
    from daemon import notifier
    calls = []
    monkeypatch.setattr(notifier.subprocess, "run", lambda *a, **k: calls.append(a))
    now = [1000.0]
    monkeypatch.setattr(notifier, "_clock", lambda: now[0])

    notifier.pop_for_app()
    now[0] += notifier.APP_POLL_TTL_SEC + 1  # приложение перестало опрашивать
    notifier.notify("MeetMind", "текст")

    assert len(calls) == 1
