"""Sink: записать готовый markdown фоллоу-ап в локальную папку как .md файл.

В v2 это были два отдельных класса (MarkdownSink + ObsidianSink) с почти
идентичной логикой. В v3 они слиты в один NoteSink — 'note' и 'obsidian'
регистрируются как два раздельных ЭКЗЕМПЛЯРА с разными base_dir (SPEC_v3 §5:
раздельные строки в `deliveries`, раздельные ретраи)."""
import logging
import re
from datetime import datetime
from pathlib import Path

from daemon.sinks.base import SinkResult

logger = logging.getLogger(__name__)


def _slugify(title: str, max_len: int = 80) -> str:
    """Сделать filename-safe slug из заголовка встречи."""
    if not title:
        return "untitled"
    # Оставляем буквы, цифры, дефис и пробел; пробелы → дефисы
    s = re.sub(r"[^\w\s\-а-яА-ЯёЁ]", "", title, flags=re.UNICODE)
    s = re.sub(r"\s+", "-", s.strip())
    return s[:max_len] or "untitled"


class NoteSink:
    """Сохраняет готовый markdown-текст в <base_dir>/YYYY-MM-DD/HHMM-slug.md.

    frontmatter=True добавляет Obsidian-friendly YAML frontmatter в начало файла —
    используется для варианта 'obsidian' (base_dir = vault/Meetings).
    """

    def __init__(self, name: str, base_dir: str | Path, frontmatter: bool = False):
        self.name = name
        self.base_dir = Path(base_dir).expanduser()
        self.frontmatter = frontmatter

    def send(self, text: str, *, title: str, when: datetime,
             session_id: str) -> SinkResult:
        try:
            if not text:
                return SinkResult(self.name, False, "пустой текст")

            date_dir = self.base_dir / when.strftime("%Y-%m-%d")
            date_dir.mkdir(parents=True, exist_ok=True)

            slug = _slugify(title or "untitled")
            time_str = when.strftime("%H%M")
            file_path = date_dir / f"{time_str}-{slug}.md"

            content = text
            if self.frontmatter:
                content = self._frontmatter(title, when, session_id) + content

            file_path.write_text(content, encoding="utf-8")

            logger.info(f"📝 {self.name}: сохранён {file_path}")
            return SinkResult(
                self.name, True, detail=str(file_path), url=f"file://{file_path}"
            )
        except Exception as e:
            logger.error(f"NoteSink[{self.name}]: {e}", exc_info=True)
            return SinkResult(self.name, False, detail=str(e))

    @staticmethod
    def _frontmatter(title: str, when: datetime, session_id: str) -> str:
        return (
            "---\n"
            f"title: {title or 'Без названия'}\n"
            f"date: {when.strftime('%Y-%m-%d')}\n"
            f"time: {when.strftime('%H:%M')}\n"
            f"session_id: {session_id}\n"
            "tags: [meeting, meetmind]\n"
            "---\n\n"
        )
