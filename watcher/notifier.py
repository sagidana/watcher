"""
Send Telegram notifications with the cai output for a watcher.
"""

from __future__ import annotations

import logging

from aiogram import Bot

from .config import Settings
from .watchers_config import WatcherConfig

log = logging.getLogger("watcher.notifier")

_MAX_MSG = 4000  # Telegram max message length (4096, leave headroom)


async def notify_change(
    settings: Settings,
    watcher: WatcherConfig,
    text: str,
) -> None:
    """Send a Telegram notification with the watcher's cai output."""
    msg = f"{watcher.name}\n\n{text}"

    if len(msg) > _MAX_MSG:
        msg = msg[:_MAX_MSG] + "\n…"

    bot = Bot(token=settings.telegram.token)
    try:
        await bot.send_message(
            chat_id=settings.telegram.chat_id,
            text=msg,
        )
        log.info("Notification sent for watcher %s", watcher.id)
    except Exception:
        log.exception("Failed to send Telegram notification for watcher %s", watcher.id)
    finally:
        await bot.session.close()
