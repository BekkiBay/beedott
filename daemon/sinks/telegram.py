"""Sink: отправка фоллоу-апа в Telegram через python-telegram-bot v20+ (async)."""
import asyncio
import logging
from typing import TYPE_CHECKING

from daemon.sinks.base import SinkResult

if TYPE_CHECKING:
    from daemon.session import Session
    from daemon.summarizer import FollowupResult

logger = logging.getLogger(__name__)

MAX_TG_MESSAGE = 4096  # лимит Telegram


class TelegramSink:
    """Отправляет фоллоу-ап в указанный Telegram chat.

    Если есть оба варианта (cloud+local), оба идут отдельными сообщениями
    с заголовками — чтобы пользователь мог сравнить.
    """

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._ok = bool(bot_token and chat_id)

    def send(self, followup: "FollowupResult", session: "Session") -> SinkResult:
        if not self._ok:
            return SinkResult(self.name, False, "не настроен (нет token/chat_id)")
        if not followup.has_any:
            return SinkResult(self.name, False, "пустой followup")

        try:
            return asyncio.run(self._send_async(followup, session))
        except Exception as e:
            logger.error(f"TelegramSink: {e}", exc_info=True)
            return SinkResult(self.name, False, detail=str(e))

    async def _send_async(
        self, followup: "FollowupResult", session: "Session"
    ) -> SinkResult:
        from telegram import Bot
        from telegram.constants import ParseMode

        bot = Bot(token=self.bot_token)

        sent: list[str] = []

        if followup.cloud:
            header = (
                f"☁️ *Claude* ({followup.cloud_engine}, {followup.cloud_duration_s:.1f}с)"
            )
            for chunk in self._chunks(f"{header}\n\n{followup.cloud}"):
                msg = await bot.send_message(
                    chat_id=self.chat_id,
                    text=chunk,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
                sent.append(str(msg.message_id))

        if followup.local:
            header = (
                f"🖥️ *Локально* ({followup.local_engine}, "
                f"{followup.local_duration_s:.1f}с)"
            )
            for chunk in self._chunks(f"{header}\n\n{followup.local}"):
                msg = await bot.send_message(
                    chat_id=self.chat_id,
                    text=chunk,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
                sent.append(str(msg.message_id))

        if followup.errors:
            err_text = "⚠️ Ошибки:\n" + "\n".join(f"• {e}" for e in followup.errors)
            await bot.send_message(chat_id=self.chat_id, text=err_text)

        logger.info(f"📨 Telegram: отправлено {len(sent)} сообщений")
        return SinkResult(self.name, True, detail=f"messages={','.join(sent)}")

    @staticmethod
    def _chunks(text: str, limit: int = MAX_TG_MESSAGE - 50) -> list[str]:
        if len(text) <= limit:
            return [text]
        # Режем по абзацам, не по символам
        out: list[str] = []
        buf = ""
        for para in text.split("\n\n"):
            if len(buf) + len(para) + 2 > limit and buf:
                out.append(buf.strip())
                buf = para + "\n\n"
            else:
                buf += para + "\n\n"
        if buf.strip():
            out.append(buf.strip())
        return out
