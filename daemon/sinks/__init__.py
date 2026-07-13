"""Sinks — модули, в которые доставляется готовый (уже отрендеренный) markdown
фоллоу-ап. daemon/sinks/notion.py — legacy (v2 Protocol), не экспортируется здесь:
его send(followup, session) всё ещё старой формы и не совместим с новым
BaseSink.send(text, *, title, when, session_id); адаптация — отдельная задача."""
from daemon.sinks.base import BaseSink, SinkResult
from daemon.sinks.note import NoteSink
from daemon.sinks.telegram import TelegramSink

__all__ = [
    "BaseSink",
    "SinkResult",
    "NoteSink",
    "TelegramSink",
]
