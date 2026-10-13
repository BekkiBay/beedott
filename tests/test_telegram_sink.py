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


def test_telegram_sink_sends_via_mocked_bot(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
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


def test_telegram_sink_splits_long_message(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
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


# ───────────── аудит T5: HTML + экранирование, нарезка, ретрай без дублей ─────


class _RecordingBot:
    """Заглушка Bot, которая запоминает parse_mode и может упасть на N-м сообщении."""

    sent: list[tuple[str, str]] = []
    fail_on_call: int | None = None
    calls = 0

    def __init__(self, token: str):
        self.token = token

    async def send_message(self, *, chat_id, text, parse_mode=None,
                            disable_web_page_preview=None):
        type(self).calls += 1
        if type(self).fail_on_call == type(self).calls:
            raise RuntimeError("сеть моргнула")
        type(self).sent.append((parse_mode, text))
        return _FakeMessage(type(self).calls)


def _reset_bot(monkeypatch, fail_on_call=None):
    _RecordingBot.sent = []
    _RecordingBot.calls = 0
    _RecordingBot.fail_on_call = fail_on_call
    monkeypatch.setattr(telegram, "Bot", _RecordingBot)


def test_telegram_sink_sends_html_with_escaped_llm_text(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    _reset_bot(monkeypatch)
    md = ("**Контекст**\nполе user_id и 5*3 <b>не тег</b> & ко\n\n"
          "_Запись хранится локально._")

    res = TelegramSink(bot_token="tok", chat_id="1").send(
        md, title="встреча", when=datetime(2026, 7, 13, 14, 30), session_id="s1")

    assert res.success
    parse_mode, text = _RecordingBot.sent[0]
    assert parse_mode == "HTML"
    assert "<b>Контекст</b>" in text
    assert "user_id и 5*3" in text
    assert "&lt;b&gt;не тег&lt;/b&gt; &amp; ко" in text
    assert "<i>Запись хранится локально.</i>" in text


def test_telegram_sink_splits_single_huge_paragraph(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    _reset_bot(monkeypatch)
    md = "**Контекст**\n" + "слово " * 2500  # один абзац ~15 000 символов

    res = TelegramSink(bot_token="tok", chat_id="1").send(
        md, title="встреча", when=datetime(2026, 7, 13, 14, 30), session_id="s1")

    assert res.success
    assert len(_RecordingBot.sent) > 1
    assert all(len(text) <= 4096 for _, text in _RecordingBot.sent)
    assert sum(text.count("слово") for _, text in _RecordingBot.sent) == 2500


def test_telegram_sink_retry_does_not_resend_delivered_parts(tmp_path, monkeypatch):
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    md = "\n\n".join(f"**Раздел {i}**\n" + "текст " * 300 for i in range(6))
    sink = TelegramSink(bot_token="tok", chat_id="1")
    when = datetime(2026, 7, 13, 14, 30)

    _reset_bot(monkeypatch, fail_on_call=2)
    first = sink.send(md, title="встреча", when=when, session_id="s1")
    assert not first.success
    delivered_first = [text for _, text in _RecordingBot.sent]
    assert len(delivered_first) == 1

    _reset_bot(monkeypatch)
    second = sink.send(md, title="встреча", when=when, session_id="s1")
    assert second.success
    resent = [text for _, text in _RecordingBot.sent]
    assert delivered_first[0] not in resent  # первая часть не задублирована
    assert len(second.detail.replace("messages=", "").split(",")) == 1 + len(resent)


def test_telegram_sink_replies_to_previous_version(tmp_path, monkeypatch):
    """Продукт P12: новая версия фоллоу-апа уходит ответом на старое сообщение,
    чтобы в чате было видно, что это обновление той же встречи."""
    from daemon.paths import session_dir
    monkeypatch.setenv("MEETMIND_APPDIR", str(tmp_path))
    (session_dir("s1") / "telegram_reply_to").write_text("555")
    kwargs_seen = []

    class Bot:
        def __init__(self, token):
            pass

        async def send_message(self, **kwargs):
            kwargs_seen.append(kwargs)
            return _FakeMessage(len(kwargs_seen))

    monkeypatch.setattr(telegram, "Bot", Bot)
    md = "\n\n".join(f"**Раздел {i}**\n" + "текст " * 300 for i in range(6))
    res = TelegramSink(bot_token="tok", chat_id="1").send(
        md, title="встреча", when=datetime(2026, 7, 13, 14, 30), session_id="s1")

    assert res.success and len(kwargs_seen) > 1
    reply = kwargs_seen[0]["reply_parameters"]
    assert reply.message_id == 555 and reply.allow_sending_without_reply is True
    assert all("reply_parameters" not in k for k in kwargs_seen[1:])
