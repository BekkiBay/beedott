"""Sink: создать страницу в Notion database с фоллоу-апом.

Требует:
  - NOTION_TOKEN (Internal Integration token)
  - NOTION_DATABASE_ID (target database, должен иметь интеграцию в Share)

В Notion есть лимит ~2000 символов на rich-text block, поэтому длинный
фоллоу-ап режется на несколько paragraph-блоков.
"""
import logging
from typing import TYPE_CHECKING

import requests

from daemon.sinks.base import SinkResult

if TYPE_CHECKING:
    from daemon.session import Session
    from daemon.summarizer import FollowupResult

logger = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"
BLOCK_TEXT_LIMIT = 2000


class NotionSink:
    """Создать новую страницу в Notion DB с фоллоу-апом."""

    name = "notion"

    def __init__(self, token: str = "", database_id: str = ""):
        self.token = token
        self.database_id = database_id
        self._ok = bool(token and database_id)

    def send(self, followup: "FollowupResult", session: "Session") -> SinkResult:
        if not self._ok:
            return SinkResult(self.name, False, "не настроен (нет TOKEN/DB_ID)")
        if not followup.has_any:
            return SinkResult(self.name, False, "пустой followup")

        try:
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            }

            children: list[dict] = []

            if followup.cloud:
                children.append(self._heading(
                    f"☁️ Claude ({followup.cloud_engine}, "
                    f"{followup.cloud_duration_s:.1f}с)"
                ))
                children.extend(self._paragraphs(followup.cloud))

            if followup.local:
                children.append(self._heading(
                    f"🖥️ Локально ({followup.local_engine}, "
                    f"{followup.local_duration_s:.1f}с)"
                ))
                children.extend(self._paragraphs(followup.local))

            payload = {
                "parent": {"database_id": self.database_id},
                "properties": {
                    # Используем дефолтное свойство Name (Title)
                    "Name": {
                        "title": [
                            {"text": {"content": session.title or "Без названия"}}
                        ]
                    }
                },
                "children": children[:100],  # Notion лимит 100 children в create
            }

            r = requests.post(
                "https://api.notion.com/v1/pages",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if r.status_code >= 300:
                logger.error(f"Notion API {r.status_code}: {r.text[:500]}")
                return SinkResult(
                    self.name, False, detail=f"HTTP {r.status_code}: {r.text[:200]}"
                )

            page = r.json()
            url = page.get("url", "")
            logger.info(f"📓 Notion: {url}")
            return SinkResult(self.name, True, detail=page.get("id", ""), url=url)
        except Exception as e:
            logger.error(f"NotionSink: {e}", exc_info=True)
            return SinkResult(self.name, False, detail=str(e))

    # ------------------------------ блоки --------------------------------------

    @staticmethod
    def _heading(text: str) -> dict:
        return {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": text[:BLOCK_TEXT_LIMIT]}}]
            },
        }

    @staticmethod
    def _paragraphs(text: str) -> list[dict]:
        # Бьём по абзацам, дальше — если кусок длиннее лимита, режем по символам
        out: list[dict] = []
        for chunk in text.split("\n\n"):
            piece = chunk.strip()
            if not piece:
                continue
            while piece:
                head = piece[:BLOCK_TEXT_LIMIT]
                piece = piece[BLOCK_TEXT_LIMIT:]
                out.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {"type": "text", "text": {"content": head}}
                            ]
                        },
                    }
                )
        return out
