"""Sink: записать фоллоу-ап в локальную папку как .md файл."""
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from daemon.sinks.base import SinkResult, render_combined_markdown

if TYPE_CHECKING:
    from daemon.session import Session
    from daemon.summarizer import FollowupResult

logger = logging.getLogger(__name__)


def _slugify(title: str, max_len: int = 80) -> str:
    """Сделать filename-safe slug из заголовка встречи."""
    if not title:
        return "untitled"
    # Оставляем буквы, цифры, дефис и пробел; пробелы → дефисы
    s = re.sub(r"[^\w\s\-а-яА-ЯёЁ]", "", title, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s.strip())
    return s[:max_len] or "untitled"


class MarkdownSink:
    """Сохраняет фоллоу-ап в ~/MeetMind/YYYY-MM-DD/title-HHMM.md"""

    name = "markdown"

    def __init__(self, base_dir: str = "~/MeetMind"):
        self.base_dir = Path(base_dir).expanduser()

    def send(self, followup: "FollowupResult", session: "Session") -> SinkResult:
        try:
            if not followup.has_any:
                return SinkResult(self.name, False, "пустой followup")

            date_dir = self.base_dir / session.start_time.strftime("%Y-%m-%d")
            date_dir.mkdir(parents=True, exist_ok=True)

            slug = _slugify(session.title or "untitled")
            time_str = session.start_time.strftime("%H%M")
            file_path = date_dir / f"{time_str}-{slug}.md"

            content = render_combined_markdown(followup, session)
            file_path.write_text(content, encoding="utf-8")

            logger.info(f"📝 Markdown сохранён: {file_path}")
            return SinkResult(
                self.name, True, detail=str(file_path), url=f"file://{file_path}"
            )
        except Exception as e:
            logger.error(f"MarkdownSink: {e}", exc_info=True)
            return SinkResult(self.name, False, detail=str(e))
