"""Sinks — модули, в которые отправляется готовый фоллоу-ап."""
from daemon.sinks.base import BaseSink, SinkResult
from daemon.sinks.markdown import MarkdownSink
from daemon.sinks.telegram import TelegramSink
from daemon.sinks.obsidian import ObsidianSink
from daemon.sinks.notion import NotionSink

__all__ = [
    "BaseSink",
    "SinkResult",
    "MarkdownSink",
    "TelegramSink",
    "ObsidianSink",
    "NotionSink",
]
