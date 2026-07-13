"""Sink: копировать фоллоу-ап в Obsidian vault."""
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from daemon.sinks.base import SinkResult, render_combined_markdown
from daemon.sinks.markdown import _slugify

if TYPE_CHECKING:
    from daemon.session import Session
    from daemon.summarizer import FollowupResult

logger = logging.getLogger(__name__)


class ObsidianSink:
    """Сохранить .md в vault Obsidian. По сути MarkdownSink с другим корнем."""

    name = "obsidian"

    def __init__(self, vault_path: str = "", subfolder: str = "Meetings"):
        self.vault = Path(vault_path).expanduser() if vault_path else None
        self.subfolder = subfolder

    def send(self, followup: "FollowupResult", session: "Session") -> SinkResult:
        if self.vault is None or not str(self.vault):
            return SinkResult(self.name, False, "vault path не задан")
        if not self.vault.exists():
            return SinkResult(
                self.name, False, f"vault не найден: {self.vault}"
            )
        if not followup.has_any:
            return SinkResult(self.name, False, "пустой followup")

        try:
            target_dir = self.vault / self.subfolder / session.start_time.strftime("%Y-%m-%d")
            target_dir.mkdir(parents=True, exist_ok=True)

            slug = _slugify(session.title or "untitled")
            time_str = session.start_time.strftime("%H%M")
            file_path = target_dir / f"{time_str}-{slug}.md"

            # Obsidian-friendly frontmatter
            frontmatter = (
                "---\n"
                f"title: {session.title or 'Без названия'}\n"
                f"date: {session.start_time.strftime('%Y-%m-%d')}\n"
                f"time: {session.start_time.strftime('%H:%M')}\n"
                f"duration_min: {session.get_duration_minutes()}\n"
                f"attendees: [{', '.join(session.attendees)}]\n"
                "tags: [meeting, meetmind]\n"
                "---\n\n"
            )
            content = frontmatter + render_combined_markdown(followup, session)
            file_path.write_text(content, encoding="utf-8")

            logger.info(f"🟣 Obsidian: {file_path}")
            return SinkResult(self.name, True, detail=str(file_path))
        except Exception as e:
            logger.error(f"ObsidianSink: {e}", exc_info=True)
            return SinkResult(self.name, False, detail=str(e))
