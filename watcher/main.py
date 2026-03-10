"""
Watcher service entrypoint.
Starts the scheduler and Telegram bot in a shared asyncio event loop.
"""

import asyncio
import logging
from pathlib import Path

from .config import load_settings
from .bot import run_bot

LOG_FILE = Path("/tmp/watcher.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("watcher")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def run() -> None:
    log.info("Watcher starting...")
    settings = load_settings()

    bot_task = asyncio.create_task(run_bot(settings), name="telegram-bot")

    try:
        await asyncio.gather(bot_task)
    except asyncio.CancelledError:
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)

    log.info("Watcher stopped.")


if __name__ == "__main__":
    asyncio.run(run())
