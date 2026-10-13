"""Sink: отправка готового markdown-текста фоллоу-апа в Telegram через
python-telegram-bot v20+ (async).

Идемпотентность между sinks обеспечивает deliver-стадия (по
`deliveries.status='delivered'`). Внутри sink — своя: длинный фоллоу-ап уходит
несколькими сообщениями, и уже отправленные части запоминаются в
`telegram_progress.json` сессии, чтобы ретрай после сбоя на середине не
дублировал их (аудит T5).

Формат — Telegram HTML, а не legacy Markdown: весь текст (включая ответ LLM)
экранируется, теги ставим только мы. Раньше любой `_` или `*` в саммари
ронял доставку с «can't parse entities».
"""
import asyncio
import hashlib
import html
import json
import logging
import re
from datetime import datetime

from daemon.paths import session_dir
from daemon.sinks.base import SinkResult

logger = logging.getLogger(__name__)

MAX_TG_MESSAGE = 4096  # лимит Telegram
# Нарезаем markdown с запасом: экранирование (& → &amp;) и теги удлиняют текст.
MD_CHUNK_LIMIT = 3500

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def md_to_tg_html(md: str) -> str:
    """Markdown из daemon/render.py → Telegram HTML. Поддерживаем ровно то, что
    генерирует рендер: `**жирный**` и строку-курсив `_…_` (дисклеймер). Всё
    остальное экранируется и уходит как текст."""
    out = []
    for line in md.split("\n"):
        esc = _BOLD.sub(r"<b>\1</b>", html.escape(line, quote=False))
        stripped = esc.strip()
        if len(stripped) > 2 and stripped.startswith("_") and stripped.endswith("_"):
            esc = f"<i>{stripped[1:-1]}</i>"
        out.append(esc)
    return "\n".join(out)


def split_text(text: str, limit: int, seps: tuple[str, ...] = ("\n\n", "\n", " ")) -> list[str]:
    """Нарезать текст на куски ≤ limit: сначала по абзацам, потом по строкам,
    потом по словам, в крайнем случае — по символам."""
    if len(text) <= limit:
        return [text] if text.strip() else []
    if not seps:
        return [text[i:i + limit] for i in range(0, len(text), limit)]
    sep, rest = seps[0], seps[1:]
    chunks: list[str] = []
    buf = ""
    for part in text.split(sep):
        candidate = part if not buf else buf + sep + part
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf.strip():
            chunks.append(buf)
        if len(part) <= limit:
            buf = part
        else:
            sub = split_text(part, limit, rest)
            chunks.extend(sub[:-1])
            buf = sub[-1] if sub else ""
    if buf.strip():
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


def html_chunks(md: str, limit: int = MD_CHUNK_LIMIT) -> list[str]:
    """Нарезать markdown и сконвертировать в HTML; кусок, который после
    экранирования вылез за 4096, режется мельче."""
    out: list[str] = []
    for piece in split_text(md, limit):
        rendered = md_to_tg_html(piece)
        if len(rendered) <= MAX_TG_MESSAGE:
            out.append(rendered)
        else:
            out.extend(html_chunks(piece, max(256, limit // 2)))
    return out


class TelegramSink:
    """Отправляет готовый markdown-текст в указанный Telegram chat."""

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._ok = bool(bot_token and chat_id)

    def send(self, text: str, *, title: str, when: datetime,
             session_id: str) -> SinkResult:
        if not self._ok:
            return SinkResult(self.name, False, "не настроен (нет token/chat_id)")
        if not text:
            return SinkResult(self.name, False, "пустой текст")

        try:
            return asyncio.run(self._send_async(text, session_id))
        except Exception as e:
            logger.error(f"TelegramSink: {e}", exc_info=True)
            return SinkResult(self.name, False, detail=str(e))

    # -------------------------------------------------------------- progress

    @staticmethod
    def _load_progress(session_id: str, digest: str) -> list[str]:
        if not session_id:
            return []
        path = session_dir(session_id) / "telegram_progress.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        # другой текст (например, «Пересобрать саммари») — шлём заново целиком
        return list(data.get("sent", [])) if data.get("hash") == digest else []

    @staticmethod
    def _save_progress(session_id: str, digest: str, sent: list[str]) -> None:
        if not session_id:
            return
        path = session_dir(session_id) / "telegram_progress.json"
        path.write_text(json.dumps({"hash": digest, "sent": sent}), encoding="utf-8")

    # ------------------------------------------------------------------ send

    @staticmethod
    def _reply_to(session_id: str) -> int | None:
        """Первое сообщение прошлой версии фоллоу-апа (после «Пересобрать
        саммари», продукт P12) — новая версия уходит ответом на него."""
        if not session_id:
            return None
        try:
            return int((session_dir(session_id) / "telegram_reply_to").read_text().strip())
        except (OSError, ValueError):
            return None

    async def _send_async(self, text: str, session_id: str) -> SinkResult:
        from telegram import Bot, ReplyParameters
        from telegram.constants import ParseMode

        bot = Bot(token=self.bot_token)
        chunks = html_chunks(text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        sent = self._load_progress(session_id, digest)
        if sent:
            logger.info(f"Telegram: {len(sent)} из {len(chunks)} частей уже отправлены — досылаю")

        reply_to = self._reply_to(session_id)
        for chunk in chunks[len(sent):]:
            extra = {}
            if not sent and reply_to is not None:
                # allow_sending_without_reply: старое сообщение могли удалить
                extra["reply_parameters"] = ReplyParameters(
                    message_id=reply_to, allow_sending_without_reply=True)
            msg = await bot.send_message(
                chat_id=self.chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                **extra,
            )
            sent.append(str(msg.message_id))
            self._save_progress(session_id, digest, sent)

        logger.info(f"📨 Telegram: отправлено {len(sent)} сообщений")
        return SinkResult(self.name, True, detail=f"messages={','.join(sent)}")
