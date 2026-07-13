"""Sink: отправка готового markdown-текста фоллоу-апа в Telegram через
python-telegram-bot v20+ (async). Идемпотентность обеспечивает deliver-стадия
(по `deliveries.status='delivered'`), не sink."""
import asyncio
import logging
from datetime import datetime

from daemon.sinks.base import SinkResult

logger = logging.getLogger(__name__)

MAX_TG_MESSAGE = 4096  # лимит Telegram


class TelegramSink:
    """Отправляет готовый markdown-текст в указанный Telegram chat.

    Длинные сообщения режутся на несколько частей по границам абзацев
    (лимит Telegram — 4096 символов на сообщение).
    """

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
            return asyncio.run(self._send_async(text))
        except Exception as e:
            logger.error(f"TelegramSink: {e}", exc_info=True)
            return SinkResult(self.name, False, detail=str(e))

    async def _send_async(self, text: str) -> SinkResult:
        from telegram import Bot
        from telegram.constants import ParseMode

        bot = Bot(token=self.bot_token)

        sent: list[str] = []
        for chunk in self._chunks(text):
            msg = await bot.send_message(
                chat_id=self.chat_id,
                text=chunk,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
            sent.append(str(msg.message_id))

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
