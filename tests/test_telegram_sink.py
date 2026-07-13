"""Тесты TelegramSink с мокнутым telegram.Bot — без сети."""
from datetime import datetime

import telegram

from daemon.sinks.telegram import TelegramSink


class _FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class _FakeBot:
    """Заглушка telegram.Bot: копит отправленные сообщения, сети не трогает."""

    def __init__(self, token: str):
        self.token = token
        self.sent: list[str] = []

    async def send_message(self, *, chat_id, text, parse_mode=None,
                            disable_web_page_preview=None):
        self.sent.append(text)
        return _FakeMessage(len(self.sent))


def test_telegram_sink_sends_via_mocked_bot(monkeypatch):
    monkeypatch.setattr(telegram, "Bot", _FakeBot)

    sink = TelegramSink(bot_token="tok", chat_id="123")
    res = sink.send("# hi\n\nсодержимое", title="встреча",
                    when=datetime(2026, 7, 13, 14, 30), session_id="abc")

    assert res.success
    assert res.detail.startswith("messages=")


def test_telegram_sink_not_configured_fails_without_network():
    sink = TelegramSink(bot_token="", chat_id="")
    res = sink.send("текст", title="встреча", when=datetime(2026, 7, 13, 14, 30),
                    session_id="abc")
    assert not res.success


def test_telegram_sink_empty_text_fails():
    sink = TelegramSink(bot_token="tok", chat_id="123")
    res = sink.send("", title="встреча", when=datetime(2026, 7, 13, 14, 30),
                    session_id="abc")
    assert not res.success


def test_telegram_sink_splits_long_message(monkeypatch):
    monkeypatch.setattr(telegram, "Bot", _FakeBot)

    captured: list[_FakeBot] = []
    orig_init = _FakeBot.__init__

    def _capture_init(self, token):
        orig_init(self, token)
        captured.append(self)

    monkeypatch.setattr(_FakeBot, "__init__", _capture_init)

    sink = TelegramSink(bot_token="tok", chat_id="123")
    long_text = "\n\n".join(f"параграф {i} " + "x" * 200 for i in range(40))
    res = sink.send(long_text, title="встреча", when=datetime(2026, 7, 13, 14, 30),
                    session_id="abc")

    assert res.success
    message_ids = res.detail.replace("messages=", "").split(",")
    assert len(message_ids) > 1
    assert all(len(chunk) <= 4096 for chunk in captured[0].sent)
