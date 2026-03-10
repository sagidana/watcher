"""
Telegram bot — long-polling receiver.

Only messages from the configured TELEGRAM_CHAT_ID are processed.
All other senders receive no response (silent drop).
"""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from .config import Settings

log = logging.getLogger("watcher.bot")


def _build_dispatcher(chat_id: int) -> Dispatcher:
    dp = Dispatcher()

    # Drop every update that doesn't come from the authorised chat.
    dp.message.filter(F.chat.id == chat_id)

    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        log.info("cmd=start chat_id=%d user=%s", message.chat.id, message.from_user.username)
        reply = "Watcher is running.\nCommands: /status /help"
        await message.answer(reply)
        log.info("reply to start: %r", reply)

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        log.info("cmd=status chat_id=%d user=%s", message.chat.id, message.from_user.username)
        reply = "Status: running"
        await message.answer(reply)
        log.info("reply to status: %r", reply)

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        log.info("cmd=help chat_id=%d user=%s", message.chat.id, message.from_user.username)
        reply = "/start  — greeting\n/status — service status\n/help   — this message"
        await message.answer(reply)
        log.info("reply to help: %r", reply)

    @dp.message()
    async def unhandled(message: Message) -> None:
        log.warning(
            "unhandled message chat_id=%d user=%s text=%r",
            message.chat.id,
            message.from_user.username,
            message.text,
        )

    return dp


async def run_bot(settings: Settings) -> None:
    """Start the bot and block until cancelled."""
    bot = Bot(token=settings.telegram.token)
    dp = _build_dispatcher(settings.telegram.chat_id)

    log.info(
        "Telegram bot starting (chat_id=%d, poll_timeout=%ds)",
        settings.telegram.chat_id,
        settings.telegram.poll_timeout,
    )
    try:
        await dp.start_polling(bot, polling_timeout=settings.telegram.poll_timeout)
    finally:
        await bot.session.close()
        log.info("Telegram bot stopped")
