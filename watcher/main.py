"""
Watcher service entrypoint.
Starts the scheduler and Telegram bot in a shared asyncio event loop.
"""

import asyncio
import logging
from pathlib import Path

from .config import load_settings
from .bot import run_bot
from .engine import run_engine

LOG_FILE = Path("/tmp/watcher.log")

log = logging.getLogger("watcher")


async def run(headed: bool = False) -> None:
    settings = load_settings()
    settings.headed = headed

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    log.info("Watcher starting (log_level=%s)...", settings.log_level)

    log.info("[startup] creating bot task")
    bot_task = asyncio.create_task(run_bot(settings), name="telegram-bot")
    log.info("[startup] creating engine task")
    engine_task = asyncio.create_task(run_engine(settings), name="engine")

    try:
        # Wait for whichever task finishes first (aiogram catches SIGTERM itself
        # and stops polling, so bot_task may return normally rather than raise).
        done, pending = await asyncio.wait(
            {bot_task, engine_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        log.info("[shutdown] first task(s) done: %s — cancelling remaining: %s",
                 [t.get_name() for t in done], [t.get_name() for t in pending])
        for task in pending:
            task.cancel()
    except asyncio.CancelledError:
        log.info("[shutdown] main task cancelled externally — cancelling bot + engine")
        bot_task.cancel()
        engine_task.cancel()

    log.info("[shutdown] waiting for bot task...")
    await asyncio.gather(bot_task, return_exceptions=True)
    log.info("[shutdown] bot task done")
    log.info("[shutdown] waiting for engine task...")
    await asyncio.gather(engine_task, return_exceptions=True)
    log.info("[shutdown] engine task done")

    log.info("[shutdown] run() complete — event loop will close")


if __name__ == "__main__":
    asyncio.run(run())
