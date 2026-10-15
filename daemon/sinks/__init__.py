"""Sinks — модули, в которые доставляется готовый (уже отрендеренный) markdown
фоллоу-ап."""
from daemon.sinks.base import BaseSink, SinkResult
from daemon.sinks.note import NoteSink
from daemon.sinks.telegram import TelegramSink

__all__ = [
    "BaseSink",
    "SinkResult",
    "NoteSink",
    "TelegramSink",
]
